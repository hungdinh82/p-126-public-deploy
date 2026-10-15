const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const vm=require('node:vm');
const {test}=require('node:test');

function harness({withEnergy=false}={}) {
  const elements=new Map(),commands=[],events=[],driving=[],announcements=[],sources=new Map(),routes=[];
  const element=()=>{const classes=new Set();return {hidden:false,value:'',textContent:'',children:[],listeners:{},get innerHTML(){return '';},set innerHTML(value){this.children=[];},append(...nodes){for(const node of nodes){node.parentElement=this;this.children.push(node);}},addEventListener(type,handler){this.listeners[type]=handler;},classList:{add:name=>classes.add(name),remove:name=>classes.delete(name),contains:name=>classes.has(name),toggle(name,on){if(on)classes.add(name);else classes.delete(name);}},setAttribute(){},scrollIntoView(){},querySelectorAll:()=>[]};};
  const sandbox={console,crypto:{randomUUID:()=>String(events.length+1)},performance:{now:()=>1000},requestAnimationFrame:()=>1,cancelAnimationFrame(){},setTimeout:()=>1,setInterval:()=>1,clearInterval(){},
    document:{createElement:tag=>({...element(),tagName:tag}),querySelector:selector=>{if(selector==='#journey-energy'&&!withEnergy)return null;if(!elements.has(selector))elements.set(selector,element());return elements.get(selector);},querySelectorAll:()=>[],body:element()},
    stubMap:{flyTo(){},easeTo(){},jumpTo(){},resize(){},getStyle:()=>({sources:{}}),getSource:name=>{if(!sources.has(name))sources.set(name,{setData(data){this.data=data;}});return sources.get(name);}},
    reportRoute:value=>routes.push(value),
    command:(...args)=>commands.push(args),event:async value=>events.push(value),setDriving:value=>driving.push(value),announce:text=>announcements.push(text),
  };
  vm.createContext(sandbox);
  for(const file of ['journey-map.js','journey-simulation.js','journey-renderer.js'])vm.runInContext(readFileSync(file,'utf8').replace(/export /g,''),sandbox);
  vm.runInContext(readFileSync('journey.js','utf8').replace(/^import .*$/gm,'').replace(/export /g,''),sandbox);
  vm.runInContext(`map=stubMap; hooks={command,event,setDriving,announce,progress(){},route:reportRoute,travel(){},confirm:grant=>command('confirm',grant)}; realOpenJourney=openJourney; openJourney=async()=>{}; placeMarker=(name,point)=>{markers[name]={point,setRotation(){},remove(){}};return markers[name];}; planRoute=()=>{throw Error('Client must not recompute server route');};`,sandbox);
  return {sandbox,commands,events,driving,announcements,elements,sources,routes};
}
function nav(status='preview',revision=1,sequence=1) {
  return {revision,event_sequence:sequence,status,origin:{lat:21,lng:105.8,label:'TEST',simulated:true},candidates:[],selected:{name:'TEST destination',lat:21,lng:105.9},route:{distance_m:10000,duration_s:500,coordinates:[[105.8,21],[105.9,21]],steps:[]},progress_m:0,energy:null,alternatives:[],pending:null};
}
async function apply(h,value,confirmation=null){h.sandbox.payload={navigation:value,confirmation,ui_events:[{type:'navigation.preview'}]};await vm.runInContext('applyNavigation(payload)',h.sandbox);}

