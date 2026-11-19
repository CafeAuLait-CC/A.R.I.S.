"use strict";
window.ARISPolicyEditor = ({ $, text, request, commands, run, onState, onViewSaved, renderDifference, duration, quota, formatDate }) => {
  const terminal = new Set(['succeeded', 'failed', 'expired', 'cancelled']);
  const input = $('editor-source');
  const reason = $('editor-reason');
  const dialog = $('editor-confirmation');
  let enabled = false, busy = false, sourceView = null, base = null, validated = null, intent = null;
  const held = () => intent !== null && !terminal.has(intent.state);
  const key = () => crypto.randomUUID();
  function note(value, error = false) {
    $('editor-message').textContent = value;
    $('editor-message').hidden = !value;
    $('editor-message').classList.toggle('error', error);
  }
  function prepareEdit() {
    if (held()) return false;
    if (intent && terminal.has(intent.state)) {
      const saved=intent.outcome?.result;
      if (intent.state==='succeeded' && saved?.reference && saved.fingerprint) {
        base={reference:saved.reference,fingerprint:saved.fingerprint};
        $('editor-base').textContent=`基于已保存版本 ${base.reference.version} 继续编辑；旧版本保持不变。`;
      }
      intent=null;
    }
    clearPreview(); return true;
  }
  const fields=window.ARISPolicyFields({$,text,run,
    onDirty(){if(prepareEdit()){note('常用参数尚未应用，请先应用到完整草稿。');changed();}},
    onApply(source){if(prepareEdit()){input.value=JSON.stringify(source,null,2);note('参数已应用到完整草稿，尚须服务端校验。');changed();}},
    onDiscard(){note('已放弃未应用的参数更改，完整 JSON 保持不变。请重新校验。');changed();},
  });
  function controls() {
    const holding = held();
    $('editor-launch').hidden = !enabled;
    $('editor-new').disabled = busy || holding;
    $('editor-clone').disabled = busy || holding || sourceView === null;
    $('editor-recover').disabled = busy || holding;
    $('editor-recover-id').disabled = busy || holding;
    input.disabled = reason.disabled = busy || holding;
    $('editor-validate').disabled = busy || holding || fields.isDirty();
    $('editor-save').hidden = validated === null || intent !== null;
    $('editor-save').disabled = busy || fields.isDirty() || validated?.existing_equivalent === true;
    $('editor-command').hidden = intent === null;
    $('editor-command-id').textContent = intent?.id || '尚未获得命令 ID；保留原请求重试。';
    $('editor-retry').hidden = !intent || !['submit_unknown', 'confirmation_unknown', 'withdraw_unknown'].includes(intent.state);
    $('editor-retry').disabled = busy;
    $('editor-status').hidden = !intent?.id;
    $('editor-status').disabled = busy;
    $('editor-review').hidden = intent?.state !== 'awaiting_confirmation';
    $('editor-review').disabled = busy;
    $('editor-withdraw').hidden = !intent?.id || !['awaiting_confirmation', 'queued'].includes(intent.state);
    $('editor-withdraw').disabled = busy;
    $('editor-view-saved').hidden = intent?.state !== 'succeeded';
    $('editor-view-saved').disabled = busy;
    $('editor-confirm').disabled = busy || intent?.state !== 'awaiting_confirmation';
    fields.setBusy(busy || holding);
  }
  function changed() {
    controls();
    onState();
  }
  function summary(target, draft) {
    const source = draft.source, spec = source.spec;
    const title = text('h3', `${source.metadata.name} · 版本 ${source.metadata.version}`);
    const list = text('dl', '', 'rules-grid editor-summary');
    const entries = [
      ['时区', spec.timezone],
      ['最长预约', duration(spec.reservation.max_duration_seconds)],
      ['至少提前', duration(spec.reservation.min_notice_seconds)],
      ['最远提前', duration(spec.reservation.max_advance_seconds)],
      ['总并发上限', `${spec.reservation.max_concurrent_gpus} GPU`],
      ['计时权重', `${spec.gpu_time_weights.default_weight.numerator}:${spec.gpu_time_weights.default_weight.denominator} · ${spec.gpu_time_weights.model_weights.length} 个型号覆盖`],
      ['角色覆盖', `${spec.role_overrides.length} 项`],
      ['空闲规则', spec.idle_reclaim.mode === 'disabled' ? '未启用' : '释放预约（规则来源内容）'],
    ];
    entries.forEach(([name, value]) => { const row = document.createElement('div'); row.append(text('dt', name), text('dd', value)); list.append(row); });
    const quotas = text('div', '', 'editor-quotas');
    const limits = spec.quota.scope === 'per_node' ? spec.quota.node_limits : [{node_id:'全局', limit:spec.quota.default_limit}];
    limits.forEach(limit => quotas.append(text('p', `${limit.node_id} · ${quota(limit.limit)} / 人`)));
    const weekdays={monday:'周一',tuesday:'周二',wednesday:'周三',thursday:'周四',friday:'周五',saturday:'周六',sunday:'周日'};
    const window = spec.quota.window.kind === 'calendar_week' ? `自然周 · ${weekdays[spec.quota.window.week_starts_on]}开始` : '滚动 7 天';
    target.replaceChildren(title, list, text('h4', `实际使用额度 · ${window}`), quotas, text('p', `保存原因：${draft.reason}`, 'editor-reason-copy'));
    if(spec.role_overrides.length || spec.gpu_time_weights.model_weights.length || spec.idle_reclaim.mode==='enabled') {
      const advanced=text('details','','editor-preserved');
      advanced.append(text('summary','核对保留的角色、型号及空闲规则'));
      spec.role_overrides.forEach(role=>{
        const values=[];
        if(role.limits.quota_limit)values.push(`额度 ${quota(role.limits.quota_limit)}`);
        if(role.limits.max_duration_seconds)values.push(`最长 ${duration(role.limits.max_duration_seconds)}`);
        if(role.limits.max_concurrent_gpus)values.push(`并发 ${role.limits.max_concurrent_gpus} GPU`);
        advanced.append(text('p',`${role.role}（优先级 ${role.priority}）· ${values.join(' · ')}`));
      });
      spec.gpu_time_weights.model_weights.forEach(item=>advanced.append(text('p',`${item.model} · ${item.weight.numerator}:${item.weight.denominator}`)));
      if(spec.idle_reclaim.mode==='enabled') {
        const idle=spec.idle_reclaim;
        advanced.append(text('p',`来源空闲规则：利用率低于 ${idle.utilization_below_percent}% 持续 ${duration(idle.observation_seconds)}，宽限 ${duration(idle.grace_seconds)}，动作释放预约。此处不执行该流程。`));
      }
      target.append(advanced);
    }
  }
  function clearPreview() {
    validated = null;
    $('editor-preview').hidden = true;
    $('editor-preview-summary').replaceChildren();
    $('editor-difference').replaceChildren();
    $('editor-difference-note').textContent = '';
    $('editor-fingerprint').textContent = '';
    $('editor-canonical').textContent = '';
  }
  function reset() {
    sourceView = null; base = null; validated = null; intent = null;
    input.value = ''; reason.value = ''; $('editor-recover-id').value = '';
    $('editor-panel').hidden = true;
    $('editor-base').textContent = '';
    $('editor-command-state').textContent = '';
    $('editor-confirm-summary').replaceChildren();
    $('editor-confirm-json').textContent = '';
    $('editor-confirm-fingerprint').textContent = '';
    $('editor-confirm-expires').textContent = '';
    fields.clear(); $('editor-json').open=false;
    clearPreview(); note('');
    if (dialog.open) dialog.close();
    controls();
  }
  function editStarted(event) {
    if (!prepareEdit()) return;
    if (event.target===input) fields.clear();
    note('内容已更改，请重新校验。'); changed();
  }
  function open(source, reference = null) {
    if (held()) return;
    intent = null; base = reference; clearPreview(); note('');
    input.value = source ? JSON.stringify(source, null, 2) : '';
    reason.value = '';
    $('editor-base').textContent = reference ? `从版本 ${reference.reference.version} 克隆；旧版本保持不变。` : '粘贴完整的策略 JSON。所有规则都须显式提供。';
    $('editor-panel').hidden = false;
    if (source) fields.source(source); else fields.clear();
    $('editor-json').open=!source;
    changed();
    if (source) fields.focus(); else input.focus();
  }
  function previewInput() {
    return {source_text:input.value, reason:reason.value, ...(base ? {base:base.reference, expected_base_fingerprint:base.fingerprint} : {})};
  }
  function applyOutcome(outcome) {
    if (!outcome || !['awaiting_confirmation','queued','succeeded','expired','cancelled','failed'].includes(outcome.state)) throw new Error('命令状态暂时不可用。');
    if (outcome.state === 'succeeded' && (outcome.result?.catalog_entry_only !== true || outcome.result?.publication_performed !== false)) {
      throw new Error('保存结果不符合草稿契约，请核对后端记录。');
    }
    note('');
    intent.id = outcome.command_id;
    intent.outcome = outcome;
    intent.state = outcome.state;
    intent.proof = outcome.state === 'awaiting_confirmation' ? outcome.proof : null;
    const states = {
      awaiting_confirmation: '申请已受理，尚未保存。请核对服务器保留的原始意图后确认。',
      queued: '已确认，正在等待草稿 Worker。尚未保存成功，可刷新状态。',
      succeeded: outcome.result?.created === false ? '目录已有相同来源，原保存证据保持。没有发布或激活规则。' : '草稿来源已保存。它不会批准、发布或改变当前预约规则。',
      expired: '命令已过期。编辑并重新校验后，可提出新的保存申请。',
      cancelled: '保存申请已撤回，没有因此保存新来源。',
      failed: `命令未完成：${outcome.code || 'UNAVAILABLE'}。`,
    };
    $('editor-command-state').textContent = states[outcome.state] || '命令状态暂时不可用。';
    if (terminal.has(outcome.state) && dialog.open) dialog.close();
    changed();
  }
  async function reviewIntent(show = false) {
    const original = await commands(`/${intent.id}/review`, undefined, {method:'GET'});
    if (original.request.command !== 'policy.record_draft') throw new Error('这不是策略草稿保存命令。');
    const draft = original.request.parameters;
    intent.parameters = draft;
    input.value = JSON.stringify(draft.source, null, 2); reason.value = draft.reason;
    fields.source(draft.source); $('editor-json').open=false;
    $('editor-panel').hidden = false;
    applyOutcome(original.outcome);
    if (original.outcome.state === 'awaiting_confirmation' && show) {
      const checked = await request('drafts/validate', {source_text:JSON.stringify(draft.source), reason:draft.reason});
      summary($('editor-confirm-summary'), checked.draft);
      $('editor-confirm-json').textContent = JSON.stringify(checked.draft.source, null, 2);
      $('editor-confirm-fingerprint').textContent = checked.fingerprint;
      $('editor-confirm-expires').textContent = `确认有效期至 ${formatDate(original.outcome.expires_at)}`;
      if (!dialog.open) dialog.showModal();
    }
  }
  async function submitOriginal() {
    const original = intent;
    original.state = 'submit_unknown'; changed();
    try {
      const accepted = await commands('', {command:'policy.record_draft', parameters:original.parameters}, {key:original.submitKey});
      applyOutcome(accepted.outcome);
      await reviewIntent(true);
    } catch (error) {
      if (!error.stale && intent === original && !original.id) note('提交结果尚未确认。原参数和原请求已保留，请重试原请求。', true);
      throw error;
    } finally { if (intent === original) changed(); }
  }
  async function confirmOriginal() {
    const original = intent;
    original.confirmKey ||= key();
    original.state = 'confirmation_unknown'; changed();
    try {
      const accepted = await commands(`/${original.id}/confirm`, {proof:original.proof}, {key:original.confirmKey});
      applyOutcome(accepted.outcome);
      if (dialog.open) dialog.close();
    } catch (error) {
      if (!error.stale && intent === original) note('确认结果尚未核实。保留原确认，可刷新状态或重试原确认。', true);
      throw error;
    } finally { if (intent === original) changed(); }
  }
  async function withdrawOriginal() {
    const original = intent;
    original.withdrawKey ||= key(); original.state = 'withdraw_unknown'; changed();
    try {
      const accepted = await commands(`/${original.id}/withdraw`, {}, {key:original.withdrawKey});
      applyOutcome(accepted.outcome);
    } catch (error) { if (!error.stale && intent === original) note('撤回结果尚未核实，请刷新状态或重试原撤回。', true); throw error; }
    finally { if (intent === original) changed(); }
  }
  $('editor-new').addEventListener('click', () => open(null));
  $('editor-clone').addEventListener('click', () => {
    if (!sourceView || held()) return;
    const source = structuredClone(sourceView.source);
    if (source.metadata.version >= 2147483647) { note('这个名称的版本号已达到上限。', true); return; }
    source.metadata.version += 1;
    open(source, {reference:{name:sourceView.source.metadata.name, version:sourceView.source.metadata.version}, fingerprint:sourceView.fingerprint});
  });
  [input, reason].forEach(field => field.addEventListener('input', editStarted));
  input.addEventListener('invalid',()=>{$('editor-json').open=true;});
  $('editor-form').addEventListener('submit', event => {
    event.preventDefault();
    if(fields.isDirty()){note('请先应用或放弃常用参数更改。',true);return;}
    run(async () => {
      if (intent && terminal.has(intent.state)) intent = null;
      clearPreview(); note('');
      validated = await request('drafts/validate', previewInput());
      fields.source(validated.draft.source);
      summary($('editor-preview-summary'), validated.draft);
      $('editor-fingerprint').textContent = validated.fingerprint;
      $('editor-canonical').textContent = JSON.stringify(validated.draft.source, null, 2);
      if (validated.difference) renderDifference(validated.difference, 'editor-difference', 'editor-difference-note');
      else $('editor-difference-note').textContent = '没有选择比较基准。这是独立的来源校验。';
      $('editor-preview').hidden = false;
      note(validated.existing_equivalent ? '同一版本和内容已存在，原保存证据会保留，无需再次申请。' : '规则格式、版本和内联大小检查已通过；尚未保存。');
      changed();
    }, '正在校验草稿…');
  });
  $('editor-save').addEventListener('click', () => run(async () => {
    if (!validated || validated.existing_equivalent || held()) return;
    intent = {id:null, state:'submit_unknown', parameters:validated.draft, submitKey:key(), proof:null};
    await submitOriginal();
  }, '正在申请保存…'));
  $('editor-review').addEventListener('click', () => run(() => reviewIntent(true), '正在读取原始意图…'));
  $('editor-confirm').addEventListener('click', () => run(confirmOriginal, '正在确认原保存意图…'));
  $('editor-close-confirm').addEventListener('click', () => dialog.close());
  $('editor-status').addEventListener('click', () => run(() => reviewIntent(false), '正在读取命令状态…'));
  $('editor-withdraw').addEventListener('click', () => run(withdrawOriginal, '正在撤回原保存申请…'));
  $('editor-retry').addEventListener('click', () => run(() => intent.state === 'submit_unknown' ? submitOriginal() : intent.state === 'confirmation_unknown' ? confirmOriginal() : withdrawOriginal(), '正在重试同一请求…'));
  $('editor-view-saved').addEventListener('click', () => run(async () => {
    const saved = intent?.outcome?.result;
    if (intent?.state === 'succeeded' && saved) await onViewSaved(saved.reference, saved.fingerprint);
  }, '正在读取已保存来源…'));
  $('editor-recover-form').addEventListener('submit', event => {
    event.preventDefault();
    run(async () => {
      const id = $('editor-recover-id').value.trim();
      if (!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(id)) throw new Error('请填写完整的保存命令 ID。');
      const original = {id, state:'recovering', parameters:null, proof:null};
      intent = original;
      try { await reviewIntent(true); }
      catch (error) { if (intent === original) { intent = null; changed(); } throw error; }
    }, '正在恢复保存命令…');
  });
  return {
    reset,
    session(info) { enabled = info.draft_editing_available === true; reset(); },
    disconnect() { enabled = false; reset(); },
    source(view) { sourceView = view; controls(); },
    holdsNavigation: held,
    setBusy(value) { busy = value; controls(); },
  };
};
