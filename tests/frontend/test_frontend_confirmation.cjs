const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const {test}=require('node:test');
const client=import('data:text/javascript;base64,'+Buffer.from(readFileSync('assistant-client.js','utf8')).toString('base64'));
const action={capability:'window.set_position',arguments:{zone:'driver',position_percent:50}};
function response(){return {status:'confirmation_required',action:{intent:action.capability,arguments:{...action.arguments}},
  pending_question:{id:'q:1',purpose:'approve_execution',accepts_yes:true}};}

test('a control click sends a typed answer bound to its returned question',async()=>{
  const {approveButtonAction}=await client;
  const calls=[];
  const result=await approveButtonAction({sessionId:'s',turnId:'t',action,response:response(),
    fetcher:async(url,options)=>{calls.push([url,JSON.parse(options.body)]);return {ok:true,json:async()=>({status:'verified'})};}});
  assert.equal(result.status,'verified');
  assert.deepEqual(calls,[['/api/v2/turn',{session_id:'s',turn_id:'t',input:{kind:'question_reply',answer:{question_id:'q:1',option_id:'yes'}}}]]);
});

test('a stale or different action cannot auto-approve the returned operation',async()=>{
  const {approveButtonAction}=await client;
  for(const payload of [{...response(),pending_question:null},{...response(),action:{intent:'door.set_open',arguments:{open:true}}},
    {...response(),action:{intent:action.capability,arguments:{zone:'driver',position_percent:100}}}]){
    const result=await approveButtonAction({sessionId:'s',turnId:'t',action,response:payload,
      fetcher:()=>{throw Error('must not dispatch');}});
    assert.equal(result,payload);
  }
});

test('typed input has one kind even when a view supplies an action and a bound answer',async()=>{
  const {buildTurnRequest}=await client;
  const value=buildTurnRequest({sessionId:'s',turnId:'t',text:'đồng ý',action,answer:{question_id:'q:2',option_id:'yes'}});
  assert.deepEqual(value.input,{kind:'question_reply',answer:{question_id:'q:2',option_id:'yes'}});
});

test('server rejection is surfaced without replaying a command',async()=>{
  const {approveButtonAction}=await client;
  let calls=0;
  await assert.rejects(approveButtonAction({sessionId:'s',turnId:'t',action,response:response(),
    fetcher:async()=>{calls++;return {ok:false,json:async()=>({detail:'stale_question'})};}}),/stale_question/);
  assert.equal(calls,1);
});
