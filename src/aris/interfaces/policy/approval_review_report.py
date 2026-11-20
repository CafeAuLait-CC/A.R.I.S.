"""Standalone approval metadata report; no other person's usage detail."""

from datetime import UTC, datetime
from html import escape

from aris.modules.nova.policy_approval_review_schemas import PolicyApprovalReview

STATE_LABELS = {
    "counted": "计入",
    "superseded": "已被更新记录替代",
    "revoked": "已撤销",
    "expired": "已到期",
    "different_requirements": "审核要求不同",
    "approver_unavailable": "当前资格或身份不可用",
}


def approval_review_report(value: PolicyApprovalReview) -> str:
    def stamp(time: datetime) -> str:
        return escape(time.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"))

    outcomes = {
        "minimum_met": "已达到配置人数要求",
        "minimum_not_met": "尚未达到配置人数要求",
        "unknown": "其余记录未核对，人数结果待定",
    }
    rows = "".join(
        "<tr><td>"
        + escape(item.approver_label or str(item.approval.approved_by))
        + "</td><td>"
        + stamp(item.approval.approved_at)
        + "</td><td>"
        + escape(STATE_LABELS[item.state])
        + "</td><td class='id'>"
        + escape(str(item.approval.id))
        + "</td></tr>"
        for item in value.entries
    )
    count = ("至少 " if value.truncated else "") + str(value.qualifying_signers)
    return (
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>审批人数核对 · ARIS</title><style>"
        "*{box-sizing:border-box}body{font:15px/1.65 system-ui,sans-serif;color:#213047;"
        "margin:36px auto;padding:0 22px;max-width:1000px}"
        "h1{font-size:28px;margin-bottom:8px}.muted{color:#63738b}"
        ".summary{background:#f1f6fd;border:1px solid #dce5ef;border-radius:10px;"
        "padding:20px;margin:24px 0}table{width:100%;border-collapse:collapse;table-layout:fixed}"
        "th,td{text-align:left;vertical-align:top;border-bottom:1px solid #dce5ef;"
        "padding:12px 8px;overflow-wrap:anywhere}th{color:#56667d}"
        "td.id,code{font:12px/1.6 ui-monospace,monospace;overflow-wrap:anywhere}"
        "dl{display:grid;grid-template-columns:130px 1fr;gap:8px 16px}"
        "dt{color:#63738b}dd{margin:0;overflow-wrap:anywhere}"
        "@media(max-width:600px){body{margin:20px auto;padding:0 14px}h1{font-size:23px}"
        "dl{grid-template-columns:1fr;gap:2px}dd{margin-bottom:12px}"
        "th,td{font-size:12px;padding:9px 4px}.summary{padding:14px}}"
        "@media print{body{margin:0;max-width:none;font-size:11px}"
        ".summary,tr{break-inside:avoid}@page{size:A4;margin:12mm}}"
        "</style><h1>审批人数核对</h1><p class='muted'>"
        + escape(value.query.source.name)
        + " · 版本 "
        + str(value.query.source.version)
        + " · 导出时重新核对</p>"
        + "<section class='summary'><strong>"
        + escape(outcomes[value.outcome])
        + "</strong><p>计入 "
        + escape(count)
        + " 人 / 要求 "
        + str(value.requirements.minimum_distinct_approvers)
        + " 位不同审批人。</p>"
        + "<p>每人仅考虑其最新记录；最新撤销或到期后，较早记录不会重新计入。"
        "策略尚未发布，运行依赖待单独核对。</p></section>"
        + (
            "<p class='muted'>本次仅核对最新128条记录；上述人数是已核对部分的下界。</p>"
            if value.truncated
            else ""
        )
        + "<table><thead><tr><th>审批人</th><th>原批准时间</th><th>计数状态</th>"
        "<th>原批准 ID</th></tr></thead><tbody>"
        + (rows or "<tr><td colspan='4'>此来源暂无批准记录。</td></tr>")
        + "</tbody></table>"
        + "<h2>核对依据</h2><dl><dt>读取时间</dt><dd>"
        + stamp(value.as_of)
        + "</dd><dt>来源指纹</dt><dd><code>"
        + escape(value.query.expected_source_fingerprint)
        + "</code></dd><dt>当前要求</dt><dd><code>"
        + escape(value.requirements.fingerprint)
        + "</code></dd><dt>结果指纹</dt><dd><code>"
        + escape(value.fingerprint)
        + "</code></dd></dl><p class='muted'>本报告只包含审批元数据；"
        "个人历史用量与批准正文留在各自权限范围内。读取结果不授予发布权限。</p></html>"
    )
