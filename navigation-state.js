// Snapshot ordering and one in-flight gear change, independent of rendering.
export class SnapshotOrder {
  constructor(){this.epoch=null;this.sequence=-1;this.retired=new Set();}
  accept(payload, authoritative=false){
    if(payload.committed===false)return false;
    if(!payload.runtime_epoch)return this.epoch===null;
    const epoch=payload.runtime_epoch;
    if(this.retired.has(epoch))return false;
    if(this.epoch && epoch!==this.epoch){
      if(!authoritative)return false;
      this.retired.add(this.epoch);this.sequence=-1;
    }
    if(epoch===this.epoch && payload.snapshot_sequence<this.sequence)return false;
    this.epoch=epoch;this.sequence=payload.snapshot_sequence;return true;
  }
}

export class GearController {
  constructor({send,synchronize,changed,now=()=>performance.now(),schedule=(fn,ms)=>setTimeout(fn,ms),cancel=timer=>clearTimeout(timer),id=()=>crypto.randomUUID()}){
    Object.assign(this,{send,synchronize,changed,now,schedule,cancel,id});
    this.pending=null;this.desired=null;this.running=null;this.confirmed='P';this.status='idle';this.timers=[];
  }
  get frozen(){return this.desired==='P' || this.status==='unknown';}
  observe(payload,authoritative=false){
    if(payload.vehicle_state)this.confirmed=payload.vehicle_state.driving?'D':'P';
    if(this.pending && authoritative){
      const receipt=(payload.control_receipts || []).find(item=>item.event_id===this.pending.id);
      const restarted=this.pending.epoch && payload.runtime_epoch!==this.pending.epoch;
      if(receipt || restarted){
        this.pending.resolved=true;
        this.status='idle';
        if(restarted)this.desired=null;
      }
    }
    if(authoritative && !this.pending && this.status==='unknown')this.status='idle';
    this.changed(this);
  }
  request(target,epoch){
    if(this.status==='unknown')return this.reconcile();
    if(this.running){
      if(target==='P'){this.desired='P';this.status='pending';this.changed(this);}
      return this.running;
    }
    this.error=null;this.desired=target;
    this.running=this.drain(epoch).finally(()=>{this.running=null;this.changed(this);});
    return this.running;
  }
  async drain(epoch){
    while(this.desired){
      const target=this.desired;
      const operation={id:this.id(),target,epoch,started:this.now(),resolved:false};
      this.pending=operation;this.status='pending';this.changed(this);
      this.timers=[this.schedule(()=>{if(this.pending===operation){this.status='syncing';this.changed(this);}},3000),
        this.schedule(()=>{if(this.pending===operation)void this.reconcile();},8000)];
      let payload;
      try{
        payload=await this.send({kind:'gear',gear:target,id:operation.id});
        if(!payload)throw Error('Chưa nhận được trạng thái xe');
        if(payload.vehicle_state)this.confirmed=payload.vehicle_state.driving?'D':'P';
        operation.resolved=true;
      }catch(error){
        this.status='unknown';this.error=error.message;this.changed(this);
        await this.reconcile();
      }finally{for(const timer of this.timers)this.cancel(timer);this.timers=[];}
      if(!operation.resolved){this.status='unknown';this.changed(this);return;}
      this.pending=null;this.status='idle';
      if(this.desired===target)this.desired=null;
      // If P was requested during D, send it next without enabling motion.
      this.changed(this);
      if(payload?.status==='blocked' && !this.desired)this.error=payload.message;
    }
  }
  async reconcile(){
    try{
      const payload=await this.synchronize();
      if(payload)this.observe(payload,true);
      if(this.pending?.resolved && !this.running){this.pending=null;this.desired=null;this.status='idle';this.changed(this);}
      return payload;
    }catch{this.status='unknown';this.changed(this);return null;}
  }
}
