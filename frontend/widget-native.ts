import type { WidgetSpec } from "./types.js";

declare const __WIDGET_SPEC__: WidgetSpec;
const spec = __WIDGET_SPEC__;
const telegram=window.TelegramWidget;
function required(id: string): HTMLElement {
  const node = document.getElementById(id);
  if (!node) throw new Error(`Missing widget element: ${id}`);
  return node;
}
const host=required("content");
const hint=required("hint");
required("title").textContent=spec.title;
let selected=0, checked=0, waiting=false;
const isRu=/[А-Яа-яЁё]/.test(spec.title);
function element<Tag extends keyof HTMLElementTagNameMap>(tag: Tag, cls: string, text?: string){
  const node=document.createElement(tag);
  if(cls)node.className=cls;
  if(text!==undefined)node.textContent=text;
  return node;
}
function sync(raw?: string | null){
  if(!raw)return false;
  try{
    const data=JSON.parse(raw) as { checked?: unknown } | null;
    if(typeof data?.checked!=="number")return false;
    checked=data.checked;
    return true;
  }catch(_){return false}
}
function requestCheck(index: number){
  if(waiting)return;
  const flag=(checked&(1<<index))?0:1;
  if(!telegram){checked^=1<<index; draw();return;}
  waiting=true;
  telegram.request("check:"+index+":"+flag).then(
    (answer)=>{
      waiting=false;
      if(sync(answer.result))draw();
      else hint.textContent=isRu?"Отметка недоступна.":"Checklist unavailable.";
    },
    ()=>{
      waiting=false;
      hint.textContent=isRu?"Не сохранилось. Нажми ещё раз.":"Could not save. Tap to retry.";
    });
}
function draw(){
  host.textContent="";
  hint.textContent="";
  if(spec.kind==="checklist"){
    let count=0;
    spec.items.forEach((item,i)=>{
      const on=!!(checked&(1<<i));
      if(on)count++;
      const btn=element("button","row check");
      btn.setAttribute("aria-pressed",String(on));
      btn.appendChild(element("span","box",on?"✓":""));
      const copy=element("span","content");
      copy.appendChild(element("span","label",item.label));
      if(item.detail)copy.appendChild(element("small","",item.detail));
      btn.appendChild(copy);
      btn.addEventListener("click",()=>requestCheck(i));
      host.appendChild(btn);
    });
    hint.textContent=count+" / "+spec.items.length;
  }else if(spec.kind==="compare"){
    const tabs=element("div","choices");
    spec.items.forEach((item,i)=>{
      const btn=element("button","tab",item.label);
      btn.setAttribute("aria-pressed",String(i===selected));
      btn.addEventListener("click",()=>{selected=i;draw()});
      tabs.appendChild(btn);
    });
    host.appendChild(tabs);
    host.appendChild(element("p","detail",spec.items[selected].detail));
  }else{
    const numbers=spec.items.map(x=>x.value);
    const low=Math.min(0,...numbers), high=Math.max(0,...numbers);
    const range=high-low||1;
    spec.items.forEach((item,i)=>{
      const btn=element("button","bar");
      btn.setAttribute("aria-pressed",String(i===selected));
      const label=element("span","bar-label");
      label.appendChild(element("span","name",item.label));
      label.appendChild(element("span","num",String(item.value)));
      btn.appendChild(label);
      const track=element("span","bar-line");
      const fill=element("span","fill");
      fill.style.left=((Math.min(0,item.value)-low)/range*100)+"%";
      fill.style.width=(Math.abs(item.value)/range*100)+"%";
      track.appendChild(fill);
      btn.appendChild(track);
      btn.addEventListener("click",()=>{selected=i;draw()});
      host.appendChild(btn);
    });
    const item=spec.items[selected];
    hint.textContent=item.label+" · "+item.value+(spec.unit?" "+spec.unit:"");
  }
}
sync(telegram?.chatData);
draw();
if(telegram){
  telegram.onEvent("chatDataChanged",()=>{if(sync(telegram.chatData))draw()});
  telegram.ready();
}
