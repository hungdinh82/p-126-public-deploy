const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const vm=require('node:vm');
const {test}=require('node:test');

function harness({withEnergy=false}={}) {
  const elements=new Map(),commands=[],events=[],driving=[],announcements=[];
  const element=()=>({hidden:false,value:'',textContent:'',innerHTML:'',children:[],listeners:{},append(...nodes){this.children.push(...nodes);},addEventListener(type,handler){this.listeners[type]=handler;},classList:{add(){},remove(){},toggle(){}},setAttribute(){},scrollIntoView(){},querySelectorAll:()=>[]});
  const sandbox={console,crypto:{randomUUID:()=>String(events.length+1)},performance:{now:()=>1000},requestAnimationFrame:()=>1,cancelAnimationFrame(){},setTimeout:()=>1,
    document:{createElement:tag=>({...element(),tagName:tag}),querySelector:selector=>{if(selector==='#journey-energy'&&!withEnergy)return null;if(!elements.has(selector))elements.set(selector,element());return elements.get(selector);},querySelectorAll:()=>[],body:element()},
    stubMap:{flyTo(){},easeTo(){},jumpTo(){},getStyle:()=>({sources:{}}),getSource:()=>({setData(){}})},
    command:(...args)=>commands.push(args),event:async value=>events.push(value),setDriving:value=>driving.push(value),announce:text=>announcements.push(text),
  };
  vm.createContext(sandbox);
  vm.runInContext(readFileSync('journey.js','utf8').replace(/export /g,''),sandbox);
  vm.runInContext(`map=stubMap; hooks={command,event,setDriving,announce,progress(){},route(){},travel(){},confirm:grant=>command('confirm',grant)}; openJourney=async()=>{}; placeMarker=(name,point)=>{markers[name]={point,setRotation(){},remove(){}};return markers[name];}; planRoute=()=>{throw Error('Client must not recompute server route');};`,sandbox);
  return {sandbox,commands,events,driving,announcements,elements};
}
function nav(status='preview',revision=1,sequence=1) {
  return {revision,event_sequence:sequence,status,origin:{lat:21,lng:105.8,label:'TEST',simulated:true},candidates:[],selected:{name:'TEST destination',lat:21,lng:105.9},route:{distance_m:10000,duration_s:500,coordinates:[[105.8,21],[105.9,21]],steps:[]},progress_m:0,energy:null,alternatives:[],pending:null};
}
async function apply(h,value,confirmation=null){h.sandbox.payload={navigation:value,confirmation,ui_events:[{type:'navigation.preview'}]};await vm.runInContext('applyNavigation(payload)',h.sandbox);}

test('server preview waits for consent and active simulation ignores old route/sequence',async()=>{
  const h=harness();
  await apply(h,nav(),{confirmation_id:'grant-1'});
  vm.runInContext('startSimulation()',h.sandbox);
  assert.equal(vm.runInContext('sim.running',h.sandbox),false);
  assert.equal(h.commands[0][0],'confirm');
  await apply(h,nav('running',2,2));
  assert.equal(vm.runInContext('sim.running',h.sandbox),true);
  assert.equal(vm.runInContext('view',h.sandbox),'mini');
  await apply(h,nav('preview',1,1));
  await apply(h,nav('preview',2,1));
  assert.equal(vm.runInContext('sim.running',h.sandbox),true);
  assert.deepEqual(h.driving,[]); // only the backend may mutate driving state
});

test('pending route keeps current simulation; completing waits for authoritative progress',async()=>{
  const h=harness();
  await apply(h,nav('running',1,1));
  const next=nav('running',2,2);next.pending={navigation:nav('preview',2,2),message:'TEST pending'};
  next.pending.navigation.route.coordinates=[[105.8,21],[106.2,21]];
  await apply(h,next);
  assert.equal(vm.runInContext('route.coordinates[1][0]',h.sandbox),105.9);
  vm.runInContext('sim.traveled=route.length; tick(2000)',h.sandbox);
  assert.equal(vm.runInContext('sim.running',h.sandbox),true);
  assert.equal(h.events[0].kind,'progress');
  assert.equal(h.events[0].distance_m,10000);
  await apply(h,{...next,status:'completed',progress_m:10000,event_sequence:3});
  assert.equal(vm.runInContext('sim.running',h.sandbox),false);
  assert.deepEqual(h.driving,[]);
  assert.match(h.announcements.at(-1),/đã đến/i);
});

test('destination results stay in voice UI until a place is explicitly chosen',async()=>{
  const h=harness();h.sandbox.opened=0;
  vm.runInContext('openJourney=async()=>{opened++;}',h.sandbox);
  await apply(h,{...nav('selecting'),selected:null,route:null,candidates:[]});
  assert.equal(h.sandbox.opened,0);
  assert.equal(vm.runInContext('sim.running',h.sandbox),false);
  await apply(h,nav('preview',2,2));
  assert.equal(h.sandbox.opened,1);
  assert.equal(vm.runInContext('sim.running',h.sandbox),false);
});