test('charging legs drive the HUD and transition announcements are deduplicated',async()=>{
  const h=harness();
  const plan={stops:[{station_id:'s1',name:'Trạm một',leg_index:0,charge_minutes:10},
                    {station_id:'s2',name:'Trạm hai',leg_index:1,charge_minutes:15}],
    legs:[{to:'s1',distance_m:3000,duration_s:150},{to:'s2',distance_m:4000,duration_s:200},
          {to:'destination',distance_m:3000,duration_s:150}],total_seconds:2000};
  const base={...nav('running'),route_id:'route:test',stop_index:0,energy:{selected_plan:plan}};
  await apply(h,base);
  let leg=vm.runInContext('currentLeg(0)',h.sandbox);
  assert.match(leg.destination,/Trạm một/);assert.equal(leg.remaining,3000);
  h.sandbox.payload={navigation:{...base,status:'charging',progress_m:3000,event_sequence:2},
    ui_events:[{type:'navigation.charging',id:'arrival-1',route_id:'route:test',message:'Đã đến trạm một. Sạc khoảng 10 phút.'}]};
  await vm.runInContext('applyNavigation(payload); applyNavigation(payload)',h.sandbox);
  assert.deepEqual(h.announcements,['Đã đến trạm một. Sạc khoảng 10 phút.']);
  assert.equal(vm.runInContext('currentLeg().chargingMinutes',h.sandbox),10);
  const atStation=vm.runInContext('getJourneyPosition()',h.sandbox);
  assert.ok(Math.abs(atStation.lng-105.83)<.001);
  await apply(h,{...base,status:'charging',progress_m:3000,charge_elapsed_s:120,event_sequence:3});
  assert.equal(vm.runInContext('currentLeg().chargingMinutes',h.sandbox),8);
  h.sandbox.payload={navigation:{...base,stop_index:1,progress_m:3000,event_sequence:4},
    ui_events:[{type:'navigation.next_leg',id:'charged-1',route_id:'route:test',message:'Đã sạc đủ. Đi tới trạm hai.'}]};
  await vm.runInContext('applyNavigation(payload)',h.sandbox);
  leg=vm.runInContext('currentLeg()',h.sandbox);
  assert.match(leg.destination,/Trạm hai/);assert.equal(leg.remaining,4000);
  assert.equal(h.announcements.length,2);
  await apply(h,{...base,stop_index:2,progress_m:7000,event_sequence:5});
  leg=vm.runInContext('currentLeg()',h.sandbox);
  assert.equal(leg.destination,'TEST destination');assert.equal(leg.remaining,3000);
});

test('a repeated charging station targets its return visit and restored charging is quiet',async()=>{
  const h=harness();
  const plan={stops:[{station_id:'same',name:'Trạm TEST',leg_index:0,charge_minutes:10},
                    {station_id:'same',name:'Trạm TEST',leg_index:2,charge_minutes:12}],
    legs:[{to:'same',distance_m:2000,duration_s:100},{to:'destination',distance_m:3000,duration_s:150},
          {to:'same',distance_m:3000,duration_s:150},{to:'return',distance_m:2000,duration_s:100}],total_seconds:2000};
  await apply(h,{...nav('running'),trip_kind:'round_trip',stop_index:1,progress_m:5000,energy:{selected_plan:plan}});
  assert.equal(vm.runInContext('currentLeg().remaining',h.sandbox),3000);
  await apply(h,{...nav('charging',1,2),trip_kind:'round_trip',stop_index:1,progress_m:8000,charge_elapsed_s:120,energy:{selected_plan:plan}});
  assert.equal(vm.runInContext('currentLeg().chargingMinutes',h.sandbox),10);
  assert.deepEqual(h.announcements,[]);
  await apply(h,{...nav('running',1,3),trip_kind:'round_trip',stop_index:2,progress_m:8000,energy:{selected_plan:plan}});
  assert.equal(vm.runInContext('currentLeg().destination',h.sandbox),'TEST');
});

