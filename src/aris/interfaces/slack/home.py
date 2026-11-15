"""Native Slack layout adapted from the user's sample, using typed Vega facts."""

import hashlib
import json
import math
from typing import Any, Literal

from aris.interfaces.slack.presentation import escape, markdown, plain, slack_time
from aris.interfaces.slack.vram_assets import asset_name
from aris.modules.vega.resource_schemas import NodeResourceStatus, ResourceStatus
from aris.modules.vega.schemas import GpuStatus

DETAILS_ACTION = "gpu_details:"
HomeLayout = Literal["two-column", "single-column"]


def section(text: str, accessory: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "section", "text": markdown(text)}
    if accessory is not None:
        result["accessory"] = accessory
    return result


def context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [markdown(text)]}


def button(label: str, action_id: str, value: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "button", "text": plain(label), "action_id": action_id}
    if value is not None:
        result["value"] = value
    return result


def gpu_current(entry: NodeResourceStatus, gpu: GpuStatus) -> bool:
    return (
        entry.node.status == "ONLINE"
        and entry.node.telemetry_status == "CURRENT"
        and gpu.present
        and gpu.observed_state != "unknown"
    )


def state_text(entry: NodeResourceStatus, gpu: GpuStatus) -> str:
    if not gpu_current(entry, gpu):
        return "未检测到设备" if not gpu.present else "观测未知"
    return "观测占用" if gpu.observed_state == "busy" else "观测空闲"


def memory_percent(entry: NodeResourceStatus, gpu: GpuStatus) -> int | None:
    if not gpu_current(entry, gpu):
        return None
    return min(100, max(0, math.floor(100 * gpu.memory_used_mib / gpu.memory_total_mib + 0.5)))


def memory_used(gpu: GpuStatus) -> str:
    return (
        f"{gpu.memory_used_mib} MiB"
        if gpu.memory_used_mib < 1024
        else f"{gpu.memory_used_mib / 1024:.1f} GiB"
    )


def owner_text(entry: NodeResourceStatus, gpu: GpuStatus) -> str:
    compute = entry.compute if entry.compute_freshness == "CURRENT" else None
    owner = compute.ownership if compute is not None and compute.quality == "ok" else None
    if owner is None or owner.quality == "unsupported":
        return "用户归属未提供"
    users = [
        user
        for user in owner.users
        if any(part.gpu_uuid == gpu.gpu_uuid and part.reported_processes for part in user.gpus)
    ]
    if not users:
        return "用户归属未知"
    names = [
        f"{escape(user.username[:32])}（UID {user.uid}）" if user.username else f"UID {user.uid}"
        for user in users[:2]
    ]
    if len(users) > 2:
        names.append(f"另 {len(users) - 2} 个 UID")
    return "Linux " + "、".join(names)


def disk_text(entry: NodeResourceStatus) -> str:
    # The disk task is being developed independently. Public optional fields
    # remain absent on existing snapshots; absence is never a zero-capacity disk.
    freshness = getattr(entry, "disks_freshness", "MISSING")
    observation = getattr(entry, "disks", None)
    if entry.node.status != "ONLINE" or freshness != "CURRENT" or observation is None:
        return "磁盘 —"
    if observation.quality != "ok" or observation.truncated:
        return "磁盘 部分未知" if observation.quality == "partial" else "磁盘 —"
    volumes = observation.volumes
    if not volumes or any(v.quality != "ok" for v in volumes):
        return "磁盘 —"
    total = sum(v.total_bytes for v in volumes)
    used = sum(v.used_bytes for v in volumes)
    unit, divisor = ("TiB", 2**40) if total >= 2**40 else ("GiB", 2**30)
    return f"磁盘 {used / divisor:.1f} / {total / divisor:.1f} {unit}（{len(volumes)}卷）"