test('home destination cards refine by server handle and ignore a stale click',async()=>{
  const h=harness(),next={...nav('selecting'),selected:null,route:null,candidates:[],clarification:{revision:1,question:'Trung tâm Hoa Lư hay nơi khác?',phase:'scope',choices:[{handle:'choice:center',label:'Trung tâm phường Hoa Lư',kind:'representative'},{handle:'choice:other',label:'Địa điểm cụ thể khác',kind:'specific'}]}};
  await apply(h,next);
  const panel=h.elements.get('#destination-choices');
  const buttons=panel.children.filter(node=>node.className==='destination-choice-card');
  assert.equal(panel.hidden,false);assert.equal(buttons.length,2);
  await buttons[0].listeners.click();
  assert.equal(h.commands[0][1].capability,'navigation.refine');
  assert.equal(h.commands[0][1].arguments.choice_handle,'choice:center');
  await apply(h,{...next,revision:2,clarification:{...next.clarification,revision:2}});
  await buttons[0].listeners.click();
  assert.equal(h.commands.length,1);
});

test('home place cards select without spoken text and hide after a preview',async()=>{
  const h=harness(),next={...nav('selecting'),selected:null,route:null,candidates:[{handle:'place:1',name:'Tràng An',address:'Phường Hoa Lư',distance_m:80000,duration_s:4000}]};
  await apply(h,next);
  const panel=h.elements.get('#destination-choices'),button=panel.children.find(node=>node.tagName==='button');
  await button.listeners.click();
  assert.equal(h.commands[0][1].capability,'navigation.select');
  assert.equal(h.commands[0][1].arguments.place_handle,'place:1');
  await apply(h,nav('preview',2,2));
  assert.equal(panel.hidden,true);
});

test('arrival uses the server short label while the map keeps the full place name',async()=>{
  const h=harness(),next=nav('running');
  next.selected={...next.selected,name:'Quán Cà Phê Habakuk Ngõ Phan Huy Chú',spoken_label:'Quán Cà Phê Habakuk, Phường Cửa Nam'};
  await apply(h,next);
  assert.equal(vm.runInContext('destination.name',h.sandbox),'Quán Cà Phê Habakuk Ngõ Phan Huy Chú');
  await apply(h,{...next,status:'completed',event_sequence:2,progress_m:10000});
  assert.deepEqual(h.announcements,['Đã đến Quán Cà Phê Habakuk, Phường Cửa Nam.']);
});

test('voice routing obtains origin without opening the map',async()=>{
  const h=harness();h.sandbox.opened=0;h.sandbox.positions=[];
  h.sandbox.navigator={geolocation:{getCurrentPosition:success=>success({coords:{latitude:21,longitude:105.8}})}};
  vm.runInContext("openJourney=async()=>{opened++;}; api=async()=>({default_center:{lat:21,lng:105.8,label:'TEST'}}); hooks.position=point=>positions.push(point);",h.sandbox);
  h.sandbox.payload={navigation:{...nav('idle',0,0),origin:null,selected:null,route:null},ui_events:[{type:'navigation.locate'}]};
  await vm.runInContext('applyNavigation(payload)',h.sandbox);
  assert.equal(h.sandbox.opened,0);
  assert.equal(h.sandbox.positions.length,1);
  assert.equal(h.sandbox.positions[0].simulated,false);
});

test('critical announcement preempts once and other speech waits in the same queue',async()=>{
  const h=harness(),spoken=[];let release;
  Object.assign(h.sandbox,{state:{sound:true,busy:false},makeId:()=> 'id',window:{},currentPlayback:null,stopPlayback(){},say(){},speak:async text=>{spoken.push(text);if(text==='critical')await new Promise(resolve=>release=resolve);}});
  const app=readFileSync('app.js','utf8');
  vm.runInContext(app.slice(app.indexOf('const announcementQueue = []'),app.indexOf('async function pollVehicleAlerts')),h.sandbox);
  vm.runInContext("queueAnnouncement('critical',2,'critical'); queueAnnouncement('turn direction',0,'nav')",h.sandbox);
  assert.deepEqual(spoken,['critical']);
  assert.equal(h.sandbox.state.audioEpoch,1);
  release(); await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(spoken,['critical','turn direction']);
});

