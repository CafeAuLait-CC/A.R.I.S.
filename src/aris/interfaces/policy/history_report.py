"""An escaped, standalone view of one authorized immutable historical sample."""

import json
from decimal import ROUND_HALF_UP, Decimal, localcontext
from html import escape

from aris.interfaces.policy.history import PolicyHistoryReviewView, PolicyStoredHistoryView
from aris.interfaces.policy.material_report import REPORT_CSP, STYLE
from aris.modules.nova.policy_simulation import ExactGpuSeconds


def _text(value: object) -> str:
    return escape(str(value), quote=True)


def _pairs(values: tuple[tuple[str, object], ...]) -> str:
    return (
        "<dl>"
        + "".join(f"<dt>{_text(label)}</dt><dd>{_text(value)}</dd>" for label, value in values)
        + "</dl>"
    )


def _amount(numerator: int, denominator: int) -> str:
    with localcontext() as context:
        context.prec = 80
        context.rounding = ROUND_HALF_UP
        value = Decimal(numerator) / Decimal(denominator)
        rounded = f"{value:.3f}"
        return "<0.001" if numerator and rounded == "0.000" else rounded.rstrip("0").rstrip(".")


def _seconds(value: ExactGpuSeconds | None) -> str:
    return "未知" if value is None else _amount(value.numerator, value.denominator) + " GPU 秒"


