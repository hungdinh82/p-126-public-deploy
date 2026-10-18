// Snapshot ordering and control receipts are independent of map/rendering.
export class SnapshotOrder {
  constructor(){this.epoch=null;this.sequence=-1;this.retired=new Set();}
  accept(payload, authoritative=false){
    if(payload.committed===false)return false;
    if(!payload.runtime_epoch)return this.epoch===null;
    if(!Number.isSafeInteger(payload.snapshot_sequence))return false;
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
  constructor({send,synchronize,changed,now=()=>performance.now(),schedule=(fn,ms)=>setTimeout(fn,ms),cancel=timer=>clearTimeout(timer),id=()=>crypto.randomUUID(),clientId=crypto.randomUUID()}){
    Object.assign(this,{send,synchronize,changed,now,schedule,cancel,id,clientId});
    this.pending=null;this.desired=null;this.running=null;this.confirmed='P';this.status='idle';
    this.sequence=0;this.vehicleVersion=null;this.controlRevision=null;this.order=new SnapshotOrder();this.operations=new Map();
  }
  get frozen(){return this.desired==='P' || this.status==='unknown';}
  observe(payload,authoritative=false){
    if(!this.order.accept(payload,authoritative))return;
    if(payload.vehicle_state){this.confirmed=payload.vehicle_state.driving?'D':'P';this.vehicleVersion=payload.vehicle_state.state_version;}
    if(payload.control_revision!=null)this.controlRevision=payload.control_revision;
    const operation=this.pending;
    if(operation){
      const receipt=payload.receipt?.event_id===operation.id?payload.receipt:(payload.control_receipts || []).find(item=>item.event_id===operation.id);
      const restarted=authoritative && operation.epoch && payload.runtime_epoch!==operation.epoch;
      if(receipt?.status==='unknown')this.status='unknown';
      if(receipt && receipt.status!=='unknown' || restarted){
        operation.resolved=true;
        this.pending=null;this.desired=null;this.status='idle';
        this.error=receipt?.status==='blocked'?receipt.message:null;
      }
    }else if(authoritative && this.status==='unknown')this.status='idle';
    if(payload.control_uncertain)this.status='unknown';
    this.changed(this);
  }
  request(target,epoch){
    if(this.pending?.target===target)return this.running || this.reconcile();
    if(this.status==='unknown' && target!=='P')return this.reconcile();
    // Parking is sent immediately, even if an older D request is still in flight.
    const operation={id:this.id(),target,epoch,sequence:++this.sequence,started:this.now(),resolved:false};
    this.pending=operation;this.desired=target;this.status='pending';this.error=null;
    this.operations.set(operation.id,operation);this.changed(this);
    const running=this.dispatch(operation).finally(()=>{
      this.operations.delete(operation.id);
      if(this.running===running)this.running=null;
      this.changed(this);
    });
    this.running=running;return running;
  }
  async dispatch(operation){
    const timers=[this.schedule(()=>{if(this.pending===operation){this.status='syncing';this.changed(this);}},3000),
      this.schedule(()=>{if(this.pending===operation)void this.reconcile();},8000)];
    try{
      const payload=await this.send({kind:'gear',gear:operation.target,id:operation.id,client_id:this.clientId,
        control_sequence:operation.sequence,runtime_epoch:operation.epoch,expected_vehicle_version:this.vehicleVersion,expected_control_revision:this.controlRevision});
      if(!payload)throw Error('Chưa nhận được trạng thái xe');
      if(this.pending===operation){
        this.observe(payload);
        // Compatibility responses have no receipt; only the current operation
        // may resolve from its own successful HTTP response.
        if(this.pending===operation && !payload.runtime_epoch && payload.status!=='unknown'){
          operation.resolved=true;this.pending=null;this.desired=null;this.status='idle';
          if(payload.vehicle_state){this.confirmed=payload.vehicle_state.driving?'D':'P';this.vehicleVersion=payload.vehicle_state.state_version;}
          if(payload.status==='blocked')this.error=payload.message;
          this.changed(this);
        }
      }
      if(this.pending===operation && (payload.receipt?.status==='unknown' || payload.status==='unknown')){this.status='unknown';this.changed(this);}
      return payload;
    }catch(error){
      if(this.pending===operation){this.status='unknown';this.error=error.message;this.changed(this);await this.reconcile();}
      return null;
    }finally{for(const timer of timers)this.cancel(timer);}
  }
  async reconcile(){
    try{
      const payload=await this.synchronize();
      if(payload)this.observe(payload,true);
      return payload;
    }catch{if(this.pending){this.status='unknown';this.changed(this);}return null;}
  }
}
