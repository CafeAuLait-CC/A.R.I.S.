"use strict";
window.ARISPolicyMaterialSave = ({$,text,request,commands,run,onState,latest,caseCard,viewSaved,formatDate}) => {
  const terminal=new Set(['succeeded','failed','cancelled','expired']);
  const dialog=$('material-confirmation');
  let implementation=null,source=null,prepared=null,validated=null,intent=null,busy=false;
  const held=()=>intent!==null && !terminal.has(intent.state);
  const key=()=>crypto.randomUUID();
  function note(value,error=false){$('material-save-note').textContent=value;$('material-save-note').classList.toggle('error',error);}
  function controls(){
    $('material-save-tools').hidden=implementation===null || source===null;
    $('material-copy-scenario').disabled=busy || held() || (prepared?.scenarios.length || 0)>=8;
    $('material-cases-clear').disabled=busy || held();
    for(const control of $('material-case-list').querySelectorAll('input,button'))control.disabled=busy || held();
    $('material-recover-id').disabled=$('material-recover').disabled=busy || held();
    $('material-save-form').hidden=prepared===null;
    $('material-save-reason').disabled=$('material-validate').disabled=busy || held();
    $('material-save-submit').hidden=validated===null || intent!==null;
    $('material-save-submit').disabled=busy;
    $('material-command').hidden=intent===null;
    $('material-command-id').textContent=intent?.id || '尚未获得 ID；请保留页面并重试原请求。';
    $('material-command-review').hidden=intent?.state!=='awaiting_confirmation';
    $('material-command-withdraw').hidden=!intent?.id || !['awaiting_confirmation','queued'].includes(intent.state);
    $('material-command-status').hidden=!intent?.id;
    $('material-command-retry').hidden=!intent || !['submit_unknown','confirm_unknown','withdraw_unknown'].includes(intent.state);
    $('material-command-view').hidden=intent?.state!=='succeeded';
    for(const id of ['material-command-review','material-command-withdraw','material-command-status','material-command-retry','material-command-view'])$(id).disabled=busy;
    $('material-confirm').disabled=busy || intent?.state!=='awaiting_confirmation';
  }
  function changed(){controls();onState();}
  function clearPreview(){validated=null;$('material-save-preview').replaceChildren();$('material-save-preview').hidden=true;}
  function reset(view=null){source=view;prepared=null;renderCases();intent=null;clearPreview();$('material-save-reason').value='';$('material-recover-id').value='';$('material-save-target').textContent='';$('material-command-state').textContent='';$('material-confirm-preview').replaceChildren();$('material-confirm-expires').textContent='';if(dialog.open)dialog.close();note('');controls();}
  function target(parameters){return `${parameters.source.name} · 版本 ${parameters.source.version} · ${parameters.scenarios.length} 个原始假设`;}
  function previewInto(element,value){
    element.replaceChildren(text('p',target(value.request),'material-save-target'),text('p',`保存原因：${value.request.reason}`,'material-reason'),text('p','输入均为手动假设；保存材料不批准、发布规则，也不授权预约。','material-assumption'),...value.request.scenarios.map((input,index)=>caseCard(input,value.results[index])));
    const raw=text('details','','source');raw.append(text('summary','核对完整原始保存意图'),text('pre',JSON.stringify(value.request,null,2)));element.append(raw);
  }
  function applyOutcome(outcome){
    if(!outcome || !['awaiting_confirmation','queued','succeeded','failed','cancelled','expired'].includes(outcome.state))throw new Error('材料保存状态暂时不可用。');
    if(outcome.state==='succeeded' && (outcome.result?.review_material_only!==true || outcome.result?.approval_performed!==false || outcome.result?.publication_performed!==false))throw new Error('保存结果不符合材料契约，请核对后端记录。');
    intent.id=outcome.command_id;intent.state=outcome.state;intent.outcome=outcome;intent.proof=outcome.state==='awaiting_confirmation'?outcome.proof:null;
    const messages={awaiting_confirmation:'申请已受理，尚未保存材料。请核对原始意图后确认。',queued:'已确认，等待策略 Worker。尚未保存成功，请手动刷新状态。',succeeded:'案例材料已保存。未批准、发布或更改预约规则。',cancelled:'申请已撤回，没有因此保存材料。',expired:'命令已过期，没有因此完成保存。',failed:`保存未完成：${outcome.code || 'UNAVAILABLE'}。`};
    $('material-command-state').textContent=messages[outcome.state];note('');if(terminal.has(outcome.state) && dialog.open)dialog.close();changed();
  }
  async function review(show=false){
    const original=await commands(`/${intent.id}/review`,undefined,{method:'GET'});
    if(original.request.command!=='policy.record_review_material')throw new Error('这不是案例材料保存命令。');
    intent.parameters=original.request.parameters;prepared=structuredClone(intent.parameters);$('material-save-reason').value=prepared.reason;$('material-save-target').textContent=target(prepared);renderCases();applyOutcome(original.outcome);
    if(show && original.outcome.state==='awaiting_confirmation'){
      const value=await request('materials/validate',original.request.parameters);
      previewInto($('material-confirm-preview'),value);$('material-confirm-expires').textContent=`确认有效期至 ${formatDate(original.outcome.expires_at)}`;
      if(!dialog.open)dialog.showModal();
    }
  }
  async function submit(){
    const original=intent;original.state='submit_unknown';changed();
    try{const accepted=await commands('',{command:'policy.record_review_material',parameters:original.parameters},{key:original.submitKey});applyOutcome(accepted.outcome);await review(true);}
    catch(error){if(!error.stale && intent===original && !original.id)note('提交结果尚未确认。保留原参数和原 key，请重试原请求。关闭页面前请记录已获得的命令 ID。',true);throw error;}
    finally{if(intent===original)changed();}
  }
  async function confirm(){
    const original=intent;original.confirmKey ||= key();original.state='confirm_unknown';changed();
    try{const accepted=await commands(`/${original.id}/confirm`,{proof:original.proof},{key:original.confirmKey});applyOutcome(accepted.outcome);if(dialog.open)dialog.close();}
    catch(error){if(!error.stale && intent===original)note('确认结果尚未核实，可刷新状态或重试原确认。',true);throw error;}
    finally{if(intent===original)changed();}
  }
  async function withdraw(){
    const original=intent;original.withdrawKey ||= key();original.state='withdraw_unknown';changed();
    try{const accepted=await commands(`/${original.id}/withdraw`,{},{key:original.withdrawKey});applyOutcome(accepted.outcome);}
    catch(error){if(!error.stale && intent===original)note('撤回结果尚未核实，可刷新状态或重试原撤回。',true);throw error;}
    finally{if(intent===original)changed();}
  }
  function basketChanged(value){intent=null;clearPreview();$('material-save-target').textContent=prepared?target(prepared):'';note(value);changed();}
  function renderCases(){
    const cases=prepared?.scenarios || [];$('material-case-basket').hidden=!cases.length;$('material-case-count').textContent=`${cases.length} / 8 个待保存案例`;
    $('material-case-list').replaceChildren(...cases.map((assumption,index)=>{
      const item=text('li','','material-basket-case'),head=text('div','','material-basket-head'),label=text('label',`案例 ${index+1} 名称`),name=document.createElement('input');name.value=assumption.name;name.maxLength=64;name.autocomplete='off';name.setAttribute('aria-label',`案例 ${index+1} 名称`);label.append(name);
      name.addEventListener('input',()=>{if(busy || held())return;prepared.scenarios[index].name=name.value;item.querySelector('pre').textContent=JSON.stringify(prepared.scenarios[index],null,2);basketChanged('案例名称已更改，请重新预览组合。');});
      const remove=text('button','移除','secondary');remove.type='button';remove.setAttribute('aria-label',`移除案例 ${index+1}`);remove.addEventListener('click',()=>{if(busy || held())return;prepared.scenarios.splice(index,1);if(!prepared.scenarios.length)prepared=null;renderCases();basketChanged('待保存案例已移除，请重新预览。');});head.append(label,remove);
      item.append(head,text('p',`节点 ${assumption.node_id} · 原参考时间 ${formatDate(assumption.as_of)}`,'muted'),text('p',`预约 ${formatDate(assumption.request.start_at)} — ${formatDate(assumption.request.end_at)}`,'muted'));
      const raw=text('details','','source');raw.append(text('summary','核对原始假设'),text('pre',JSON.stringify(assumption,null,2)));item.append(raw);return item;
    }));
  }
  $('material-copy-scenario').addEventListener('click',()=>{
    if(held() || busy)return;
    const assumption=latest();if(!assumption || !source){note('请先完成下方的假设约束试算。输入改动或重置后须重新试算。',true);return;}
    if((prepared?.scenarios.length || 0)>=8){note('每份材料最多包含 8 个案例。',true);return;}
    if(!prepared){$('material-save-reason').value='';prepared={source:{name:source.source.metadata.name,version:source.source.metadata.version},expected_source_fingerprint:source.fingerprint,expected_implementation_ref:implementation,scenarios:[],reason:''};}
    const names=new Set(prepared.scenarios.map(value=>value.name.trim()));let suffix=1;while(names.has(`Case ${suffix}`))suffix++;assumption.name=`Case ${suffix}`;prepared.scenarios.push(structuredClone(assumption));
    renderCases();basketChanged('已添加原假设及原参考时间。可继续试算其他条件并添加到同一份材料。');
  });
  $('material-cases-clear').addEventListener('click',()=>{if(busy || held())return;prepared=null;renderCases();basketChanged('待保存组合已清空。');});
  $('material-save-reason').addEventListener('input',()=>{if(!held()){intent=null;clearPreview();note('原因已更改，请重新预览。');changed();}});
  $('material-save-form').addEventListener('submit',event=>{event.preventDefault();run(async()=>{
    if(!prepared || held())return;
    intent=null;clearPreview();const cases=prepared.scenarios.map(value=>({...value,name:value.name.trim()}));
    if(!cases.length || cases.length>8 || cases.some(value=>!value.name || value.name.length>64) || new Set(cases.map(value=>value.name)).size!==cases.length)throw new Error('请为每个案例填写不重复的名称（1–64 个字符）。');
    const parameters={...prepared,scenarios:cases,reason:$('material-save-reason').value.trim()};
    if(new TextEncoder().encode(JSON.stringify(parameters)).length>16384)throw new Error('组合内容超过保存大小上限，请减少案例内容。');
    validated=await request('materials/validate',parameters);prepared=structuredClone(validated.request);renderCases();previewInto($('material-save-preview'),validated);$('material-save-preview').hidden=false;note('服务器已重新试算，尚未保存。');changed();
  },'正在预览案例材料…');});
  $('material-save-submit').addEventListener('click',()=>run(async()=>{if(!validated || held())return;intent={id:null,state:'submit_unknown',parameters:structuredClone(validated.request),submitKey:key(),proof:null};await submit();},'正在申请保存材料…'));
  $('material-command-review').addEventListener('click',()=>run(()=>review(true),'正在核对原始材料意图…'));
  $('material-command-status').addEventListener('click',()=>run(()=>review(false),'正在读取材料保存状态…'));
  $('material-command-withdraw').addEventListener('click',()=>run(withdraw,'正在撤回原材料申请…'));
  $('material-command-retry').addEventListener('click',()=>run(()=>intent.state==='submit_unknown'?submit():intent.state==='confirm_unknown'?confirm():withdraw(),'正在重试同一材料请求…'));
  $('material-command-view').addEventListener('click',()=>run(async()=>{if(intent?.state!=='succeeded')return;const receipt=intent.outcome.result,parameters=intent.parameters;await viewSaved(parameters.source,parameters.expected_source_fingerprint,receipt.id,receipt.fingerprint);},'正在读取已保存案例材料…'));
  $('material-confirm').addEventListener('click',()=>run(confirm,'正在确认原材料保存意图…'));
  $('material-confirm-close').addEventListener('click',()=>dialog.close());
  $('material-recover-form').addEventListener('submit',event=>{event.preventDefault();run(async()=>{
    const id=$('material-recover-id').value.trim();if(!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(id))throw new Error('请填写完整的材料保存命令 ID。');
    const original={id,state:'recovering',parameters:null,proof:null};intent=original;clearPreview();
    try{await review(true);}catch(error){if(intent===original && original.state==='recovering'){intent=null;changed();}throw error;}
  },'正在恢复材料保存命令…');});
  return {session(info){implementation=info.review_material_implementation_ref || null;reset();},source:reset,reset(){reset();},disconnect(){implementation=null;reset();},holdsNavigation:held,setBusy(value){busy=value;controls();}};
};
