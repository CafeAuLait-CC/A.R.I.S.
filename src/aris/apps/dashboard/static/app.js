"use strict";
const $ = (id) => document.getElementById(id);
const samplesByGpu = new Map();
let source = null, lastSnapshot = null, lastReceived = 0, healthy = false;
let liveSource=null, liveMode=false, lastPreview=null;
const displayZone=document.documentElement.dataset.timeZone||"UTC";
const refreshSeconds=Number(document.documentElement.dataset.refreshSeconds)||15;
const clock = (value) => value ? new Date(value).toLocaleTimeString("zh-CN", {hour12:false,timeZone:displayZone}) : "—";
const make = (tag, className, text) => {const el=document.createElement(tag); if(className) el.className=className; if(text!==undefined) el.textContent=text; return el;};
const qualityNames = {CURRENT:"采样新鲜",STALE:"采样陈旧",MISSING:"尚无采样",ERROR:"采集异常"};
function connection(label, mode){$("connection").textContent=label; $("connection").className="connection "+mode;}
function notice(text){$("notice").textContent=text; $("notice").hidden=!text;}
function chart(gpu, stamp, known){
  const key=gpu.gpu_uuid;
  let values=samplesByGpu.get(key)||[];
  if(known && (!values.length || values[values.length-1].stamp!==stamp)){
    values.push({stamp,value:gpu.utilization_percent}); values=values.slice(-40); samplesByGpu.set(key,values);
  }
  const box=make("div","sparkline");
  const ns="http://www.w3.org/2000/svg", svg=document.createElementNS(ns,"svg");
  svg.setAttribute("viewBox","0 0 480 72"); svg.setAttribute("preserveAspectRatio","none"); svg.setAttribute("aria-hidden","true");
  for(const y of [18,36,54]){const line=document.createElementNS(ns,"line"); line.setAttribute("x1","0");line.setAttribute("x2","480");line.setAttribute("y1",y);line.setAttribute("y2",y);line.setAttribute("class","gridline");svg.append(line);}
  if(known && values.length){
    const points=values.map((entry,index)=>[values.length===1?480:index*480/(values.length-1),68-Math.min(100,Math.max(0,entry.value))*0.62]);
    const line=document.createElementNS(ns,"polyline");line.setAttribute("points",points.map(p=>p.join(",")).join(" "));svg.append(line);
    const dot=document.createElementNS(ns,"circle"), last=points[points.length-1];dot.setAttribute("cx",last[0]);dot.setAttribute("cy",last[1]);dot.setAttribute("r","2.5");svg.append(dot);
  }
  box.append(svg);return {box,count:values.length};
}
function gpuCard(gpu,node,connected){
  const known=connected && node.status==="ONLINE" && node.telemetry_status==="CURRENT" && gpu.present && gpu.observed_state!=="unknown";
  const state=known?gpu.observed_state:"unknown", card=make("article","gpu "+(known?"":"unknown"));
  const top=make("div","gpu-top"), title=make("div");title.append(make("div","gpu-id","GPU "+String(gpu.index).padStart(2,"0")),make("h3","gpu-name",gpu.model));
  top.append(title,make("span","gpu-state "+state,state==="busy"?"观测占用":state==="idle"?"观测空闲":gpu.present?"状态未知":"未检测到"));card.append(top);
  const data=make("div","gpu-data"), usage=make("div"), memory=make("div"), u=make("div","value-row"), m=make("div","value-row memory-value");
  u.append(make("strong","",known?gpu.utilization_percent:"—"),make("small","",known?"%":""));usage.append(u,make("div","data-label","GPU 利用率"));
  m.append(make("strong","",known?(gpu.memory_used_mib/1024).toFixed(1):"—"),make("small","",known?"/ "+(gpu.memory_total_mib/1024).toFixed(1)+" GiB":""));memory.append(m,make("div","data-label","显存使用"));
  for(const [parent,value,cls] of [[usage,known?gpu.utilization_percent:0,"bar"],[memory,known?gpu.memory_used_mib/gpu.memory_total_mib*100:0,"bar memory"]]){const bar=make("progress",cls);bar.max=100;if(known){bar.value=value;}else{bar.removeAttribute("value");bar.setAttribute("aria-valuetext","未知");}bar.setAttribute("aria-label",parent===usage?"GPU 利用率":"显存使用比例");parent.append(bar);}
  data.append(usage,memory);card.append(data);
  const trend=chart(gpu,node.observed_at,known);card.append(trend.box);const note=make("div","chart-note");note.append(make("span","","利用率 · 最近 "+trend.count+" 次真实采样"),make("span","",known?"0 — 100%":"等待新鲜数据"));card.append(note);
  const bottom=make("div","gpu-bottom"), temp=make("span","","温度");temp.append(make("strong","",known && gpu.temperature_c!==null?gpu.temperature_c+" °C":"—"));bottom.append(temp,make("span","","采样 "+clock(node.observed_at)));card.append(bottom);return card;
}
function render(snapshot,connected){
  const nodes=snapshot.nodes||[], gpus=nodes.flatMap(n=>n.gpus.filter(g=>g.present).map(g=>({gpu:g,node:n})));
  const known=gpus.filter(x=>connected && x.node.status==="ONLINE" && x.node.telemetry_status==="CURRENT" && x.gpu.observed_state!=="unknown");
  $("online-count").textContent=connected?nodes.filter(n=>n.status==="ONLINE").length:"—";
  $("node-count").textContent=nodes.length?"共 "+nodes.length+" 个已接入节点":"尚无已接入节点";
  $("gpu-count").textContent=gpus.length || (nodes.length?"0":"—");
  $("known-count").textContent=known.length+" 项新鲜观测 · "+(gpus.length-known.length)+" 项未知";
  $("memory-total").textContent=known.length?(known.reduce((s,x)=>s+x.gpu.memory_used_mib,0)/1024).toFixed(1)+" GiB":"—";
  $("memory-note").textContent=known.length?"总量 "+(known.reduce((s,x)=>s+x.gpu.memory_total_mib,0)/1024).toFixed(1)+" GiB（已知部分）":"仅汇总新鲜观测";
  $("received-time").textContent=clock(new Date(lastReceived).toISOString());
  if(!nodes.length){const empty=make("div","empty-state");empty.append(make("h2","","尚无节点观测数据"),make("p","","等待 Vega 采集器上报。"));$("nodes").replaceChildren(empty);return;}
  const fragment=document.createDocumentFragment();
  for(const node of nodes){
    const section=make("section","node"), heading=make("div","node-heading"), title=make("div","node-title"), health=connected?node.status:"UNKNOWN";
    const healthNames=liveMode?{ONLINE:"实时预览",STALE:"等待新帧",OFFLINE:"通道未连接",UNKNOWN:"连接中断"}:{ONLINE:"在线",STALE:"陈旧",OFFLINE:"离线",UNKNOWN:"连接中断"};
    title.append(make("h2","",node.node_id),make("span","badge "+(health==="ONLINE"?"":health.toLowerCase()),healthNames[health]));
    title.append(make("span","hostname",node.hostname));const meta=make("div","node-meta");
    for(const [label,value] of [[liveMode?"预览采样":"采样",node.observed_at],["心跳归档",node.heartbeat_at]]){const item=make("span","",label);item.append(make("time","",clock(value)));meta.append(item);}
    meta.append(make("span","",connected?(node.preview_label||qualityNames[node.telemetry_status]||"未知"):"数据新鲜度未知"));heading.append(title,meta);
    const grid=make("div","gpu-grid");for(const gpu of node.gpus) grid.append(gpuCard(gpu,node,connected));
    if(!node.gpus.length) grid.append(make("div","empty-state","该节点尚无 GPU 观测数据。"));
    section.append(heading,grid);fragment.append(section);
  }
  $("nodes").replaceChildren(fragment);
}
function previewSnapshot(){
  const archived=new Map((lastSnapshot?.nodes||[]).map(n=>[n.node_id,n]));
  const previews=new Map((lastPreview?.nodes||[]).map(n=>[n.node_id,n]));
  const ids=new Set([...archived.keys(),...previews.keys()]);
  return {nodes:[...ids].map(id=>{
    const base=archived.get(id)||{}, preview=previews.get(id), fresh=preview?.quality==="live";
    const gpus=fresh?preview.gpus.map(g=>({...g,present:true,observed_state:g.utilization_percent>0||g.memory_used_mib>0?"busy":"idle"})):(base.gpus||[]);
    return {...base,node_id:id,hostname:preview?.hostname||base.hostname||"",gpus,
      status:fresh?"ONLINE":preview?.connected?"STALE":"OFFLINE",observed_at:preview?.observed_at,
      telemetry_status:fresh?"CURRENT":"MISSING",
      preview_label:({live:"实时预览 · 未归档",waiting:"等待首帧",stale:"预览已超时",error:"实时采集异常",offline:"实时通道未连接"})[preview?.quality]||"等待 Vega 实时通道"};
  })};
}
function currentSnapshot(){return liveMode?previewSnapshot():lastSnapshot;}
function unavailable(message){healthy=false;connection("连接异常","disconnected");notice(message);const snapshot=currentSnapshot();if(snapshot)render(snapshot,false);}
function startPreview(){
  if(liveSource)liveSource.close();lastPreview=null;healthy=false;connection("等待实时预览","connecting");
  render(previewSnapshot(),false);notice("正在申请只读实时采样。预览不会逐帧归档。");
  liveSource=new EventSource("/live");
  liveSource.addEventListener("preview",event=>{
    if(!liveMode)return;
    try{
      lastPreview=JSON.parse(event.data);lastReceived=Date.now();healthy=true;
      const fresh=lastPreview.nodes.filter(n=>n.quality==="live").length;
      connection(fresh?"实时预览 · 未归档":"等待实时采样",fresh?"connected":"connecting");
      notice(fresh?"":"实时帧尚未就绪，指标按未知显示。归档数据保留在常规视图。");
      render(previewSnapshot(),true);
    }catch{unavailable("无法解析实时预览，正在等待下一帧。");}
  });
  liveSource.addEventListener("preview_error",()=>unavailable("实时预览暂不可用。可关闭预览查看归档观测；连接会自动重试。"));
  liveSource.onerror=()=>{if(liveMode)unavailable("实时链路已断开，正在自动重连。预览指标按未知显示。");};
}
function connect(){
  if(source)source.close();connection("连接中","connecting");
  source=new EventSource("/events");
  source.addEventListener("resources",event=>{
    try{const snapshot=JSON.parse(event.data);lastSnapshot=snapshot;if(!liveMode){lastReceived=Date.now();healthy=true;connection("归档观测","connected");notice("");render(snapshot,true);}}catch{if(!liveMode)unavailable("无法解析状态数据，正在等待下一次更新。");}
  });
  source.addEventListener("dependency_error",()=>{if(!liveMode)unavailable("资源数据暂时不可用。指标按未知显示，连接会自动重试。");});
  source.onerror=()=>{if(!liveMode)unavailable("状态连接已断开。指标按未知显示，正在自动重连。");};
}
$("reconnect").addEventListener("click",()=>{connect();if(liveMode)startPreview();});
$("live-toggle").disabled=document.documentElement.dataset.liveEnabled!=="true";
$("live-toggle").addEventListener("click",()=>{
  liveMode=!liveMode;samplesByGpu.clear();$("live-toggle").setAttribute("aria-pressed",String(liveMode));
  $("live-toggle").textContent=liveMode?"关闭实时预览":"开启实时预览";
  $("node-metric-label").textContent=liveMode?"实时采样节点":"在线节点";
  $("refresh-note").textContent=liveMode?"约每秒预览 · 不逐帧归档":"归档观测 · 约每 "+refreshSeconds+" 秒更新";
  if(liveMode)startPreview();else{if(liveSource)liveSource.close();liveSource=null;lastPreview=null;healthy=false;connect();}
});
const watchdogMs=Math.max(10000,refreshSeconds*3000+1000);
setInterval(()=>{if(healthy && Date.now()-lastReceived>(liveMode?5000:watchdogMs))unavailable("长时间未收到状态。指标按未知显示，正在等待连接恢复。");},1000);
window.addEventListener("pagehide",()=>{if(source)source.close();if(liveSource)liveSource.close();});
$("time-zone").textContent=displayZone;
$("refresh-note").textContent="归档观测 · 约每 "+refreshSeconds+" 秒更新";
connect();