def system_text(entry: NodeResourceStatus) -> str:
    system = (
        entry.system
        if (entry.node.status == "ONLINE" and entry.system_freshness == "CURRENT")
        else None
    )
    cpu = "CPU —"
    memory = "RAM —"
    if system is not None:
        if system.cpu.quality == "ok" and system.cpu.utilization_percent is not None:
            cpu = f"CPU {system.cpu.utilization_percent:.0f}%"
        elif system.cpu.quality == "warming":
            cpu = "CPU 建立基线"
        ram = system.memory
        if ram.quality == "ok" and ram.total_bytes is not None and ram.available_bytes is not None:
            memory = (
                f"RAM {(ram.total_bytes - ram.available_bytes) / 2**30:.1f} / "
                f"{ram.total_bytes / 2**30:.1f} GiB"
            )
    return f"{cpu} · {memory} · {disk_text(entry)}"


def device_blocks(
    entry: NodeResourceStatus, gpu: GpuStatus, asset_base_url: str | None
) -> list[dict[str, Any]]:
    percent = memory_percent(entry, gpu)
    metric = (
        "*显存未知*"
        if percent is None
        else (f"*VRAM {percent}%* · {memory_used(gpu)} / {gpu.memory_total_mib / 1024:.1f} GiB")
    )
    state = state_text(entry, gpu)
    if gpu_current(entry, gpu) and gpu.observed_state == "busy":
        state += " · " + owner_text(entry, gpu)
    heading = section(f"*GPU {gpu.index} · {escape(gpu.model)}*\n{state}\n\n{metric}")
    blocks = [heading]
    if asset_base_url:
        blocks.append(
            {
                "type": "image",
                "image_url": f"{asset_base_url.rstrip('/')}/{asset_name(percent)}",
                "alt_text": f"{entry.node.node_id} GPU {gpu.index}: VRAM "
                + ("未知" if percent is None else f"{percent}%"),
            }
        )
    if percent is None:
        footer = "当前指标不可用"
    else:
        free = (gpu.memory_total_mib - gpu.memory_used_mib) / 1024
        temperature = "温度未知" if gpu.temperature_c is None else f"{gpu.temperature_c} °C"
        footer = f"剩余 {free:.1f} GiB · GPU {gpu.utilization_percent}% · {temperature}"
    selection = json.dumps(
        {"node_id": entry.node.node_id, "gpu_uuid": gpu.gpu_uuid},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    action_id = DETAILS_ACTION + hashlib.sha256(selection.encode()).hexdigest()[:16]
    blocks.append(section(footer, button("详情", action_id, selection)))
    return blocks


def paired_layout(
    entry: NodeResourceStatus, pair: list[GpuStatus], asset_base_url: str | None
) -> dict[str, Any]:
    devices = [device_blocks(entry, gpu, asset_base_url) for gpu in pair]
    return {
        "type": "layout",
        "label": (entry.node.node_id + " GPU 观测")[:150],
        "is_label_visible": False,
        "grid_lines": "none",
        "rows": [
            {"items": [{"width": "standard", "block": device[row]} for device in devices]}
            for row in range(len(devices[0]))
        ],
        "fallback": [block for device in devices for block in device],
    }


def block_cost(block: dict[str, Any]) -> int:
    if block["type"] != "layout":
        return 1
    nested = sum(block_cost(item["block"]) for row in block["rows"] for item in row["items"])
    fallback = sum(block_cost(child) for child in block["fallback"])
    return max(1 + nested, fallback)


def home_view(
    snapshot: ResourceStatus,
    *,
    asset_base_url: str | None = None,
    layout: HomeLayout = "two-column",
) -> dict[str, Any]:
    if layout not in {"two-column", "single-column"}:
        raise ValueError("unsupported Home layout")
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": plain("硬件资源")},
        section("快照更新于 " + slack_time(snapshot.generated_at), button("刷新", "refresh_home")),
    ]
    cost = len(blocks)
    truncated = False
    health = {"ONLINE": "在线", "STALE": "活动陈旧", "OFFLINE": "离线"}
    quality = {
        "CURRENT": "采样新鲜",
        "STALE": "采样陈旧",
        "ERROR": "采样异常",
        "MISSING": "尚无采样",
    }
    for entry in snapshot.nodes:
        node = entry.node
        counts = {
            state: sum(gpu_current(entry, gpu) and gpu.observed_state == state for gpu in node.gpus)
            for state in ("idle", "busy")
        }
        unknown = len(node.gpus) - counts["idle"] - counts["busy"]
        node_blocks: list[dict[str, Any]] = [
            {"type": "divider"},
            {"type": "header", "text": plain(node.node_id[:150])},
            section(
                f"*{counts['idle']} GPU 观测空闲* · {counts['busy']} 占用 · {unknown} 未知"
                f"\n{health[node.status]} · {quality[node.telemetry_status]}"
            ),
            context(system_text(entry)),
            context(
                "GPU "
                + slack_time(node.observed_at)
                + " · 系统 "
                + slack_time(entry.system_observed_at)
                + " · 心跳 "
                + slack_time(node.heartbeat_at)
            ),
        ]
        disk_stamp = getattr(entry, "disks_observed_at", None)
        if cost + len(node_blocks) + bool(disk_stamp is not None) + 3 > 98:
            truncated = True
            break
        blocks.extend(node_blocks)
        cost += len(node_blocks)
        if disk_stamp is not None:
            blocks.append(context("磁盘采样 " + slack_time(disk_stamp) + " · 约每小时查询"))
            cost += 1
        if not node.gpus:
            blocks.append(context("尚无 GPU 清单。"))
            cost += 1
        stride = 2 if layout == "two-column" else 1
        for index in range(0, len(node.gpus), stride):
            pair = node.gpus[index : index + stride]
            unit = (
                [paired_layout(entry, pair, asset_base_url)]
                if len(pair) == 2
                else device_blocks(entry, pair[0], asset_base_url)
            )
            unit_cost = sum(block_cost(block) for block in unit)
            if cost + unit_cost > 98:
                truncated = True
                break
            blocks.extend(unit)
            cost += unit_cost
        if truncated:
            break
    if not snapshot.nodes:
        blocks.append(context("尚无节点观测数据。"))
    if truncated:
        blocks.append(context("已达页面展示上限，部分节点或 GPU 未显示；完整清单请查看 WebUI。"))
    blocks.append(
        context(
            "状态以快照生成时为准，点击刷新获取最新观测。"
            "Linux 用户未关联 Slack 身份；预约可用性尚未评估。"
        )
    )
    return {"type": "home", "blocks": blocks}