test('real charging choices show source and route context; selecting still goes through the backend',async()=>{
  const h=harness({withEnergy:true}),next=nav();
  next.energy={status:'station_data_required',message:'Chưa có công suất',station_candidates:[{
    id:'map-station-1',handle:'place:charging-1',name:'Trạm Sạc VinFast TEST',address:'Xã TEST',rest_area_name:'Trạm Dừng Nghỉ TEST',road_context:'Cao tốc TEST',lat:21,lng:105.95,
    distance_m:70000,duration_s:3600,arrival_soc:26.67,detour_m:1200,equipment_status:'missing',power_kw:null
  }]};
  await apply(h,next);
  const item=h.elements.get('#journey-energy').children[0];
  assert.match(item.children[1].textContent,/Trạm Dừng Nghỉ TEST.*Cao tốc TEST.*pin đến 26%/);
  assert.match(item.children[2].textContent,/Chưa có công suất/);
  assert.match(item.children[3].textContent,/VietMap/);
  assert.equal(vm.runInContext('sim.running',h.sandbox),false);
  assert.equal(h.commands.length,0);
  item.children.at(-1).listeners.click();
  assert.equal(h.commands[0][1].capability,'navigation.select');
  assert.equal(h.commands[0][1].arguments.place_handle,'place:charging-1');
  assert.deepEqual(Array.from(vm.runInContext("markers['charging:map-station-1'].point",h.sandbox)),[105.95,21]);
  assert.deepEqual(h.driving,[]);
});

test('old station markers disappear after destination revision and stale responses cannot restore them',async()=>{
  const h=harness(),first=nav();
  first.energy={station_candidates:[{id:'old',lat:21,lng:105.95}]};
  await apply(h,first);
  assert.equal(vm.runInContext("!!markers['charging:old']",h.sandbox),true);
  const next=nav('preview',2,2);next.energy={station_candidates:[{id:'new',lat:21,lng:106}]};
  await apply(h,next);
  assert.equal(vm.runInContext("!!markers['charging:old']",h.sandbox),false);
  await apply(h,first);
  assert.equal(vm.runInContext("!!markers['charging:new'] && !markers['charging:old']",h.sandbox),true);
});

test('hiding a place panel survives progress and can be reopened without cancelling',async()=>{
  const h=harness();const next={...nav('selecting'),request_id:'request:one',selected:null,route:null,candidates:[{handle:'place:1',name:'Tràng An'}]};
  await apply(h,next);
  const panel=h.elements.get('#destination-choices');const header=panel.children.find(node=>node.className==='information-heading');
  header.children[1].listeners.click();assert.equal(panel.hidden,true);assert.equal(h.commands.length,0);
  await apply(h,{...next,event_sequence:2});assert.equal(panel.hidden,true);
  const tabs=h.elements.get('#information-tabs');await tabs.children.at(-1).listeners.click();assert.equal(panel.hidden,false);
  await apply(h,{...next,request_id:'request:two',revision:2,event_sequence:3});assert.equal(panel.hidden,false);
});

test('trip planning presents a card without opening map or changing running route',async()=>{
  const h=harness();h.sandbox.opened=0;vm.runInContext('openJourney=async()=>{opened++;}',h.sandbox);
  await apply(h,nav('running',2,1));const before=vm.runInContext('JSON.stringify(route)',h.sandbox);
  h.sandbox.payload={trip_plan:{...nav(),id:'trip:1',event_sequence:2,trip_kind:'round_trip',calculation_status:'ready',offer:{status:'pending'},summary:null},navigation:nav('running',2,2),ui_events:[{type:'trip.summary'}]};
  await vm.runInContext('applyNavigation(payload)',h.sandbox);
  assert.equal(h.sandbox.opened,1); // only the running navigation opened the map
  assert.equal(vm.runInContext('JSON.stringify(route)',h.sandbox),before);
  assert.equal(vm.runInContext('sim.running',h.sandbox),true);
  assert.equal(h.elements.get('#trip-plan-card').hidden,false);
  const actions=h.elements.get('#trip-plan-card').children.find(node=>node.className==='trip-actions');
  await actions.children[2].listeners.click();assert.equal(h.commands.at(-1)[1].capability,'trip.start');assert.equal(h.commands.at(-1)[1].arguments.plan_id,'trip:1');
  h.sandbox.payload={trip_plan:{...h.sandbox.payload.trip_plan,id:'trip:2',revision:2,event_sequence:3},ui_events:[{type:'trip.summary'}]};await vm.runInContext('applyNavigation(payload)',h.sandbox);
  const count=h.commands.length;await actions.children[2].listeners.click();assert.equal(h.commands.length,count);
});


test('server arrival retains its route and supplies the actual endpoint for the next trip',async()=>{
  const h=harness();
  await apply(h,nav('running',1,1));
  await apply(h,{...nav('completed',1,2),progress_m:10000});
  assert.equal(vm.runInContext('route.coordinates.length',h.sandbox),2);
  assert.equal(vm.runInContext('getJourneyPosition().lng',h.sandbox),105.9);
  assert.deepEqual(h.driving,[]);
  const roundTrip={...nav('completed',2,3),trip_kind:'round_trip'};
  roundTrip.route.coordinates=[[105.8,21],[105.9,21],[105.8,21]];
  await apply(h,roundTrip);
  assert.equal(vm.runInContext('getJourneyPosition().lng',h.sandbox),105.8);
});
