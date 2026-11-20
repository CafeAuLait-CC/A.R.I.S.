"use strict";
window.ARISPolicyHistoryRecords = ({$,text,request,commands,run,onState,formatDate,formatUsage}) => {
  const terminal=new Set(['succeeded','failed','cancelled','expired']),dialog=$('history-record-confirmation');
  let info=null,source=null,current=null,intent=null,busy=false,items=[],next=null,selected=null;
  const held=()=>intent!==null && !terminal.has(intent.state),key=()=>crypto.randomUUID();
  const reference=()=>({name:source.source.metadata.name,version:source.source.metadata.version});
  const canonical=value=>JSON.stringify((function sorted(value){return Array.isArray(value)?value.map(sorted):value && typeof value==='object'?Object.fromEntries(Object.entries(value).sort(([a],[b])=>a.localeCompare(b)).map(([k,v])=>[k,sorted(v)])):value;})(value));
  function note(value,error=false){$('history-record-note').textContent=value;$('history-record-note').classList.toggle('error',error);}
  function controls(){
    const readable=info?.historical_review_records_available && source!==null,writable=readable && info?.historical_review_writing_available;
    $('history-record-panel').hidden=!readable;$('history-record-write').hidden=!writable;
    for(const id of ['history-record-reason','history-record-recover-id','history-record-recover'])$(id).disabled=busy || held();
    $('history-record-prepare').disabled=busy || held() || !current;
    $('history-record-current').textContent=current?`待保存：${current.input.node_id} · ${formatDate(current.view.evidence.window_start_at)} — ${formatDate(current.view.evidence.window_end_at)}`:'先读取上方原历史证据，再预览保存。';
    $('history-record-command').hidden=intent===null;
    $('history-record-command-id').textContent=intent?.id?`原命令 ID：${intent.id}`:intent?`尚未获得命令 ID；保留页面重试原请求。原 key：${intent.submitKey}`:'';
    $('history-record-review').hidden=intent?.state!=='awaiting_confirmation';
    $('history-record-status').hidden=!intent?.id;
    $('history-record-withdraw').hidden=!intent?.id || !['awaiting_confirmation','queued'].includes(intent.state);
    $('history-record-retry').hidden=!intent || !['submit_unknown','confirm_unknown','withdraw_unknown'].includes(intent.state);
    $('history-record-view').hidden=intent?.state!=='succeeded';
    for(const id of ['history-record-review','history-record-status','history-record-withdraw','history-record-retry','history-record-view'])$(id).disabled=busy;
    $('history-record-confirm').disabled=busy || !intent?.canConfirm || !['awaiting_confirmation','confirm_unknown'].includes(intent?.state);
    $('history-record-load').disabled=busy || held() || !$('history-node').value;
    $('history-record-more').hidden=next===null || items.length>=128;$('history-record-more').disabled=busy || held();
    $('history-record-export').disabled=busy || held() || !selected;
    for(const button of $('history-record-list').querySelectorAll('button'))button.disabled=busy || held();
  }
  function changed(){controls();onState();}
  function clearDirectory(){items=[];next=null;selected=null;$('history-record-list').replaceChildren();$('history-record-detail').hidden=true;$('history-record-original').replaceChildren();controls();}
  function reset(view=null){source=view;current=null;intent=null;clearDirectory();$('history-record-reason').value='';$('history-record-recover-id').value='';$('history-record-command-state').textContent='';$('history-record-confirm-preview').replaceChildren();$('history-record-confirm-expires').textContent='';if(dialog.open)dialog.close();note('');controls();}
  function isBound(parameters){const q=parameters?.query;return source && q?.policy?.name===reference().name && q.policy.version===reference().version && q.expected_policy_fingerprint===source.fingerprint && q.usage?.owner_id===info.actor.user_id;}
  function applyOutcome(outcome){
    if(!outcome || !['awaiting_confirmation','queued','succeeded','failed','cancelled','expired'].includes(outcome.state) || (intent.id && intent.id!==outcome.command_id))throw new Error('历史审核保存状态与原请求不一致。');
    if(outcome.state==='succeeded' && (outcome.result?.historical_only!==true || outcome.result?.approval_performed!==false || outcome.result?.publication_performed!==false || outcome.result?.context_fingerprint!==intent.parameters.expected_context_fingerprint || canonical(outcome.result?.source)!==canonical(intent.parameters.query.policy)))throw new Error('历史审核保存结果不符合原请求，请核对后端记录。');
    intent.id=outcome.command_id;intent.state=outcome.state;intent.outcome=outcome;intent.proof=outcome.state==='awaiting_confirmation'?outcome.proof:null;intent.canConfirm=false;
    const messages={awaiting_confirmation:'原保存申请已受理，尚未保存。请核对原意图后确认。',queued:'已确认，等待策略 Worker；请手动刷新状态。',succeeded:'原历史审核已保存；未批准、发布或更改预约规则。',cancelled:'申请已撤回，没有因此保存审核记录。',expired:'申请已过期，没有因此完成保存。',failed:`保存未完成：${outcome.code || 'UNAVAILABLE'}。`};
    $('history-record-command-state').textContent=messages[outcome.state];if(terminal.has(outcome.state) && dialog.open)dialog.close();note('');changed();
  }
  function validationInput(parameters){const q=parameters.query;return {reference:q.policy,expected_fingerprint:q.expected_policy_fingerprint,node_id:q.usage.node_id,snapshot_id:q.usage.snapshot_id,expected_snapshot_fingerprint:q.usage.expected_snapshot_fingerprint,period_at:q.period_at,assumed_roles:q.assumed_roles,expected_implementation_ref:parameters.expected_implementation_ref,expected_context_fingerprint:parameters.expected_context_fingerprint,reason:parameters.reason};}
  function preview(value){
    const p=value.request,q=p.query,e=value.context.evidence,a=value.context.assessment,target=$('history-record-confirm-preview');
    target.replaceChildren(text('h3',`${q.policy.name} · 版本 ${q.policy.version} · ${q.usage.node_id}`),text('p',`${formatDate(e.window_start_at)} — ${formatDate(e.window_end_at)}`),text('p',`原截止的历史用量：${formatUsage(a.used_gpu_seconds)}`),text('p',`角色仅为手动假设：${q.assumed_roles.join(', ') || '基础规则'}`),text('p',`保存原因：${p.reason}`),text('p','保存原结果不批准或发布策略，也不表示当前仍有额度。','history-context'));
    const proof=text('details','','source');proof.append(text('summary','完整原保存意图与上下文指纹'),text('pre',JSON.stringify(p,null,2)));target.append(proof);
  }
  async function review(show=false){
    const original=await commands(`/${intent.id}/review`,undefined,{method:'GET'});
    if(original.request.command!=='policy.record_historical_review' || !isBound(original.request.parameters))throw new Error('该原请求不属于当前来源或当前账号。请选择原来源后恢复。');
    intent.parameters=structuredClone(original.request.parameters);$('history-record-reason').value=intent.parameters.reason;applyOutcome(original.outcome);
    if(show && original.outcome.state==='awaiting_confirmation'){
      const value=await request('history/records/validate',validationInput(intent.parameters));
      if(canonical(value.request)!==canonical(intent.parameters))throw new Error('重新校验与原保存意图不同，不能确认。');
      preview(value);intent.canConfirm=true;$('history-record-confirm-expires').textContent=`确认有效期至 ${formatDate(original.outcome.expires_at)}`;if(!dialog.open)dialog.showModal();changed();
    }
  }
  async function submit(){
    const original=intent;original.state='submit_unknown';changed();
    try{const accepted=await commands('',{command:'policy.record_historical_review',parameters:original.parameters},{key:original.submitKey});applyOutcome(accepted.outcome);await review(true);}
    catch(error){if(!error.stale && intent===original && !original.id)note('提交结果尚未核实。保留原参数和原 key，重试原请求；已有命令 ID 时可记录后恢复。',true);throw error;}finally{if(intent===original)changed();}
  }
  async function confirm(){
    if(!intent?.canConfirm)return;const original=intent;original.confirmKey ||= key();original.state='confirm_unknown';changed();
    try{const accepted=await commands(`/${original.id}/confirm`,{proof:original.proof},{key:original.confirmKey});applyOutcome(accepted.outcome);if(dialog.open)dialog.close();}
    catch(error){if(!error.stale && intent===original)note('确认结果尚未核实，可刷新状态或重试原确认。',true);throw error;}finally{if(intent===original)changed();}
  }
  async function withdraw(){
    const original=intent;original.withdrawKey ||= key();original.state='withdraw_unknown';changed();
    try{const accepted=await commands(`/${original.id}/withdraw`,{},{key:original.withdrawKey});applyOutcome(accepted.outcome);}
    catch(error){if(!error.stale && intent===original)note('撤回结果尚未核实，可刷新状态或重试原撤回。',true);throw error;}finally{if(intent===original)changed();}
  }
  function recordInput(item){return {source:item.source,expected_source_fingerprint:item.source_fingerprint,node_id:item.node_id,id:item.id,expected_fingerprint:item.fingerprint};}
  async function showRecord(input){
    const value=await request('history/records/detail',input),a=value.context.assessment,e=value.context.evidence;
    selected=input;const target=$('history-record-original'),states={within_limit:'原窗口未达到限额',exhausted:'原窗口已达到限额',unlimited:'原规则不限额',not_evaluated:'原历史额度未评估'};
    target.replaceChildren(text('h4',`原审核 · 保存于 ${formatDate(value.created_at)}`),text('p',`节点 ${a.selection.node_id} · ${formatDate(e.window_start_at)} — ${formatDate(e.window_end_at)}`),text('p',`${states[a.state]} · 历史用量 ${formatUsage(a.used_gpu_seconds)}`),text('p',`原保存原因：${value.reason}`),text('p','以下为保存时的原结果；未重新解释为当前额度或批准。','muted'));
    const proof=text('details','','source');proof.append(text('summary','原记录、上下文与精确数值'),text('pre',value.canonical_record_json));target.append(proof);$('history-record-detail').hidden=false;controls();
  }
  function renderList(){
    $('history-record-list').replaceChildren(...items.map(item=>{const button=text('button',`${formatDate(item.created_at)} · ${item.node_id}`,'secondary');button.type='button';button.append(text('small',`窗口 ${item.snapshot_id.slice(0,8)} · 原实现 ${item.implementation_ref}`));button.addEventListener('click',()=>run(()=>showRecord(recordInput(item)),'正在读取原历史审核…'));return button;}));controls();
  }
  async function load(more=false){
    if(!source || !$('history-node').value)return;if(!more)clearDirectory();
    const page=await request('history/records/query',{source:reference(),expected_source_fingerprint:source.fingerprint,node_id:$('history-node').value,limit:8,...(more?{before:next}:{})});
    items=[...new Map([...items,...page.items].map(item=>[item.id,item])).values()].slice(0,128);next=page.next_cursor;renderList();note(items.length?`已加载 ${items.length} 条本人原审核记录。`:'此来源和节点下尚无本人保存的审核；没有记录不代表用量为零。');
  }
  $('history-record-form').addEventListener('submit',event=>{event.preventDefault();run(async()=>{
    if(!current || held())return;const value=await request('history/records/validate',{...current.input,expected_context_fingerprint:current.view.context_fingerprint,reason:$('history-record-reason').value.trim()});
    intent={id:null,state:'submit_unknown',parameters:structuredClone(value.request),submitKey:key(),proof:null,canConfirm:false};await submit();
  },'正在重新核对原上下文并申请保存…');});
  $('history-record-review').addEventListener('click',()=>run(()=>review(true),'正在核对原保存意图…'));
  $('history-record-status').addEventListener('click',()=>run(()=>review(false),'正在读取原保存状态…'));
  $('history-record-withdraw').addEventListener('click',()=>run(withdraw,'正在撤回原保存申请…'));
  $('history-record-retry').addEventListener('click',()=>run(()=>intent.state==='submit_unknown'?submit():intent.state==='confirm_unknown'?confirm():withdraw(),'正在重试同一保存请求…'));
  $('history-record-confirm').addEventListener('click',()=>run(confirm,'正在确认原历史审核保存…'));
  $('history-record-confirm-close').addEventListener('click',()=>dialog.close());
  $('history-record-view').addEventListener('click',()=>run(async()=>{if(intent?.state!=='succeeded')return;const p=intent.parameters,r=intent.outcome.result;await load();await showRecord({source:p.query.policy,expected_source_fingerprint:p.query.expected_policy_fingerprint,node_id:p.query.usage.node_id,id:r.id,expected_fingerprint:r.fingerprint});},'正在读取已保存原结果…'));
  $('history-record-load').addEventListener('click',()=>run(()=>load(),'正在读取本人已保存审核…'));
  $('history-record-more').addEventListener('click',()=>run(()=>load(true),'正在读取更多原审核…'));
  $('history-record-export').addEventListener('click',()=>run(async()=>{if(!selected)return;const input=selected,blob=await request('history/records/report',input,{download:true});if(input!==selected)return;const url=URL.createObjectURL(blob),link=document.createElement('a');try{link.href=url;link.download='aris-saved-historical-review.html';document.body.append(link);link.click();}finally{link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}},'正在重新鉴权并导出原审核报告…'));
  $('history-record-recover-form').addEventListener('submit',event=>{event.preventDefault();run(async()=>{
    const id=$('history-record-recover-id').value.trim();if(!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(id))throw new Error('请填写完整的历史审核保存命令 ID。');
    const original={id,state:'recovering',parameters:null,proof:null,canConfirm:false};intent=original;changed();try{await review(true);}catch(error){if(intent===original && original.state==='recovering'){intent=null;changed();}throw error;}
  },'正在恢复原审核保存请求…');});
  $('history-node').addEventListener('change',()=>{if(!held())clearDirectory();});
  return {session(value){info=value;reset();},source:reset,reset(){reset();},disconnect(){info=null;reset();},holdsNavigation:held,setBusy(value){busy=value;controls();},clearCurrent(){if(!held()){current=null;controls();}},current(value,input){if(held())return;current={view:structuredClone(value),input:structuredClone(input)};intent=null;note('已读取固定证据。可以预览保存原审核上下文。');changed();}};
};
