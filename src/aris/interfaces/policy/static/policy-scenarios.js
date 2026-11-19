"use strict";
window.ARISPolicyScenarios = ({ $, text, request, versions, run, formatDate }) => {
  const form = $('scenario-form');
  const result = $('scenario-result');
  const controls = [...form.querySelectorAll('input,button')];
  const names = {
    'reservation.max_duration': '预约时长',
    'reservation.min_notice': '至少提前',
    'reservation.max_advance': '最远提前',
    'reservation.concurrent': '总并发 GPU',
    'quota.actual_usage': '实际使用额度',
  };
  const reasons = {
    WITHIN_LIMIT: '在该版本的限制内',
    MAX_DURATION_EXCEEDED: '超过最长预约时长',
    MIN_NOTICE_UNMET: '提前时间不足',
    ADVANCE_HORIZON_EXCEEDED: '超过可提前预约的范围',
    CONCURRENCY_UNKNOWN: '没有提供假设并发数',
    CONCURRENCY_LIMIT_EXCEEDED: '超过总并发 GPU 上限',
    QUOTA_SCOPE_UNCONFIGURED: '该节点没有配置额度',
    QUOTA_UNLIMITED: '该版本不限制额度',
    USAGE_UNKNOWN: '没有提供完整使用数据',
  };
  function clear() {
    result.replaceChildren();
    result.classList.remove('single');
    result.hidden = true;
    $('scenario-note').textContent = '';
  }
  function reset() {
    clear();
    form.reset();
    $('scenario-nodes').replaceChildren();
  }
  function inputInteger(id, minimum, maximum, optional = false) {
    const raw = $(id).value.trim();
    if (!raw && optional) return null;
    if (!/^\d+$/.test(raw)) throw new Error('请填写整分钟和整数 GPU 数。');
    const value = Number(raw);
    if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
      throw new Error('输入超出范围，请检查表单中的限制。');
    }
    return value;
  }
  function scenario() {
    const node = $('scenario-node').value.trim();
    if (!node || node.length > 128 || /\s/.test(node)) throw new Error('请填写不含空格的节点 ID。');
    const notice = inputInteger('scenario-notice', 0, 525600);
    const duration = inputInteger('scenario-duration', 1, 525600);
    const concurrent = inputInteger('scenario-concurrent', 1, 1000, true);
    const roles = $('scenario-roles').value.split(',').map(value => value.trim()).filter(Boolean);
    if (roles.length > 32 || new Set(roles).size !== roles.length || roles.some(role => !/^[a-z][a-z0-9_-]{0,63}$/.test(role))) {
      throw new Error('假设角色应使用逗号分隔、不重复的规则角色名。');
    }
    const reference = new Date();
    const start = new Date(reference.getTime() + notice * 60000);
    const end = new Date(start.getTime() + duration * 60000);
    return {
      name: 'interactive-scenario', user_key: 'hypothetical-user', node_id: node,
      as_of: reference.toISOString(),
      request: { start_at: start.toISOString(), end_at: end.toISOString() },
      projected_concurrent_gpus: concurrent, assumed_roles: roles,
    };
  }
  function show(response, assumption) {
    const comparison = response.cases[0];
    if (!comparison || response.comparison_only !== true) throw new Error('试算结果暂时不可用。');
    const decisions = {
      WOULD_ALLOW: ['假设条件符合', 'pass'],
      WOULD_DENY: ['假设条件超限', 'deny'],
      NOT_EVALUATED: ['信息不足，无法完整评估', 'unknown'],
    };
    const sides = response.before_source_id === response.after_source_id ? ['after'] : ['before', 'after'];
    const cards = sides.map(side => {
      const value = comparison[side];
      if (!value || value.simulation_only !== true) throw new Error('试算结果暂时不可用。');
      const card = text('section', '', 'scenario-card');
      const heading = text('div', '', 'section-head');
      heading.append(text('h4', `版本 ${value.source_version}`));
      const [label, state] = decisions[value.decision];
      heading.append(text('span', label, `scenario-state ${state}`));
      card.append(heading);
      const table = document.createElement('table');
      const body = document.createElement('tbody');
      value.results.forEach(rule => {
        const row = document.createElement('tr');
        const status = { pass: '符合', deny: '超出', unknown: '未知' }[rule.state];
        const detail = text('td', reasons[rule.reason] || '缺少可用的假设上下文');
        detail.append(text('small', rule.reason));
        row.append(text('th', names[rule.rule_id]), text('td', status, `scenario-state ${rule.state}`), detail);
        body.append(row);
      });
      table.append(body);
      const scrolling = text('div', '', 'table-scroll');
      scrolling.append(table);
      card.append(scrolling);
      card.append(text('p', value.selected_role_override ? `假设角色覆盖：${value.selected_role_override}` : '没有命中假设角色覆盖。', 'muted'));
      return card;
    });
    result.replaceChildren(...cards);
    result.classList.toggle('single', sides.length === 1);
    result.hidden = false;
    $('scenario-note').textContent = `节点 ${assumption.node_id} · 参考时间 ${formatDate(assumption.as_of)} · 所有输入均为假设，未读取真实使用数据。`;
  }
  form.addEventListener('input', clear);
  form.addEventListener('change', clear);
  form.addEventListener('reset', clear);
  form.addEventListener('submit', event => {
    event.preventDefault();
    run(async () => {
      clear();
      const assumption = scenario();
      const chosen = versions();
      if (!chosen.before || !chosen.after) throw new Error('请先选择已保存的策略版本。');
      const response = await request('compare', {
        before: chosen.before.reference, after: chosen.after.reference,
        expected_before_fingerprint: chosen.before.fingerprint,
        expected_after_fingerprint: chosen.after.fingerprint,
        scenarios: [assumption],
      });
      show(response, assumption);
    }, '正在试算假设条件…');
  });
  return {
    clear, reset,
    setBusy(value) { controls.forEach(control => { control.disabled = value; }); },
    source(view) {
      const quota = view.source.spec.quota;
      const nodes = quota.scope === 'per_node' ? quota.node_limits.map(item => item.node_id) : [];
      $('scenario-nodes').replaceChildren(...nodes.map(node => new Option(node, node)));
    },
  };
};
