// Focused browser fixture: real HTML/CSS/journey controller, mocked backend/map.
// Install Playwright separately; no project runtime dependency required.
const {chromium}=require('playwright');const fs=require('node:fs');const http=require('node:http');const assert=require('node:assert/strict');
const path=require('node:path');
const root=path.resolve(__dirname,'..');
const screenshots=process.env.VIVI_UI_SCREENSHOTS || '/tmp/vivi-ui-check';fs.mkdirSync(screenshots,{recursive:true});
let html=fs.readFileSync(root+'/index.html','utf8').replace(/<script type="module"[^>]*><\/script>/,'').replace(/<link rel="stylesheet" href="\.\/style.css[^>]*\/>/,`<style>${fs.readFileSync(root+'/style.css','utf8')}</style>`);
const server=http.createServer((req,res)=>res.end(html));
(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({headless:true,args:['--no-sandbox']});try{
for(const size of [{width:1440,height:900},{width:1024,height:768},{width:390,height:844},{width:390,height:568}]){
 const page=await browser.newPage({viewport:size});const errors=[];page.on('pageerror',e=>errors.push(e.message));await page.goto(`http://127.0.0.1:${server.address().port}`);
 for(const file of ['journey-map.js','journey-simulation.js','journey-renderer.js'])await page.addScriptTag({content:fs.readFileSync(root+'/'+file,'utf8').replace(/export /g,'')});
 await page.addScriptTag({content:fs.readFileSync(root+'/journey.js','utf8').replace(/^import .*$/gm,'').replace(/export /g,'')});
 await page.evaluate(()=>{window.commands=[];hooks={command:(...args)=>commands.push(args),event(){},announce(){},progress(){},route(){},travel(){}};ensureMap=async()=>{map={resize(){},easeTo(){},getSource(){return{setData(){}}}};};window.vietmapgl=null;});
 await page.evaluate(async()=>{await document.fonts.ready;});
 const initialLayout=await page.evaluate(()=>{const box=s=>{const r=document.querySelector(s).getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};};return {scene:box('.space'),assistant:box('.assistant'),dock:box('.dock')};});
 assert.equal(initialLayout.scene.width,size.width,'full-width scene before choices');
 assert.ok(Math.abs(initialLayout.assistant.x+initialLayout.assistant.width/2-size.width/2)<2,'centered assistant');
 const fixture={id:'trip:browser',request_id:'request:browser',revision:2,event_sequence:2,status:'preview',trip_kind:'round_trip',calculation_status:'ready',origin:{lat:21,lng:105.8,label:'Hà Nội'},candidates:[],selected:{name:'Nghi Sơn',spoken_label:'Nghi Sơn',lat:20,lng:105.8},route:{distance_m:270000,duration_s:13500,coordinates:[[105.8,21],[105.8,20],[105.8,21]],steps:Array.from({length:40},(_,i)=>({text:`Hướng rẽ ${i+1}`,sign:2,point_index:0}))},energy:{status:'planned',selected_plan:{total_seconds:15000,arrival_soc:17,stops:Array.from({length:5},(_,i)=>({station_id:String(i),name:`Trạm dừng nghỉ ${i+1}`,charge_minutes:20+i,arrival_soc:15,departure_soc:70,power_kw:120,connector:'CCS2'}))}},summary:null,offer:{status:'pending'},alternatives:[]};
 await page.evaluate(async plan=>{await applyNavigation({trip_plan:plan,ui_events:[{type:'trip.summary'}]});},fixture);
 assert.equal(await page.locator('#trip-plan-card').isVisible(),true);
 const afterLayout=await page.evaluate(()=>{const box=s=>{const r=document.querySelector(s).getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};};return {scene:box('.space'),assistant:box('.assistant'),dock:box('.dock')};});
 assert.deepEqual(afterLayout,initialLayout,'overlay must not shift scene or controls');
 const overlay=await page.locator('#information-rail').boundingBox();const dock=await page.locator('.dock').boundingBox();assert.ok(overlay.y+overlay.height<=dock.y,'overlay above dock');
 await page.getByRole('button',{name:'Ẩn kế hoạch',exact:true}).click();assert.equal(await page.locator('#trip-plan-card').isVisible(),false);
 await page.evaluate(async plan=>{await applyNavigation({trip_plan:{...plan,event_sequence:3},ui_events:[]});},fixture);assert.equal(await page.locator('#trip-plan-card').isVisible(),false);
 await page.getByRole('button',{name:'Kế hoạch',exact:true}).click();assert.equal(await page.locator('#trip-plan-card').isVisible(),true);
 await page.getByRole('button',{name:'Xem bản đồ',exact:true}).click();assert.equal(await page.locator('#journey').isVisible(),true);assert.equal(await page.evaluate(()=>commands.length),0);
 if(size.width<=760){
  const panel=await page.locator('.journey-panel').boundingBox();assert.ok(panel.y>size.height*.25,'compact sheet leaves map visible');
  await page.locator('#journey-details-toggle').evaluate(el=>el.onclick=()=>setJourneyDetails(!document.querySelector('#journey').classList.contains('details-expanded')));await page.locator('#journey-details-toggle').click();
 }
 const scroll=page.locator('#journey-scroll');await scroll.hover();await page.mouse.wheel(0,600);await page.waitForTimeout(180);assert.ok(await scroll.evaluate(el=>el.scrollTop)>0,'map content scroll '+JSON.stringify(size));
 const actions=await page.locator('#journey-controls').boundingBox();const area=await scroll.boundingBox();assert.ok(actions.y+actions.height<=area.y+1,'actions outside scroll');
 await page.locator('#journey-close').evaluate(el=>el.onclick=()=>closeJourney());await page.locator('#journey-close').click();assert.equal(await page.locator('#journey').isVisible(),false);
 await page.evaluate(async plan=>{await applyNavigation({trip_plan:{...plan,revision:3,event_sequence:4,status:'selecting',route:null,offer:null,candidates:[1,2,3].map(i=>({handle:`place:${i}`,name:`Địa điểm ${i}`,short_address:'Phường Hoa Lư'}))},ui_events:[{type:'trip.results'}]});},fixture);
 assert.equal(await page.locator('#destination-choices .destination-choice-card').count(),3);
 await page.locator('#destination-choices .destination-choice-card').nth(1).click();
 assert.equal(await page.evaluate(()=>commands.at(-1)[1].arguments.place_handle),'place:2');
 await page.getByRole('button',{name:'Ẩn địa điểm',exact:true}).click();assert.equal(await page.locator('#destination-choices').isVisible(),false);
 await page.getByRole('button',{name:'Địa điểm',exact:true}).click();
 await page.getByRole('button',{name:'Tìm địa điểm khác',exact:true}).click();assert.equal(await page.evaluate(()=>commands.at(-1)[1].arguments.reject_candidates),true);
 const headingBox=await page.locator('#destination-choices .information-heading').boundingBox();const railBox=await page.locator('#information-rail').boundingBox();assert.ok(headingBox.y>=railBox.y && headingBox.y+headingBox.height<=railBox.y+railBox.height,'hide control remains visible after scrolling');
 assert.deepEqual(errors,[]);await page.screenshot({path:`${screenshots}/layout-${size.width}-${size.height}.png`});console.log('PASS',size.width,size.height,'full scene/stable controls/hide/reopen/map scroll');await page.close();
}
}finally{await browser.close();server.close();}})().catch(e=>{console.error(e);server.close();process.exitCode=1});
