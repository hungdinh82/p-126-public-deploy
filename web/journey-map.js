// Map SDK lifecycle owns resources and presentation; it never issues a trip command.
export class JourneyMap {
  constructor({fetchConfig,notice,ready,attach}){
    Object.assign(this,{fetchConfig,notice,ready,attach});
    this.map=null;this.markers={};this.promise=null;this.sdkPromise=null;this.status='idle';
    this.lastCamera=0;this.lastMarker=0;this.resizeObserver=null;
  }
  async sdk(){
    if(globalThis.vietmapgl)return;
    if(!this.sdkPromise)this.sdkPromise=new Promise((resolve,reject)=>{
      const base='https://unpkg.com/@vietmap/vietmap-gl-js@6.0.1/dist';
      const style=document.createElement('link');style.rel='stylesheet';style.href=`${base}/vietmap-gl.css`;document.head.append(style);
      const script=document.createElement('script');script.src=`${base}/vietmap-gl.js`;
      const timer=setTimeout(()=>{script.remove();reject(Error('Quá thời gian tải thư viện bản đồ'));},10000);
      script.onload=()=>{clearTimeout(timer);resolve();};
      script.onerror=()=>{clearTimeout(timer);script.remove();reject(Error('Không tải được thư viện bản đồ VietMap'));};
      document.head.append(script);
    }).catch(error=>{this.sdkPromise=null;throw error;});
    await this.sdkPromise;
  }
  ensure(){
    if(this.map)return Promise.resolve(this.map);
    if(!this.promise)this.promise=this.create().catch(error=>{this.status='error';this.promise=null;throw error;});
    return this.promise;
  }
  layers(){
    const map=this.map;if(!map)return;
    const empty={type:'Feature',geometry:{type:'LineString',coordinates:[]}};
    for(const id of ['vivi-route','vivi-traveled','vivi-trip-preview','vivi-pending-preview']){
      if(!map.getSource(id))map.addSource(id,{type:'geojson',data:empty});
    }
    const line={'line-join':'round','line-cap':'round'};
    for(const [id,source,paint] of [
      ['vivi-route-glow','vivi-route',{'line-color':'#9aa8c8','line-width':14,'line-opacity':.14,'line-blur':6}],
      ['vivi-route-line','vivi-route',{'line-color':'#d0d8ea','line-width':4}],
      ['vivi-trip-preview-line','vivi-trip-preview',{'line-color':'#f0cd92','line-width':5,'line-dasharray':[2,1]}],
      ['vivi-pending-preview-line','vivi-pending-preview',{'line-color':'#f0cd92','line-width':4,'line-dasharray':[2,1]}],
      ['vivi-traveled-line','vivi-traveled',{'line-color':'#4b6378','line-width':5}],
    ])if(!map.getLayer(id))map.addLayer({id,type:'line',source,layout:line,paint});
  }
  async create(){
    this.status='loading';const config=await this.fetchConfig();
    if(!config.available){this.status='unavailable';throw Error('Chưa cấu hình dịch vụ bản đồ.');}
    await this.sdk();
    const sdk=globalThis.vietmapgl;
    const created=new sdk.Map({container:'journey-map',style:config.style_url,center:[config.default_center.lng,config.default_center.lat],zoom:13,attributionControl:{compact:true}});
    created.on('error',event=>{
      const status=event.error?.status;
      if([401,403,423,429].includes(status))this.notice(`Chưa tải được nền bản đồ (HTTP ${status}). Bạn vẫn có thể tìm điểm đến và xem kế hoạch.`);
    });
    await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(Error('Không tải được style bản đồ VietMap.')),15000);
      const ready=()=>{clearTimeout(timer);resolve();};
      if(created.isStyleLoaded())ready();else created.once('style.load',ready);
    }).catch(error=>{created.remove();throw error;});
    this.map=created;this.status='ready';
    created.addControl(new sdk.NavigationControl({showCompass:true}),'bottom-right');
    this.layers();this.attach(created);
    created.on('style.load',()=>{this.layers();this.ready(created);});
    if(typeof ResizeObserver!=='undefined'){
      this.resizeObserver=new ResizeObserver(()=>{
        const container=created.getContainer();
        if(container.clientWidth && container.clientHeight)created.resize();
      });this.resizeObserver.observe(created.getContainer());
    }
    this.ready(created);return created;
  }
  marker(name,lngLat,className){
    if(!this.map)return null;
    let marker=this.markers[name];
    if(!marker){
      const element=document.createElement('div');element.className=`journey-marker ${className}`;element.innerHTML='<i></i>';
      marker=new globalThis.vietmapgl.Marker({element,rotationAlignment:'map'}).setLngLat(lngLat).addTo(this.map);
      this.markers[name]=marker;
    }else marker.setLngLat(lngLat);
    return marker;
  }
  removeMarker(name){this.markers[name]?.remove();delete this.markers[name];}
  camera(options,now){if(now-this.lastCamera<1000/30)return;this.lastCamera=now;this.map?.jumpTo(options);}
  dispose(){this.resizeObserver?.disconnect();for(const name of Object.keys(this.markers))this.removeMarker(name);this.map?.remove();this.map=null;this.promise=null;this.status='idle';}
}
