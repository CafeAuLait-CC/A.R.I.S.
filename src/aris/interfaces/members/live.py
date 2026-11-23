"""Read-only projection from the existing loopback Vega dashboard preview."""

from collections.abc import AsyncIterator
from time import monotonic
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import Field, model_validator
from starlette.concurrency import run_in_threadpool

from aris.core.access.schemas import AccessContract, Identifier
from aris.core.contracts import DomainError
from aris.interfaces.members.service import GeneralGpu, GeneralNode, MemberPortalSurface
from aris.modules.vega.live_schemas import LiveNode, LiveSnapshot

PREVIEW_MEDIA_TYPE = "application/x-ndjson"
AUTHORIZATION_INTERVAL_SECONDS = 5
MAX_VEGA_EVENT_BYTES = 1024 * 1024


def dashboard_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or not parsed.port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or value != f"http://{parsed.netloc}"
    ):
        raise ValueError("Vega dashboard requires an explicit HTTP loopback origin")
    return value


class PreviewQuery(AccessContract):
    node_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_scope(self) -> "PreviewQuery":
        if len(set(self.node_ids)) != len(self.node_ids):
            raise ValueError("Preview nodes must be unique")
        return self


class PreviewFrame(AccessContract):
    nodes: tuple[GeneralNode, ...]


def public_node(node: LiveNode) -> GeneralNode:
    telemetry = {
        "live": "CURRENT",
        "waiting": "MISSING",
        "stale": "STALE",
        "error": "ERROR",
        "offline": "MISSING",
    }[node.quality]
    system = node.system if node.connected and node.system_freshness == "CURRENT" else None
    memory = system.memory if system and system.memory.quality == "ok" else None
    return GeneralNode(
        node_id=node.node_id,
        status="ONLINE" if node.connected else "OFFLINE",
        telemetry_status=telemetry,
        observed_at=node.observed_at,
        system_observed_at=node.system_observed_at,
        system_freshness=node.system_freshness,
        cpu_percent=system.cpu.utilization_percent
        if system and system.cpu.quality == "ok"
        else None,
        memory_total_bytes=memory.total_bytes if memory else None,
        memory_available_bytes=memory.available_bytes if memory else None,
        gpus=tuple(
            GeneralGpu(
                index=gpu.index,
                name=gpu.model,
                state=gpu.observed_state,
                memory_used_mib=gpu.memory_used_mib,
                memory_total_mib=gpu.memory_total_mib,
                utilization_percent=gpu.utilization_percent,
                temperature_c=gpu.temperature_c,
            )
            for gpu in node.gpus
        )
        if node.connected and node.quality == "live"
        else (),
    )


async def preview_stream(
    surface: MemberPortalSurface,
    origin: str,
    token: str,
    csrf: str,
    query: PreviewQuery,
    actor_id: UUID,
) -> AsyncIterator[str]:
    check_at = monotonic() + AUTHORIZATION_INTERVAL_SECONDS
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8, connect=2), trust_env=False, follow_redirects=False
        ) as client:
            async with client.stream("GET", origin + "/live") as upstream:
                upstream.raise_for_status()
                if not upstream.headers.get("content-type", "").startswith("text/event-stream"):
                    raise ValueError("Expected a Vega preview stream")
                async for line in upstream.aiter_lines():
                    if monotonic() >= check_at:
                        current = await run_in_threadpool(
                            surface.authorize_preview, token, csrf, query.node_ids
                        )
                        if current != actor_id:
                            raise DomainError("PERMISSION_DENIED", "Current identity differs", 403)
                        check_at = monotonic() + AUTHORIZATION_INTERVAL_SECONDS
                    if line == "event: preview_error" or len(line) > MAX_VEGA_EVENT_BYTES:
                        raise ValueError("Vega preview is unavailable")
                    if not line.startswith("data: "):
                        continue
                    snapshot = LiveSnapshot.model_validate_json(line[6:])
                    by_id = {node.node_id: node for node in snapshot.nodes}
                    yield (
                        PreviewFrame(
                            nodes=tuple(
                                public_node(by_id[node])
                                if node in by_id
                                else GeneralNode(
                                    node_id=node, status="UNKNOWN", telemetry_status="MISSING"
                                )
                                for node in query.node_ids
                            )
                        ).model_dump_json()
                        + "\n"
                    )
    except DomainError as error:
        if error.status in (401, 403):
            yield '{"error":"PERMISSION_DENIED"}\n'
            return
    except (httpx.HTTPError, ValueError):
        pass
    yield '{"error":"PREVIEW_UNAVAILABLE"}\n'
