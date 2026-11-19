"use strict";
window.createHardwareCharts = ({el:$,text,stamp,ratio}) => {
  let values=[], busy=false;
  const ns='http://www.w3.org/2000/svg';
  function reset(){values=[];$('hardware-charts').hidden=true;$('hardware-series').replaceChildren();$('hardware-chart-note').textContent='';['utilization','memory','temperature'].forEach(name=>$(`hardware-chart-${name}`).replaceChildren());}
  function chart(series,key,title,unit,minimum,maximum,divisor=1n){
    const holder=$(`hardware-chart-${key}`);holder.replaceChildren();
    const points=series.points, numbers=points.filter(point=>point[title]!==null);
    if(!numbers.length){holder.append(text('p',key==='temperature'?'无有效温度采样':'无有效独立采样','hardware-chart-empty'));return;}
    const width=360,height=150,left=42,right=15,top=14,bottom=30,plot=height-top-bottom;
    const start=Date.parse(points[0].gpu_observed_at),end=Date.parse(points[points.length-1].gpu_observed_at);
    const svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox',`0 0 ${width} ${height}`);svg.setAttribute('role','img');svg.setAttribute('aria-label',`${numbers.length} 个有效原采样，${stamp(points[0].gpu_observed_at)} 至 ${stamp(points[points.length-1].gpu_observed_at)}，缺口处不连线`);
    function item(kind,attrs,value){const element=document.createElementNS(ns,kind);Object.entries(attrs).forEach(([name,entry])=>element.setAttribute(name,String(entry)));if(value!==undefined)element.textContent=value;svg.append(element);return element;}
    const axis=value=>key==='memory'?ratio(value.toString(),'1',divisor):value.toString();
    for(const value of [minimum,maximum]){const y=top+plot*(1-Number((value-minimum)*100000n/(maximum-minimum))/100000);item('line',{x1:left,x2:width-right,y1:y,y2:y,class:'chart-axis'});item('text',{x:left-7,y:y+3,'text-anchor':'end',class:'chart-label'},axis(value));}
    item('text',{x:left,y:height-6,'text-anchor':'start',class:'chart-label'},stamp(points[0].gpu_observed_at).slice(-8));item('text',{x:width-right,y:height-6,'text-anchor':'end',class:'chart-label'},stamp(points[points.length-1].gpu_observed_at).slice(-8));
    const unitTitle=document.createElementNS(ns,'title');unitTitle.textContent=`${unit}；仅连接相邻可用原采样，非连续使用证据。`;svg.append(unitTitle);
    let segment=[];
    function flush(){if(!segment.length)return;const line=segment.map((point,index)=>`${index?'L':'M'}${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(' ');if(segment.length>1){item('path',{d:`${line} L${segment[segment.length-1].x.toFixed(2)},${top+plot} L${segment[0].x.toFixed(2)},${top+plot} Z`,class:'hardware-chart-area'});item('path',{d:line,class:'hardware-chart-line',fill:'none'});}else{const dot=item('circle',{cx:segment[0].x,cy:segment[0].y,r:2.8,class:'hardware-chart-point'});const description=document.createElementNS(ns,'title');description.textContent=`seq ${segment[0].seq} · ${segment[0].at}`;dot.append(description);}segment=[];}
    points.forEach(point=>{if(point.break_before || point[title]===null)flush();if(point[title]===null)return;const value=BigInt(point[title]);segment.push({x:left+(Date.parse(point.gpu_observed_at)-start)/Math.max(1,end-start)*(width-left-right),y:top+plot*(1-Number((value-minimum)*100000n/(maximum-minimum))/100000),seq:point.seq,at:stamp(point.gpu_observed_at)});});flush();holder.append(svg);
  }
  function render(){const series=values[Number($('hardware-series').value)];if(!series)return;$('hardware-chart-note').textContent=series.points.length?`${series.points.length} 个独立原采样 · ${stamp(series.points[0].gpu_observed_at)} 至 ${stamp(series.points[series.points.length-1].gpu_observed_at)}；缺口和未知采样处断线。`:'没有可用于绘图的独立采样。';
    chart(series,'utilization','utilization_percent','%',0n,100n);
    chart(series,'memory','memory_used_mib','GB',0n,BigInt(series.memory_total_mib),1024n);
    const temperatures=series.points.filter(point=>point.temperature_c!==null).map(point=>point.temperature_c);
    chart(series,'temperature','temperature_c','°C',BigInt(temperatures.length?Math.min(...temperatures)-2:0),BigInt(temperatures.length?Math.max(...temperatures)+2:1));
  }
  $('hardware-series').addEventListener('change',render);
  return {reset,configure(series){if(series.length===values.length && series.every((item,index)=>item===values[index]))return;values=series;$('hardware-charts').hidden=!values.length;$('hardware-series').replaceChildren(...values.map((item,index)=>new Option(`GPU ${item.index} · ${item.model} · ${ratio(item.memory_total_mib,'1',1024n)} GB`,String(index))));$('hardware-series').disabled=busy;render();},setBusy(value){busy=value;$('hardware-series').disabled=value;}};
};
