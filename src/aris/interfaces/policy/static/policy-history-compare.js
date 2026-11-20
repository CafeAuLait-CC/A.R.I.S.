"use strict";
window.ARISPolicyHistoryCompare=({$,text,request,run,versions,input,usage,quota,formatDate,renderDifference})=>{
  let enabled=false,implementation=null,source=null,locked=false,hasWindow=false,result=null,exportInput=null;
  const panel=$('history-comparison-panel');
  function clear(){result=null;exportInput=null;$('history-comparison-result').hidden=true;$('history-comparison-result').replaceChildren();$('history-comparison-note').textContent='';$('history-comparison-export').disabled=true;}
  function choices(){
    const current=$('history-comparison-target').value;
    const allowed=source?versions().filter(item=>item.reference.name===source.source.metadata.name && item.reference.version!==source.source.metadata.version):[];
    $('history-comparison-target').replaceChildren(...allowed.map(item=>new Option(`版本 ${item.reference.version}`,String(item.reference.version))));
    if(allowed.some(item=>String(item.reference.version)===current))$('history-comparison-target').value=current;
    return allowed;
  }
  function controls(){
    const allowed=choices();panel.hidden=!enabled || !source;
    $('history-comparison-target').disabled=locked || allowed.length===0;
    $('history-comparison-load').disabled=locked || !hasWindow || allowed.length===0;
    $('history-comparison-export').disabled=locked || !exportInput;
    $('history-comparison-empty').hidden=allowed.length>0;
  }
  function column(value,label){
    const card=text('section','','history-comparison-card');
    card.append(text('h4',`${label} · 版本 ${value.policy.version}`));
    const states={within_limit:'历史窗口内未达到限额',exhausted:'历史窗口内已达到限额',unlimited:'此历史规则不限额',not_evaluated:'历史额度未评估'};
    card.append(text('p',states[value.state],'history-comparison-state'));
    const facts=text('dl','','history-times');
    for(const [name,content] of [['按规则折算用量',usage(value.used_gpu_seconds)],['规则额度',value.limit?quota(value.limit):'未配置'],['历史剩余',value.state==='unlimited'?'不限额':usage(value.remaining_gpu_seconds)],['额度周期',`${formatDate(value.period.start_at)} — ${formatDate(value.period.end_at)}`],['采用的假设角色',value.selected_assumed_role || '基础规则'],['原因码',value.reason]]){
      const row=text('div','');row.append(text('dt',name),text('dd',content));facts.append(row);
    }
    card.append(facts);return card;
  }
  function show(value){
    const target=$('history-comparison-result'),pair=text('div','','history-comparison-pair');
    pair.append(column(value.before,'基准'),column(value.after,'对照'));
    const note=text('p',value.quota_state_changed===null?'至少一侧历史额度未知，不判断状态是否改变。':value.quota_state_changed?'两侧历史额度状态不同。':'两侧历史额度状态相同；用量和规则差异请分别核对。','history-context');
    const evidence=text('p',`同一原窗口 ${formatDate(value.evidence.window_start_at)} — ${formatDate(value.evidence.window_end_at)} · 来源截止 ${value.evidence.source_ceiling} · 账本修订 ${value.evidence.ledger_revision}`,'muted');
    const changes=text('div','','table-scroll');changes.id='history-comparison-changes';
    const changeNote=text('p','','muted');changeNote.id='history-comparison-change-note';
    const original=text('details','','source');original.append(text('summary','精确数值与完整原始对照'),text('pre',value.canonical_comparison_json));
    target.replaceChildren(note,evidence,pair,text('h4','规则差异'),changeNote,changes,original);target.hidden=false;
    renderDifference(value.difference,changes.id,changeNote.id);
    $('history-comparison-note').textContent='同一份原历史用于两侧计算；角色仍是手动假设，当前配额和发布状态未评估。';
  }
  $('history-comparison-target').addEventListener('change',()=>{clear();controls();});
  $('history-comparison-load').addEventListener('click',()=>run(async()=>{
    clear();const selected=versions().find(item=>String(item.reference.version)===$('history-comparison-target').value && item.reference.name===source?.source.metadata.name);
    if(!selected || !implementation)throw new Error('请先加载并选择另一个策略版本。');
    const query={...input(),expected_implementation_ref:implementation,target:selected.reference,expected_target_fingerprint:selected.fingerprint};
    const value=await request('history/compare',query);
    if(value.historical_only!==true || value.implementation_ref!==implementation || value.before.policy_fingerprint!==query.expected_fingerprint || value.after.policy_fingerprint!==query.expected_target_fingerprint)throw new Error('历史对照与选择的来源不一致，请刷新。');
    result=value;exportInput={...query,expected_context_fingerprint:value.context_fingerprint};show(result);
  },'正在用同一份原历史对照两个版本…'));
  $('history-comparison-export').addEventListener('click',()=>run(async()=>{
    if(!exportInput)return;const query=exportInput;let blob;
    try{blob=await request('history/compare/report',query,{download:true});}catch(error){clear();controls();throw error;}
    if(query!==exportInput)return;const url=URL.createObjectURL(blob),link=document.createElement('a');
    try{link.href=url;link.download='aris-historical-comparison.html';document.body.append(link);link.click();}finally{link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  },'正在重新核对同一历史对照并导出报告…'));
  return {clear,session(available,reference){enabled=available===true;implementation=reference || null;clear();controls();},source(value){source=value;clear();controls();},setBusy(value,windowAvailable){locked=value;hasWindow=windowAvailable;controls();}};
};
