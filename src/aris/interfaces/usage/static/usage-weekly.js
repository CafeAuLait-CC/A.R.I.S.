"use strict";
window.createWeeklyUsage = function ({el, request, lock, message, isBusy, selection, plansAvailable}) {
  let active=false, cursor=null, selectedQuery=null, chosen=null, version=0;
  const node=(tag,value,className)=>{const item=document.createElement(tag);item.textContent=value;if(className)item.className=className;return item;};
  const hours=value=>(value/3600000000).toLocaleString('zh-CN',{maximumFractionDigits:3});
  const format=(value,zone)=>new Intl.DateTimeFormat('zh-CN',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(value));
  function reset(){version++;cursor=null;selectedQuery=null;chosen=null;el('weekly-list').replaceChildren();el('weekly-days').replaceChildren();el('weekly-owners').replaceChildren();el('weekly-plan-list').replaceChildren();el('weekly-plans').hidden=true;el('weekly-detail').hidden=true;el('weekly-more').hidden=true;el('weekly-empty').hidden=true;}
  function renderPlans(page){
    el('weekly-plans').hidden=false;
    el('weekly-plan-heading').textContent=page ? `周报计划 · ${page.items.length} 项`:'周报计划 · 状态暂不可用';
    el('weekly-plan-note').textContent=!page ? '暂时无法读取计划状态，已保存的报告仍可单独查看。':!page.items.length ? '当前视图还没有保存的周报计划。':`${page.has_more ? '仅显示最近 16 项。':''}计划状态按当前节点显示；下方报告仍按日期筛选。`;
    if(!page)return;
    el('weekly-plan-list').replaceChildren(...page.items.map(plan=>{
      const counts=plan.jobs.counts,waiting=counts.pending+counts.running+counts.retry,failed=counts.blocked+counts.dead,total=Object.values(counts).reduce((a,b)=>a+b,0),card=node('article','','weekly-plan-card'),head=node('div','','weekly-plan-head');
      let status='计划已保存',tone='';
      if(plan.last_error_code || failed){status='需要检查';tone='issue';}
      else if(plan.jobs.overdue_count){status='生成延迟';tone='issue';}
      else if(new Date(plan.next_due_at)<=new Date(page.read_at)){status='待补跑';tone='waiting';}
      else if(waiting){status=counts.retry ? '等待重试':'等待生成';tone='waiting';}
      else if(counts.completed){status='已安排任务完成';}
      head.append(node('h3',plan.name),node('span',status,`coverage-badge ${tone}`));card.append(head,
        node('p',`下一次计划时间 ${format(plan.next_due_at,plan.timezone)} · ${plan.timezone}`),
        node('p',total ? `已安排时段截至 ${plan.next_week} 00:00`:'尚未安排任务','muted'),
        node('p',`已完成 ${counts.completed} · 待生成 ${waiting} · 失败 ${failed}`,'weekly-plan-counts'));
      if(plan.last_prepared_at)card.append(node('small',`最近安排 ${format(plan.last_prepared_at,plan.timezone)}`));
      if(plan.jobs.overdue_count)card.append(node('p',`${plan.jobs.overdue_count} 个报告任务尚未完成，已等待超过 30 分钟。`,'weekly-plan-warning'));
      if(plan.last_error_code)card.append(node('p',plan.last_error_code==='PERMISSION_DENIED' ? '计划所需的当前权限不足。':'最近安排任务时遇到错误。','weekly-plan-warning'));
      return card;
    }));
  }
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
    const planResponse=!more && plansAvailable() ? request('weekly/plans',{node_id:input.node_id,mode:input.mode}).then(response=>response.json()).catch(()=>null):null;
    try{const page=await (await request('weekly/query',input)).json();if(ticket!==version)return;selectedQuery=input;cursor=page.next_cursor;
      el('weekly-list').append(...page.items.map(item=>{const li=node('li',''),button=node('button','');button.type='button';button.dataset.report=item.id;button.setAttribute('aria-current',String(chosen?.id===item.id));button.append(node('strong',`${item.week} 当周`),node('span','已记录用量','coverage-badge'),node('small',`${hours(item.recorded_microseconds)} GPU 小时 · ${item.timezone}`));button.addEventListener('click',()=>open(item,input));li.append(button);return li;}));
      el('weekly-empty').hidden=el('weekly-list').children.length!==0;el('weekly-more').hidden=!cursor;if(!more && page.items.length)await openItem(page.items[0],input,ticket);if(planResponse){const plans=await planResponse;if(ticket===version)renderPlans(plans);}if(ticket===version)message('');
    }catch(error){if(ticket===version)message(error.message,true);}finally{if(ticket===version)lock(false);}
  }
  async function download(kind){if(isBusy() || !chosen)return;const ticket=version,input={...chosen};lock(true);try{const blob=await (await request(`weekly/${kind}`,input)).blob();if(ticket!==version)return;const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=`aris-weekly-usage.${kind==='report' ? 'html':'csv'}`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);message('已导出这份保存的周报。');}catch(error){if(ticket===version)message(error.message,true);}finally{if(ticket===version)lock(false);}}
  let combined = null;
  let combinedVersion = 0;
  const combinedDate = el('combined-week-date');
  const coverage = {complete:'完整', partial:'部分覆盖', unknown:'未知', missing:'待生成'};

  function clearCombined() {
    combinedVersion++;
    combined = null;
    el('combined-week-result').hidden = true;
    el('combined-week-exports').hidden = true;
    el('combined-week-nodes').replaceChildren();
    el('combined-week-summary').textContent = '';
  }

  function configureCombined(available, timezone) {
    clearCombined();
    el('combined-week-panel').hidden = !available;
    if (!available) return;
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone:timezone, year:'numeric', month:'2-digit', day:'2-digit',
    }).formatToParts(new Date());
    const fields = Object.fromEntries(parts.map(part => [part.type, part.value]));
    const monday = new Date(`${fields.year}-${fields.month}-${fields.day}T00:00:00Z`);
    monday.setUTCDate(monday.getUTCDate() - ((monday.getUTCDay() + 6) % 7) - 7);
    combinedDate.value = monday.toISOString().slice(0, 10);
  }

  async function readCombined(event) {
    event.preventDefault();
    if (isBusy() || el('combined-week-panel').hidden) return;
    clearCombined();
    const ticket = combinedVersion;
    const input = {source:'combined_daily_rollups', week:combinedDate.value};
    lock(true);
    message('正在读取全部节点周报…');
    try {
      const report = await (await request('weekly/read', input)).json();
      if (ticket !== combinedVersion) return;
      const present = report.nodes.some(value => value.report_id);
      const amount = value => value === null ? '未知' : hours(value);
      el('combined-week-summary').textContent = present
        ? `${report.week} 起 · ${report.timezone} · ${coverage[report.coverage]} · 已核实合计 ${amount(report.verified_usage_microseconds)} GPU 小时 · 完整用量 ${amount(report.total_usage_microseconds)}`
        : `${report.week} 起 · ${report.timezone} · 这周还没有保存的节点周报。`;
      el('combined-week-nodes').replaceChildren(...report.nodes.map(value => {
        const row = node('tr', '');
        const title = node('th', value.node_id);
        title.scope = 'row';
        row.append(title, node('td', coverage[value.coverage]),
          node('td', amount(value.verified_usage_microseconds), 'number'),
          node('td', amount(value.verified_retained_gap_microseconds), 'number'));
        return row;
      }));
      if (present) combined = {...input, expected_fingerprint:report.fingerprint};
      el('combined-week-exports').hidden = !present;
      el('combined-week-result').hidden = false;
      message('');
    } catch (error) {
      if (ticket === combinedVersion) message(error.message, true);
    } finally {
      if (ticket === combinedVersion) lock(false);
    }
  }

  async function downloadCombined(kind, extension) {
    if (isBusy() || !combined) return;
    const ticket = combinedVersion;
    const input = combined;
    lock(true);
    try {
      const blob = await (await request(`weekly/${kind}`, input)).blob();
      if (ticket !== combinedVersion) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `aris-combined-week-${input.week}.${extension}`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      message('已导出全部节点周报。');
    } catch (error) {
      if (ticket === combinedVersion) message(error.message, true);
    } finally {
      if (ticket === combinedVersion) lock(false);
    }
  }

  el('combined-week-form').addEventListener('submit', readCombined);
  combinedDate.addEventListener('change', clearCombined);
  el('combined-week-html').addEventListener('click', () => downloadCombined('report', 'html'));
  el('combined-week-csv').addEventListener('click', () => downloadCombined('csv', 'csv'));
  el('weekly-more').addEventListener('click',()=>load(true));el('weekly-report').addEventListener('click',()=>download('report'));el('weekly-csv').addEventListener('click',()=>download('csv'));
  return {get active(){return active;},set active(value){active=value;el('weekly-results').hidden=!value;},load,reset,configureCombined};
};
