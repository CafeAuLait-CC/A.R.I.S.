"use strict";
window.ARISPolicyFields = ({ $, text, run, onDirty, onApply, onDiscard }) => {
  const panel = $('editor-quick');
  const form = $('editor-quick-form');
  const YEAR = 366 * 86400, MAX_QUOTA = 1000 * 7 * 86400;
  const timeUnits = [['秒',1],['分钟',60],['小时',3600],['天',86400]];
  const quotaUnits = [['GPU 秒',1],['GPU 小时',3600]];
  const weekdays = {monday:'周一',tuesday:'周二',wednesday:'周三',thursday:'周四',friday:'周五',saturday:'周六',sunday:'周日'};
  let original = null, dirty = false, locked = false, quotaInputs = [];
  function integer(input, minimum, maximum, label) {
    const raw = input.value.trim();
    if (!/^\d+$/.test(raw)) throw new Error(`${label}须填写整数。`);
    const value = Number(raw);
    if (!Number.isSafeInteger(value) || value < minimum || value > maximum) throw new Error(`${label}超出范围。`);
    return value;
  }
  function numberInput(id, value, minimum, maximum) {
    const input = document.createElement('input');
    input.id = id; input.type = 'number'; input.min = String(minimum); input.max = String(maximum);
    input.step = '1'; input.required = true; input.value = value === null ? '' : String(value);
    return input;
  }
  function labeled(label, input) {
    const row = text('div','','quick-field'), name = text('label',label);
    name.htmlFor = input.id; row.append(name,input); return row;
  }
  function units(id, label, choices, value) {
    const select = document.createElement('select');
    select.id = id; select.setAttribute('aria-label',label);
    select.append(...choices.map(([name, scale]) => new Option(name,String(scale))));
    select.value = String(value); return select;
  }
  function exactUnit(value, choices) {
    if (value === 0) return 1;
    return [...choices].reverse().find(([,scale]) => value % scale === 0)[1];
  }
  function durationField(label, id, seconds, minimum) {
    const scale = exactUnit(seconds,timeUnits), input = numberInput(id,seconds/scale,minimum,YEAR);
    const select = units(id+'-unit',label+'单位',timeUnits,scale);
    const pair = text('div','','quick-value-unit'); pair.append(input,select);
    const row=text('div','','quick-field'), name=text('label',label);name.htmlFor=input.id;row.append(name,pair);return row;
  }
  function seconds(input, unit, choices, minimum, maximum, label) {
    const scale = Number(unit.value);
    if (!choices.some(([,value]) => value === scale)) throw new Error(`${label}单位无效。`);
    const value = integer(input,minimum,maximum,label) * scale;
    if (!Number.isSafeInteger(value) || value < minimum || value > maximum) throw new Error(`${label}换算后超出范围。`);
    return value;
  }
  function controls() {
    panel.hidden = original === null;
    for (const control of form.querySelectorAll('input,select,button')) control.disabled = locked || original === null;
    for (const entry of quotaInputs) {
      const unlimited = entry.kind.value === 'unlimited';
      entry.amounts.hidden = unlimited;
      entry.amount.disabled = entry.unit.disabled = locked || unlimited;
    }
    $('editor-quick-apply').disabled = locked || !dirty || original === null;
    $('editor-quick-discard').disabled = locked || !dirty || original === null;
    $('editor-quick-state').textContent = dirty ? '参数尚未应用。先应用到完整草稿，再做服务端校验。' : '这些参数与完整草稿一致；高级规则会保留。';
  }
  function source(value) {
    original = structuredClone(value); dirty = false; quotaInputs = [];
    const spec = original.spec;
    const version = numberInput('editor-quick-version',original.metadata.version,1,2147483647);
    const timezone = document.createElement('input'); timezone.id='editor-quick-timezone'; timezone.value=spec.timezone; timezone.maxLength=128; timezone.required=true;
    const concurrency = numberInput('editor-quick-concurrent',spec.reservation.max_concurrent_gpus,1,1000);
    const general = [
      labeled('来源版本',version), labeled('规则时区（IANA）',timezone), labeled('总并发 GPU 上限',concurrency),
      durationField('最长预约时长','editor-quick-duration',spec.reservation.max_duration_seconds,1),
      durationField('最少提前时间','editor-quick-notice',spec.reservation.min_notice_seconds,0),
      durationField('最远提前时间','editor-quick-advance',spec.reservation.max_advance_seconds,0),
    ];
    $('editor-quick-general').replaceChildren(...general);
    const quota = spec.quota;
    const period = quota.window.kind === 'calendar_week' ? `自然周 · ${weekdays[quota.window.week_starts_on]}开始` : '滚动 7 天';
    $('editor-quick-scope').textContent = `${quota.scope === 'per_node' ? '各节点独立额度' : '全局额度'} · ${period} · 按实际 GPU 使用计时`;
    const limits = quota.scope === 'per_node' ? quota.node_limits : [{node_id:'全局',limit:quota.default_limit}];
    const rows = limits.map((item,index) => {
      const row = document.createElement('fieldset'); row.className='quick-quota';
      row.append(text('legend',item.node_id));
      const kind = document.createElement('select'); kind.id=`editor-quick-quota-${index}-kind`; kind.setAttribute('aria-label',`${item.node_id}额度类型`);
      kind.append(new Option('每人限额','limited'),new Option('不设上限','unlimited')); kind.value=item.limit.kind;
      const value = item.limit.kind === 'limited' ? item.limit.gpu_seconds : null;
      const scale = value === null ? 3600 : exactUnit(value,quotaUnits);
      const amount=numberInput(`editor-quick-quota-${index}-value`,value===null?null:value/scale,0,MAX_QUOTA);
      amount.setAttribute('aria-label',`${item.node_id}额度数值`);
      const unit=units(`editor-quick-quota-${index}-unit`,`${item.node_id}额度单位`,quotaUnits,scale);
      const amounts=text('div','','quick-value-unit'); amounts.append(amount,unit);
      row.append(kind,amounts); quotaInputs.push({kind,amount,unit,amounts,node:item.node_id}); return row;
    });
    $('editor-quick-quotas').replaceChildren(...rows);
    $('editor-quick-preserved').textContent=`保留 ${spec.role_overrides.length} 项角色覆盖、${spec.gpu_time_weights.model_weights.length} 个型号系数和原空闲规则。角色覆盖仍可能采用单独限制；额度周期和节点集合保持不变。`;
    controls();
  }
  function clear() {
    original=null;dirty=false;quotaInputs=[];
    $('editor-quick-general').replaceChildren(); $('editor-quick-quotas').replaceChildren();
    $('editor-quick-scope').textContent=''; $('editor-quick-preserved').textContent=''; controls();
  }
  function build() {
    if (!original) throw new Error('请先取得完整的已校验规则。');
    const value=structuredClone(original), reservation=value.spec.reservation;
    value.metadata.version=integer($('editor-quick-version'),1,2147483647,'来源版本');
    value.spec.timezone=$('editor-quick-timezone').value.trim();
    if (!value.spec.timezone) throw new Error('请填写规则时区。');
    reservation.max_concurrent_gpus=integer($('editor-quick-concurrent'),1,1000,'总并发 GPU');
    for (const [id,field,minimum,label] of [
      ['editor-quick-duration','max_duration_seconds',1,'最长预约时长'],
      ['editor-quick-notice','min_notice_seconds',0,'最少提前时间'],
      ['editor-quick-advance','max_advance_seconds',0,'最远提前时间'],
    ]) reservation[field]=seconds($(id),$(id+'-unit'),timeUnits,minimum,YEAR,label);
    if (reservation.min_notice_seconds>reservation.max_advance_seconds) throw new Error('最少提前时间不能超过最远提前时间。');
    quotaInputs.forEach((entry,index) => {
      let limit;
      if (entry.kind.value==='unlimited') limit={kind:'unlimited'};
      else if (entry.kind.value==='limited') limit={kind:'limited',gpu_seconds:seconds(entry.amount,entry.unit,quotaUnits,0,MAX_QUOTA,`${entry.node}额度`)};
      else throw new Error('请选择有效的额度类型。');
      if (value.spec.quota.scope==='per_node') value.spec.quota.node_limits[index].limit=limit;
      else value.spec.quota.default_limit=limit;
    });
    return value;
  }
  function edited() {
    if (!original || locked) return;
    const first=!dirty;dirty=true;controls();if(first)onDirty();
  }
  form.addEventListener('input',edited);form.addEventListener('change',edited);
  form.addEventListener('submit',event=>{
    event.preventDefault();
    run(async()=>{const value=build();source(value);onApply(value);},'正在应用常用参数…');
  });
  $('editor-quick-discard').addEventListener('click',()=>{if(original&&!locked){source(original);onDiscard();}});
  return {source,clear,isDirty:()=>dirty,setBusy(value){locked=value;controls();},focus(){ $('editor-quick-version')?.focus(); }};
};
