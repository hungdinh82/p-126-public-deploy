// Real journey controller/DOM, 10k-point route; map drawing uses an instrumented SDK fixture.
const {chromium}=require('playwright');const fs=require('node:fs');const http=require('node:http');const path=require('node:path');const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),p95=xs=>[...xs].sort((a,b)=>a-b)[Math.ceil(xs.length*.95)-1];
const html=fs.readFileSync(root+'/index.html','utf8').replace(/<script type="module"[^>]*><\/script>/,'').replace(/<link rel="stylesheet" href="\.\/style.css[^>]*\/>/,`<style>${fs.readFileSync(root+'/style.css','utf8')}</style>`);
const server=http.createServer((req,res)=>res.end(html));
(async()=>{await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const browser=await chromium.launch({headless:true,args:['--no-sandbox']});try{
 const page=await browser.newPage({viewport:{width:1440,height:900}});const errors=[];page.on('pageerror',error=>errors.push(error.message));await page.goto(`http://127.0.0.1:${server.address().port}`);
 for(const file of ['journey-map.js','journey-simulation.js','journey-renderer.js','journey.js'])await page.addScriptTag({content:fs.readFileSync(root+'/'+file,'utf8').replace(/^import .*$/gm,'').replace(/export /g,'')});
 await page.evaluate(()=>{
  window.stats={frames:[],ticks:[],longTasks:[],cameras:0,markers:0,paint:0};window.freeze=false;
  const source=()=>({setData(){stats.paint++;}});
  map={getSource:source,resize(){},easeTo(){},fitBounds(){},flyTo(){},queryRenderedFeatures(){return[];},jumpTo(){stats.cameras++;}};mapAdapter.map=map;mapAdapter.status='ready';mapAdapter.resizeObserver={};
  const marker={setRotation(){return this;},setLngLat(){stats.markers++;return this;},remove(){},getLngLat(){return{lng:105,lat:21};}};
  mapAdapter.marker=()=>{stats.markers++;return marker;};
  hooks={...hooks,announce(){},progress(){},travel(){},network(){},event:async()=>({})};
  new PerformanceObserver(list=>stats.longTasks.push(...list.getEntries().map(entry=>entry.duration))).observe({entryTypes:['longtask']});
  const original=tick;tick=now=>{const start=performance.now();original(now);stats.ticks.push(performance.now()-start);};
  let previous;const measure=now=>{if(previous)stats.frames.push(now-previous);previous=now;if(!freeze)requestAnimationFrame(measure);};requestAnimationFrame(measure);
 });
 const coordinates=Array.from({length:10000},(_,i)=>[105+i*.00001,21]);
 await page.evaluate(async coordinates=>{
  await applyNavigation({navigation:{status:'running',revision:1,route_id:'route:10k',event_sequence:1,origin:{lat:21,lng:105,label:'Fixture'},selected:{name:'Fixture',lat:21,lng:105.1},progress_m:5000,route:{coordinates,steps:Array.from({length:100},(_,i)=>({text:'Tiếp tục',sign:0,point_index:i*100,distance_m:100})),distance_m:10000,duration_s:10000}},ui_events:[]});
  setView('mini');stats.frames=[];stats.ticks=[];stats.longTasks=[];stats.cameras=stats.markers=stats.paint=0;
 },coordinates);
 await page.waitForTimeout(5000);
 const visible=await page.evaluate(()=>({...stats,travel:sim.traveled}));
 assert.ok(visible.frames.length>100);assert.ok(p95(visible.frames)<=33);assert.ok(!visible.longTasks.some(time=>time>100));assert.ok(visible.cameras<=151);assert.ok(visible.markers<=151);
 await page.evaluate(()=>{setView('hidden');stats.frames=[];stats.ticks=[];stats.longTasks=[];stats.cameras=stats.markers=stats.paint=0;});await page.waitForTimeout(1000);
 const hidden=await page.evaluate(()=>{freeze=true;pauseJourney();return {...stats,travel:sim.traveled};});assert.equal(hidden.cameras,0);assert.equal(hidden.markers,0);assert.ok(hidden.travel>visible.travel);assert.deepEqual(errors,[]);
 const report={points:10000,visible_samples:visible.frames.length,frame_p95_ms:p95(visible.frames),navigation_tick_p95_ms:p95(visible.ticks),max_long_task_ms:Math.max(0,...visible.longTasks),camera_updates:visible.cameras,marker_updates:visible.markers,paint_updates:visible.paint,hidden_map_render_updates:hidden.cameras+hidden.markers,conditions:'Chromium headless; real journey controller and DOM; SDK drawing fixture; excludes GPU tiles and 3D'};
 fs.writeFileSync('/tmp/vivi-ui-check/navigation-frame-performance.json',JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
 }finally{await browser.close();server.close();}})().catch(error=>{console.error(error);process.exitCode=1;server.close();});
