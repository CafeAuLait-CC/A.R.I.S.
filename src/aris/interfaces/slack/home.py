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
        return "Not detected" if not gpu.present else "Observed unknown"
    return "Observed busy" if gpu.observed_state == "busy" else "Observed idle"


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
        return "User attribution not provided"
    if compute is not None and compute.reported_processes == 0 and owner.quality == "ok":
        return "No compute processes reported"
    users = [
        user
        for user in owner.users
        if any(part.gpu_uuid == gpu.gpu_uuid and part.reported_processes for part in user.gpus)
    ]
    if not users:
        return "User attribution unknown"
    names = [
        f"{escape(user.username[:32])} (UID {user.uid})" if user.username else f"UID {user.uid}"
        for user in users[:2]
    ]
    if len(users) > 2:
        names.append(f"{len(users) - 2} more UIDs")
    suffix = " · partial attribution" if owner.quality == "partial" else ""
    return "Linux " + ", ".join(names) + suffix


def disk_text(entry: NodeResourceStatus) -> str:
    # The disk task is being developed independently. Public optional fields
    # remain absent on existing snapshots; absence is never a zero-capacity disk.
    freshness = getattr(entry, "disks_freshness", "MISSING")
    observation = getattr(entry, "disks", None)
    if entry.node.status != "ONLINE" or freshness != "CURRENT" or observation is None:
        return "Disk unknown"
    if observation.quality != "ok" or observation.truncated:
        return "Disk partially unknown" if observation.quality == "partial" else "Disk unknown"
    volumes = observation.volumes
    if not volumes or any(v.quality != "ok" for v in volumes):
        return "Disk unknown"
    total = sum(v.total_bytes for v in volumes)
    used = sum(v.used_bytes for v in volumes)
    unit, divisor = ("TiB", 2**40) if total >= 2**40 else ("GiB", 2**30)
    return f"Disk {used / divisor:.1f} / {total / divisor:.1f} {unit} ({len(volumes)} volumes)"


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
            cpu = "CPU warming up"
        ram = system.memory
        if ram.quality == "ok" and ram.total_bytes is not None and ram.available_bytes is not None:
            memory = (
                f"RAM {(ram.total_bytes - ram.available_bytes) / 2**30:.1f} / "
                f"{ram.total_bytes / 2**30:.1f} GiB"
            )
    return f"{cpu} · {memory} · {disk_text(entry)}"