def loading_details_view() -> dict[str, Any]:
    return {
        "type": "modal",
        "title": plain("GPU 详情"),
        "close": plain("关闭"),
        "blocks": [section("正在读取最新资源观测…")],
    }


def unavailable_details_view(message: str) -> dict[str, Any]:
    result = loading_details_view()
    result["blocks"] = [section(message)]
    return result


def details_view(entry: NodeResourceStatus, gpu: GpuStatus) -> dict[str, Any]:
    percent = memory_percent(entry, gpu)
    known = percent is not None
    metric = f"{memory_used(gpu)} / {gpu.memory_total_mib / 1024:.1f} GiB" if known else "未知"
    fields = [
        ("节点", escape(entry.node.node_id)),
        ("设备", f"GPU {gpu.index} · {escape(gpu.model)}"),
        ("观测状态", state_text(entry, gpu)),
        ("显存", metric),
        ("GPU 利用率", f"{gpu.utilization_percent}%" if known else "未知"),
        ("温度", f"{gpu.temperature_c} °C" if known and gpu.temperature_c is not None else "未知"),
        ("GPU 采样", slack_time(entry.node.observed_at)),
        ("心跳", slack_time(entry.node.heartbeat_at)),
    ]
    result = loading_details_view()
    result["blocks"] = [
        {"type": "section", "fields": [markdown(f"*{label}*\n{value}") for label, value in fields]},
        context(system_text(entry)),
        context("GPU UUID：" + escape(gpu.gpu_uuid)),
        context("以上为资源观测；预约可用性尚未评估。"),
    ]
    disk_stamp = getattr(entry, "disks_observed_at", None)
    if disk_stamp is not None:
        result["blocks"].append(context("磁盘采样 " + slack_time(disk_stamp) + " · 约每小时查询"))
    return result
