"use strict";
(() => {
  const $ = id => document.getElementById(id);
  let token = "", session = null, current = null, selection = null, busy = false, epoch = 0;
  const text = (tag, value, className) => {const node = document.createElement(tag); node.textContent = value; if(className) node.className = className; return node;};
  const hours = value => (value / 3600000000).toLocaleString("zh-CN", {maximumFractionDigits:3});
  const duration = value => {const seconds = value / 1000000; return seconds >= 3600 ? `${hours(value)} 小时` : seconds >= 60 ? `${(seconds/60).toLocaleString("zh-CN",{maximumFractionDigits:2})} 分钟` : `${seconds.toLocaleString("zh-CN",{maximumFractionDigits:3})} 秒`;};
  function message(value, error = false) {$('message').textContent = value; $('message').hidden = !value; $('message').classList.toggle('error', error);}
  function lock(value) {busy = value; ['query','export','report','previous','next','node','mode','range','start','end','owner','tab-records','tab-saved','saved-more','tab-weekly','weekly-more','weekly-report','weekly-csv','tab-group','group-report','group-csv'].forEach(id => $(id).disabled = value);group.setBusy(value);if(!value)$('node').disabled=group.active; if(!value && current){$('previous').disabled = current.offset === 0; $('next').disabled = !current.has_more;}}
  async function request(path, body) {
    const pendingEpoch = epoch;
    const response = await fetch(`/internal/usage/${path}`, {method:'POST', cache:'no-store', headers:{Authorization:`Bearer ${token}`, ...(body ? {'Content-Type':'application/json'} : {})}, body:body ? JSON.stringify(body) : undefined});
    if(pendingEpoch !== epoch) throw new Error('连接已结束。');
    if(!response.ok) {let code; try{code = (await response.json()).error.code;}catch{code = 'UNAVAILABLE';} const errors = {PERMISSION_DENIED:'没有相应的使用记录读取权限，或凭据已失效。', VALIDATION_ERROR:'请检查日期范围与查询条件。', USAGE_REPORT_TOO_LARGE:'这份账本超出当前查询上限，请稍后再试或联系管理员。', CONFLICT:'这份查询快照暂时不可用，请重新查询。'}; throw new Error(errors[code] || '使用记录暂时不可用，请稍后重试。');}
    return response;
  }
  function localDate(value) {const parts = new Intl.DateTimeFormat('en-CA',{timeZone:session.actor.timezone,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(value); const p = Object.fromEntries(parts.map(item => [item.type,item.value])); return `${p.year}-${p.month}-${p.day}`;}
  function dates() {const end = localDate(new Date()); const start = new Date(`${end}T12:00:00Z`); if($('range').value === 'week') start.setUTCDate(start.getUTCDate() - (start.getUTCDay()+6)%7); else if($('range').value === 'seven') start.setUTCDate(start.getUTCDate()-6); else return; $('start').value = start.toISOString().slice(0,10); $('end').value = end;}
  function modes() {const node = group.active ? {mine:session.nodes.some(value=>value.mine),node:session.nodes.some(value=>value.node)}:session.nodes.find(value => value.node_id === $('node').value); [...$('mode').options].forEach(option => option.disabled = !node[option.value === 'mine' ? 'mine':'node']); if($('mode').selectedOptions[0].disabled) $('mode').value = node.mine ? 'mine':'node'; $('owner-filter').hidden = group.active || weekly.active || $('mode').value !== 'node';$('node-filter').hidden=group.active;$('node').disabled=group.active || busy;}
  const label = id => current.labels[id] || id;
  function drawTrend(report) {
    const ns = 'http://www.w3.org/2000/svg'; const svg = document.createElementNS(ns,'svg'); const width=720, height=173, left=34, right=8, top=10, bottom=29, plot=height-top-bottom, maximum=Math.max(1,...report.days.map(day => day.recorded_microseconds/3600000000)); svg.setAttribute('viewBox',`0 0 ${width} ${height}`); svg.setAttribute('role','presentation');
    function el(type, attrs, content) {const item=document.createElementNS(ns,type); Object.entries(attrs).forEach(([key,val])=>item.setAttribute(key,String(val))); if(content !== undefined) item.textContent=content; svg.append(item); return item;}
    for(let step=0;step<=2;step++){const y=top+plot*(1-step/2); el('line',{x1:left,x2:width-right,y1:y,y2:y,class:'chart-axis'}); el('text',{x:left-7,y:y+3,'text-anchor':'end',class:'chart-label'},(maximum*step/2).toLocaleString('zh-CN',{maximumFractionDigits:1}));}
    const slot=(width-left-right)/report.days.length, stride=Math.max(1,Math.ceil(report.days.length/8));
    report.days.forEach((day,index)=>{const h=day.recorded_microseconds/3600000000/maximum*plot; const bar=el('rect',{x:left+index*slot+slot*.18,y:top+plot-h,width:Math.max(.6,slot*.64),height:h,rx:Math.min(3,slot*.1),class:'chart-bar'}); const title=document.createElementNS(ns,'title');title.textContent=`${day.date} · ${duration(day.recorded_microseconds)}`;bar.append(title);if(index%stride===0 || index===report.days.length-1) el('text',{x:left+(index+.5)*slot,y:height-5,'text-anchor':'middle',class:'chart-label'},day.date.slice(5));});
    $('trend').replaceChildren(svg); $('trend').setAttribute('aria-label',report.days.map(day=>`${day.date} ${duration(day.recorded_microseconds)}`).join('；')); $('trend-note').textContent=report.days.length > 31 ? '按日汇总，日期标签已简化显示。' : '按所选时区的自然日汇总。';
  }
  function render() {
    const report=current.report; $('results').hidden=false; $('total').textContent=hours(report.recorded_microseconds); $('gpu-count').textContent=report.gpu_count; $('segment-count').textContent=report.segment_count; drawTrend(report);
    const owners=report.owners.map(owner=>{const row=text('div','', 'owner-row');row.append(text('span',label(owner.owner_id),'owner-name'),text('span',hours(owner.recorded_microseconds),'owner-value'));const ns='http://www.w3.org/2000/svg', track=document.createElementNS(ns,'svg');track.setAttribute('viewBox','0 0 100 5');track.setAttribute('preserveAspectRatio','none');track.classList.add('owner-track');const bar=document.createElementNS(ns,'rect');bar.setAttribute('width',String(owner.recorded_microseconds / Math.max(1,report.recorded_microseconds)*100));bar.setAttribute('height','5');bar.classList.add('owner-fill');track.append(bar);row.append(track);return row;}); $('owner-summary').replaceChildren(...(owners.length ? owners : [text('p','暂无已记录用量','muted')]));
    const formatter=new Intl.DateTimeFormat('zh-CN',{timeZone:report.timezone,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
    $('records').replaceChildren(...report.rows.map(row=>{const tr=document.createElement('tr'), gpu=text('td',row.model);gpu.append(text('small',row.gpu_uuid));tr.append(text('td',label(row.owner_id)),gpu,text('td',formatter.format(new Date(row.start_at))),text('td',formatter.format(new Date(row.end_at))),text('td',duration(row.recorded_microseconds),'number'));return tr;}));
    $('empty').hidden=report.segment_count!==0; $('page-note').textContent=report.segment_count ? `第 ${current.offset+1}–${current.offset+report.rows.length} 段，共 ${report.segment_count} 段` : '0 条记录'; $('snapshot-note').textContent=`${report.node_id} · ${formatter.format(new Date(report.start_at))} 至 ${formatter.format(new Date(report.end_at))} · 固定记录快照`; $('previous').disabled=current.offset===0; $('next').disabled=!current.has_more;
  }
  const saved = window.createSavedUsage({el:$, request, lock, message, isBusy:()=>busy,
    selection:()=>({node_id:$('node').value,mode:$('mode').value,owner_id:$('mode').value==='node' && $('owner').value ? $('owner').value:null,
      window:{kind:'calendar',start_date:$('start').value,end_date:$('end').value,timezone:session.actor.timezone}}),
    timezone:()=>session.actor.timezone});
  const weekly = window.createWeeklyUsage({el:$,request,lock,message,isBusy:()=>busy,
    plansAvailable:()=>Boolean(session?.weekly_plans_available),
    selection:()=>({node_id:$('node').value,mode:$('mode').value,window:{kind:'calendar',start_date:$('start').value,end_date:$('end').value,timezone:session.actor.timezone}})});
  const group=window.createGroupUsage({el:$,request,lock,message,isBusy:()=>busy,
    selection:()=>({mode:$('mode').value,window:{kind:'calendar',start_date:$('start').value,end_date:$('end').value,timezone:session.actor.timezone}})});
  async function tab(value){if(busy)return;saved.active=value==='saved';weekly.active=value==='weekly';group.active=value==='group';$('results').hidden=saved.active || weekly.active || group.active || !current;modes();
    for(const name of ['records','saved','weekly','group'])$(`tab-${name}`).setAttribute('aria-selected',String(value===name));
    if(group.active){group.refreshNodes();await group.load();}else if(weekly.active)await weekly.load();else if(saved.active)await saved.load();else{$('results').hidden=true;await load();}}
  $('tab-records').addEventListener('click',()=>tab('records'));
  $('tab-saved').addEventListener('click',()=>tab('saved'));
  $('tab-weekly').addEventListener('click',()=>tab('weekly'));
  $('tab-group').addEventListener('click',()=>tab('group'));
  async function load(offset=0, fixed=false) {
    if(group.active)return group.load();
    if(weekly.active)return weekly.load();
    if(saved.active)return saved.load();
    if(busy)return;lock(true);message('正在读取使用记录…');
    const input=fixed && selection ? {...selection,offset} : {node_id:$('node').value,mode:$('mode').value,owner_id:$('mode').value==='node' && $('owner').value ? $('owner').value:null, window:{kind:'calendar',start_date:$('start').value,end_date:$('end').value,timezone:session.actor.timezone},limit:25,offset:0};
    try{const result=await (await request('query',input)).json();current=result;selection={...input,window:{kind:'utc',start_at:result.report.start_at,end_at:result.report.end_at,timezone:result.report.timezone},snapshot:result.report.snapshot,offset:0};if(!fixed && !input.owner_id){$('owner').replaceChildren(new Option('全部用户',''),...result.report.owners.map(owner=>new Option(result.labels[owner.owner_id] || owner.owner_id,owner.owner_id)));}render();message('');}catch(error){message(error.message,true);if(!fixed){$('results').hidden=true;current=null;selection=null;}}finally{lock(false);}
  }
  $('connect-form').addEventListener('submit',async event=>{
    event.preventDefault();if(busy)return;epoch++;const opening=epoch;lock(true);
    token=$('credential').value.trim();$('credential').value='';
    try{session=await (await request('session')).json();saved.active=false;saved.reset();weekly.active=false;weekly.reset();group.active=false;group.reset();group.configure(session.nodes);
      $('tab-saved').hidden=!session.saved_snapshots_available;$('tab-weekly').hidden=!session.weekly_reports_available;$('tab-group').hidden=!session.group_reports_available;$('tab-records').setAttribute('aria-selected','true');$('tab-saved').setAttribute('aria-selected','false');$('tab-weekly').setAttribute('aria-selected','false');$('tab-group').setAttribute('aria-selected','false');
      $('node').replaceChildren(...session.nodes.map(node=>new Option(node.node_id,node.node_id)));$('identity').textContent=session.actor.display_name;
      $('timezone').textContent=`时区 ${session.actor.timezone}`;$('connect-panel').hidden=true;$('workspace').hidden=false;modes();dates();lock(false);await load();
    }catch(error){if(opening===epoch){token='';message(error.message,true);}}finally{if(opening===epoch)lock(false);}
  });
  $('disconnect').addEventListener('click',()=>{epoch++;token='';session=null;current=null;selection=null;saved.active=false;saved.reset();weekly.active=false;weekly.reset();group.active=false;group.reset();lock(false);$('node-filter').hidden=false;
    $('workspace').hidden=true;$('results').hidden=true;$('connect-panel').hidden=false;$('identity').textContent='本地只读查询';message('');});
  $('node').addEventListener('change',()=>{modes();$('owner').replaceChildren(new Option('全部用户',''));});$('mode').addEventListener('change',()=>{modes();$('owner').value='';if(group.active)group.refreshNodes();});$('range').addEventListener('change',dates);['start','end'].forEach(id=>$(id).addEventListener('change',()=>{$('range').value='custom';}));$('query').addEventListener('click',()=>load());$('owner').addEventListener('change',()=>load());$('previous').addEventListener('click',()=>load(Math.max(0,current.offset-current.limit),true));$('next').addEventListener('click',()=>load(current.offset+current.limit,true));
  async function download(path,filename,success){if(!selection || busy)return;lock(true);try{const blob=await (await request(path,selection)).blob();const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);message(success);}catch(error){message(error.message,true);}finally{lock(false);}}
  $('export').addEventListener('click',()=>download('export','aris-recorded-usage.csv','已导出当前快照的全部匹配记录。'));
  $('report').addEventListener('click',()=>download('report','aris-recorded-usage-report.html','报告已导出，可在浏览器阅读或打印。'));
  window.addEventListener('pagehide',()=>{$('disconnect').click();});
})();
