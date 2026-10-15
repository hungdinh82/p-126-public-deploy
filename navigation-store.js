import {SnapshotOrder} from './navigation-state.js?v=2';

export class MissingRouteError extends Error {}

// The only receiver of authoritative backend state. Views hold derived caches.
export class NavigationStore {
  constructor(){this.order=new SnapshotOrder();this.current=null;this.routes=new Map();this.listeners=new Set();}
  get knownRoutes(){return [...this.routes.keys()].slice(-10);}
  subscribe(listener){this.listeners.add(listener);return()=>this.listeners.delete(listener);}
  receive(payload,authoritative=false){
    // Resolve references before advancing sequence; a missing cache must be
    // recoverable by fetching the full authoritative snapshot.
    const newEpoch=this.order.epoch && payload.runtime_epoch!==this.order.epoch;
    const routes=new Map(newEpoch?[]:this.routes);
    const hydrate=value=>{
      if(Array.isArray(value))return value.map(hydrate);
      if(!value || typeof value!=='object')return value;
      const result={...value};
      if(value.route_reference){
        const cached=routes.get(value.route_reference);
        if(!cached)throw new MissingRouteError('Thiếu dữ liệu tuyến');
        result.route=cached.route;result.waypoint_routes=cached.waypoint_routes;
        delete result.route_reference;
      }
      if(value.route_id && value.route)routes.set(value.route_id,{route:value.route,waypoint_routes:value.waypoint_routes || []});
      for(const [key,item] of Object.entries(result)){
        // Immutable geometry is intentionally shared, never walked per tick.
        if(['route','waypoint_routes','coordinates'].includes(key))continue;
        if(item && typeof item==='object')result[key]=hydrate(item);
      }
      return result;
    };
    const next=hydrate(payload);
    if(!this.order.accept(next,authoritative))return null;
    if(this.current?.runtime_epoch && next.runtime_epoch!==this.current.runtime_epoch)this.routes.clear();
    this.routes=routes;
    while(this.routes.size>24)this.routes.delete(this.routes.keys().next().value);
    this.current=next;
    for(const listener of this.listeners)listener(next);
    return next;
  }
  renderSnapshot(){
    const snapshot=this.current || {}, workspace=snapshot.navigation_workspace || {};
    const draft=workspace.draft, journey=workspace.journey;
    const navigation={...(journey || (draft?.purpose!=='itinerary'?draft:null) || {status:'idle'}),
      revision:workspace.revision || 0,event_sequence:workspace.event_sequence || 0,origin:workspace.origin};
    if(journey && draft?.purpose!=='itinerary' && draft && !draft.started_revision){
      navigation.candidates=draft.candidates;navigation.clarification=draft.clarification;
      navigation.pending={status:draft.status,navigation:draft};
    }
    return {...snapshot,navigation,trip_plan:draft?.purpose==='itinerary'?draft:null};
  }
  select(){
    const snapshot=this.current || {};
    const workspace=snapshot.navigation_workspace || {};
    return {vehicle:snapshot.vehicle_state,draft:workspace.draft,journey:workspace.journey,
      tripPlan:snapshot.trip_plan,receipts:snapshot.control_receipts || [],sequence:snapshot.snapshot_sequence};
  }
}
