// Keyed presentation preserves inputs/focus/scroll and performs no domain actions.
export class JourneyRenderer {
  constructor(){this.signatures=new WeakMap();}
  begin(element,signature){
    if(this.signatures.get(element)===signature)return false;
    this.signatures.set(element,signature);
    const active=typeof document!=='undefined'?document.activeElement:null;
    const input=active && element.contains?.(active)?{id:active.id,name:active.name,value:active.value,start:active.selectionStart,end:active.selectionEnd}:null;
    element._presentation={scroll:element.scrollTop,input};element.innerHTML='';return true;
  }
  finish(element){
    const saved=element._presentation;if(!saved)return;
    element.scrollTop=saved.scroll;
    const inputs=element.querySelectorAll?.('input,textarea') || [];
    if(saved.input)for(const input of inputs){if(saved.input.id && input.id===saved.input.id || saved.input.name && input.name===saved.input.name){
      input.value=saved.input.value;input.focus({preventScroll:true});input.setSelectionRange?.(saved.input.start,saved.input.end);break;
    }}
    delete element._presentation;
  }
  forget(element){this.signatures.delete(element);}
}