def device_blocks(
    entry: NodeResourceStatus,
    gpu: GpuStatus,
    asset_base_url: str | None,
    *,
    show_title: bool = True,
) -> list[dict[str, Any]]:
    percent = memory_percent(entry, gpu)
    metric = (
        "*VRAM unknown*"
        if percent is None
        else (f"*VRAM {percent}%* · {memory_used(gpu)} / {gpu.memory_total_mib / 1024:.1f} GiB")
    )
    state = state_text(entry, gpu)
    if gpu_current(entry, gpu) and gpu.observed_state == "busy":
        state += " · " + owner_text(entry, gpu)
    title = f"*GPU {gpu.index} · {escape(gpu.model)}*\n" if show_title else ""
    heading = section(f"{title}{state}\n\n{metric}")
    blocks = [heading]
    if asset_base_url:
        blocks.append(
            {
                "type": "image",
                "image_url": f"{asset_base_url.rstrip('/')}/{asset_name(percent)}",
                "alt_text": f"{entry.node.node_id} GPU {gpu.index}: VRAM "
                + ("Unknown" if percent is None else f"{percent}%"),
            }
        )
    if percent is None:
        footer = "Current metrics unknown"
    else:
        free = (gpu.memory_total_mib - gpu.memory_used_mib) / 1024
        temperature = (
            "Temperature unknown" if gpu.temperature_c is None else f"{gpu.temperature_c} °C"
        )
        footer = f"{free:.1f} GiB free · GPU {gpu.utilization_percent}% · {temperature}"
    selection = json.dumps(
        {"node_id": entry.node.node_id, "gpu_uuid": gpu.gpu_uuid},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    action_id = DETAILS_ACTION + hashlib.sha256(selection.encode()).hexdigest()[:16]
    blocks.append(section(footer, button("Details", action_id, selection)))
    return blocks


def paired_layout(
    entry: NodeResourceStatus, pair: list[GpuStatus], asset_base_url: str | None
) -> dict[str, Any]:
    devices = [device_blocks(entry, gpu, asset_base_url) for gpu in pair]
    return {
        "type": "layout",
        "label": (entry.node.node_id + " GPU observations")[:150],
        "is_label_visible": False,
        "grid_lines": "none",
        # Image blocks have their own minimum width. Keep each complete device
        # in one constrained container so a wrapping image cannot separate from
        # its heading or spill into the other device's column.
        "rows": [
            {
                "items": [
                    {
                        "width": "standard",
                        "horizontal_alignment": "start",
                        "block": {
                            "type": "container",
                            "title": plain(f"GPU {gpu.index} · {gpu.model}"[:150]),
                            "width": "narrow",
                            "child_blocks": device_blocks(
                                entry, gpu, asset_base_url, show_title=False
                            ),
                        },
                    }
                    for gpu in pair
                ]
            }
        ],
        "fallback": [block for device in devices for block in device],
    }


def block_cost(block: dict[str, Any]) -> int:
    if block["type"] == "container":
        return 1 + sum(block_cost(child) for child in block["child_blocks"])
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
        {"type": "header", "text": plain("Hardware resources")},
        section(
            "Snapshot updated " + slack_time(snapshot.generated_at),
            button("Refresh", "refresh_home"),
        ),
    ]
    cost = len(blocks)
    truncated = False
    health = {"ONLINE": "Online", "STALE": "Activity stale", "OFFLINE": "Offline"}
    quality = {
        "CURRENT": "Current sample",
        "STALE": "Stale sample",
        "ERROR": "Collection error",
        "MISSING": "No samples",
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
                f"*Observed idle: {counts['idle']}* · Observed busy: {counts['busy']}"
                f" · Unknown: {unknown}"
                f"\n{health[node.status]} · {quality[node.telemetry_status]}"
            ),
            context(system_text(entry)),
            context(
                "GPU "
                + slack_time(node.observed_at)
                + " · System "
                + slack_time(entry.system_observed_at)
                + " · Heartbeat "
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
            blocks.append(
                context("Disk sampled " + slack_time(disk_stamp) + " · sampled at most hourly")
            )
            cost += 1
        if not node.gpus:
            blocks.append(context("GPU inventory not reported."))
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
        blocks.append(context("No node observations yet."))
    if truncated:
        blocks.append(
            context(
                "Display limit reached. Some nodes or GPUs are omitted; "
                "open the WebUI for the full inventory."
            )
        )
    blocks.append(
        context(
            "Status is a snapshot. Refresh for the latest observations. "
            "Linux users are not linked to Slack identities. "
            "Reservation availability not evaluated."
        )
    )
    return {"type": "home", "blocks": blocks}


def loading_details_view() -> dict[str, Any]:
    return {
        "type": "modal",
        "title": plain("GPU details"),
        "close": plain("Close"),
        "blocks": [section("Loading latest resource observations…")],
    }


def unavailable_details_view(message: str) -> dict[str, Any]:
    result = loading_details_view()
    result["blocks"] = [section(message)]
    return result


def details_view(entry: NodeResourceStatus, gpu: GpuStatus) -> dict[str, Any]:
    percent = memory_percent(entry, gpu)
    known = percent is not None
    metric = f"{memory_used(gpu)} / {gpu.memory_total_mib / 1024:.1f} GiB" if known else "Unknown"
    fields = [
        ("Node", escape(entry.node.node_id)),
        ("Device", f"GPU {gpu.index} · {escape(gpu.model)}"),
        ("Observed state", state_text(entry, gpu)),
        ("VRAM", metric),
        ("GPU utilization", f"{gpu.utilization_percent}%" if known else "Unknown"),
        (
            "Temperature",
            f"{gpu.temperature_c} °C" if known and gpu.temperature_c is not None else "Unknown",
        ),
        ("GPU sampled", slack_time(entry.node.observed_at)),
        ("Heartbeat", slack_time(entry.node.heartbeat_at)),
    ]
    result = loading_details_view()
    result["blocks"] = [
        {"type": "section", "fields": [markdown(f"*{label}*\n{value}") for label, value in fields]},
        context(system_text(entry)),
        context("GPU UUID: " + escape(gpu.gpu_uuid)),
        context("Resource observations only. Reservation availability not evaluated."),
    ]
    disk_stamp = getattr(entry, "disks_observed_at", None)
    if disk_stamp is not None:
        result["blocks"].append(
            context("Disk sampled " + slack_time(disk_stamp) + " · sampled at most hourly")
        )
    return result
