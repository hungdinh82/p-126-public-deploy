/* Optional model streaming for the diagnostic UI; no vehicle/workflow effects. */
function streamTrace(endpoint,result){traces.push({endpoint,response:result});traces=traces.slice(-8);$('trace').textContent=JSON.stringify(traces,null,2)}
function stopStreams(){streamController?.abort();activeSocket?.close();for(const n of playbackNodes){try{n.stop()}catch{}}playbackNodes=[];if(playbackContext){playbackContext.close().catch(()=>{});playbackContext=null}if(recorder?.state==='recording')recorder.stop()}
async function readSSE(response,onEvent){
 const reader=response.body.getReader(),decoder=new TextDecoder();let pending='';
 try{while(true){const {value,done}=await reader.read();pending+=done?decoder.decode():decoder.decode(value,{stream:true});pending=pending.replace(/\r\n/g,'\n');
 let boundary;while((boundary=pending.indexOf('\n\n'))>=0){const frame=pending.slice(0,boundary);pending=pending.slice(boundary+2);let event='message',data=[];
 for(const line of frame.split('\n')){if(line.startsWith('event:'))event=line.slice(6).trim();if(line.startsWith('data:'))data.push(line.slice(5).trimStart())}
 if(data.length)await onEvent(event,data.join('\n'))}if(pending.length>262144)throw new Error('SSE frame quá lớn');if(done){if(pending.trim())throw new Error('SSE stream bị ngắt giữa frame');break}}
 }finally{await reader.cancel().catch(()=>{});reader.releaseLock()}
}
async function generateStream(text){
 const start=performance.now();let answer='',finished=false,first=null;
 const response=await api('/v1/llm/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({contract_version:'1.0',capability:'llm',request_id:callId(),call_id:callId(),attempt:1,stream:true,timeout_ms:180000,input:{messages:[{role:'system',content:'Trả lời bằng tiếng Việt, ngắn gọn. /no_think'},{role:'user',content:text}],max_tokens:128,chat_template_kwargs:{enable_thinking:false}}})});
 await readSSE(response,(event,data)=>{const result=JSON.parse(data);if(event==='error'||result.error)throw new Error(result.error?.message||'SLM stream lỗi');if(event==='delta'){answer+=result.text||'';if(result.text&&first==null){first=performance.now()-start;$('progress').textContent='SLM đang streaming · phần đầu '+seconds(first)}$('answer').textContent=answer}else if(event==='done'){finished=true;answer=result.result.text;$('answer').textContent=answer;stage('llm',result.latency_ms);streamTrace('POST /v1/llm/generate · SSE',{first_chunk_ms:first,result})}});
 if(!finished)throw new Error('SLM stream kết thúc thiếu done');return answer;
}
function pcmWav(pcm,rate){const out=new Uint8Array(44+pcm.length),view=new DataView(out.buffer);const write=(offset,s)=>[...s].forEach((c,i)=>view.setUint8(offset+i,c.charCodeAt(0)));write(0,'RIFF');view.setUint32(4,36+pcm.length,true);write(8,'WAVE');write(12,'fmt ');view.setUint32(16,16,true);view.setUint16(20,1,true);view.setUint16(22,1,true);view.setUint32(24,rate,true);view.setUint32(28,rate*2,true);view.setUint16(32,2,true);view.setUint16(34,16,true);write(36,'data');view.setUint32(40,pcm.length,true);out.set(pcm,44);return new Blob([out],{type:'audio/wav'})}
async function synthesizeStream(text){
 if(!playbackContext)playbackContext=new AudioContext();await playbackContext.resume();
 const context=playbackContext,start=performance.now(),parts=[];let total=0,pending=new Uint8Array(),next=context.currentTime+.05,first=null;
 const response=await api('/v1/tts/synthesize',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,stream:true,format:'pcm_s16le'})});
 const rate=Number(response.headers.get('X-Audio-Sample-Rate'));if(!Number.isFinite(rate)||rate<=0)throw new Error('Thiếu sample rate TTS');const reader=response.body.getReader();
 try{while(true){const {value,done}=await reader.read();if(done)break;parts.push(value);total+=value.length;if(total>20*1024*1024)throw new Error('TTS audio quá lớn');
 const bytes=new Uint8Array(pending.length+value.length);bytes.set(pending);bytes.set(value,pending.length);const size=bytes.length-bytes.length%2;pending=bytes.slice(size);if(!size)continue;
 const buffer=context.createBuffer(1,size/2,rate),samples=buffer.getChannelData(0),view=new DataView(bytes.buffer);for(let i=0;i<samples.length;i++)samples[i]=view.getInt16(i*2,true)/32768;
 const source=context.createBufferSource();source.buffer=buffer;source.connect(context.destination);next=Math.max(next,context.currentTime+.02);source.start(next);next+=buffer.duration;playbackNodes.push(source);source.onended=()=>{playbackNodes=playbackNodes.filter(n=>n!==source)};
 if(first==null){first=performance.now()-start;$('progress').textContent='TTS đang phát từng đoạn · phần đầu '+seconds(first)}}
 if(pending.length||!total)throw new Error('PCM stream không hoàn chỉnh');stage('tts',performance.now()-start);const all=new Uint8Array(total);let offset=0;for(const part of parts){all.set(part,offset);offset+=part.length}if(audioUrl)URL.revokeObjectURL(audioUrl);audioUrl=URL.createObjectURL(pcmWav(all,rate));$('audio').src=audioUrl;$('audio').hidden=false;streamTrace('POST /v1/tts/synthesize · PCM',{bytes:total,first_chunk_ms:first,sample_rate:rate});
 }finally{await reader.cancel().catch(()=>{});reader.releaseLock()}
}
function openStt(){
 const socket=new WebSocket(location.origin.replace(/^http/,'ws')+'/v1/stt/stream');activeSocket=socket;let queued=[],waiting=false,ready=false,ending=false,stopped=false,bytes=0,settled=false;
 let resolve,reject;const result=new Promise((yes,no)=>{resolve=yes;reject=no});result.catch(()=>{});const timer=setTimeout(()=>finishError(new Error('STT vượt thời gian chờ')),90000);
 const clean=()=>{clearTimeout(timer);if(activeSocket===socket)activeSocket=null;socket.close()};
 function finishError(error){if(!settled){settled=true;reject(error);clean()}}
 function pump(){if(!ready||waiting||stopped)return;if(queued.length){socket.send(queued.shift());waiting=true}else if(ending){socket.send('stop');stopped=true}}
 socket.onopen=()=>socket.send(JSON.stringify({type:'start',sample_rate:16000,format:'pcm_s16le',...(authRequired?{token}:{})}));
 socket.onmessage=e=>{try{const update=JSON.parse(e.data);if(update.type==='error')return finishError(new Error(update.error.message));if(update.type==='ready'){ready=true;pump()}else if(update.type==='ack'){waiting=false;pump()}else if(update.type==='partial'){$('prompt').value=update.text;$('answer').textContent='Transcript tạm: '+update.text;$('progress').textContent='STT đang cập nhật · phần đầu '+seconds(update.first_chunk_ms)}else if(update.type==='done'){settled=true;resolve(update);clean()}}catch(error){finishError(error)}};
 socket.onerror=()=>finishError(new Error('Không kết nối được STT WebSocket'));socket.onclose=()=>{if(!settled)finishError(new Error('STT stream đã ngắt'))};
 streamController?.signal.addEventListener('abort',()=>finishError(new DOMException('Đã dừng','AbortError')),{once:true});
 return {push(data){bytes+=data.byteLength;if(bytes>1920000){finishError(new Error('Audio dài quá 60 giây'));return}queued.push(data);pump()},finish(){ending=true;pump();return result},result,cancel:finishError};
}
async function transcribeStream(blob){const pcm=new Uint8Array(await(await toWav(blob)).arrayBuffer()).slice(44),session=openStt();for(let pos=0;pos<pcm.length;pos+=6400)session.push(pcm.slice(pos,pos+6400));const result=await session.finish();$('prompt').value=result.text;$('answer').textContent='Transcript: '+result.text;stage('stt',result.latency_ms);streamTrace('WS /v1/stt/stream',result)}
async function streamMicrophone(){
 let media,context,node,url,session,flushed;
 try{media=await navigator.mediaDevices.getUserMedia({audio:true});context=new AudioContext();await context.resume();session=openStt();
 const code=`class Capture extends AudioWorkletProcessor{constructor(){super();this.buf=new Int16Array(3200);this.n=0;this.phase=0;this.sum=0;this.count=0;this.port.onmessage=()=>{this.flush();this.port.postMessage({type:'flushed'})}}flush(){if(this.n){this.port.postMessage(this.buf.slice(0,this.n).buffer);this.n=0}}process(inputs){const samples=inputs[0]?.[0];if(samples)for(const s of samples){this.sum+=s;this.count++;this.phase+=16000/sampleRate;if(this.phase>=1){this.phase-=1;this.buf[this.n++]=Math.max(-1,Math.min(1,this.sum/this.count))*32767;this.sum=0;this.count=0;if(this.n===3200)this.flush()}}return true}}registerProcessor('capture',Capture)`;
 url=URL.createObjectURL(new Blob([code],{type:'application/javascript'}));await context.audioWorklet.addModule(url);node=new AudioWorkletNode(context,'capture');context.createMediaStreamSource(media).connect(node);const mute=context.createGain();mute.gain.value=0;node.connect(mute).connect(context.destination);node.port.onmessage=e=>{if(e.data?.type==='flushed'){flushed?.();return}session.push(e.data)};
 $('mic').textContent='Đang ghi âm streaming';$('progress').textContent='STT đang nghe…';let stop;const stopped=new Promise(resolve=>{stop=resolve});const stopButton=$('stop');let ending=false;const end=async()=>{if(ending)return;ending=true;await new Promise(resolve=>{flushed=resolve;node.port.postMessage('flush');setTimeout(resolve,500)});node.disconnect();media.getTracks().forEach(t=>t.stop());stop()};stopButton.addEventListener('click',end,{once:true});const timer=setTimeout(end,59000);
 try{await Promise.race([stopped,session.result]);const result=await session.finish();$('prompt').value=result.text;stage('stt',result.latency_ms);streamTrace('WS /v1/stt/stream · microphone',result);$('mic').textContent='Ghi âm';if(!streamController.signal.aborted)await generate()}finally{clearTimeout(timer);stopButton.removeEventListener('click',end)}
 }finally{session?.cancel(new DOMException('Đã dừng','AbortError'));node?.disconnect();media?.getTracks().forEach(t=>t.stop());if(context)await context.close();if(url)URL.revokeObjectURL(url);$('mic').textContent='Ghi âm'}
}
$('stop').onclick=()=>{if($('mic').textContent==='Đang ghi âm streaming')return;stopStreams()};
