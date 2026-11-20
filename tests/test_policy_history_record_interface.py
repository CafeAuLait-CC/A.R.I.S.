import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from aris.apps.api.main import create_app
from aris.apps.composition.policy import build_policy_draft_components
from aris.core.access.models import RoleBinding
from aris.interfaces.policy.history import PolicyHistoricalUsage
from aris.interfaces.policy.service import PolicyReadSurface
from aris.interfaces.slack.config import SlackSettings
from aris.modules.nova.policy_history_record_service import PolicyHistoricalReviewService
from tests.test_policy_draft_editor import draft_runtime
from tests.test_policy_history_records import (
    adjustments,
    admin_state,
    catalog_state,
    closed,
    command_state,
    counts,
    coverage,
    derivations,
    evidence,
    history_state,
    history_surface,
    ledger,
    recording,
    review_surface,
    windows,
)
from tests.test_policy_history_review import TOKEN, query

__all__ = [
    "adjustments",
    "admin_state",
    "catalog_state",
    "closed",
    "command_state",
    "coverage",
    "derivations",
    "evidence",
    "history_state",
    "history_surface",
    "ledger",
    "recording",
    "review_surface",
    "windows",
]


def headers(key=None):
    return {"Authorization": "Bearer " + TOKEN, **({"Idempotency-Key": key} if key else {})}


@pytest.fixture
def record_editor(recording):
    s = recording
    graph = build_policy_draft_components(
        s.service.sessions,
        s.surface.authenticator,
        s.surface.policy_names,
        draft_runtime(historical_review_storage=s.service.configuration),
        clock=s.service.catalog.clock,
    )
    store = graph.historical_reviews
    history = PolicyHistoricalUsage(
        store.assessments, store.assessments.usage, store.configuration.nodes
    )
    surface = PolicyReadSurface(
        store.catalog,
        s.surface.authenticator,
        s.surface.policy_names,
        draft_commands=graph.user_api,
        historical_usage=history,
        historical_reviews=store,
        historical_review_implementation_ref=store.configuration.implementation_ref,
    )
    return surface, graph, s


def app_for(surface, graph, settings):
    return create_app(
        settings,
        SlackSettings(_env_file=None),
        policy_sources=surface,
        user_commands=graph.user_api,
    )


