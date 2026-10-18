// Typed turn construction and bound button consent, independent of DOM and audio.
export function buildTurnRequest({sessionId,turnId,text='',action=null,answer=null,provider=null,position=null}) {
  const input=answer?{kind:'question_reply',answer}:action?{kind:'action',action}:{kind:'utterance',text};
  return {session_id:sessionId,turn_id:turnId,input,...(provider?{provider}:{}),...(position?{position}:{})};
}
const canonical=value=>JSON.stringify(value && typeof value==='object' && !Array.isArray(value)
  ?Object.fromEntries(Object.keys(value).sort().map(key=>[key,value[key]])):value);
export function boundButtonAnswer(action,response) {
  const question=response.pending_question;
  if(!action || !question || question.purpose!=='approve_execution' || !question.accepts_yes
    || !['window.set_position','door.set_lock','door.set_open','hood.set_open','trunk.set_open','body.set_open'].includes(action.capability)
    || response.action?.intent!==action.capability || canonical(response.action.arguments)!==canonical(action.arguments))return null;
  return {question_id:question.id,option_id:'yes'};
}
export async function approveButtonAction({fetcher=fetch,base='',sessionId,turnId,action,response,signal}) {
  const answer=boundButtonAnswer(action,response);
  if(!answer)return response;
  const result=await fetcher(`${base}/api/v2/turn`,{method:'POST',headers:{'Content-Type':'application/json'},signal,
    body:JSON.stringify(buildTurnRequest({sessionId,turnId,answer}))});
  const payload=await result.json();
  if(!result.ok)throw Error(payload.detail || 'Không xác nhận được thao tác');
  return payload;
}
