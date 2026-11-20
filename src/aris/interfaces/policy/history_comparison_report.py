"""A standalone, escaped two-policy report retaining one original history window."""

from aris.interfaces.policy.history_comparison import PolicyHistoryComparisonView
from aris.interfaces.policy.history_report import _pairs, _seconds, _text
from aris.interfaces.policy.material_report import REPORT_CSP, STYLE
from aris.modules.nova.quota_history_schemas import HistoricalQuotaAssessment


def historical_comparison_report(value: PolicyHistoryComparisonView) -> str:
    value = PolicyHistoryComparisonView.model_validate(value.model_dump(mode="json"))
    original = value.comparison
    states = {
        "within_limit": "历史窗口内未达到限额",
        "exhausted": "历史窗口内已达到限额",
        "unlimited": "此历史规则不限额",
        "not_evaluated": "历史额度未评估",
    }

    def card(side: str, result: HistoricalQuotaAssessment) -> str:
        limit = result.limit
        return (
            f"<section><h2>{_text(side)} · 版本 {result.query.policy.version}</h2>"
            + _pairs(
                (
                    ("历史状态", states[result.state]),
                    ("按该规则折算用量", _seconds(result.used_gpu_seconds)),
                    (
                        "规则额度",
                        "未配置"
                        if limit is None
                        else "不限额"
                        if limit.kind == "unlimited"
                        else str(limit.gpu_seconds) + " GPU 秒",
                    ),
                    (
                        "历史剩余",
                        "不限额"
                        if result.state == "unlimited"
                        else _seconds(result.remaining_gpu_seconds),
                    ),
                    (
                        "额度周期",
                        result.period.start_at.isoformat()
                        + " — "
                        + result.period.end_at.isoformat(),
                    ),
                    ("原因码", result.reason),
                    ("采用的假设角色", result.selected_assumed_role or "基础规则"),
                    ("原策略指纹", result.query.expected_policy_fingerprint),
                )
            )
            + "</section>"
        )

    evidence = original.evidence
    context = _pairs(
        (
            ("同一历史节点", evidence.selection.node_id),
            (
                "原观察窗口",
                f"{evidence.window.start_at.isoformat()} — {evidence.window.end_at.isoformat()}",
            ),
            ("覆盖状态", evidence.coverage),
            ("原证据指纹", evidence.fingerprint),
            ("快照 ID", evidence.selection.snapshot_id),
            ("快照指纹", evidence.selection.expected_snapshot_fingerprint),
            ("原来源截止序号", evidence.source_ceiling),
            ("原账本修订", evidence.ledger.revision),
            ("原身份修订", evidence.identity_revision),
            ("原处理方法", evidence.configuration_fingerprint),
            ("原计时方法", evidence.timing_fingerprint),
            ("同一计算锚点", original.query.before.period_at.isoformat()),
            ("同一角色假设", ", ".join(original.query.before.assumed_roles) or "未提供"),
            ("实现引用", value.implementation_ref),
            ("本次对照指纹", value.context_fingerprint),
        )
    )
    changes = "".join(
        "<tr><th>"
        + _text(item.path)
        + "</th><td>"
        + _text(item.before if item.before_exists else "未配置")
        + "</td><td>"
        + _text(item.after if item.after_exists else "未配置")
        + "</td></tr>"
        for item in original.difference.changes
    )
    changes = changes or '<tr><td colspan="3">两个版本的规则内容相同。</td></tr>'
    conclusion = (
        "至少一侧历史额度未知，不判断状态是否改变。"
        if value.quota_state_changed is None
        else "两侧历史额度状态不同。"
        if value.quota_state_changed
        else "两侧历史额度状态相同；用量、额度和规则差异请分别核对。"
    )
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta http-equiv="Content-Security-Policy" content="{_text(REPORT_CSP)}">'
        f"<title>{_text(original.query.before.policy.name)} · 历史双版本对照</title>"
        f"<style>{STYLE}.comparison-pair{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}"
        ".comparison-pair>section{min-width:0}@media(max-width:640px){"
        ".comparison-pair{grid-template-columns:1fr}}"
        "@media print{.comparison-pair{display:block}}</style></head><body><main>"
        '<p class="eyebrow">ARIS / HISTORICAL COMPARISON</p><h1>同一历史 · 双版本对照</h1>'
        '<p class="notice">同一份原历史证据用于两个策略版本。角色是手动假设；'
        "当前依赖未评估，不批准、发布策略或授权预约。</p>"
        f'<p>{_text(conclusion)}</p><div class="comparison-pair">'
        + card("基准", original.before)
        + card("对照", original.after)
        + '</div><section><h2>规则差异</h2><div class="scroll"><table><thead>'
        "<tr><th>规则路径</th><th>基准版本</th><th>对照版本</th></tr></thead><tbody>"
        + changes
        + "</tbody></table></div></section><section><h2>同一固定历史与计算依据</h2>"
        + context
        + "</section><section><details><summary>精确数值与完整原始对照</summary><pre>"
        + _text(value.canonical_comparison_json)
        + "</pre></details></section></main></body></html>"
    )
