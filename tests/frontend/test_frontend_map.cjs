const {test}=require('node:test');const assert=require('node:assert/strict');const vm=require('node:vm');const fs=require('node:fs');
function fixture(){
 const timers=[],maps=[],notices=[];let loaded=true,resizes=0,observer;
 class MapSDK {
  constructor(){this.handlers={};this.sources={};this.layers={};maps.push(this);}
  on(name,fn){(this.handlers[name] ||= []).push(fn);}
  once(name,fn){this.on(name,fn);}
  emit(name,value){for(const fn of this.handlers[name] || [])fn(value);}
  isStyleLoaded(){return loaded;}
  addControl(){} getSource(id){return this.sources[id];}getLayer(id){return this.layers[id];}
  addSource(id,source){this.sources[id]=source;}addLayer(layer){this.layers[layer.id]=layer;}
  getContainer(){return {clientWidth:400,clientHeight:200};}resize(){resizes++;}
  remove(){this.removed=true;}jumpTo(){this.jumps=(this.jumps || 0)+1;}
 }
 const context={setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout(){},ResizeObserver:class{constructor(fn){observer=this;this.fn=fn;}observe(){}disconnect(){this.disconnected=true;}},vietmapgl:{Map:MapSDK,NavigationControl:class{}},document:{createElement:()=>({remove(){}}),head:{append(){}}}};
 vm.createContext(context);vm.runInContext(fs.readFileSync('web/journey-map.js','utf8').replace('export class','class')+';globalThis.JourneyMap=JourneyMap',context);
 let renders=0,latest='route:one';const seen=[];
 const map=new context.JourneyMap({fetchConfig:async()=>({available:true,style_url:'fixture',default_center:{lat:21,lng:105}}),notice:text=>notices.push(text),ready:()=>{renders++;seen.push(latest);},attach(){}});
 return {map,maps,timers,notices,seen,get renders(){return renders;},setLatest:value=>latest=value,setLoaded:value=>loaded=value,get observer(){return observer;},get resizes(){return resizes;}};
}
test('style reload rebuilds layers from the latest snapshot; resizing and camera are bounded',async()=>{
 const f=fixture(),map=await f.map.ensure();assert.equal(f.renders,1);assert.ok(map.getSource('vivi-route'));
 map.sources={};map.layers={};f.setLatest('route:two');map.emit('style.load');assert.ok(map.getLayer('vivi-route-line'));assert.deepEqual(f.seen,['route:one','route:two']);
 f.observer.fn();assert.equal(f.resizes,1);
 for(let now=0;now<1000;now++)f.map.camera({},now);
 assert.ok(map.jumps<=30);
 map.emit('error',{error:{status:403}});assert.match(f.notices.at(-1),/HTTP 403/);assert.equal(f.map.status,'ready');
 f.map.dispose();assert.equal(map.removed,true);assert.equal(f.observer.disconnected,true);
});
test('style timeout removes resources and explicit retry starts a fresh map',async()=>{
 const f=fixture();f.setLoaded(false);const pending=f.map.ensure();await new Promise(setImmediate);
 f.timers.at(-1)();await assert.rejects(pending,/style/);assert.equal(f.maps[0].removed,true);assert.equal(f.map.status,'error');
 f.setLoaded(true);const recovered=await f.map.ensure();assert.equal(f.maps.length,2);assert.equal(f.map.status,'ready');assert.ok(recovered.getSource('vivi-route'));
});
test('SDK failure can be retried without coupling search to map creation',async()=>{
 const f=fixture();f.map.sdk=async()=>{throw Error('SDK failed');};await assert.rejects(f.map.ensure(),/SDK/);assert.equal(f.map.status,'error');assert.equal(f.maps.length,0);
 f.map.sdk=async()=>{};await f.map.ensure();assert.equal(f.map.status,'ready');
});
