"""Self-contained, escaped presentation of an authorized stored-case comparison."""

import json
from html import escape

from aris.interfaces.policy.materials import (
    PolicyExampleResultView,
    PolicyMaterialComparisonView,
)

REPORT_CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
GRADES = {
    "WOULD_ALLOW": "假设可通过",
    "WOULD_DENY": "假设被拒绝",
    "NOT_EVALUATED": "假设未评估",
}
RULES = {
    "reservation.max_duration": "预约时长",
    "reservation.min_notice": "至少提前",
    "reservation.max_advance": "最远提前",
    "reservation.concurrent": "总并发 GPU",
    "quota.actual_usage": "假设使用额度",
}
STATES = {"pass": "符合", "deny": "超出", "unknown": "未知"}
STYLE = """
:root{color-scheme:light}*{box-sizing:border-box}body{margin:0;background:#f5f7fa;
color:#182535;font:15px/1.6 system-ui,sans-serif}main{max-width:1080px;margin:36px auto;
padding:32px;background:white;border:1px solid #dce3ec;border-radius:14px}
h1{font-size:28px;margin:6px 0}h2{font-size:21px;margin:0 0 14px}h3{font-size:16px}
p{margin:8px 0}.eyebrow,.muted,dt{color:#617084}.eyebrow{font-size:12px;letter-spacing:.08em}
.notice{background:#edf3fa;border-left:3px solid #6284ab;padding:14px 18px;margin:22px 0}
section{margin-top:30px;padding-top:24px;border-top:1px solid #e1e6ed}
dl{display:grid;grid-template-columns:160px minmax(0,1fr);gap:8px 16px}dd{margin:0}
dd,td,th,pre{overflow-wrap:anywhere}.scroll{overflow-x:auto;max-width:100%}
table{width:100%;border-collapse:collapse;text-align:left;font-size:13px}
th,td{padding:10px 12px;border-bottom:1px solid #e3e8ef;vertical-align:top}
thead{background:#f4f7fb}tbody th{font-weight:500}.overview{min-width:560px}
.columns{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}
.card{min-width:0;padding:16px;border:1px solid #dce3ec;border-radius:10px}
.card h3{margin:0 0 12px}.card table th,.card table td{padding:8px 4px}
.card small{display:block;color:#617084;font-size:11px}.grade{font-weight:650}
pre{white-space:pre-wrap;word-break:break-word;font:12px/1.6 ui-monospace,monospace;
background:#f5f7fa;padding:14px;border-radius:8px}summary{cursor:pointer;color:#365c86}
.pass{color:#247653}.deny{color:#a33d36}.unknown{color:#856019}
@media(max-width:800px){main{margin:12px;padding:20px}.columns{grid-template-columns:1fr}
dl{grid-template-columns:1fr;gap:2px}dd{margin-bottom:9px}h1{font-size:24px}}
@media print{body{background:white;font-size:10pt}main{margin:0;padding:0;border:0;
max-width:none}.columns{display:block}.card{margin-bottom:10px}details{display:none}
section{break-inside:auto}h2,h3{break-after:avoid}.scroll{overflow:visible}}
"""


def _text(value: object) -> str:
    return escape(str(value), quote=True)


def _change_value(value: object, exists: bool) -> str:
    return _text(json.dumps(value, ensure_ascii=False) if exists else "未设置")


def _pairs(values: tuple[tuple[str, object], ...]) -> str:
    return (
        "<dl>"
        + "".join(f"<dt>{_text(name)}</dt><dd>{_text(value)}</dd>" for name, value in values)
        + "</dl>"
    )


def _result(title: str, value: PolicyExampleResultView) -> str:
    rows = "".join(
        f'<tr><th>{RULES[item.rule_id]}</th><td class="{item.state}">'
        f"{STATES[item.state]}<small>{_text(item.reason)}</small></td></tr>"
        for item in value.results
    )
    amount = value.assumed_used_gpu_seconds
    usage = f"{amount.numerator} / {amount.denominator} GPU 秒" if amount else "未知"
    limit = value.quota_limit
    quota = (
        f"{limit.gpu_seconds} GPU 秒"
        if limit is not None and limit.kind == "limited"
        else "不设上限"
        if limit is not None
        else "未配置"
    )
    return (
        f'<article class="card"><h3>{_text(title)}</h3>'
        f'<p class="grade">{GRADES[value.decision]}</p>'
        f'<table aria-label="{_text(title)}约束"><tbody>{rows}</tbody></table>'
        f"<p>假设加权用量：{_text(usage)}</p><p>规则额度：{_text(quota)}</p>"
        f'<p class="muted">假设角色覆盖：{_text(value.selected_role_override or "未命中")}</p>'
        "</article>"
    )


