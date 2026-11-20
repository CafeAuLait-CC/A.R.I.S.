"use strict";
window.ARISPolicyApprovals = ({$,text,request,commands,run,onState,formatDate,duration}) => {
  const terminal=new Set(['succeeded','failed','cancelled','expired']),dialog=$('approval-confirmation');
  let info=null,source=null,intent=null,busy=false,items=[],next=null,selections=new Map(),selected=null;
  const held=()=>intent!==null && !terminal.has(intent.state),key=()=>crypto.randomUUID();
  const canonical=value=>JSON.stringify((function sorted(v){return Array.isArray(v)?v.map(sorted):v && typeof v==='object'?Object.fromEntries(Object.entries(v).sort(([a],[b])=>a.localeCompare(b)).map(([k,x])=>[k,sorted(x)])):v;})(value));
  const reference=()=>({name:source.source.metadata.name,version:source.source.metadata.version});
  const nodes=()=>source?.source.spec.quota.scope==='per_node'?source.source.spec.quota.node_limits.map(n=>n.node_id).sort():[];
  const scope=()=>({source:reference(),expected_source_fingerprint:source.fingerprint});
  function note(value,error=false){$('approval-note').textContent=value;$('approval-note').classList.toggle('error',error);}
  function controls(){
    const readable=!!info?.source_approval_requirements && source!==null,writable=readable && info?.source_approval_writing_available;
    $('approval-panel').hidden=!readable;$('approval-write').hidden=!writable;
    for(const id of ['approval-reason','approval-recover-id','approval-recover','approval-load-evidence','approval-load'])$(id).disabled=busy || held();
    for(const {select} of selections.values())select.disabled=busy || held();
    $('approval-revoke-form').hidden=!selected || !writable || selected.state==='revoked';
    $('approval-revoke-reason').disabled=busy || held();$('approval-revoke-prepare').disabled=busy || held() || !selected || selected.state==='revoked';
    $('approval-prepare').disabled=busy || held() || !nodes().length || nodes().some(node=>!selections.get(node)?.select.value);
    $('approval-command').hidden=intent===null;
    $('approval-command-id').textContent=intent?.id?`原批准命令 ID：${intent.id}`:intent?`尚未获得命令 ID；保留页面重试。原 key：${intent.submitKey}`:'';
    $('approval-review').hidden=intent?.state!=='awaiting_confirmation';$('approval-status').hidden=!intent?.id;
    $('approval-withdraw').hidden=!intent?.id || !['awaiting_confirmation','queued'].includes(intent.state);
    $('approval-retry').hidden=!intent || !['submit_unknown','confirm_unknown','withdraw_unknown'].includes(intent.state);
    $('approval-view').hidden=intent?.state!=='succeeded';
    for(const id of ['approval-review','approval-status','approval-withdraw','approval-retry','approval-view'])$(id).disabled=busy;
    $('approval-confirm').disabled=busy || !intent?.canConfirm || !['awaiting_confirmation','confirm_unknown'].includes(intent?.state);
    $('approval-more').hidden=next===null || items.length>=128;$('approval-more').disabled=busy || held();
    for(const button of $('approval-list').querySelectorAll('button'))button.disabled=busy || held();
  }
  function changed(){controls();onState();}
  function requirements(){
    const r=info?.source_approval_requirements,target=$('approval-requirements');target.replaceChildren();if(!r)return;
    target.append(text('p',`材料：每个来源节点一份本人完整历史审核，至少 ${duration(r.minimum_window_seconds)}，距窗口末尾不超过 ${duration(r.maximum_history_age_seconds)}。`),text('p',`源作者自审：${r.allow_source_author_approval?'允许':'不允许'} · 批准有效期 ${duration(r.validity_seconds)}。`),text('p',r.require_evaluated_quota?'历史周期额度必须已经可评估。':'此配置允许历史周期额度未知；个人批准不会补全未知用量。'),text('p','当前入口仅支持按节点额度、无角色覆盖、关闭自动空闲回收的来源。','muted'));
  }
  function clearRecords(){items=[];next=null;selected=null;$('approval-revoke-form').hidden=true;$('approval-revoke-reason').value='';$('approval-list').replaceChildren();$('approval-detail').hidden=true;$('approval-detail').replaceChildren();}
  function reset(view=null){source=view;intent=null;selections=new Map();clearRecords();$('approval-samples').replaceChildren();$('approval-reason').value='';$('approval-recover-id').value='';$('approval-command-state').textContent='';$('approval-confirm-preview').replaceChildren();$('approval-confirm-expires').textContent='';if(dialog.open)dialog.close();requirements();note('');controls();}
  function bound(parameters){return source && parameters?.source?.name===reference().name && parameters.source.version===reference().version && parameters.expected_source_fingerprint===source.fingerprint;}
  function outcome(value){
    if(!value || !['awaiting_confirmation','queued','succeeded','failed','cancelled','expired'].includes(value.state) || (intent.id && intent.id!==value.command_id))throw new Error('批准状态与原请求不一致。');
    const revoking=intent.command==='policy.revoke_source_approval';
    if(value.state==='succeeded' && (canonical(value.result?.source)!==canonical(intent.parameters.source) || (revoking ? value.result?.revocation_only!==true || value.result?.publication_performed!==false || value.result?.approval_id!==intent.parameters.approval_id : value.result?.individual_source_approval_only!==true || value.result?.publication_authorized!==false || value.result?.activation_performed!==false)))throw new Error('审核操作结果不符合原请求，请核对后端记录。');
    intent.id=value.command_id;intent.state=value.state;intent.outcome=value;intent.proof=value.state==='awaiting_confirmation'?value.proof:null;intent.canConfirm=false;
    const messages={awaiting_confirmation:'原批准申请已受理，尚未批准；请核对原意图。',queued:'已确认，等待策略 Worker；请刷新批准状态。',succeeded:revoking?(value.result?.created?'已追加撤销记录，原批准保留；不会撤回既有发布或停止程序。':'该批准此前已撤销；保留首份撤销记录及原原因。'):'原个人批准已记录；当前有效状态请查看批准记录，策略尚未发布或生效。',cancelled:'原批准申请已撤回，未因此新增批准记录。',expired:'原批准申请已过期。',failed:`批准未完成：${value.code || 'UNAVAILABLE'}。`};
    $('approval-command-state').textContent=messages[value.state];if(terminal.has(value.state) && dialog.open)dialog.close();note('');changed();
  }
  function renderPreview(value){
    if(value.revocation_only){
      const target=$('approval-confirm-preview'),p=value.request;
      $('approval-confirm-title').textContent='核对撤销原批准';$('approval-confirm').textContent='确认撤销此批准';
      target.replaceChildren(text('h3',`${p.source.name} · 版本 ${p.source.version}`),text('p',`原批准 ${p.approval_id} · 批准于 ${formatDate(value.approved_at)}`),text('p',`撤销原因：${p.reason}`),text('p',value.existing_revocation_id?'这份批准此前已撤销；确认会沿用首份原记录，不改写原原因。':'追加撤销记录，保留原批准与审核材料；不会撤回既有发布、修改预约或停止程序。','history-context'));
      const original=text('details','','source');original.append(text('summary','完整原撤销意图'),text('pre',JSON.stringify(p,null,2)));target.append(original);return;
    }
    $('approval-confirm-title').textContent='核对个人批准意图';$('approval-confirm').textContent='确认批准此来源';
    const p=value.request,r=value.requirements,target=$('approval-confirm-preview');target.replaceChildren(text('h3',`${p.source.name} · 版本 ${p.source.version}`),text('p',`批准原因：${p.reason}`),text('p',`有效期 ${duration(r.validity_seconds)} · ${r.allow_source_author_approval?'允许源作者自审':'源作者不可自审'}`));
    const states={within_limit:'原周期未达到限额',exhausted:'原周期已达到限额',unlimited:'原规则不限额',not_evaluated:'原历史周期额度未知'};
    for(const sample of value.samples)target.append(text('p',`${sample.reference.node_id}：${formatDate(sample.window_start_at)} — ${formatDate(sample.window_end_at)} · ${states[sample.quota_state]}`));
    target.append(text('p','这只记录你的个人来源批准；不会发布、激活、扣额或改变现有预约。','history-context'));
    const details=text('details','','source');details.append(text('summary','完整原批准意图与要求'),text('pre',JSON.stringify(value,null,2)));target.append(details);
  }
  async function review(show=false){
    const value=await commands(`/${intent.id}/review`,undefined,{method:'GET'});
    if(!['policy.approve_source','policy.revoke_source_approval'].includes(value.request.command) || !bound(value.request.parameters))throw new Error('原批准申请不属于当前来源，请选择原版本后恢复。');
    intent.command=value.request.command;intent.parameters=structuredClone(value.request.parameters);$('approval-reason').value=intent.parameters.reason;outcome(value.outcome);
    if(show && value.outcome.state==='awaiting_confirmation'){
      const validated=await request(intent.command==='policy.revoke_source_approval'?'approvals/revoke/validate':'approvals/validate',intent.parameters);
      if(canonical(validated.request)!==canonical(intent.parameters))throw new Error('当前校验与原批准意图不同，不能确认。');
      renderPreview(validated);intent.canConfirm=true;$('approval-confirm-expires').textContent=`确认有效期至 ${formatDate(value.outcome.expires_at)}`;if(!dialog.open)dialog.showModal();changed();
    }
  }
  async function submit(){
    const original=intent;original.state='submit_unknown';changed();
    try{const value=await commands('',{command:original.command,parameters:original.parameters},{key:original.submitKey});outcome(value.outcome);await review(true);}
    catch(error){if(!error.stale && intent===original && !original.id)note('提交结果尚未核实；保留原参数和 key，重试同一请求。',true);throw error;}finally{if(intent===original)changed();}
  }
  async function confirm(){
    if(!intent?.canConfirm)return;const original=intent;original.confirmKey ||= key();original.state='confirm_unknown';changed();
    try{const value=await commands(`/${original.id}/confirm`,{proof:original.proof},{key:original.confirmKey});outcome(value.outcome);if(dialog.open)dialog.close();}
    catch(error){if(!error.stale && intent===original)note('确认结果尚未核实；可以刷新状态或重试原确认。',true);throw error;}finally{if(intent===original)changed();}
  }
  async function withdraw(){
    const original=intent;original.withdrawKey ||= key();original.state='withdraw_unknown';changed();
    try{const value=await commands(`/${original.id}/withdraw`,{},{key:original.withdrawKey});outcome(value.outcome);}
    catch(error){if(!error.stale && intent===original)note('撤回结果尚未核实；可以刷新状态或重试原撤回。',true);throw error;}finally{if(intent===original)changed();}
  }
  async function loadEvidence(){
    selections=new Map();$('approval-samples').replaceChildren();
    if(!nodes().length){note('这个来源不是按节点额度，当前批准入口不支持。',true);controls();return;}
    for(const node of nodes()){
      const page=await request('history/records/query',{...scope(),node_id:node,limit:8});
      const label=text('label',`节点 ${node} 的原历史审核`),select=document.createElement('select');select.required=true;select.append(new Option('请选择已核对的原审核记录',''));
      for(const item of page.items)select.append(new Option(`${formatDate(item.created_at)} · ${item.id.slice(0,8)} · ${item.implementation_ref}`,item.id));
      const help=text('p',page.items.length?'只列出最近 8 条本人记录；需要其他材料时，可先重新保存原历史审核。':'尚无本人保存的审核，请先完成上方历史核对并保存原结果。','muted');
      select.addEventListener('change',()=>{if(!held()){intent=null;note('材料选择已更改，请重新核对批准意图。');changed();}});label.append(select,help);$('approval-samples').append(label);selections.set(node,{select,items:page.items});
    }
    note('已读取本人保存的原材料；选择后由服务器核对覆盖、方法和时效。');controls();
  }
  function detailInput(item){return {...scope(),id:item.id,expected_fingerprint:item.fingerprint};}
  async function showRecord(input){
    const view=await request('approvals/detail',input),value=view.record,p=value.preview.request,target=$('approval-detail');selected=view;
    const states={recorded:'个人批准已记录',expired:'已到期',revoked:'已撤销'};
    target.replaceChildren(text('h3',`个人批准 · 版本 ${p.source.version} · ${states[view.state]}`),text('p',`批准于 ${formatDate(value.approved_at)} · 到期 ${formatDate(value.expires_at)}`),text('p',`原批准原因：${p.reason}`),text('p','原批准记录未改写；是否满足完整审批和发布条件需要另行核对。','muted'));
    if(!view.current_requirements_match)target.append(text('p','当前审核要求已变化；原批准只保留原要求，不自动满足新要求。','muted'));
    if(view.revocation)target.append(text('p',`撤销于 ${formatDate(view.revocation.revoked_at)} · 原撤销原因：${view.revocation.request.reason}`));
    for(const sample of value.preview.samples)target.append(text('p',`${sample.reference.node_id} · 原历史记录 ${sample.reference.id} · ${sample.quota_state==='not_evaluated'?'原历史周期额度未知':sample.quota_state}`));
    const original=text('details','','source');original.append(text('summary','原批准、撤销记录与指纹'),text('pre',JSON.stringify(view,null,2)));target.append(original);target.hidden=false;controls();
  }
  async function load(more=false){
    if(!more)clearRecords();const page=await request('approvals/query',{...scope(),limit:8,...(more?{before:next}:{})});items=[...new Map([...items,...page.items].map(item=>[item.id,item])).values()].slice(0,128);next=page.next_cursor;
    $('approval-list').replaceChildren(...items.map(item=>{const button=text('button',`${formatDate(item.approved_at)} · ${item.revoked_at?'已撤销':new Date(item.expires_at)<=new Date(page.as_of)?'已到期':'个人批准已记录'}`,'secondary');button.type='button';button.append(text('small',`到期 ${formatDate(item.expires_at)} · ${item.id.slice(0,8)}`));button.addEventListener('click',()=>run(()=>showRecord(detailInput(item)),'正在读取原批准记录…'));return button;}));
    note(items.length?`已加载 ${items.length} 条本人批准记录；此列表不代表策略已发布。`:'此来源尚无本人批准记录。');controls();
  }
  $('approval-form').addEventListener('submit',event=>{event.preventDefault();run(async()=>{
    if(held())return;const refs=nodes().map(node=>{const field=selections.get(node),item=field?.items.find(item=>item.id===field.select.value);if(!item)throw new Error('请为每个来源节点选择一份原历史审核。');return {node_id:node,id:item.id,expected_fingerprint:item.fingerprint};});
    const value=await request('approvals/validate',{...scope(),expected_requirements_fingerprint:info.source_approval_requirements_fingerprint,history:refs,reason:$('approval-reason').value.trim()});intent={id:null,command:'policy.approve_source',state:'submit_unknown',parameters:structuredClone(value.request),submitKey:key(),proof:null,canConfirm:false};await submit();
  },'正在核对原材料并申请个人批准…');});
  $('approval-revoke-form').addEventListener('submit',event=>{event.preventDefault();run(async()=>{
    if(held() || !selected || selected.state==='revoked')return;
    const original=selected.record,value=await request('approvals/revoke/validate',{...scope(),approval_id:original.id,expected_approval_fingerprint:original.fingerprint,reason:$('approval-revoke-reason').value.trim()});
    intent={id:null,command:'policy.revoke_source_approval',state:'submit_unknown',parameters:structuredClone(value.request),submitKey:key(),proof:null,canConfirm:false};await submit();
  },'正在核对原批准并申请撤销…');});
  $('approval-load-evidence').addEventListener('click',()=>run(loadEvidence,'正在读取本人原历史审核…'));
  $('approval-load').addEventListener('click',()=>run(()=>load(),'正在读取本人批准记录…'));
  $('approval-more').addEventListener('click',()=>run(()=>load(true),'正在读取更多批准记录…'));
  $('approval-review').addEventListener('click',()=>run(()=>review(true),'正在核对原批准意图…'));
  $('approval-status').addEventListener('click',()=>run(()=>review(false),'正在读取原批准状态…'));
  $('approval-withdraw').addEventListener('click',()=>run(withdraw,'正在撤回原批准申请…'));
  $('approval-retry').addEventListener('click',()=>run(()=>intent.state==='submit_unknown'?submit():intent.state==='confirm_unknown'?confirm():withdraw(),'正在重试同一批准请求…'));
  $('approval-confirm').addEventListener('click',()=>run(confirm,'正在确认个人来源批准…'));
  $('approval-confirm-close').addEventListener('click',()=>dialog.close());
  $('approval-view').addEventListener('click',()=>run(async()=>{if(intent?.state!=='succeeded')return;await load();await showRecord({...scope(),id:intent.command==='policy.revoke_source_approval'?intent.parameters.approval_id:intent.outcome.result.id,expected_fingerprint:intent.command==='policy.revoke_source_approval'?intent.parameters.expected_approval_fingerprint:intent.outcome.result.fingerprint});},'正在读取已记录批准…'));
  $('approval-recover-form').addEventListener('submit',event=>{event.preventDefault();run(async()=>{
    const id=$('approval-recover-id').value.trim();if(!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(id))throw new Error('请填写完整的原批准命令 ID。');
    const original={id,state:'recovering',parameters:null,proof:null,canConfirm:false};intent=original;changed();try{await review(true);}catch(error){if(intent===original && original.state==='recovering'){intent=null;changed();}throw error;}
  },'正在恢复原批准申请…');});
  return {session(value){info=value;reset();},source:reset,reset(){reset();},disconnect(){info=null;reset();},holdsNavigation:held,setBusy(value){busy=value;controls();}};
};
