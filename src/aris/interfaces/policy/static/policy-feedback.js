"use strict";
window.ARISPolicyFeedback = ({$,text,request,commands,run,onState,showMaterial,formatDate}) => {
  const terminal = new Set(['succeeded','failed','cancelled','expired']);
  const dialog = $('feedback-confirmation');
  let config = {read:false,write:false,materials:false}, source = null, busy = false;
  let items = [], next = null, materialNext = null, materialOptions = new Map();
  let validated = null, intent = null;
  const held = () => intent !== null && !terminal.has(intent.state);
  const reference = () => ({name:source.source.metadata.name,version:source.source.metadata.version});
  const sameSource = p => source && p.source.name === reference().name && p.source.version === reference().version && p.expected_source_fingerprint === source.fingerprint;
  const tag = p => JSON.stringify([p.source.name,p.source.version,p.expected_source_fingerprint,p.material?.id || null,p.material?.fingerprint || null,p.kind,p.body,p.reason]);
  const uuid = () => crypto.randomUUID();
  function note(value,error=false) {
    $('feedback-note').textContent=value;
    $('feedback-note').classList.toggle('error',error);
  }
  function controls() {
    $('feedback-panel').hidden=!config.read || source===null;
    const locked=busy || held();
    $('feedback-load').disabled=busy;
    $('feedback-more').hidden=next===null;
    $('feedback-more').disabled=busy;
    $('feedback-form').hidden=!config.write;
    for(const field of $('feedback-form').querySelectorAll('input,textarea,select,button'))field.disabled=locked;
    $('feedback-material-load').hidden=!config.materials;
    $('feedback-material-more').hidden=!config.materials || materialNext===null;
    $('feedback-start').hidden=validated===null || intent!==null;
    $('feedback-start').disabled=busy;
    $('feedback-recover-tools').hidden=!config.write;
    $('feedback-recover-id').disabled=$('feedback-recover').disabled=locked;
    $('feedback-command').hidden=intent===null;
    $('feedback-command-id').textContent=intent?.id || '尚未获得 ID，请保留页面并重试原请求。';
    $('feedback-command-review').hidden=intent?.state!=='awaiting_confirmation';
    $('feedback-command-status').hidden=!intent?.id;
    $('feedback-command-withdraw').hidden=!intent?.id || !['awaiting_confirmation','queued'].includes(intent.state);
    $('feedback-command-retry').hidden=!intent?.retry;
    for(const id of ['feedback-command-review','feedback-command-status','feedback-command-withdraw','feedback-command-retry'])$(id).disabled=busy;
    $('feedback-confirm').disabled=busy || intent?.state!=='awaiting_confirmation';
    for(const button of $('feedback-list').querySelectorAll('button'))button.disabled=locked;
  }
  function changed(){controls();onState();}
  function clearPreview(){validated=null;$('feedback-preview').replaceChildren();$('feedback-preview').hidden=true;}
  function renderList() {
    $('feedback-list').replaceChildren(...items.map(value=>{
      const item=text('li','','feedback-entry'), head=text('div','','feedback-entry-head');
      head.append(text('strong',value.request.kind==='changes_requested'?'请求修改':'审核意见'),text('span',formatDate(value.created_at),'muted'));
      item.append(head,text('p',value.request.body,'feedback-body'),text('p','记录原因：'+value.request.reason,'muted'));
      if(value.request.material){
        const button=text('button','查看关联材料','secondary');button.type='button';
        button.addEventListener('click',()=>run(()=>showMaterial(value.request.material.id,value.request.material.fingerprint),'正在读取关联材料…'));
        item.append(button);
      }
      const evidence=text('details','','feedback-evidence');evidence.append(text('summary','记录依据'),text('pre',JSON.stringify({author_id:value.created_by,command_id:value.command_id,feedback_id:value.id,fingerprint:value.fingerprint},null,2)));
      item.append(evidence);return item;
    }));
    $('feedback-empty').hidden=items.length>0;
    $('feedback-empty').textContent='这个版本还没有人工意见。';
    controls();
  }
  async function load(more=false) {
    const page=await request('feedback',{source:reference(),expected_source_fingerprint:source.fingerprint,limit:8,before:more?next:null});
    if(!sameSource(page.query))throw new Error('意见列表与当前来源不同，请重新读取。');
    items=more?[...items,...page.items]:page.items;next=page.next_cursor;renderList();
    note(items.length?'已读取原始人工意见。':'当前来源还没有人工意见。');
  }
  async function loadMaterials(more=false) {
    const page=await request('materials/query',{source:reference(),expected_source_fingerprint:source.fingerprint,limit:16,before:more?materialNext:null});
    if(!sameSource(page.query))throw new Error('材料列表与当前来源不同。');
    if(!more)materialOptions.clear();
    for(const item of page.items)materialOptions.set(item.id,item);
    const selected=$('feedback-material').value;
    $('feedback-material').replaceChildren(new Option('仅对策略来源发表评论',''),...[...materialOptions].map(([id,item])=>new Option(`${formatDate(item.created_at)} · ${item.scenarios} 个案例 · ${id.slice(0,8)}`,id)));
    if(materialOptions.has(selected))$('feedback-material').value=selected;
    if($('feedback-material').value!==selected)edited();
    materialNext=page.next_cursor;controls();
  }
  function previewInto(target,parameters) {
    target.replaceChildren(text('p',`${parameters.source.name} · 版本 ${parameters.source.version}`),text('h3',parameters.kind==='changes_requested'?'请求修改':'审核意见'),text('p',parameters.body,'feedback-body'),text('p','记录原因：'+parameters.reason));
    target.append(text('p',parameters.material?`关联同源审核材料：${parameters.material.id.slice(0,8)}。`:'意见仅针对所选策略来源。','muted'));
    const raw=text('details','','source');raw.append(text('summary','核对完整原始意图'),text('pre',JSON.stringify(parameters,null,2)));target.append(raw);
  }
  function applyOutcome(outcome) {
    if(!outcome || !['awaiting_confirmation','queued','succeeded','failed','cancelled','expired'].includes(outcome.state))throw new Error('意见记录状态暂时不可用。');
    if(intent.id && intent.id!==outcome.command_id)throw new Error('返回的命令 ID 与原申请不同。');
    if(outcome.state==='succeeded' && (outcome.result?.feedback_only!==true || outcome.result?.approval_performed!==false || outcome.result?.publication_performed!==false))throw new Error('意见记录结果不符合契约。');
    intent.id=outcome.command_id;intent.state=outcome.state;intent.proof=outcome.state==='awaiting_confirmation'?outcome.proof:null;intent.retry=null;
    const messages={awaiting_confirmation:'申请已受理，尚未记录意见。请核对原意图后确认。',queued:'已确认，等待 Worker 记录。请手动刷新状态。',succeeded:'人工意见已记录，可刷新意见列表查看。',cancelled:'申请已撤回。',expired:'命令已过期，未因此完成记录。',failed:`意见未完成：${outcome.code || 'UNAVAILABLE'}。`};
    $('feedback-command-state').textContent=messages[outcome.state];
    if(terminal.has(outcome.state) && dialog.open)dialog.close();note('');changed();
  }
  async function review(show=false) {
    const original=await commands(`/${intent.id}/review`,undefined,{method:'GET'});
    if(original.request.command!=='policy.record_feedback' || original.request.scope.type!=='global' || original.request.scope.id!=='*' || !sameSource(original.request.parameters))throw new Error('请先选中这条意见命令的原策略版本，再恢复命令。');
    if(intent.parameters && tag(intent.parameters)!==tag(original.request.parameters))throw new Error('服务器返回的意见与原申请不同。');
    intent.parameters=original.request.parameters;
    $('feedback-kind').value=intent.parameters.kind;$('feedback-body').value=intent.parameters.body;$('feedback-reason').value=intent.parameters.reason;
    applyOutcome(original.outcome);
    if(show && intent.state==='awaiting_confirmation'){
      const value=await request('feedback/validate',intent.parameters);
      previewInto($('feedback-confirm-preview'),value.request);
      $('feedback-confirm-expires').textContent='确认有效期至 '+formatDate(original.outcome.expires_at);
      if(!dialog.open)dialog.showModal();
    }
  }
  async function submit() {
    const original=intent;original.state='submit_unknown';original.retry='submit';changed();
    try {const result=await commands('',{command:'policy.record_feedback',parameters:original.parameters},{key:original.submitKey});applyOutcome(result.outcome);await review(true);}
    catch(error){if(!error.stale && intent===original)note('提交结果尚未核实，请保留页面并重试原请求。',true);throw error;}
    finally{if(intent===original)changed();}
  }
  async function confirm() {
    const original=intent;original.confirmKey ||= uuid();original.state='confirm_unknown';original.retry='confirm';changed();
    try{const result=await commands(`/${original.id}/confirm`,{proof:original.proof},{key:original.confirmKey});applyOutcome(result.outcome);if(dialog.open)dialog.close();}
    catch(error){if(!error.stale && intent===original)note('确认结果尚未核实，可刷新状态或重试原确认。',true);throw error;}
    finally{if(intent===original)changed();}
  }
  async function withdraw() {
    const original=intent;original.withdrawKey ||= uuid();original.state='withdraw_unknown';original.retry='withdraw';changed();
    try{const result=await commands(`/${original.id}/withdraw`,{},{key:original.withdrawKey});applyOutcome(result.outcome);}
    catch(error){if(!error.stale && intent===original)note('撤回结果尚未核实，可刷新状态或重试原撤回。',true);throw error;}
    finally{if(intent===original)changed();}
  }
  function edited(){if(held())return;intent=null;clearPreview();note('意见已更改，请重新预览。');changed();}
  function reset(view=null) {
    source=view;items=[];next=null;materialNext=null;materialOptions.clear();intent=null;clearPreview();
    $('feedback-kind').value='comment';$('feedback-body').value=$('feedback-reason').value=$('feedback-recover-id').value='';
    $('feedback-material').replaceChildren(new Option('仅对策略来源发表评论',''));
    $('feedback-list').replaceChildren();$('feedback-empty').hidden=false;$('feedback-empty').textContent='点击“读取意见”查看这个版本的人工反馈。';
    $('feedback-source').textContent=source?`${reference().name} · 版本 ${reference().version}`:'';
    $('feedback-confirm-preview').replaceChildren();$('feedback-confirm-expires').textContent='';$('feedback-command-state').textContent='';
    if(dialog.open)dialog.close();note('');controls();
  }
  $('feedback-load').addEventListener('click',()=>run(()=>load(),'正在读取人工意见…'));
  $('feedback-more').addEventListener('click',()=>run(()=>load(true),'正在读取较早意见…'));
  $('feedback-material-load').addEventListener('click',()=>run(()=>loadMaterials(),'正在读取可选材料…'));
  $('feedback-material-more').addEventListener('click',()=>run(()=>loadMaterials(true),'正在读取更多材料…'));
  for(const id of ['feedback-kind','feedback-material','feedback-body','feedback-reason'])$(id).addEventListener(id==='feedback-body' || id==='feedback-reason'?'input':'change',edited);
  $('feedback-form').addEventListener('submit',event=>{event.preventDefault();if(held())return;run(async()=>{
    intent=null;clearPreview();
    const material=materialOptions.get($('feedback-material').value);
    const parameters={source:reference(),expected_source_fingerprint:source.fingerprint,material:material?{id:material.id,fingerprint:material.fingerprint}:null,kind:$('feedback-kind').value,body:$('feedback-body').value,reason:$('feedback-reason').value};
    const value=await request('feedback/validate',parameters);
    if(!sameSource(value.request))throw new Error('预览的意见来源不同。');
    validated=value;previewInto($('feedback-preview'),value.request);$('feedback-preview').hidden=false;note('已校验，请核对下面的原始意见后申请记录。');changed();
  },'正在校验意见与来源…');});
  $('feedback-start').addEventListener('click',()=>{if(!validated || held())return;intent={id:null,state:'submit_unknown',retry:'submit',submitKey:uuid(),parameters:structuredClone(validated.request)};changed();run(submit,'正在受理原始意见申请…');});
  $('feedback-command-review').addEventListener('click',()=>run(()=>review(true),'正在核对原始意见…'));
  $('feedback-command-status').addEventListener('click',()=>run(()=>review(),'正在读取意见命令状态…'));
  $('feedback-command-withdraw').addEventListener('click',()=>run(withdraw,'正在撤回未执行的意见…'));
  $('feedback-command-retry').addEventListener('click',()=>run(intent?.retry==='confirm'?confirm:intent?.retry==='withdraw'?withdraw:submit,'正在重试原请求…'));
  $('feedback-confirm').addEventListener('click',()=>run(confirm,'正在确认原始意见…'));
  $('feedback-confirm-close').addEventListener('click',()=>dialog.close());
  $('feedback-recover-form').addEventListener('submit',event=>{event.preventDefault();if(held())return;const id=$('feedback-recover-id').value.trim();if(!/^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(id)){note('请输入完整命令 UUID。',true);return;}run(async()=>{
    intent={id,state:'recovering',parameters:null};changed();
    try{await review();note('已恢复原命令。未重复提交意见。');}
    catch(error){if(!error.stale){intent=null;changed();}throw error;}
  },'正在恢复原意见命令…');});
  return {session(info){config={read:info.feedback_available===true,write:info.feedback_writing_available===true,materials:info.review_materials_available===true};reset();},source:reset,reset:()=>reset(),disconnect(){config={read:false,write:false,materials:false};reset();},setBusy(value){busy=value;controls();},holdsNavigation:held};
};
