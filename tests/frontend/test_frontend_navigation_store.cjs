const {test}=require('node:test');const assert=require('node:assert/strict');const vm=require('node:vm');const {readFileSync}=require('node:fs');
const box={crypto:require('node:crypto').webcrypto};vm.createContext(box);
for(const file of ['navigation-state.js','navigation-store.js','journey-simulation.js'])vm.runInContext(readFileSync(file,'utf8').replace(/^import .*$/gm,'').replace(/export /g,''),box);
vm.runInContext('globalThis.types={NavigationStore,MissingRouteError,DriveSimulation}',box);
const {NavigationStore,MissingRouteError,DriveSimulation}=box.types;
const full={runtime_epoch:'one',snapshot_sequence:1,navigation:{route_id:'route:1',route:{coordinates:[[105,21],[106,21]],steps:[],distance_m:1000},waypoint_routes:[],progress_m:0}};
test('telemetry uses shared geometry; missing cache does not advance sequence',()=>{
 const store=new NavigationStore();store.receive(full,true);
 const next=store.receive({runtime_epoch:'one',snapshot_sequence:2,navigation:{route_id:'route:1',route_reference:'route:1',progress_m:10}});
 assert.equal(next.navigation.route,full.navigation.route);
 assert.throws(()=>store.receive({runtime_epoch:'one',snapshot_sequence:3,navigation:{route_reference:'missing'}}),MissingRouteError);
 assert.equal(store.order.sequence,2);
 assert.equal(store.receive(full),null);
});
test('new runtime must be authoritative and retired snapshots never restore a route',()=>{
 const store=new NavigationStore();store.receive(full,true);
 assert.equal(store.receive({...full,runtime_epoch:'two',snapshot_sequence:0}),null);
 assert.ok(store.receive({...full,runtime_epoch:'two',snapshot_sequence:0},true));
 assert.equal(store.receive(full,true),null);
});
test('10000 point routes interpolate and find maneuvers by binary search',()=>{
 const coordinates=Array.from({length:10000},(_,i)=>[105+i*.00001,21]);
 const route=DriveSimulation.prepare({coordinates,distance_m:10000,duration_s:1000,steps:[{point_index:0},{point_index:5000}]},()=>1);
 const sample=DriveSimulation.position(route,5000.5,()=>90);
 assert.equal(sample.index,5000);assert.equal(sample.heading,90);assert.equal(DriveSimulation.step(route,sample.index),1);
});
