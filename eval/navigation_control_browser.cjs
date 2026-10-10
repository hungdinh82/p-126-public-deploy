// Actual app, API, SQLite checkpoints, vehicle simulator and WebGL scene.
// VietMap is replaced by deterministic server fixtures; Three.js is served locally.
const {chromium}=require('playwright');
const fs=require('node:fs');const path=require('node:path');const assert=require('node:assert/strict');
const base=process.env.VIVI_BROWSER_URL || 'http://127.0.0.1:8768';
const three=path.resolve(path.dirname(require.resolve('three')),'..');
const p95=values=>[...values].sort((a,b)=>a-b)[Math.ceil(values.length*.95)-1];
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
 const report=[];
 try{
 for(const viewport of [{width:1440,height:900},{width:390,height:844}]){
  const context=await browser.newContext({viewport,deviceScaleFactor:.5});
  await context.request.post(base+'/api/v1/session/role',{data:{role:'driver'}});
  const page=await context.newPage();page.setDefaultTimeout(20000);console.log('start',viewport);const errors=[];page.on('pageerror',error=>{errors.push(error.message);console.log('pageerror',error.message);});page.on('console',msg=>{if(msg.type()==='warning' || msg.type()==='error')console.log('browser',msg.text().slice(0,200));});

  await page.route('https://cdn.jsdelivr.net/npm/three@0.169.0/**',async route=>{
   const file=decodeURIComponent(new URL(route.request().url()).pathname.split('three@0.169.0/')[1]);
   await route.fulfill({body:fs.readFileSync(path.join(three,file)),contentType:'text/javascript'});
  });
  await page.route('https://unpkg.com/**',route=>route.abort());
  const session=`browser-${viewport.width}-${Date.now()}`;
  await page.addInitScript(session=>{localStorage.setItem('vivi-session-id',session);localStorage.setItem('vivi-sound','false');},session);
  await page.goto(base+'/ivi?model=lite');console.log('page loaded');
  await page.evaluate(()=>{
    window.gearTimings={visual:[],ack:[]};let pending=null;
    document.addEventListener('click',event=>{if(event.target.closest('#drive-toggle'))pending={at:performance.now(),target:document.querySelector('#drive-toggle span').textContent.startsWith('D')?'P':'D',visual:false};},true);
    new MutationObserver(()=>{
      if(!pending)return;
      if(pending.target==='P' && !pending.visual && !document.body.classList.contains('driving')){gearTimings.visual.push(performance.now()-pending.at);pending.visual=true;}
      if(document.querySelector('#drive-toggle span').textContent===pending.target){gearTimings.ack.push(performance.now()-pending.at);pending=null;}
    }).observe(document.body,{attributes:true,subtree:true,childList:true});
  });
  await page.waitForFunction(()=>document.body.classList.contains('space-ready'),null,{timeout:30000});
  console.log('scene ready');const button=page.locator('#drive-toggle');await button.waitFor();await page.waitForFunction(()=>!document.querySelector('#drive-toggle').disabled);
  await page.locator('#sound-toggle').click();console.log('controls ready');
  const snapshot=async()=>await(await context.request.get(base+'/api/v1/navigation/snapshot',{params:{session_id:session}})).json();
  // UI gear free-driving, then park: no route is accepted implicitly.
  await button.click();try{await page.waitForFunction(()=>document.querySelector('#drive-toggle span').textContent==='D');}catch(error){console.log('ui',await button.evaluate(el=>({text:el.textContent,title:el.title,disabled:el.disabled})));console.log('snapshot',await snapshot());throw error;}
  assert.equal((await snapshot()).vehicle_state.driving,true);
  await button.click();await page.waitForFunction(()=>document.querySelector('#drive-toggle span').textContent==='P');
  assert.equal((await snapshot()).vehicle_state.driving,false);
  console.log('free gear passed');
  // Voice/typed plan through the same real API, then use the actual start card.
  await page.locator('#keyboard-toggle').click();
  await page.locator('#command-input').fill('đi Tràng An');await page.locator('#command-form').evaluate(el=>el.requestSubmit());
  console.log('plan submitted');await page.locator('#destination-choices .destination-choice-card').first().click();
  console.log('place chosen');await page.locator('#trip-plan-card').getByRole('button',{name:'Bắt đầu',exact:true}).click();
  await page.waitForFunction(()=>document.body.classList.contains('driving') && document.querySelector('#drive-toggle span').textContent==='D');
  assert.equal((await snapshot()).navigation.status,'running');
  await page.waitForTimeout(1200);
  // Delayed D response with queued P: visual must remain parked until P ack.
  await button.click();await page.waitForFunction(()=>document.querySelector('#drive-toggle span').textContent==='P');
  let held=false,release;const barrier=new Promise(resolve=>release=resolve);
  await page.route('**/api/v1/turn',async route=>{
   const event=route.request().postDataJSON().navigation_event;
   if(event?.kind==='gear' && event.gear==='D' && !held){held=true;const response=await route.fetch();await barrier;await route.fulfill({response}).catch(()=>{});}
   else await route.continue();
  });
  await button.evaluate(el=>{el.click();el.click();});assert.equal(await page.locator('body').evaluate(el=>el.classList.contains('driving')),false);
  release();await page.waitForFunction(()=>document.querySelector('#drive-toggle span').textContent==='P');
  assert.equal((await snapshot()).navigation.status,'paused');await page.unroute('**/api/v1/turn');
  await page.evaluate(()=>{gearTimings.visual=[];gearTimings.ack=[];});
  // Record full HTTP ack and server stages for actual UI toggles.
  const latencies=[],queues=[],controls=[],visuals=[];
  page.on('response',async response=>{
   if(response.url().endsWith('/api/v1/turn') && response.request().postDataJSON()?.navigation_event?.kind==='gear'){
    const payload=await response.json();latencies.push(payload.latency_ms.total);queues.push(payload.latency_ms.queue);controls.push(payload.latency_ms.control);
   }
  });
  for(let i=0;i<8;i++){
   const desired=i%2===0?'D':'P';const started=Date.now();await button.evaluate(el=>el.click());
   if(desired==='P'){assert.equal(await page.locator('body').evaluate(el=>el.classList.contains('driving')),false);visuals.push(Date.now()-started);}
   await page.waitForFunction(target=>document.querySelector('#drive-toggle span').textContent===target,desired);
  }
  assert.equal((await snapshot()).navigation.status,'paused');
  await page.screenshot({path:`/tmp/vivi-ui-check/gear-${viewport.width}.png`,timeout:10000}).catch(()=>console.log("Software WebGL screenshot exceeded time budget"));
  assert.deepEqual(errors,[]);
  const domTiming=await page.evaluate(()=>gearTimings);
  report.push({viewport,parking_dom_p95_ms:p95(domTiming.visual.slice(-4)),click_to_ack_dom_p95_ms:p95(domTiming.ack.slice(-8)),gear_ack_p95_ms:p95(latencies),writer_queue_p95_ms:p95(queues),control_p95_ms:p95(controls),automation_click_p95_ms:p95(visuals),samples:latencies.length,webgl:true,map_sdk:'blocked intentionally'});
  await context.close();
 }
 console.log(JSON.stringify(report,null,2));fs.writeFileSync('/tmp/vivi-ui-check/navigation-performance.json',JSON.stringify(report,null,2));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
