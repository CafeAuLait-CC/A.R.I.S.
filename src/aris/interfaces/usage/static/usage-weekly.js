"use strict";
window.createWeeklyUsage = function ({el, request, lock, message, isBusy, selection}) {
  let active=false, cursor=null, selectedQuery=null, chosen=null, version=0;
  const node=(tag,value,className)=>{const item=document.createElement(tag);item.textContent=value;if(className)item.className=className;return item;};
  const hours=value=>(value/3600000000).toLocaleString('zh-CN',{maximumFractionDigits:3});
  const format=(value,zone)=>new Intl.DateTimeFormat('zh-CN',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(value));
  function reset(){version++;cursor=null;selectedQuery=null;chosen=null;el('weekly-list').replaceChildren();el('weekly-days').replaceChildren();el('weekly-owners').replaceChildren();el('weekly-detail').hidden=true;el('weekly-more').hidden=true;el('weekly-empty').hidden=true;}
  function render(report){
    const history=report.history;
    el('weekly-detail').hidden=false;
    el('weekly-title').textContent=`${history.node_id} · ${report.request.mode==='mine' ? '我的周报':'节点周报'}`;
    el('weekly-period').textContent=`${format(history.start_at,history.timezone)} 至 ${format(history.end_at,history.timezone)} · ${history.timezone}`;
    el('weekly-total').textContent=hours(history.recorded_microseconds);el('weekly-gpus').textContent=history.gpu_count;el('weekly-segments').textContent=history.segment_count;
    el('weekly-generated').textContent=`生成于 ${format(report.created_at,history.timezone)} · 账本快照 ${history.snapshot.revision}`;
    const maximum=Math.max(1,...history.days.map(day=>day.recorded_microseconds));
    el('weekly-days').replaceChildren(...history.days.map(day=>{
      const row=node('div','','weekly-day'),track=node('div','','weekly-day-track'),ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg'),rect=document.createElementNS(ns,'rect');
      svg.setAttribute('viewBox','0 0 100 8');svg.setAttribute('preserveAspectRatio','none');svg.setAttribute('aria-hidden','true');rect.setAttribute('width',String(day.recorded_microseconds/maximum*100));rect.setAttribute('height','8');svg.append(rect);track.append(svg);
      row.append(node('span',day.date.slice(5)),track,node('strong',hours(day.recorded_microseconds)));return row;
    }));
    el('weekly-owners').replaceChildren(...(history.owners.length ? history.owners.map(owner=>{
      const row=node('div','','owner-row');row.append(node('span',report.labels[owner.owner_id] || 'Unknown','owner-name'),node('span',`${hours(owner.recorded_microseconds)} GPU 小时`,'owner-value'));return row;
    }):[node('p','这个时段没有已记录用量；不代表 GPU 未被使用。','muted')]));
  }
  async function openItem(item,input,ticket){const report=await (await request('weekly/read',{node_id:input.node_id,mode:input.mode,id:item.id})).json();if(ticket!==version)return;chosen={node_id:input.node_id,mode:input.mode,id:item.id};render(report);for(const button of el('weekly-list').querySelectorAll('button'))button.setAttribute('aria-current',String(button.dataset.report===item.id));}
  async function open(item,input){if(isBusy())return;const ticket=version;lock(true);message('正在读取周报…');try{await openItem(item,input,ticket);if(ticket===version)message('');}catch(error){if(ticket===version){el('weekly-detail').hidden=true;message(error.message,true);}}finally{if(ticket===version)lock(false);}}
  async function load(more=false){
    if(isBusy())return;lock(true);message('正在读取已保存的周报…');if(!more)reset();const ticket=version;
    const {owner_id,...base}=selection();const input=more && selectedQuery ? {...selectedQuery,before:cursor}:{...base,limit:16};
    try{const page=await (await request('weekly/query',input)).json();if(ticket!==version)return;selectedQuery=input;cursor=page.next_cursor;
      el('weekly-list').append(...page.items.map(item=>{const li=node('li',''),button=node('button','');button.type='button';button.dataset.report=item.id;button.setAttribute('aria-current',String(chosen?.id===item.id));button.append(node('strong',`${item.week} 当周`),node('span','已记录用量','coverage-badge'),node('small',`${hours(item.recorded_microseconds)} GPU 小时 · ${item.timezone}`));button.addEventListener('click',()=>open(item,input));li.append(button);return li;}));
      el('weekly-empty').hidden=el('weekly-list').children.length!==0;el('weekly-more').hidden=!cursor;if(!more && page.items.length)await openItem(page.items[0],input,ticket);if(ticket===version)message('');
    }catch(error){if(ticket===version)message(error.message,true);}finally{if(ticket===version)lock(false);}
  }
  async function download(kind){if(isBusy() || !chosen)return;const ticket=version,input={...chosen};lock(true);try{const blob=await (await request(`weekly/${kind}`,input)).blob();if(ticket!==version)return;const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=`aris-weekly-usage.${kind==='report' ? 'html':'csv'}`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);message('已导出这份保存的周报。');}catch(error){if(ticket===version)message(error.message,true);}finally{if(ticket===version)lock(false);}}
  el('weekly-more').addEventListener('click',()=>load(true));el('weekly-report').addEventListener('click',()=>download('report'));el('weekly-csv').addEventListener('click',()=>download('csv'));
  return {get active(){return active;},set active(value){active=value;el('weekly-results').hidden=!value;},load,reset};
};