def comparison_report(value: PolicyMaterialComparisonView) -> str:
    """Render server-loaded inputs; no browser round trip or floating-point conversion."""
    material = value.material
    changed = sum(case.decision_changed for case in value.cases)
    matches = sum(case.stored_replay_matches for case in value.cases)
    overview = "".join(
        f"<tr><th>{_text(case.scenario.name)}</th>"
        f"<td>{GRADES[case.recorded_result.decision]}</td>"
        f"<td>{GRADES[case.before_result.decision]}</td>"
        f"<td>{GRADES[case.after_result.decision]}</td>"
        f"<td>{'一致' if case.stored_replay_matches else '不一致'}</td></tr>"
        for case in value.cases
    )
    differences = "".join(
        f"<tr><th>{_text(change.path)}</th>"
        f"<td>{_change_value(change.before, change.before_exists)}</td>"
        f"<td>{_change_value(change.after, change.after_exists)}</td></tr>"
        for change in value.difference.changes
    )
    cases = []
    for index, case in enumerate(value.cases, 1):
        scenario = case.scenario
        facts = _pairs(
            (
                ("假设用户 / 节点", f"{scenario.user_key} / {scenario.node_id}"),
                ("原参考时刻 UTC", scenario.as_of.isoformat()),
                (
                    "假设预约 UTC",
                    f"{scenario.request.start_at.isoformat()} — "
                    f"{scenario.request.end_at.isoformat()}",
                ),
                (
                    "假设总并发",
                    scenario.projected_concurrent_gpus
                    if scenario.projected_concurrent_gpus is not None
                    else "未知",
                ),
                ("假设角色", ", ".join(scenario.assumed_roles) or "未提供"),
            )
        )
        cards = (
            _result("保存时结果", case.recorded_result)
            + _result(f"原版本 {material.source.version} · 当前实现重演", case.before_result)
            + _result(f"目标版本 {value.target.version} · 当前实现重演", case.after_result)
        )
        rules = ", ".join(RULES[rule] for rule in case.changed_rules) or "无"
        cases.append(
            f"<section><h2>{index}. {_text(scenario.name)}</h2>{facts}"
            f'<p class="muted">约束状态变化：{_text(rules)}</p><div class="columns">{cards}</div>'
            f"<details><summary>完整原始假设</summary><pre>{_text(case.raw_scenario)}</pre></details></section>"
        )
    proof = _pairs(
        (
            ("材料 ID", material.id),
            ("材料指纹", material.fingerprint),
            ("材料保存时间 UTC", material.created_at.isoformat()),
            ("保存实现引用", material.implementation_ref),
            ("重演实现引用", value.comparison_implementation_ref),
            ("原来源", f"{material.source.name} v{material.source.version} · {material.source_id}"),
            ("原来源指纹", material.source_fingerprint),
            ("目标来源", f"{value.target.name} v{value.target.version} · {value.target_source_id}"),
            ("目标来源指纹", value.target_source_fingerprint),
        )
    )
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta http-equiv="Content-Security-Policy" content="{escape(REPORT_CSP, quote=True)}">'
        '<meta name="referrer" content="no-referrer"><title>ARIS · 策略案例比较</title>'
        f"<style>{STYLE}</style></head><body><main>"
        '<p class="eyebrow">ARIS · POLICY REVIEW</p><h1>策略案例比较</h1>'
        f'<p class="muted">{_text(material.source.name)} · '
        f"版本 {material.source.version} → {value.target.version}</p>"
        '<div class="notice">全部输入来自已保存的手动假设，沿用原参考时间。'
        "未评估当前真实依赖，未批准或发布，不产生预约、账本或配额变更。</div>"
        f"<p>{len(value.cases)} 个原案例 · 保存结果重演一致 {matches}/{len(value.cases)}"
        f" · 目标决策变化 {changed} 个</p>"
        '<div class="scroll"><table class="overview"><thead><tr><th>案例</th><th>保存时结果</th>'
        "<th>原版本重演</th><th>目标版本重演</th><th>原结果重现</th></tr></thead>"
        f"<tbody>{overview}</tbody></table></div><section><h2>来源差异</h2>"
        + (
            '<div class="scroll"><table><thead><tr><th>路径</th><th>原版本</th><th>目标版本</th>'
            f"</tr></thead><tbody>{differences}</tbody></table></div>"
            if differences
            else "<p>两个来源的语义内容相同。</p>"
        )
        + "</section>"
        + "".join(cases)
        + f"<section><h2>实现与来源依据</h2>{proof}"
        '<p class="muted">实现引用是配置标识，不是独立代码校验。'
        "本文件是导出时只读重演的呈现，不是批准凭证；导出后不会自动更新。</p>"
        "</section></main></body></html>"
    )