test('server preview waits for consent and active simulation ignores old route/sequence',async()=>{
  const h=harness();
  await apply(h,nav(),{confirmation_id:'grant-1'});
  vm.runInContext('startSimulation()',h.sandbox);
  assert.equal(vm.runInContext('sim.running',h.sandbox),false);
  assert.equal(h.commands[0][1].capability,'navigation.start');
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

test('destination results and ready plans stay on home until map is explicitly opened',async()=>{
  const h=harness();h.sandbox.opened=0;
  vm.runInContext('openJourney=async()=>{opened++;}',h.sandbox);
  await apply(h,{...nav('selecting'),selected:null,route:null,candidates:[]});
  assert.equal(h.sandbox.opened,0);
  assert.equal(vm.runInContext('sim.running',h.sandbox),false);
  await apply(h,nav('preview',2,2));
  assert.equal(h.sandbox.opened,0);
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
  assert.equal(h.sandbox.opened,0); // driving and trip planning leave the 3D scene visible
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


test('delayed explicit map opening cannot restore a cancelled route',async()=>{
  const h=harness();let release;
  await apply(h,nav('preview',1,1));
  h.sandbox.waitForMap=()=>new Promise(resolve=>{release=resolve;});
  vm.runInContext('openJourney=waitForMap;',h.sandbox);
  const opening=vm.runInContext("showPlanMap(backendNavigation,'navigation')",h.sandbox);
  await apply(h,{...nav('idle',2,2),route:null,selected:null});
  release();await opening;
  assert.equal(vm.runInContext('backendNavigation.revision',h.sandbox),2);
  assert.equal(vm.runInContext('route',h.sandbox),null);
  assert.equal(vm.runInContext('sim.running',h.sandbox),false);
});

test('stale navigation list cannot hide the current trip choices',async()=>{
  const h=harness();await apply(h,nav('preview',4,4));
  h.sandbox.trip={id:'trip:new',revision:1,event_sequence:5,status:'selecting',calculation_status:'draft',route:null,candidates:[{handle:'trip:place',name:'Trip destination'}]};
  await vm.runInContext("applyNavigation({trip_plan:trip,ui_events:[{type:'trip.results'}]})",h.sandbox);
  h.sandbox.oldList={...nav('selecting',3,3),route:null,selected:null};
  await vm.runInContext("applyNavigation({navigation:oldList,ui_events:[{type:'navigation.results'}]})",h.sandbox);
  assert.equal(vm.runInContext('choiceOwner',h.sandbox),'trip');
  assert.equal(h.elements.get('#destination-choices').hidden,false);
});

test('a zero-distance route sends a finite completion progress',async()=>{
  const h=harness();const same=nav('running');same.route={...same.route,distance_m:0,duration_s:0,coordinates:[[105.8,21],[105.8,21]]};
  await apply(h,same);
  vm.runInContext('tick(2500)',h.sandbox);
  assert.equal(h.events[0].distance_m,0);
  assert.equal(Number.isFinite(vm.runInContext('sim.traveled',h.sandbox)),true);
});

test('starting and repeated responses reveal the 3D screen instead of reopening the full map',async()=>{
  const h=harness();vm.runInContext('openJourney=realOpenJourney;',h.sandbox);
  await apply(h,nav('preview',1,1));
  assert.equal(h.sandbox.document.body.classList.contains('journey-open'),false);
  await vm.runInContext('openJourney()',h.sandbox);
  assert.equal(h.sandbox.document.body.classList.contains('journey-open'),true);
  await apply(h,nav('running',1,2));
  assert.equal(vm.runInContext('view',h.sandbox),'mini');
  assert.equal(h.sandbox.document.body.classList.contains('journey-open'),false);
  assert.equal(vm.runInContext('sim.running',h.sandbox),true);
  await apply(h,nav('running',1,3));
  assert.equal(vm.runInContext('view',h.sandbox),'mini');
  assert.equal(h.sandbox.document.body.classList.contains('journey-open'),false);
  await vm.runInContext('openJourney()',h.sandbox);
  await apply(h,nav('running',1,4));
  assert.equal(vm.runInContext('view',h.sandbox),'full'); // explicitly opening the map still works
});

test('starting a trip from home loads the map without covering the 3D simulation',async()=>{
  const h=harness();vm.runInContext('map=null; ensureMap=async()=>{map=stubMap;}; openJourney=realOpenJourney;',h.sandbox);
  h.sandbox.started={trip_plan:{...nav(),id:'trip:started',calculation_status:'ready',started_revision:1,offer:{status:'accepted'}},navigation:nav('running',2,2),ui_events:[{type:'navigation.start'}]};
  await vm.runInContext('applyNavigation(started)',h.sandbox);
  assert.equal(vm.runInContext('view',h.sandbox),'mini');
  assert.equal(vm.runInContext('sim.running',h.sandbox),true);
  assert.equal(h.sandbox.document.body.classList.contains('journey-open'),false);
  assert.equal(h.elements.get('#planning-controls').hidden,true);
});

test('home cancellation remains available after closing the preview and clears the cancelled route',async()=>{
  const h=harness();vm.runInContext('openJourney=realOpenJourney;',h.sandbox);
  await apply(h,nav('preview',1,1));
  await vm.runInContext('closeJourney()',h.sandbox);
  const panel=h.elements.get('#trip-plan-card'),cancel=panel.children.at(-1);
  assert.equal(panel.hidden,false);assert.equal(cancel.textContent,'Huỷ kế hoạch');
  await cancel.listeners.click();
  assert.equal(h.commands[0][1].capability,'navigation.cancel');
  assert.equal(h.commands[0][1].arguments.route_revision,1);
  await apply(h,{...nav('idle',2,2),route:null,selected:null});
  assert.equal(panel.hidden,true);
  assert.equal(vm.runInContext('route',h.sandbox),null);
  assert.equal(vm.runInContext('destination',h.sandbox),null);
  assert.equal(vm.runInContext('view',h.sandbox),'hidden');
  const count=h.commands.length;await cancel.listeners.click();assert.equal(h.commands.length,count);
});

test('hidden trip card hides its cancellation action until the card is reopened',async()=>{
  const h=harness();h.sandbox.plan={...nav(),id:'trip:cancel',calculation_status:'ready',offer:{status:'pending'}};
  await vm.runInContext("applyNavigation({trip_plan:plan,ui_events:[{type:'trip.summary'}]})",h.sandbox);
  const card=h.elements.get('#trip-plan-card');card.children[0].children[1].listeners.click();
  assert.equal(card.hidden,true);
  const controls=h.elements.get('#planning-controls');assert.equal(controls.hidden,true);
  h.elements.get('#information-tabs').children[0].listeners.click();
  const cancel=card.children.at(-1);assert.equal(card.hidden,false);assert.equal(cancel.textContent,'Huỷ kế hoạch');
  await cancel.listeners.click();
  assert.equal(h.commands[0][1].capability,'trip.cancel');
  assert.equal(h.commands[0][1].arguments.plan_id,'trip:cancel');
  assert.equal(h.commands[0][1].arguments.route_revision,1);
  await vm.runInContext("applyNavigation({trip_plan:{...plan,revision:2,event_sequence:2,calculation_status:'cancelled'},ui_events:[{type:'trip.cancel'}]})",h.sandbox);
  assert.equal(controls.hidden,true);assert.equal(card.hidden,true);
  const count=h.commands.length;await cancel.listeners.click();assert.equal(h.commands.length,count);
});

test('a draft without origin or map results can be cancelled from home',async()=>{
  const h=harness();h.sandbox.draft={navigation:{...nav('idle',0,1),origin:null,route:null,selected:null},steps:[{capability:'navigation.plan',status:'waiting_slot'}]};
  await vm.runInContext('applyNavigation(draft)',h.sandbox);
  const panel=h.elements.get('#planning-controls');assert.equal(panel.hidden,false);
  await panel.children[0].listeners.click();
  assert.equal(h.commands[0][1].capability,'navigation.cancel');
  assert.equal(h.commands[0][1].arguments.route_revision,0);
});

test('cancelling a pending route preserves the running simulation',async()=>{
  const h=harness();vm.runInContext('openJourney=realOpenJourney;',h.sandbox);
  await apply(h,nav('running',1,1));
  const next=nav('running',2,2);next.pending={navigation:nav('preview',2,2),message:'Đổi tuyến'};
  await apply(h,next);
  const cancel=h.elements.get('#trip-plan-card').children.at(-1);await cancel.listeners.click();
  assert.equal(h.commands[0][1].capability,'navigation.cancel');
  await apply(h,nav('running',3,3));
  assert.equal(vm.runInContext('sim.running',h.sandbox),true);
  assert.equal(vm.runInContext('view',h.sandbox),'mini');
  assert.equal(h.elements.get('#planning-controls').hidden,true);
});

test('changing destination replaces the map and 3D route even at the same revision',async()=>{
  const h=harness();await apply(h,nav('running',1,1));
  const changed=nav('running',1,2);changed.route_id='route:vinhomes';changed.selected={name:'Vinhomes',lat:21,lng:106};changed.route.coordinates=[[105.8,21],[106,21]];
  await apply(h,changed);
  assert.equal(vm.runInContext('destination.name',h.sandbox),'Vinhomes');
  assert.equal(vm.runInContext('route.coordinates.at(-1)[0]',h.sandbox),106);
  assert.equal(h.routes.at(-1).coordinates.at(-1)[0],106);
  assert.equal(h.sources.get('vivi-route').data.geometry.coordinates.at(-1)[0],106);
  assert.equal(h.sources.get('vivi-traveled').data.geometry.coordinates.length,0);
  assert.equal(vm.runInContext('sim.traveled',h.sandbox),0);
});

test('a pending Vinhomes route does not overwrite the current map or 3D route',async()=>{
  const h=harness();await apply(h,nav('running',1,1));
  const next=nav('running',2,2);next.pending={navigation:nav('preview',2,2),message:'Tuyến mới chờ xác nhận'};
  next.pending.navigation.selected.name='Vinhomes';next.pending.navigation.route.coordinates=[[105.8,21],[106,21]];
  await apply(h,next);
  assert.equal(h.sources.get('vivi-route').data.geometry.coordinates.at(-1)[0],105.9);
  assert.equal(h.sources.get('vivi-pending-preview').data.geometry.coordinates.at(-1)[0],106);
  assert.equal(h.routes.at(-1).coordinates.at(-1)[0],105.9);
  assert.match(h.elements.get('#journey-state').textContent,/Đang đi.*TEST destination.*Chờ xác nhận: Vinhomes/);
});

test('P stops immediately, ignores in-flight running state, and D resumes at server progress',async()=>{
  const h=harness();await apply(h,nav('running',1,1));
  vm.runInContext('setJourneyMotionBlocked(true)',h.sandbox);
  await vm.runInContext('pauseJourney()',h.sandbox);
  assert.equal(h.events[0].kind,'pause');
  assert.equal(vm.runInContext('sim.running',h.sandbox),false);
  assert.equal(vm.runInContext('view',h.sandbox),'hidden');
  await apply(h,nav('running',1,2));
  assert.equal(vm.runInContext('sim.running',h.sandbox),false);
  const paused={...nav('paused',1,3),resume_status:'running',progress_m:1000};await apply(h,paused);
  await vm.runInContext('closeJourney()',h.sandbox);assert.equal(vm.runInContext('view',h.sandbox),'hidden');
  await vm.runInContext('changeJourneyDriving(true)',h.sandbox);
  assert.equal(h.events.at(-1).kind,'gear');assert.equal(h.events.at(-1).gear,'D');
  h.sandbox.resumed={...paused,status:'running',event_sequence:4,resume_status:null};
  vm.runInContext('setJourneyMotionBlocked(false)',h.sandbox);
  await vm.runInContext("applyNavigation({navigation:resumed,ui_events:[{type:'navigation.resume'}]})",h.sandbox);
  assert.equal(vm.runInContext('sim.running',h.sandbox),true);
  assert.ok(Math.abs(vm.runInContext('sim.traveled/route.length',h.sandbox)-0.1)<1e-9);
  assert.equal(vm.runInContext('view',h.sandbox),'mini');
});

test('hide all removes the parked map, planning controls and reopen tabs until explicit opening',async()=>{
  const h=harness();await apply(h,nav('paused',1,1));
  h.sandbox.plan={...nav(),id:'trip:hidden',calculation_status:'ready',offer:{status:'pending'}};
  await vm.runInContext("applyNavigation({trip_plan:plan,ui_events:[{type:'trip.summary'}]}); hideInformation(informationKey(plan,'Kế hoạch')); hideJourneyInformation();",h.sandbox);
  assert.equal(h.elements.get('#information-rail').classList.contains('journey-information-hidden'),true);
  assert.equal(vm.runInContext('view',h.sandbox),'hidden');
  await apply(h,nav('paused',1,2));
  assert.equal(h.elements.get('#information-rail').classList.contains('journey-information-hidden'),true);
  vm.runInContext('openJourney=realOpenJourney;',h.sandbox);await vm.runInContext('openJourney()',h.sandbox);
  assert.equal(h.elements.get('#information-rail').classList.contains('journey-information-hidden'),false);
  await apply(h,nav('paused',1,3));
  assert.equal(vm.runInContext('view',h.sandbox),'full');
});

test('clearing removes saved plan and route, and stale responses cannot restore them',async()=>{
  const h=harness();await apply(h,nav('paused',1,1));
  h.sandbox.saved={...nav(),id:'trip:old',calculation_status:'ready',offer:{status:'pending'}};
  await vm.runInContext("applyNavigation({trip_plan:saved,ui_events:[{type:'trip.summary'}]})",h.sandbox);
  h.sandbox.cleared={...nav('idle',2,2),route:null,selected:null};
  await vm.runInContext("applyNavigation({navigation:cleared,trip_plan:null,ui_events:[{type:'navigation.clear'}]})",h.sandbox);
  assert.equal(vm.runInContext('backendTripPlan',h.sandbox),null);
  assert.equal(vm.runInContext('route',h.sandbox),null);
  assert.equal(h.elements.get('#information-tabs').children.length,0);
  assert.equal(h.elements.get('#journey-toolbar').hidden,true);
  await vm.runInContext("applyNavigation({trip_plan:saved,ui_events:[{type:'trip.summary'}]})",h.sandbox);
  assert.equal(vm.runInContext('backendTripPlan',h.sandbox),null);
  assert.equal(h.elements.get('#trip-plan-card').hidden,true);
});

test('gear button uses the journey reducer and does not overwrite a blocked resume',async()=>{
  let click;const phases=[],changes=[],fixtures=[];
  const sandbox={state:{busy:false,driving:false,backendAvailable:true,navigationSynced:true},$:()=>({addEventListener:(type,handler)=>{click=handler;}}),lockControls(){},snapshotOrder:{epoch:'test'},gearController:{request:async target=>{changes.push(target);phases.push('blocked');return true;}},setDemoDriving:async value=>fixtures.push(value),setPhase:phase=>phases.push(phase)};
  vm.createContext(sandbox);const app=readFileSync('app.js','utf8');
  vm.runInContext(app.slice(app.indexOf("$('#drive-toggle').addEventListener"),app.indexOf('function setSound')),sandbox);
  await click();assert.deepEqual(changes,["D"]);assert.deepEqual(fixtures,[]);assert.deepEqual(phases,['blocked']);
});

test('roundabout presentation leaves server route data unchanged for replay',async()=>{
  const h=harness(),next=nav('running',1,1);
  next.route.steps=[{sign:6,point_index:0,text:'Vòng xuyến'},{sign:2,point_index:1,text:'Rẽ phải'}];
  const original=JSON.stringify(next.route);await apply(h,next);
  assert.equal(JSON.stringify(next.route),original);
  const count=h.routes.length;await apply(h,{...next,event_sequence:2});
  assert.equal(h.routes.length,count);
});

test('P remains available during an assistant turn and interrupts it before pausing',async()=>{
  let click,interrupts=0;const changes=[];
  const sandbox={state:{busy:true,driving:true,backendAvailable:true,navigationSynced:true},$:()=>({addEventListener:(type,handler)=>{click=handler;}}),lockControls(){},interruptActiveTurn:()=>{interrupts++;sandbox.state.busy=false;},snapshotOrder:{epoch:'test'},gearController:{request:async target=>{changes.push(target);return true;}},setDemoDriving:async()=>{throw Error('Must pause through the journey reducer');},setPhase(){}};
  vm.createContext(sandbox);const app=readFileSync('app.js','utf8');
  vm.runInContext(app.slice(app.indexOf("$('#drive-toggle').addEventListener"),app.indexOf('function setSound')),sandbox);
  await click();assert.equal(interrupts,1);assert.deepEqual(changes,["P"]);
});

test('stale navigation cannot restore cleared workspace history',async()=>{
  const h=harness();h.sandbox.current=nav('paused',3,4);
  await vm.runInContext('applyNavigation({navigation:current,navigation_workspace:{history:[]}})',h.sandbox);
  h.sandbox.old=nav('paused',2,3);
  await vm.runInContext("applyNavigation({navigation:old,navigation_workspace:{history:[{id:'old'}]}})",h.sandbox);
  assert.equal(vm.runInContext('backendWorkspace.history.length',h.sandbox),0);
});

test('P during charging does not freeze the charging clock',async()=>{
  const h=harness();await apply(h,nav('charging',1,1));
  await vm.runInContext('changeJourneyDriving(false)',h.sandbox);
  assert.equal(vm.runInContext('motionBlocked',h.sandbox),false);
  assert.equal(vm.runInContext('sim.running',h.sandbox),false);
  assert.notEqual(vm.runInContext('chargingTimer',h.sandbox),null);
  assert.equal(h.events.at(-1).gear,'P');
});

test('a newly requested trip opens information but progress preserves hidden state',async()=>{
  const h=harness();await apply(h,nav('idle',1,1));
  vm.runInContext('journeyInformationHidden=true',h.sandbox);
  h.sandbox.plan={...nav(),id:'trip:new',calculation_status:'ready'};
  await vm.runInContext("applyNavigation({trip_plan:plan,ui_events:[{type:'trip.summary'}]})",h.sandbox);
  assert.equal(vm.runInContext('journeyInformationHidden',h.sandbox),false);
  vm.runInContext('journeyInformationHidden=true',h.sandbox);
  await vm.runInContext("applyNavigation({trip_plan:plan,ui_events:[{type:'trip.summary'}]})",h.sandbox);
  assert.equal(vm.runInContext('journeyInformationHidden',h.sandbox),true);
});

test('map SDK failure does not prevent accepted route from reaching the 3D controller',async()=>{
  const h=harness();vm.runInContext("map=null;ensureMap=async()=>{throw Error('Map unavailable');};",h.sandbox);
  await apply(h,nav('running',1,1));
  assert.equal(vm.runInContext('sim.running',h.sandbox),true);
  assert.ok(h.routes.at(-1).coordinates.length>1);
  await new Promise(setImmediate);
  assert.equal(vm.runInContext('backendNavigation.status',h.sandbox),'running');
});

test('running snapshots never move interpolated distance backwards',async()=>{
  const h=harness();await apply(h,nav('running',1,1));
  vm.runInContext('sim.traveled=route.length*.5',h.sandbox);
  await apply(h,{...nav('running',1,2),progress_m:1000});
  assert.equal(vm.runInContext('sim.traveled/route.length',h.sandbox),.5);
});