def historical_review_report(
    value: PolicyHistoryReviewView, *, saved: PolicyStoredHistoryView | None = None
) -> str:
    value = PolicyHistoryReviewView.model_validate(value.model_dump(mode="json"))
    saved_header = ""
    if saved is not None:
        saved = PolicyStoredHistoryView.model_validate(saved.model_dump(mode="json"))
        if saved.context != value:
            raise ValueError("saved historical report differs from its original context")
        saved_header = (
            "<section><h2>已保存审核记录</h2>"
            + _pairs(
                (
                    ("记录 ID", saved.id),
                    ("保存于", saved.created_at.isoformat()),
                    ("作者 ID", saved.created_by),
                    ("保存原因", saved.reason),
                    ("上下文指纹", saved.context.context_fingerprint),
                    ("记录指纹", saved.fingerprint),
                )
            )
            + "</section>"
        )
    review, evidence = value.review, value.review.evidence
    result = review.assessment
    states = {
        "within_limit": "历史窗口内未达到限额",
        "exhausted": "历史窗口内已达到限额",
        "unlimited": "该历史规则不设限额",
        "not_evaluated": "历史额度未评估",
    }
    coverage = {"complete": "该历史窗口覆盖完整", "partial": "部分覆盖", "unknown": "覆盖未知"}
    source = result.query.policy
    reasons = {
        "GLOBAL_QUOTA_REQUIRES_ALL_NODE_EVIDENCE": "全局额度需要全部节点的完整证据。",
        "QUOTA_SCOPE_UNCONFIGURED": "此规则未配置该节点的额度。",
        "USAGE_METHOD_MISMATCH": "历史快照的计时或处理方法与审核要求不同。",
        "USAGE_WINDOW_MISMATCH": "历史窗口未完整覆盖相应额度周期。",
        "USAGE_COVERAGE_INCOMPLETE": "来源存在未知或未覆盖区间，完整实际用量仍未知。",
        "QUOTA_UNLIMITED": "此规则不设使用额度上限。",
        "WITHIN_RECORDED_HISTORICAL_LIMIT": "原截止时的历史用量未达到规则限额。",
        "HISTORICAL_QUOTA_EXHAUSTED": "原截止时的历史用量已达到规则限额。",
    }
    overview = _pairs(
        (
            ("策略来源", f"{source.name} · 版本 {source.version}"),
            ("历史节点", evidence.selection.node_id),
            (
                "观察窗口",
                f"{evidence.window.start_at.isoformat()} — {evidence.window.end_at.isoformat()}",
            ),
            ("窗口时区", evidence.window.timezone),
            ("原采样截止", result.sampled_until.isoformat()),
            ("历史结论", states[result.state]),
            ("覆盖状态", coverage[evidence.coverage]),
            ("结论原因", reasons.get(result.reason, "请在原始上下文中核对原因。")),
        )
    )
    limit = result.limit
    totals = _pairs(
        (
            ("按规则折算用量", _seconds(result.used_gpu_seconds)),
            (
                "规则额度",
                "未配置"
                if limit is None
                else "不限额"
                if limit.kind == "unlimited"
                else str(limit.gpu_seconds) + " GPU 秒",
            ),
            (
                "历史剩余额度",
                "不限额" if result.state == "unlimited" else _seconds(result.remaining_gpu_seconds),
            ),
            (
                "额度周期",
                f"{result.period.start_at.isoformat()} — {result.period.end_at.isoformat()}",
            ),
            ("角色假设", ", ".join(result.query.assumed_roles) or "未提供，采用基础规则"),
            ("角色来源", "操作者手动假设；未验证历史真实角色"),
        )
    )
    if evidence.models is None:
        models = '<p class="muted">完整型号用量仍未知，保留原覆盖缺口。</p>'
    else:
        rows = "".join(
            f"<tr><th>{_text(item.model)}</th><td>{item.observed_microseconds}</td>"
            f"<td>{_text(_amount(item.observed_microseconds, 1_000_000))} GPU 秒</td></tr>"
            for item in evidence.models
        )
        models = (
            '<div class="scroll"><table><thead><tr><th>原型号</th><th>原微秒</th>'
            "<th>未加权驻留时长</th></tr></thead><tbody>"
            + (rows or '<tr><td colspan="3">该完整历史窗口没有已计量用量。</td></tr>')
            + "</tbody></table></div>"
        )
    provenance = _pairs(
        (
            ("快照 ID", evidence.selection.snapshot_id),
            ("快照指纹", evidence.selection.expected_snapshot_fingerprint),
            ("真实历史证据指纹", evidence.fingerprint),
            ("原始来源截止序号", evidence.source_ceiling),
            ("处理来源前缀完整", "是" if evidence.source_prefix_complete else "否"),
            (
                "账本固定版本",
                json.dumps(evidence.ledger.model_dump(mode="json"), ensure_ascii=False),
            ),
            ("实际处理方法指纹", evidence.configuration_fingerprint),
            ("核对要求的方法指纹", result.query.expected_configuration_fingerprint),
            ("实际时序规则指纹", evidence.timing_fingerprint),
            ("核对要求的时序指纹", result.query.expected_timing_fingerprint),
            (
                "历史身份修订",
                evidence.identity_revision if evidence.identity_revision is not None else "未知",
            ),
            ("计量起算", evidence.recording_start_at.isoformat()),
            ("快照读取于", evidence.captured_at.isoformat()),
            ("快照保存于", evidence.created_at.isoformat()),
            ("策略来源指纹", result.query.expected_policy_fingerprint),
            ("审核运行实现引用", value.implementation_ref),
        )
    )
    raw = _text(
        saved.canonical_record_json
        if saved is not None
        else (
            '{"implementation_ref": '
            + json.dumps(value.implementation_ref, ensure_ascii=False)
            + ', "review": '
            + value.canonical_review_json
            + "}"
        )
    )
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta http-equiv="Content-Security-Policy" content="{_text(REPORT_CSP)}">'
        f"<title>{_text(source.name)} · 历史证据审核</title><style>{STYLE}</style></head>"
        '<body><main><p class="eyebrow">ARIS / HISTORICAL REVIEW</p>'
        '<h1>真实历史样本审核</h1><p class="notice">用于固定历史窗口的证据核对。'
        "当前角色、维护和运行依赖未评估；尚未批准、发布或授权预约。</p>"
        + overview
        + saved_header
        + "<section><h2>历史额度核对</h2>"
        + totals
        + "</section>"
        + "<section><h2>原型号驻留时长</h2>"
        + models
        + "</section>"
        + "<section><h2>固定来源与处理依据</h2>"
        + provenance
        + "</section>"
        + "<section><details><summary>完整原始审核上下文</summary><pre>"
        + raw
        + "</pre></details></section></main></body></html>"
    )
