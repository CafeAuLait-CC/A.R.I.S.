"use strict";
window.createGroupUsage=function({el,request,lock,message,isBusy,selection}){
  let active=false,nodes=[],selected=new Set(),displayed=null,version=0;
  const node=(tag,value,className)=>{const item=document.createElement(tag);item.textContent=value;if(className)item.className=className;return item;};
  const hours=value=>(value/3600000000).toLocaleString('zh-CN',{maximumFractionDigits:3});
  function setBusy(value){for(const input of el('group-node-list').querySelectorAll('input'))input.disabled=value || (!input.checked && selected.size>=8);}
  function refreshNodes(){
    const mode=selection().mode,available=nodes.filter(value=>value[mode==='mine' ? 'mine':'node']);
    selected=new Set([...selected].filter(id=>available.some(value=>value.node_id===id)));
    if(!selected.size)selected=new Set(available.slice(0,8).map(value=>value.node_id));
    function count(){el('group-selection-note').textContent=`已选 ${selected.size} / ${available.length} · 最多 8 个`;setBusy(isBusy());}
    el('group-node-list').replaceChildren(...available.map(value=>{const label=node('label',''),input=document.createElement('input');input.type='checkbox';input.value=value.node_id;input.checked=selected.has(value.node_id);input.addEventListener('change',()=>{if(input.checked)selected.add(input.value);else selected.delete(input.value);count();});label.append(input,node('span',value.node_id));return label;}));count();
  }
  function reset(){version++;nodes=[];selected.clear();displayed=null;el('group-node-list').replaceChildren();el('group-nodes').replaceChildren();el('group-owners').replaceChildren();el('group-trend').replaceChildren();el('group-summary').hidden=true;}
  function render(page){
    const report=page.report;el('group-summary').hidden=false;el('group-total').textContent=hours(report.recorded_microseconds);el('group-node-count').textContent=report.nodes.length;el('group-segment-count').textContent=report.segment_count;
    const formatter=new Intl.DateTimeFormat('zh-CN',{timeZone:report.query.timezone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
    el('group-snapshot-note').textContent=`当前显示 ${report.query.node_ids.join('、')} · ${formatter.format(new Date(report.query.start_at))} 至 ${formatter.format(new Date(report.query.end_at))}`;
    el('group-nodes').replaceChildren(...report.nodes.map(value=>{const tr=node('tr','');tr.append(node('td',value.node_id),node('td',hours(value.recorded_microseconds),'number'),node('td',value.gpu_count,'number'),node('td',value.segment_count,'number'),node('td',value.snapshot.revision,'number'));return tr;}));
    el('group-owners').replaceChildren(...(report.owners.length ? report.owners.map(value=>{const row=node('div','','owner-row');row.append(node('span',page.labels[value.owner_id] || 'Unknown','owner-name'),node('span',`${hours(value.recorded_microseconds)} GPU 小时`,'owner-value'));return row;}):[node('p','这个时段没有已记录用量，不代表 GPU 未被使用。','muted')]));
    const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg'),width=720,height=173,left=34,top=10,plot=134,max=Math.max(1,...report.days.map(day=>day.recorded_microseconds/3600000000)),slot=(width-left-8)/report.days.length;
    svg.setAttribute('viewBox',`0 0 ${width} ${height}`);svg.setAttribute('role','img');svg.setAttribute('aria-label','每日已记录 GPU 小时合计');
    function element(type,attributes,text){const item=document.createElementNS(ns,type);for(const [key,value] of Object.entries(attributes))item.setAttribute(key,String(value));if(text!==undefined)item.textContent=text;svg.append(item);return item;}
    for(let index=0;index<=2;index++){const y=top+plot*(1-index/2);element('line',{x1:left,x2:width-8,y1:y,y2:y,class:'chart-axis'});element('text',{x:left-7,y:y+3,'text-anchor':'end',class:'chart-label'},(max*index/2).toLocaleString('zh-CN',{maximumFractionDigits:1}));}
    const stride=Math.max(1,Math.ceil(report.days.length/8));report.days.forEach((day,index)=>{const h=day.recorded_microseconds/3600000000/max*plot,rect=element('rect',{x:left+(index+.18)*slot,y:top+plot-h,width:Math.max(.5,slot*.64),height:h,rx:2,class:'chart-bar'}),title=document.createElementNS(ns,'title');title.textContent=`${day.date} · ${hours(day.recorded_microseconds)} GPU 小时`;rect.append(title);if(index%stride===0 || index===report.days.length-1)element('text',{x:left+(index+.5)*slot,y:height-5,'text-anchor':'middle',class:'chart-label'},day.date.slice(5));});el('group-trend').replaceChildren(svg);
  }
  async function load(){
    if(isBusy())return;if(!selected.size){message('至少选择一个节点。',true);return;}
    lock(true);message('正在汇总选定节点…');const ticket=++version,input={...selection(),node_ids:[...selected].sort()};displayed=null;el('group-summary').hidden=true;
    try{const page=await (await request('groups/query',input)).json();if(ticket!==version)return;render(page);displayed={node_ids:page.report.query.node_ids,mode:input.mode,window:{kind:'utc',start_at:page.report.query.start_at,end_at:page.report.query.end_at,timezone:page.report.query.timezone},snapshots:page.report.query.snapshots};message('');}catch(error){if(ticket===version)message(error.message,true);}finally{if(ticket===version)lock(false);}
  }
  async function download(kind){if(!displayed || isBusy())return;const ticket=version,input=displayed;lock(true);try{const blob=await (await request(`groups/${kind}`,input)).blob();if(ticket!==version)return;const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=`aris-selected-node-summary.${kind==='report' ? 'html':'csv'}`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);message('已导出当前显示节点的固定快照汇总。');}catch(error){if(ticket===version)message(error.message,true);}finally{if(ticket===version)lock(false);}}
  el('group-report').addEventListener('click',()=>download('report'));el('group-csv').addEventListener('click',()=>download('csv'));
  return{get active(){return active;},set active(value){active=value;el('group-results').hidden=!value;},configure(values){nodes=values;},refreshNodes,load,reset,setBusy};
};
