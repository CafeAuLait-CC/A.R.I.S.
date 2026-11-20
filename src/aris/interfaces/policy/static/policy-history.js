"use strict";
window.ARISPolicyHistory = ({$,text,request,run,formatDate,quota,versions,renderDifference,onReview=()=>{},onClear=()=>{}}) => {
  let configured=[],source=null,windows=[],next=null,base=null,locked=false,implementation=null,reportInput=null;
  const panel=$('history-panel');
  let comparison=null;
  function clearResult(){ $('history-result').hidden=true;$('history-result').replaceChildren();$('history-evidence').hidden=true;$('history-evidence').replaceChildren();reportInput=null;$('history-report').disabled=true;comparison?.clear();onClear(); }
  function clearWindows(){windows=[];next=null;base=null;$('history-window').replaceChildren();$('history-note').textContent='';$('history-selected').textContent='';clearResult();controls();}
  function controls(){
    const usable=source?.source.spec.quota.scope==='per_node' && $('history-node').options.length>0;
    panel.hidden=configured.length===0 || source===null;
    $('history-form').hidden=!usable;
    $('history-unavailable').hidden=usable;
    for(const el of panel.querySelectorAll('input,select,button'))el.disabled=locked || !usable;
    $('history-window').disabled=locked || windows.length===0;
    $('history-assess').disabled=locked || windows.length===0;
    $('history-review-actions').hidden=!implementation;
    $('history-review').disabled=locked || !usable || windows.length===0 || !implementation;
    $('history-report').disabled=locked || !reportInput;
    $('history-more').hidden=next===null || windows.length>=128;
    comparison?.setBusy(locked || !usable,windows.length>0);
  }
  function showSource(view){
    source=view;clearWindows();
    const allowed=view?.source.spec.quota.scope==='per_node' ? configured.filter(id=>view.source.spec.quota.node_limits.some(node=>node.node_id===id)) : [];
    $('history-node').replaceChildren(...allowed.map(id=>new Option(id,id)));
    $('history-source').textContent=view?`${view.source.metadata.name} · 版本 ${view.source.metadata.version}`:'';
    $('history-unavailable').textContent=view?.source.spec.quota.scope==='global'?'该来源使用全局额度，需要全部节点证据；此入口只支持逐节点核对。':'该来源没有当前账号可用的已配置节点。';
    comparison?.source(view);controls();
  }
  function renderWindows(){
    const old=$('history-window').value;
    $('history-window').replaceChildren(...windows.map(item=>new Option(`${formatDate(item.start_at)} — ${formatDate(item.end_at)} · 保存 ${formatDate(item.created_at)}`,item.selection.snapshot_id)));
    if(windows.some(item=>item.selection.snapshot_id===old))$('history-window').value=old;
    $('history-note').textContent=windows.length?`已加载 ${windows.length} 个固定窗口。重复时间段可能是不同的保存版本，请核对保存时间。`:'此范围内尚无可选择的统计窗口；不能据此判断使用量为零。';
    controls();showSelection();
  }
  function showSelection(){const item=windows.find(item=>item.selection.snapshot_id===$('history-window').value);$('history-selected').textContent=item?`覆盖 ${formatDate(item.start_at)} — ${formatDate(item.end_at)} · 保存于 ${formatDate(item.created_at)}`:'';clearResult();}
  async function load(more){
    if(!source)return;
    if(!more){clearWindows();const end=new Date(),start=new Date(end.getTime()-Number($('history-range').value)*86400000);base={node_id:$('history-node').value,start_at:start.toISOString(),end_at:end.toISOString(),limit:8};}
    clearResult();
    const page=await request('history/windows',{...base,...(more?{before:next}:{})});
    windows=[...new Map([...windows,...page.items].map(item=>[item.selection.snapshot_id,item])).values()].slice(0,128);next=page.next_cursor;renderWindows();
  }
  function amount(value,scale){
    if(value===null)return '未知';
    const numerator=BigInt(value.numerator),denominator=BigInt(value.denominator)*scale;
    if(numerator===0n)return '0';
    const scaled=(numerator*1000n+denominator/2n)/denominator;
    if(scaled===0n)return '<0.001';
    return `${scaled/1000n}.${String(scaled%1000n).padStart(3,'0')}`.replace(/\.?0+$/,'');
  }
  function usage(value){if(value===null)return '未知';const hours=BigInt(value.numerator)>=BigInt(value.denominator)*3600n;return `${amount(value,hours?3600n:1n)} GPU ${hours?'小时':'秒'}`;}
  function exact(value){return value===null?'未知':`${value.numerator} / ${value.denominator} GPU 秒`;}
  function resultView(result){
    const target=$('history-result'),head=text('div','','history-result-head');
    const states={within_limit:'历史窗口内未达到限额',exhausted:'历史窗口内已达到限额',unlimited:'该历史规则不设限额',not_evaluated:'历史额度未评估'};
    head.append(text('h3',states[result.state]),text('span','固定历史核对','badge'));
    const reasons={USAGE_COVERAGE_INCOMPLETE:'采样来源存在未知或未覆盖区间，完整实际用量仍未知。',USAGE_METHOD_MISMATCH:'快照的计时或处理规则与此入口配置不同。',USAGE_WINDOW_MISMATCH:'该快照未从所选额度周期的起点连续覆盖；滚动周期还需要完整七天。',QUOTA_SCOPE_UNCONFIGURED:'此策略没有该节点的额度。',GLOBAL_QUOTA_REQUIRES_ALL_NODE_EVIDENCE:'全局额度需要全部节点的证据。'};
    const note=text('p',reasons[result.reason]||'只对该固定窗口核对；不表示当前仍有额度，不批准预约或激活策略。','history-context');
    const cards=text('div','','history-totals');
    const limit=result.limit?quota(result.limit):'未配置';
    const remaining=result.state==='unlimited'?'不限额':usage(result.remaining_gpu_seconds);
    for(const [label,value] of [['历史用量',usage(result.used_gpu_seconds)],['规则额度',limit],['历史剩余额度',remaining]]){const card=text('div','','history-total');card.append(text('span',label),text('strong',value));cards.append(card);}
    const times=text('dl','','history-times');
    for(const [label,value] of [['额度周期',`${formatDate(result.period.start_at)} — ${formatDate(result.period.end_at)}`],['采样截至',formatDate(result.sampled_until)],['快照读取于',formatDate(result.captured_at)],['快照保存于',formatDate(result.snapshot_created_at)]]){const row=text('div','');row.append(text('dt',label),text('dd',value));times.append(row);}
    const roles=text('p',result.selected_assumed_role?`采用假设角色 ${result.selected_assumed_role} 的覆盖规则。`:'采用基础规则；没有命中假设角色覆盖。','muted');
    const proof=text('details','','source');proof.append(text('summary','精确数值与固定来源'),text('pre',`用量：${exact(result.used_gpu_seconds)}\n剩余：${exact(result.remaining_gpu_seconds)}\n规则：${result.policy.name} v${result.policy.version}\n规则指纹：${result.policy_fingerprint}\n快照 ID：${result.selection.snapshot_id}\n快照指纹：${result.selection.expected_snapshot_fingerprint}\n历史证据：${result.evidence_fingerprint}\n原因：${result.reason}`));
    target.replaceChildren(head,note,cards,times,roles,proof);target.hidden=false;
  }
  function selectedInput(){
    const item=windows.find(item=>item.selection.snapshot_id===$('history-window').value);
    if(!item || !source)throw new Error('请先选择一个已保存的统计窗口。');
    const roles=$('history-roles').value.split(',').map(value=>value.trim()).filter(Boolean);
    if(roles.length>32 || new Set(roles).size!==roles.length)throw new Error('请填写不重复的假设角色，最多32项。');
    return {reference:{name:source.source.metadata.name,version:source.source.metadata.version},expected_fingerprint:source.fingerprint,node_id:$('history-node').value,snapshot_id:item.selection.snapshot_id,expected_snapshot_fingerprint:item.selection.expected_snapshot_fingerprint,period_at:source.source.spec.quota.window.kind==='calendar_week'?item.start_at:item.end_at,assumed_roles:roles};
  }
  function evidenceView(value){
    const target=$('history-evidence'),evidence=value.evidence,result=value.assessment;
    const coverage={complete:'该历史窗口覆盖完整',partial:'部分覆盖',unknown:'覆盖未知'};
    const heading=text('div','','history-result-head');heading.append(text('h3','原历史证据'),text('span',coverage[evidence.coverage],'badge'));
    const note=text('p','原窗口、处理方法与账本版本保持不变。角色为手动假设；当前依赖未评估，本次不批准或发布策略。','history-context');
    const facts=text('dl','','history-times');
    for(const [label,content] of [['原始来源截止序号',evidence.source_ceiling],['账本版本',evidence.ledger_revision],['来源前缀完整',evidence.source_prefix_complete?'是':'否'],['历史身份修订',evidence.identity_revision ?? '未知'],['计量起算',formatDate(evidence.recording_start_at)],['审核运行实现引用',value.implementation_ref]]){const row=text('div','');row.append(text('dt',label),text('dd',content));facts.append(row);}
    const models=text('div','','history-models');models.append(text('h4','原型号驻留时长（未加权）'));
    if(evidence.models===null)models.append(text('p','完整实际用量未知，保留原观测缺口。','muted'));
    else if(!evidence.models.length)models.append(text('p','该完整历史窗口没有已计量用量。','muted'));
    else for(const item of evidence.models)models.append(text('p',`${item.model} · ${usage({numerator:item.observed_microseconds,denominator:'1000000'})}`));
    const proof=text('details','','source');proof.append(text('summary','精确原始上下文与方法指纹'),text('pre',`规则指纹：${result.policy_fingerprint}\n快照指纹：${result.selection.expected_snapshot_fingerprint}\n证据指纹：${result.evidence_fingerprint}\n账本指纹：${evidence.ledger_fingerprint ?? '空账本'}\n处理方法：${evidence.configuration_fingerprint}\n时序规则：${evidence.timing_fingerprint}\n\n${value.canonical_review_json}`));
    target.replaceChildren(heading,note,facts,models,proof);target.hidden=false;
  }
  $('history-load').addEventListener('click',()=>run(()=>load(false),'正在读取已保存统计窗口…'));
  $('history-more').addEventListener('click',()=>run(()=>load(true),'正在读取更多固定窗口…'));
  $('history-form').addEventListener('submit',event=>{
    event.preventDefault();
    run(async()=>{
      clearResult();
      const result=await request('history/assess',selectedInput());
      resultView(result);
    },'正在核对固定历史额度…');
  });
  comparison=window.ARISPolicyHistoryCompare({$,text,request,run,versions,input:selectedInput,usage,quota,formatDate,renderDifference});
  $('history-window').addEventListener('change',showSelection);
  for(const id of ['history-node','history-range'])$(id).addEventListener('change',clearWindows);
  $('history-roles').addEventListener('input',clearResult);
  $('history-review').addEventListener('click',()=>run(async()=>{
    if(!implementation)return;clearResult();const input={...selectedInput(),expected_implementation_ref:implementation};
    const value=await request('history/review',input);
    if(value.review_context_only!==true || value.implementation_ref!==implementation)throw new Error('历史审核上下文与配置不一致，请重新登录。');
    resultView(value.assessment);evidenceView(value);reportInput=input;onReview(value,input);
  },'正在读取同一固定窗口的原历史证据…'));
  $('history-report').addEventListener('click',()=>run(async()=>{
    if(!reportInput)return;const input=reportInput,blob=await request('history/report',input,{download:true});
    if(input!==reportInput)return;const url=URL.createObjectURL(blob),link=document.createElement('a');
    try{link.href=url;link.download='aris-historical-policy-review.html';document.body.append(link);link.click();}finally{link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  },'正在重新核对权限与来源并导出报告…'));
  return {usage,session(nodes,reference,comparisonAvailable){configured=[...nodes];implementation=reference || null;comparison.session(comparisonAvailable,implementation);showSource(null);},source:showSource,reset(){showSource(null);},disconnect(){configured=[];implementation=null;$('history-roles').value='';$('history-selected').textContent='';showSource(null);},setBusy(value){locked=value;controls();}};
};
