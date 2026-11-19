"use strict";
window.createHardwarePreview = ({el:$,text,stamp}) => {
  let report=null, offset=0, target='', busy=false;
  const size=32;
  const names={GPU_COMPONENT_STALE:'GPU 采样陈旧',GPU_COMPONENT_UNAVAILABLE:'GPU 采集不可用',SOURCE_CLOCK_REGRESSION:'时钟回拨',SAME_TIME_HARDWARE_CONFLICT:'同刻硬件事实冲突'};
  function ratio(numerator,denominator,divisor=1n){const n=BigInt(numerator),d=BigInt(denominator)*divisor,absolute=n<0n?-n:n,rounded=(absolute*20n+d)/(2n*d);return `${n<0n && rounded!==0n?'-':''}${rounded/10n}.${rounded%10n}`;}
  function statistics(value,unit,divisor=1n){if(!value.samples)return '—';return `${ratio(value.minimum,'1',divisor)} / ${ratio(value.mean_numerator,value.mean_denominator,divisor)} / ${ratio(value.maximum,'1',divisor)} ${unit}`;}
  const charts=window.createHardwareCharts({el:$,text,stamp,ratio});
  function reset(){charts.reset();report=null;offset=0;target='';$('activity-hardware').hidden=true;$('activity-hardware').open=false;$('hardware-rows').replaceChildren();['counts','quality','page-note'].forEach(id=>$(`hardware-${id}`).textContent='');}
  function rows(){if(!report)return;const values=report.series.filter(item=>!target || item.gpu_uuid===target),page=values.slice(offset,offset+size);charts.configure(values);
    $('hardware-rows').replaceChildren(...page.map(series=>{const row=document.createElement('tr'),gpu=text('td',`GPU ${series.index}`);gpu.append(text('small',series.model),text('small',`${ratio(series.memory_total_mib,'1',1024n)} GB 总显存`));
      const utilization=text('td',statistics(series.utilization_percent,'%')),memory=text('td',statistics(series.memory_used_mib,'GB',1024n)),temperature=text('td',statistics(series.temperature_c,'°C'));temperature.append(text('small',`${series.temperature_c.samples} 次有温度 · ${series.missing_temperature_samples} 次未知`));
      const samples=text('td',`${series.independent_samples} 次独立采样`);samples.append(text('small',`${series.cached_envelopes} 次缓存 · ${series.rejected_envelopes} 次剔除`));if(series.first_gpu_observed_at)samples.append(text('small',`${stamp(series.first_gpu_observed_at)} → ${stamp(series.last_gpu_observed_at)}`));row.append(gpu,utilization,memory,temperature,samples);return row;}));
    $('hardware-empty').hidden=values.length!==0;$('hardware-page-note').textContent=values.length?`第 ${offset+1}–${offset+page.length} 个系列，共 ${values.length} 个`:'0 个 GPU 样本系列';$('hardware-previous').disabled=busy || offset===0;$('hardware-next').disabled=busy || offset+size>=values.length;
  }
  function render(value){reset();if(!value)return;report=value;$('activity-hardware').hidden=false;$('hardware-counts').textContent=`${value.independent_frames} 个独立帧 · ${value.cached_frames} 个缓存帧 · ${value.rejected_frames} 个不可用帧`;
    const rejected=Object.entries(value.rejection_reasons).map(([reason,count])=>`${names[reason] || reason} ${count}`);$('hardware-quality').textContent=`最小 / 平均 / 最大。均值按独立样本计算，不按持续时间加权。${rejected.length?`剔除原因：${rejected.join('；')}。`:''}${value.no_target_frames?`有 ${value.no_target_frames} 个帧未报告 GPU 指标。`:''}`;rows();
  }
  $('hardware-previous').addEventListener('click',()=>{offset=Math.max(0,offset-size);rows();});$('hardware-next').addEventListener('click',()=>{offset+=size;rows();});
  return {reset,render,filter(value){if(target!==value){target=value;offset=0;}rows();},setBusy(value){busy=value;charts.setBusy(value);rows();}};
};
