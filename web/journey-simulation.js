// Route interpolation has no map, DOM or backend ownership.
export class DriveSimulation {
  constructor(){Object.assign(this,{running:false,speedup:5,traveled:0,last:0,frame:0,step:-1,follow:true,lastPaint:0,bearing:0,lastMarker:0});}
  advance(now,route){const elapsed=Math.min(.25,Math.max(0,(now-this.last)/1000));this.last=now;
    this.traveled=Math.min(route.length,this.traveled+elapsed*route.length/Math.max(1,route.duration_s)*this.speedup);return elapsed;}
  static prepare(route,distance){
    const cumulative=[0];for(let i=1;i<route.coordinates.length;i++)cumulative.push(cumulative[i-1]+distance(route.coordinates[i-1],route.coordinates[i]));
    return {...route,steps:route.steps.map(step=>({...step})),cumulative,length:cumulative.at(-1)||route.distance_m};
  }
  static position(route,distance,bearing){
    const {cumulative,coordinates}=route;let low=0,high=cumulative.length-1;
    while(low<high-1){const mid=(low+high)>>1;if(cumulative[mid]<=distance)low=mid;else high=mid;}
    const t=Math.min(1,Math.max(0,(distance-cumulative[low])/(cumulative[high]-cumulative[low]||1)));
    const [a,b]=[coordinates[low],coordinates[high]];
    return {index:low,point:[a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t],heading:bearing(a,b)};
  }
  static step(route,index){let low=0,high=route.steps.length;
    while(low<high){const mid=(low+high)>>1;if(route.steps[mid].point_index<=index)low=mid+1;else high=mid;}
    return Math.max(0,low-1);
  }
}
