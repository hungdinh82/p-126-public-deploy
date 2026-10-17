const {test}=require('node:test');const assert=require('node:assert/strict');const vm=require('node:vm');const {readFileSync}=require('node:fs');
const sandbox={setTimeout,clearTimeout,performance,crypto:require('node:crypto').webcrypto};vm.createContext(sandbox);
vm.runInContext(readFileSync('navigation-state.js','utf8').replace(/export /g,'')+';globalThis.types={GearController,SnapshotOrder}',sandbox);
const {GearController,SnapshotOrder}=sandbox.types;
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};};
function setup(){const calls=[],changes=[],timers=[],replies=[];let snapshot={runtime_epoch:'one',snapshot_sequence:1,vehicle_state:{driving:false},control_receipts:[]};const gear=new GearController({send:event=>{calls.push(event);const reply=deferred();replies.push(reply);return reply.promise;},synchronize:async()=>snapshot,changed:controller=>changes.push({status:controller.status,frozen:controller.frozen,desired:controller.desired}),schedule:(fn,ms)=>{timers.push({fn,ms});return timers.length;},cancel(){},id:()=>String(calls.length+1),now:()=>0});return{gear,calls,changes,timers,replies,setSnapshot:value=>snapshot=value};}

test('D is sent once; P while waiting D freezes and is sent immediately',async()=>{
 const h=setup();const run=h.gear.request('D','one');assert.equal(h.gear.confirmed,'P');
 assert.equal(h.gear.request('D','one'),run);assert.equal(h.calls.length,1);
 const park=h.gear.request('P','one');assert.equal(h.gear.frozen,true);
 h.replies[0].resolve({vehicle_state:{driving:true}});await new Promise(setImmediate);
 assert.equal(h.calls.length,2);assert.equal(h.calls[1].gear,'P');assert.equal(h.gear.frozen,true);
 h.replies[1].resolve({vehicle_state:{driving:false}});await Promise.all([run,park]);
 assert.equal(h.gear.confirmed,'P');assert.equal(h.gear.status,'idle');
});

test('lost response resolves from server receipt; missing receipt stays frozen',async()=>{
 const h=setup();const run=h.gear.request('D','one');h.replies[0].reject(Error('response lost'));await run;
 assert.equal(h.gear.status,'unknown');assert.equal(h.gear.frozen,true);
 h.setSnapshot({runtime_epoch:'one',snapshot_sequence:2,vehicle_state:{driving:true},control_receipts:[{event_id:'1',status:'completed'}]});
 await h.gear.reconcile();assert.equal(h.gear.status,'idle');assert.equal(h.gear.confirmed,'D');
});

test('slow request reports syncing at 3 seconds and reconciles at 8',async()=>{
 const h=setup();const run=h.gear.request('P','one');assert.deepEqual(h.timers.map(t=>t.ms),[3000,8000]);
 h.timers[0].fn();assert.equal(h.gear.status,'syncing');
 h.setSnapshot({runtime_epoch:'one',snapshot_sequence:2,vehicle_state:{driving:false},control_receipts:[{event_id:'1'}]});
 await h.timers[1].fn();await new Promise(setImmediate);assert.equal(h.gear.pending,null);assert.equal(h.gear.status,'idle');
 h.replies[0].resolve({vehicle_state:{driving:false}});await run;
});

test('only authoritative snapshot changes runtime epoch; retired epoch cannot return',()=>{
 const order=new SnapshotOrder();assert.equal(order.accept({runtime_epoch:'old',snapshot_sequence:50}),true);
 assert.equal(order.accept({runtime_epoch:'old',snapshot_sequence:49}),false);
 assert.equal(order.accept({runtime_epoch:'new',snapshot_sequence:1}),false);
 assert.equal(order.accept({runtime_epoch:'new',snapshot_sequence:1},true),true);
 assert.equal(order.accept({runtime_epoch:'old',snapshot_sequence:100},true),false);
 assert.equal(order.accept({navigation:{status:'running'}}),false);
 assert.equal(order.accept({runtime_epoch:'new',snapshot_sequence:2,committed:false}),false);
});

test('restart cancels uncertain D rather than replaying it',async()=>{
 const h=setup();const run=h.gear.request('D','one');h.setSnapshot({runtime_epoch:'two',snapshot_sequence:1,vehicle_state:{driving:false},control_receipts:[]});
 h.replies[0].reject(Error('restart'));await run;
 assert.equal(h.gear.status,'idle');assert.equal(h.gear.confirmed,'P');assert.equal(h.calls.length,1);
});

for(const order of ['lost-drive','reverse-response'])test(`newer P survives ${order}`,async()=>{
 const h=setup();const drive=h.gear.request('D','one');const park=h.gear.request('P','one');
 assert.equal(h.calls.length,2);assert.equal(h.calls[1].control_sequence,h.calls[0].control_sequence+1);
 if(order==='lost-drive'){h.replies[0].reject(Error('lost D'));await drive;
  h.setSnapshot({runtime_epoch:'one',snapshot_sequence:2,vehicle_state:{driving:true},control_receipts:[{event_id:'1',status:'completed'}]});
  await h.gear.reconcile();assert.equal(h.gear.desired,'P');assert.equal(h.gear.frozen,true);
 }else{h.replies[1].resolve({runtime_epoch:'one',snapshot_sequence:3,vehicle_state:{driving:false},receipt:{event_id:'2',status:'completed'}});await park;
  h.replies[0].resolve({runtime_epoch:'one',snapshot_sequence:2,vehicle_state:{driving:true}});await drive;}
 if(order==='lost-drive'){h.replies[1].resolve({runtime_epoch:'one',snapshot_sequence:3,vehicle_state:{driving:false},receipt:{event_id:'2',status:'completed'}});await park;}
 assert.equal(h.gear.confirmed,'P');assert.equal(h.gear.desired,null);assert.equal(h.gear.frozen,false);
});

test('unknown receipt and reload snapshot freeze movement until a newer confirmed P',async()=>{
 const h=setup();const drive=h.gear.request('D','one');
 h.gear.observe({runtime_epoch:'one',snapshot_sequence:2,vehicle_state:{driving:true},receipt:{event_id:'1',status:'unknown'}});
 assert.equal(h.gear.frozen,true);assert.equal(h.gear.status,'unknown');
 h.replies[0].resolve({runtime_epoch:'one',snapshot_sequence:2,vehicle_state:{driving:true},receipt:{event_id:'1',status:'unknown'}});await drive;
 const reload=setup();reload.gear.observe({runtime_epoch:'one',snapshot_sequence:2,vehicle_state:{driving:true},control_uncertain:true},true);
 assert.equal(reload.gear.frozen,true);
 const park=reload.gear.request('P','one');reload.replies[0].resolve({runtime_epoch:'one',snapshot_sequence:3,vehicle_state:{driving:false},receipt:{event_id:'1',status:'completed'}});await park;
 assert.equal(reload.gear.status,'idle');assert.equal(reload.gear.confirmed,'P');
});
