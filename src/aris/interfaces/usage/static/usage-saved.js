"use strict";
window.createSavedUsage = function ({el, request, lock, message, isBusy, selection, timezone, plansAvailable=()=>false}) {
  let active = false, cursor = null, chosen = null, selectedQuery = null, chosenRead = null, version = 0;
  const names = {complete:'完整', partial:'部分覆盖', unknown:'未知'};
  const states = {recorded:'已记录', observed_idle:'空闲', unknown:'未知', uncovered:'未覆盖'};
  const hours = value => (value / 3600000000).toLocaleString('zh-CN',{maximumFractionDigits:3});
  const duration = value => value >= 3600000000 ? `${hours(value)} 小时` : value >= 60000000 ? `${(value / 60000000).toLocaleString('zh-CN',{maximumFractionDigits:2})} 分钟` : `${(value / 1000000).toLocaleString('zh-CN',{maximumFractionDigits:3})} 秒`;
  const node = (tag, value, className) => {const item=document.createElement(tag);item.textContent=value;if(className)item.className=className;return item;};
  function format(value) {return new Intl.DateTimeFormat('zh-CN',{timeZone:timezone(),month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(value));}
  function badge(status) {return node('span',names[status],`coverage-badge ${status}`);}
  function reset() {version++;cursor=null;chosen=null;chosenRead=null;selectedQuery=null;el('saved-list').replaceChildren();el('saved-cells').replaceChildren();el('saved-timeline').replaceChildren();el('saved-plan-list').replaceChildren();el('saved-plans').hidden=true;el('saved-detail').hidden=true;el('saved-more').hidden=true;el('saved-empty').hidden=true;}
  function plans(page) {
    const stateNames={scheduled:'尚未到期',pending:'等待生成',delayed:'生成延迟'};
    const stamp=(value,zone)=>new Intl.DateTimeFormat('zh-CN',{timeZone:zone,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(value));
    const cards=page.items.map(item=>{
      const card=node('article','','weekly-plan-card'),head=node('div','','weekly-plan-head');
      head.append(node('h3',`${item.node_id} · 每周统计`),node('span',stateNames[item.state],`coverage-badge ${item.state==='delayed'?'issue':item.state==='pending'?'waiting':''}`));
      card.append(head,node('p',`下一统计周 · ${item.next_week}`),node('p',`预计生成 · ${stamp(item.next_due_at,item.timezone)}`));
      if(item.last_result)card.append(node('p',`最近保存 · ${item.last_result.week} · ${names[item.last_result.coverage]}`));
      else card.append(node('p','尚未保存周期结果','muted'));
      card.append(node('small',item.timezone));return card;
    });
    el('saved-plan-list').replaceChildren(...cards);el('saved-plans').hidden=false;
    el('saved-plan-note').textContent=cards.length ? '计划状态按周历和已保存结果显示；尚未判断后台进程是否运行。日期筛选仅影响下方快照列表。' : '当前节点和视图没有已登记的自动统计计划。';
  }
  function detail(result) {
    const view=result.view;const gpuLabel=id=>Object.hasOwn(result.gpu_indices||{},id)?`GPU ${result.gpu_indices[id]}`:id;
    el('saved-detail').hidden=false;
    el('saved-title').textContent=`${view.query.node_id} · ${view.detail_scope==='mine' ? '我的统计':'节点统计'}`;
    el('saved-period').textContent=`${format(view.query.start_at)} 至 ${format(view.query.end_at)} · ${timezone()}`;
    el('saved-status').textContent=names[view.coverage];el('saved-status').className=`coverage-badge ${view.coverage}`;
    el('saved-recorded').textContent=hours(view.recorded_microseconds);
    el('saved-verified').textContent=hours(view.verified_usage_microseconds);
    el('saved-total').textContent=view.total_usage_microseconds===null ? '暂不可用':hours(view.total_usage_microseconds);
    el('saved-total-note').textContent=view.total_usage_microseconds===null ? '仍有待处理或缺失区间':'GPU 小时';
    el('saved-quality').textContent=`${view.source_prefix_complete ? '来源段完整':'来源段尚未齐全'} · 已覆盖 ${duration(view.covered_gpu_microseconds)} · 未知 ${duration(view.unknown_gpu_microseconds)} · 未覆盖 ${duration(view.uncovered_gpu_microseconds)}`;
    el('saved-generated').textContent=`生成于 ${format(view.created_at)}`;
    el('saved-interval-title').textContent=view.detail_scope==='mine' ? '本人的已验证区间':'GPU 时间区间';
    const rows=view.cells.map(cell=>{
      const tr=document.createElement('tr');const owner=cell.owner_id ? ` · ${result.labels[cell.owner_id] || 'Unknown'}`:'';
      tr.append(node('td',gpuLabel(cell.gpu_uuid)),node('td',`${states[cell.state]}${owner}`,'saved-state'),node('td',format(cell.start_at)),node('td',format(cell.end_at)),node('td',duration((new Date(cell.end_at)-new Date(cell.start_at))*1000),'number'));
      return tr;
    });
    if(!rows.length){const tr=document.createElement('tr'),td=node('td',view.detail_scope==='mine' ? '这份快照中没有本人的已验证区间。':'这份快照中没有可展示的区间。','saved-empty-row');td.colSpan=5;tr.append(td);rows.push(tr);}
    el('saved-cells').replaceChildren(...rows);
    const grouped=new Map();for(const cell of view.cells){if(!grouped.has(cell.gpu_uuid))grouped.set(cell.gpu_uuid,[]);grouped.get(cell.gpu_uuid).push(cell);}
    const ns='http://www.w3.org/2000/svg',first=new Date(view.query.start_at).getTime(),span=new Date(view.query.end_at)-first;
    el('saved-timeline').replaceChildren(...[...grouped].map(([gpu,cells])=>{
      const row=node('div','','saved-gpu'),track=node('div','','saved-track'),svg=document.createElementNS(ns,'svg');
      svg.setAttribute('viewBox','0 0 1000 13');svg.setAttribute('preserveAspectRatio','none');svg.setAttribute('role','img');svg.setAttribute('aria-label',`${gpuLabel(gpu)} ${view.detail_scope==='mine' ? '本人使用':'采样状态'}时间线`);
      for(const cell of cells){const rect=document.createElementNS(ns,'rect');rect.setAttribute('x',String((new Date(cell.start_at)-first)/span*1000));rect.setAttribute('width',String((new Date(cell.end_at)-new Date(cell.start_at))/span*1000));rect.setAttribute('height','13');rect.setAttribute('class',`saved-fill-${cell.state}`);const title=document.createElementNS(ns,'title');title.textContent=`${states[cell.state]} · ${format(cell.start_at)}–${format(cell.end_at)}`;rect.append(title);svg.append(rect);}
      track.append(svg);const display=gpuLabel(gpu),gpuName=node('span',display.length>18 ? `${display.slice(0,6)}…${display.slice(-6)}`:display);gpuName.title=display;row.append(gpuName,track);return row;
    }));
    el('saved-scope-note').textContent=`${view.projection==='personal_from_node' ? '从节点统计中整理本人的用量和区间；完整性按节点采样证据评估。':view.detail_scope==='mine' ? '本人视图展示自己的已验证区间；完整性按节点采样证据评估。':'状态区间按归档采样和驻留计时方法整理。'} 这是保存时的统计结果，生成后不会自动更新。`;
  }
  async function openItem(item, input, ticket) {
    const result=await (await request('snapshots/read',{node_id:input.node_id,mode:input.mode,id:item.id})).json();
    if(ticket!==version)return;
    chosen=item.id;chosenRead={node_id:input.node_id,mode:input.mode,id:item.id};detail(result);
    for(const button of el('saved-list').querySelectorAll('button'))button.setAttribute('aria-current',String(button.dataset.snapshot===chosen));
  }
  async function open(item, input) {if(isBusy())return;const ticket=version;lock(true);message('正在读取统计快照…');try{await openItem(item,input,ticket);if(ticket===version)message('');}catch(error){if(ticket===version){el('saved-detail').hidden=true;chosenRead=null;message(error.message,true);}}finally{if(ticket===version)lock(false);}}
  async function load(more=false) {
    if(isBusy())return;lock(true);message('正在读取统计快照…');
    const input=more && selectedQuery ? {...selectedQuery,before:cursor}:{...selection(),limit:16,...(plansAvailable()?{include_plans:true}:{})};
    if(!more)reset();const ticket=version;
    try{
      const response=await (await request('snapshots/query',input)).json();if(ticket!==version)return;const result=input.include_plans?response.snapshots:response;if(input.include_plans)plans(response.plans);selectedQuery=input;cursor=result.next_cursor;
      const cards=result.items.map(item=>{const li=document.createElement('li'),button=node('button','');button.type='button';button.dataset.snapshot=item.id;button.setAttribute('aria-current',String(chosen===item.id));button.append(node('strong',`${format(item.start_at)} – ${format(item.end_at)}`),badge(item.coverage),node('small',`保存于 ${format(item.created_at)}`));button.addEventListener('click',()=>open(item,input));li.append(button);return li;});
      el('saved-list').append(...cards);el('saved-more').hidden=!cursor;el('saved-empty').hidden=el('saved-list').children.length!==0;
      if(!more && result.items.length)await openItem(result.items[0],input,ticket);
      if(ticket===version)message('');
    }catch(error){if(ticket===version)message(error.message,true);}finally{if(ticket===version)lock(false);}
  }
  async function download(kind){
    if(isBusy() || !chosenRead)return;const ticket=version,input={...chosenRead};lock(true);message('正在准备下载…');
    try{const blob=await (await request('snapshots/read',input,kind==='report'?'text/html':'text/csv')).blob();if(ticket!==version)return;const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=`aris-saved-usage.${kind==='report' ? 'html':'csv'}`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);message('已导出这份统计快照。');}catch(error){if(ticket===version)message(error.message,true);}finally{if(ticket===version)lock(false);}
  }
  el('saved-more').addEventListener('click',()=>load(true));
  el('saved-report').addEventListener('click',()=>download('report'));el('saved-csv').addEventListener('click',()=>download('export'));
  return {get active(){return active;},set active(value){active=value;el('saved-results').hidden=!value;},load,reset};
};
