import {request, signed} from "./store.js";

const tg = window.Telegram?.WebApp;
const frame = document.querySelector("#card");
const status = document.querySelector("#status");
const back = document.querySelector("#back");
const id = new URLSearchParams(location.search).get("id") || "";
const originKey = crypto.randomUUID();
const endpoint = "/api/widgets/" + id;
const russian = !tg?.initDataUnsafe?.user?.language_code || /^(ru|be|uk)/.test(tg.initDataUnsafe.user.language_code);
const text = (ru,en) => russian ? ru : en;

back.textContent = text("В чат","Back to chat");
back.addEventListener("click", () => {
  if (tg?.platform !== "unknown" && tg?.close) tg.close();
  else location.assign("https://t.me/AlephAgentBot");
});
function syncTheme() {
  const scheme = tg?.platform !== "unknown" && tg?.colorScheme
    ? tg.colorScheme : matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  document.documentElement.dataset.theme = scheme;
  frame.contentWindow?.postMessage({originKey, type:"theme", scheme}, "*");
}
syncTheme();
tg?.ready?.();
tg?.onEvent?.("themeChanged", syncTheme);

const bootstrap = `
const originKey = ${JSON.stringify(originKey)};
let seq=0;
const events={}, pending=new Map();
function notify(type,data){parent.postMessage({originKey,type,...data},"*");}
function size(){notify("size",{height:document.body.scrollHeight});}
window.TelegramWidget={
  chatData:null,
  ready(){size();new ResizeObserver(size).observe(document.body);},
  onEvent(t,c){events[t]=c;},
  request(action){
    return new Promise((resolve,reject)=>{
      const id=++seq;
      pending.set(id,{resolve,reject});
      notify("action",{id,action});
    });
  }
};
addEventListener("message", (event)=>{
  const m=event.data;
  if(event.source!==parent || m?.originKey!==originKey)return;
  if(m.type==="theme"){
    document.documentElement.style.colorScheme=m.scheme;
  }else if(m.type==="state"){
    TelegramWidget.chatData=m.chatData;
    events.chatDataChanged?.();
  }else if(m.type==="answer"){
    const call=pending.get(m.id);pending.delete(m.id);
    if(m.ok)call?.resolve({result:m.chatData});else call?.reject();
  }
});
`;
addEventListener("message", async (event) => {
  if (event.source !== frame.contentWindow || event.data?.originKey !== originKey) return;
  const message = event.data;
  if (message.type === "size" && Number.isFinite(message.height)) {
    frame.style.height = `${Math.min(900, Math.max(92, message.height + 2))}px`;
  }
  if (message.type !== "action" || !Number.isSafeInteger(message.id)) return;
  const action = typeof message.action === "string" && /^check:([0-5]):([01])$/.exec(message.action);
  if (!action) return;
  let result;
  try {
    result = await request(endpoint, "PATCH", {index:Number(action[1]), checked:action[2] === "1"});
  } catch {}
  frame.contentWindow?.postMessage({originKey, type:"answer", id:message.id, ok:!!result, chatData:result?.chat_data}, "*");
});
try {
  if (!/^\d{1,18}$/.test(id) || !signed()) throw new Error("401");
  const result = await request(endpoint);
  const csp = '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'; form-action \'none\'; base-uri \'none\'">';
  const shim = `<script>${bootstrap}\nTelegramWidget.chatData=${JSON.stringify(result.chat_data)};\n</script>`;
  frame.addEventListener("load",syncTheme,{once:true});
  frame.srcdoc = result.html.replace('<meta charset="utf-8">', '<meta charset="utf-8">' + csp + shim);
  frame.hidden = false;
  status.hidden = true;
} catch (error) {
  status.textContent = error.message === "401" || error.message === "404"
    ? text("Открой виджет из своего чата с Алефом.","Open this widget from your chat with Aleph.")
    : text("Виджет не загрузился. Закрой и открой его ещё раз.","The widget didn’t load. Close and reopen it.");
}
