"use strict";
window.ARISPolicyMaterials = ({$,text,request,run,formatDate,quota,comparison}) => {
  let available=false,source=null,items=[],next=null,locked=false,revision=0;
  const panel=$('materials-panel');
  const rules={'reservation.max_duration':'预约时长','reservation.min_notice':'至少提前','reservation.max_advance':'最远提前','reservation.concurrent':'总并发 GPU','quota.actual_usage':'假设使用额度'};
  const reasons={WITHIN_LIMIT:'在限制内',MAX_DURATION_EXCEEDED:'超过最长预约时长',MIN_NOTICE_UNMET:'提前时间不足',ADVANCE_HORIZON_EXCEEDED:'超过提前预约范围',CONCURRENCY_UNKNOWN:'没有提供假设并发数',CONCURRENCY_LIMIT_EXCEEDED:'超过并发上限',QUOTA_SCOPE_UNCONFIGURED:'此节点未配置额度',QUOTA_UNLIMITED:'规则不设限额',USAGE_UNKNOWN:'未提供完整假设用量',USAGE_SCOPE_MISMATCH:'假设用量的用户或范围不匹配',USAGE_CUTOFF_MISMATCH:'假设用量的截止时刻不匹配',USAGE_WINDOW_MISMATCH:'假设用量的统计周期不匹配',USAGE_CONTEXT_UNAVAILABLE:'假设用量上下文不可用',QUOTA_NOT_EXHAUSTED:'假设用量未达到限额',WEEKLY_QUOTA_EXHAUSTED:'假设用量已达到限额'};
  function clearDetail(){comparison.reset(); $('material-detail').replaceChildren();$('material-detail').hidden=true; }
  function controls(){panel.hidden=!available || !source;for(const el of [$('materials-load'),$('materials-more'),...$('materials-list').querySelectorAll('button')])el.disabled=locked || !source;$('materials-more').hidden=next===null || items.length>=128;}
  function reset(view=null){revision++;source=view;items=[];next=null;$('materials-list').replaceChildren();$('materials-note').textContent='';$('materials-source').textContent=view?`${view.source.metadata.name} · 版本 ${view.source.metadata.version}`:'';clearDetail();controls();}
  const selection=()=>({source:{name:source.source.metadata.name,version:source.source.metadata.version},expected_source_fingerprint:source.fingerprint});
  async function load(more){
    if(!source)return;
    const current=revision,input={...selection(),limit:8,...(more?{before:next}:{})};
    if(!more){items=[];next=null;$('materials-list').replaceChildren();clearDetail();}
    const page=await request('materials/query',input);
    if(current!==revision)return;
    items=[...new Map([...items,...page.items].map(item=>[item.id,item])).values()].slice(0,128);next=page.next_cursor;
    $('materials-list').replaceChildren(...items.map(item=>{
      const button=text('button','','material-item secondary');button.type='button';button.dataset.materialId=item.id;
      button.append(text('strong',`${formatDate(item.created_at)} · ${item.scenarios} 个案例`),text('span',`来源版本 ${item.source.version} · 查看保存结果`));
      button.addEventListener('click',()=>run(()=>open(item),'正在读取案例材料…'));return button;
    }));
    $('materials-note').textContent=items.length?`已加载 ${items.length} 份材料。${items.length>=128 && next?'已达到本次浏览上限，可刷新目录查看最新材料。':'点击一项核对其原始假设和结果。'}`:'这个版本尚无保存的案例材料；这不表示已经通过审核。';controls();
  }
  function pairs(values){const dl=text('dl','','material-facts');for(const [name,value] of values){const row=text('div','');row.append(text('dt',name),text('dd',value));dl.append(row);}return dl;}
  function scenarioCard(input,result,rawSource=null){
    const card=text('section','','material-case'),head=text('div','','section-head');
    const decisions={WOULD_ALLOW:['假设可通过','pass'],WOULD_DENY:['假设被拒绝','deny'],NOT_EVALUATED:['假设未评估','unknown']};
    const [label,state]=decisions[result.decision];head.append(text('h4',input.name),text('span',label,`scenario-state ${state}`));card.append(head);
    card.append(pairs([['假设用户 / 节点',`${input.user_key} / ${input.node_id}`],['参考时刻',formatDate(input.as_of)],['预约时段',`${formatDate(input.request.start_at)} — ${formatDate(input.request.end_at)}`],['假设总并发',input.projected_concurrent_gpus===null?'未知':`${input.projected_concurrent_gpus} GPU`],['假设角色',input.assumed_roles.join(', ') || '未提供']]));
    const usage=input.usage,quality={complete:'手动假设完整',partial:'手动假设部分已知',unknown:'手动假设未知'};
    card.append(text('p',usage?`用量输入：${quality[usage.quality]}。${usage.amounts.length===0?'未列出模型用量；完整条件下的零值也只是手动假设。':''}`:'用量输入：未提供。','material-assumption'));
    const assumed=result.assumed_used_gpu_seconds;
    card.append(text('p',`假设加权用量：${assumed?`${assumed.numerator} / ${assumed.denominator} GPU 秒`:'未知'} · 规则额度：${result.quota_limit?quota(result.quota_limit):'未配置'}`,'muted'));
    const table=document.createElement('table'),body=document.createElement('tbody');
    result.results.forEach(rule=>{const row=document.createElement('tr'),reason=text('td',reasons[rule.reason]||rule.reason);row.append(text('th',rules[rule.rule_id]),text('td',{pass:'符合',deny:'超出',unknown:'未知'}[rule.state],`scenario-state ${rule.state}`),reason);body.append(row);});table.append(body);const scroll=text('div','','table-scroll');scroll.append(table);card.append(scroll);
    card.append(text('p',result.selected_role_override?`命中假设角色覆盖：${result.selected_role_override}`:'未命中假设角色覆盖。','muted'));
    const raw=text('details','','source');raw.append(text('summary','查看完整原始假设'),text('pre',rawSource || JSON.stringify(input,null,2)));card.append(raw);return card;
  }
  async function open(item){
    if(!source)return;const current=revision;clearDetail();
    const value=await request('materials/read',{...selection(),id:item.id,expected_fingerprint:item.fingerprint});
    if(current!==revision)return;
    const target=$('material-detail'),title=text('div','','section-head');title.append(text('h3','保存的案例结果'),text('span','固定材料 · 未批准','badge'));
    const context=text('p','以下是保存时的假设推演，未读取当前真实用量、权限、维护或预约冲突；不构成批准或发布。','material-assumption');
    const reason=text('p',value.reason,'material-reason'),saved=text('p',`保存于 ${formatDate(value.summary.created_at)} · ${value.summary.scenarios} 个案例`,'muted');
    const cases=text('div','','material-cases');value.scenarios.forEach((input,index)=>cases.append(scenarioCard(input,value.results[index],value.raw_scenarios?.[index])));
    const proof=text('details','','source');proof.append(text('summary','原实现引用与保存依据'),text('pre',`配置的实现引用：${value.summary.implementation_ref}\n此引用不是独立代码校验；页面未用当前实现重演。\n来源：${value.summary.source.name} v${value.summary.source.version}\n来源指纹：${value.summary.source_fingerprint}\n材料 ID：${value.summary.id}\n材料指纹：${value.summary.fingerprint}\nCore 命令：${value.command_id}\n保存者：${value.created_by}`));
    target.replaceChildren(title,saved,reason,context,cases,proof);target.hidden=false;comparison.open(value);
    for(const el of $('materials-list').children)el.setAttribute('aria-pressed',String(el.dataset.materialId===item.id));
  }
  $('materials-load').addEventListener('click',()=>run(()=>load(false),'正在读取案例材料目录…'));
  $('materials-more').addEventListener('click',()=>run(()=>load(true),'正在读取更多案例材料…'));
  return {caseCard:scenarioCard,async showSaved(id,fingerprint){await load(false);await open({id,fingerprint});},session(enabled){available=enabled===true;reset();},source:reset,reset(){reset();},disconnect(){available=false;reset();},setBusy(value){comparison.setBusy(value);locked=value;controls();}};
};
