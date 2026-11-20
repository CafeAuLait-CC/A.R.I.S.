"use strict";
window.ARISPolicyApprovalReview = ({$,text,request,run,formatDate}) => {
  let info=null,source=null,current=null,busy=false;
  const states={counted:'计入',superseded:'已被更新记录替代',revoked:'已撤销',expired:'已到期',different_requirements:'审核要求不同',approver_unavailable:'当前资格或身份不可用'};
  const outcomes={minimum_met:'已达到配置人数要求',minimum_not_met:'尚未达到配置人数要求',unknown:'其余记录未核对，人数结果待定'};
  const query=()=>({source:{name:source.source.metadata.name,version:source.source.metadata.version},expected_source_fingerprint:source.fingerprint,expected_review_requirements_fingerprint:info.source_approval_review_requirements_fingerprint});
  function note(value,error=false){$('approval-review-note').textContent=value;$('approval-review-note').classList.toggle('error',error);}
  function controls(){const readable=!!info?.source_approval_review_available && source!==null;$('approval-review-panel').hidden=!readable;$('approval-review-load').disabled=busy || !readable;$('approval-review-export').disabled=busy || !readable || !current;}
  function reset(view=null){source=view;current=null;$('approval-review-result').replaceChildren();$('approval-review-requirements').textContent=info?.source_approval_review_available?`当前要求：至少 ${info.source_approval_review_minimum_distinct_approvers} 位不同审批人，均须仍具备相应资格。`:'';note('');controls();}
  function render(value){
    const target=$('approval-review-result'),prefix=value.truncated?'至少 ':'';
    target.replaceChildren(text('h4',`${outcomes[value.outcome]} · ${prefix}${value.qualifying_signers} / ${value.requirements.minimum_distinct_approvers} 人`,'approval-review-count'),text('p',`核对于 ${formatDate(value.as_of)}`,'muted'));
    if(value.truncated)target.append(text('p','本次只核对最新128条记录，人数为已核对部分的下界。未达人数要求时保留待定结果。','muted'));
    if(value.entries.length){
      const table=text('table','','approval-review-table'),head=document.createElement('thead'),row=document.createElement('tr'),body=document.createElement('tbody');
      for(const title of ['审批人','原批准时间','计数状态'])row.append(text('th',title));head.append(row);table.append(head);
      for(const entry of value.entries){const row=document.createElement('tr'),author=text('td',entry.approver_label || `用户 ${entry.approval.approved_by.slice(0,8)}`);author.append(text('small',`记录 ${entry.approval.id.slice(0,8)}`));row.append(author,text('td',formatDate(entry.approval.approved_at)),text('td',states[entry.state] || '待核对'));body.append(row);}
      table.append(body);target.append(table);
    }else target.append(text('p','此来源暂无批准记录。','muted'));
    const details=text('details','','source');details.append(text('summary','完整审批元数据与结果指纹'),text('pre',JSON.stringify(value,null,2)));target.append(details);
    note('当前审批人数已核对；策略发布与运行依赖仍需单独确认。');
  }
  async function load(){
    current=null;$('approval-review-result').replaceChildren();controls();note('正在核对当前审批人、记录及状态…');
    try{
      const input=query(),value=await request('approvals/review',input);
      if(value.review_only!==true || value.publication_authorized!==false || value.current_runtime_dependencies_assessed!==false || value.query.source.name!==input.source.name || value.query.source.version!==input.source.version || value.query.expected_source_fingerprint!==input.expected_source_fingerprint || value.query.expected_review_requirements_fingerprint!==input.expected_review_requirements_fingerprint || !Array.isArray(value.entries) || value.entries.length>128)throw new Error('审批核对结果不符合当前来源与要求。');
      current={input,value};render(value);controls();
    }catch(error){if(!error.stale)note('本次审批核对未完成，未提供新的结论。',true);throw error;}
  }
  $('approval-review-load').addEventListener('click',()=>run(load,'正在核对当前审批人数…'));
  $('approval-review-export').addEventListener('click',()=>run(async()=>{
    if(!current)return;const original=current;
    try{
      const blob=await request('approvals/review/report',{...original.input,expected_result_fingerprint:original.value.fingerprint},{download:true});
      if(original!==current)return;const url=URL.createObjectURL(blob),link=document.createElement('a');try{link.href=url;link.download='aris-approval-review.html';document.body.append(link);link.click();}finally{link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
    }catch(error){if(!error.stale){current=null;$('approval-review-result').replaceChildren();controls();note('请重新核对当前审批状态后再导出报告。',true);}throw error;}
  },'正在重新鉴权并核对审批报告…'));
  return {session(value){info=value;reset();},source:reset,reset(){reset();},disconnect(){info=null;reset();},setBusy(value){busy=value;controls();}};
};