def test_history_record_preview_core_worker_read_and_export_preserve_the_confirmed_context(
    record_editor, settings
):
    surface, graph, s = record_editor
    input = {
        **query(s.surface, s.state),
        "expected_context_fingerprint": s.request.expected_context_fingerprint,
        "reason": "Fixed historical <b>evidence</b>",
    }
    with TestClient(app_for(surface, graph, settings)) as client:
        session = client.post("/internal/policy/session", headers=headers()).json()
        assert (
            session["historical_review_records_available"]
            and session["historical_review_writing_available"]
        )
        value = client.post(
            "/internal/policy/history/records/validate", headers=headers(), json=input
        )
        assert value.status_code == 200, value.text
        params = value.json()["request"]
        assert counts(s) == (0, 0)
        assert params["query"]["usage"]["owner_id"] == str(s.state.actor.actor_id)
        assert (
            value.json()["context"]["context_fingerprint"] == input["expected_context_fingerprint"]
        )
        assert isinstance(value.json()["context"]["evidence"]["source_ceiling"], str)
        assert (
            client.post(
                "/internal/policy/history/records/validate",
                headers=headers(),
                json={**input, "reason": "invalid\nreason"},
            ).status_code
            == 422
        )
        pending = client.post(
            "/internal/commands",
            headers=headers("history-record-http"),
            json={"command": "policy.record_historical_review", "parameters": params},
        ).json()["outcome"]
        path = "/internal/commands/" + pending["command_id"]
        review = client.get(path + "/review", headers=headers()).json()
        assert review["request"]["parameters"] == params
        confirmed = client.post(
            path + "/confirm",
            headers=headers("history-record-confirm"),
            json={"proof": review["outcome"]["proof"]},
        )
        assert confirmed.json()["outcome"]["state"] == "queued"
        assert graph.worker.run_once().state == "succeeded"
        recovered = client.get(path + "/review", headers=headers()).json()["outcome"]
        assert recovered["state"] == "succeeded" and counts(s) == (1, 1)
        choice = {
            "source": params["query"]["policy"],
            "expected_source_fingerprint": params["query"]["expected_policy_fingerprint"],
            "node_id": params["query"]["usage"]["node_id"],
        }
        page = client.post("/internal/policy/history/records/query", headers=headers(), json=choice)
        assert page.status_code == 200 and len(page.json()["items"]) == 1
        receipt = recovered["result"]
        item = {**choice, "id": receipt["id"], "expected_fingerprint": receipt["fingerprint"]}
        detail = client.post(
            "/internal/policy/history/records/detail", headers=headers(), json=item
        )
        assert detail.status_code == 200 and detail.headers["cache-control"] == "no-store"
        assert (
            detail.json()["context"]["canonical_review_json"]
            == value.json()["context"]["canonical_review_json"]
        )
        assert detail.json()["context"]["assessment"]["state"] == "not_evaluated"
        report = client.post(
            "/internal/policy/history/records/report", headers=headers(), json=item
        )
        assert report.status_code == 200 and "attachment" in report.headers["content-disposition"]
        assert "&lt;b&gt;evidence&lt;/b&gt;" in report.text and "<b>evidence</b>" not in report.text
        assert receipt["fingerprint"] in report.text and "已保存审核记录" in report.text
        assert str(s.state.other) not in detail.text
        assert (
            client.post(
                "/internal/policy/history/records/validate",
                headers=headers(),
                json={**input, "owner_id": str(s.state.other)},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/internal/policy/history/records/detail",
                headers=headers(),
                json={**item, "expected_fingerprint": "f" * 64},
            ).status_code
            == 409
        )
        with surface.catalog.sessions.begin() as db:
            db.execute(
                delete(RoleBinding).where(RoleBinding.user_id == str(s.state.actor.actor_id))
            )
        assert (
            client.post(
                "/internal/policy/history/records/report", headers=headers(), json=item
            ).status_code
            == 403
        )
        assert counts(s) == (1, 1)


def test_historical_record_routes_and_commands_are_explicit_opt_in(record_editor, settings):
    surface, _, s = record_editor
    graph = build_policy_draft_components(
        s.service.sessions,
        s.surface.authenticator,
        s.surface.policy_names,
        draft_runtime(),
        clock=s.service.catalog.clock,
    )
    payload = {
        "query": s.request.query.model_dump(mode="json"),
        "expected_context_fingerprint": s.request.expected_context_fingerprint,
        "expected_implementation_ref": s.request.expected_implementation_ref,
        "reason": s.request.reason,
    }
    with TestClient(app_for(s.surface, graph, settings)) as client:
        assert (
            client.post(
                "/internal/policy/history/records/query", headers=headers(), json={}
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/internal/commands",
                headers=headers("off-history-save"),
                json={"command": "policy.record_historical_review", "parameters": payload},
            ).status_code
            == 403
        )
    store = PolicyHistoricalReviewService(
        s.surface.catalog, s.surface.historical_usage.assessments, s.service.configuration
    )
    reader = PolicyReadSurface(
        s.surface.catalog,
        s.surface.authenticator,
        s.surface.policy_names,
        draft_commands=graph.user_api,
        historical_usage=s.surface.historical_usage,
        historical_reviews=store,
        historical_review_implementation_ref=store.configuration.implementation_ref,
    )
    with TestClient(app_for(reader, graph, settings)) as client:
        session = client.post("/internal/policy/session", headers=headers()).json()
        assert (
            session["historical_review_records_available"]
            and not session["historical_review_writing_available"]
        )
        assert (
            client.post(
                "/internal/policy/history/records/validate", headers=headers(), json={}
            ).status_code
            == 404
        )
        response = client.post(
            "/internal/policy/history/records/query",
            headers=headers(),
            json={
                "source": s.request.query.policy.model_dump(),
                "expected_source_fingerprint": s.request.query.expected_policy_fingerprint,
                "node_id": "node-a",
            },
        )
        assert response.status_code == 200 and response.json()["items"] == []
