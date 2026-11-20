import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from aris.apps.api.main import create_app
from aris.apps.composition.policy import build_policy_draft_components
from aris.core.access.models import RoleBinding
from aris.interfaces.policy.history import PolicyHistoricalUsage
from aris.interfaces.policy.service import PolicyReadSurface
from aris.interfaces.slack.config import SlackSettings
from aris.modules.nova.policy_approval_service import PolicyApprovalService
from tests.test_policy_draft_editor import draft_runtime
from tests.test_policy_history_review import TOKEN
from tests.test_policy_source_approvals import (
    adjustments,
    admin_state,
    approval,
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

__all__ = [
    "adjustments",
    "admin_state",
    "approval",
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
def approval_editor(approval):
    s = approval
    graph = build_policy_draft_components(
        s.service.sessions,
        s.recording.surface.authenticator,
        s.recording.surface.policy_names,
        draft_runtime(
            historical_review_storage=s.service.history.configuration,
            source_approval=s.service.requirements,
        ),
        clock=s.service.catalog.clock,
    )
    history = graph.historical_reviews
    surface = PolicyReadSurface(
        history.catalog,
        s.recording.surface.authenticator,
        s.recording.surface.policy_names,
        draft_commands=graph.user_api,
        historical_usage=PolicyHistoricalUsage(
            history.assessments, history.assessments.usage, history.configuration.nodes
        ),
        historical_reviews=history,
        historical_review_implementation_ref=history.configuration.implementation_ref,
        approvals=graph.approvals,
    )
    return surface, graph, s


def app_for(surface, graph, settings):
    return create_app(
        settings,
        SlackSettings(_env_file=None),
        policy_sources=surface,
        user_commands=graph.user_api,
    )


def test_approval_http_confirmation_worker_records_only_the_exact_individual_source_review(
    approval_editor, settings
):
    surface, graph, s = approval_editor
    parameters = s.request.model_dump(mode="json")
    with TestClient(app_for(surface, graph, settings)) as client:
        session = client.post("/internal/policy/session", headers=headers()).json()
        assert session["source_approval_writing_available"]
        assert (
            session["source_approval_requirements_fingerprint"]
            == s.service.requirements.fingerprint
        )
        assert client.get("/internal/policy/ui/assets/policy-approvals.js").status_code == 200
        value = client.post(
            "/internal/policy/approvals/validate", headers=headers(), json=parameters
        )
        assert value.status_code == 200, value.text
        assert (
            not value.json()["publication_authorized"]
            and value.json()["samples"][0]["quota_state"] == "not_evaluated"
        )
        assert counts(s) == (0, 0)
        assert (
            client.post(
                "/internal/policy/approvals/validate",
                headers=headers(),
                json={**parameters, "approved_by": str(s.actor.actor_id)},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/internal/policy/approvals/validate",
                headers=headers(),
                json={**parameters, "reason": "invalid\nreason"},
            ).status_code
            == 422
        )
        proposal = client.post(
            "/internal/commands",
            headers=headers("approve-http"),
            json={"command": "policy.approve_source", "parameters": parameters},
        ).json()["outcome"]
        path = "/internal/commands/" + proposal["command_id"]
        original = client.get(path + "/review", headers=headers()).json()
        assert original["request"]["parameters"] == parameters
        accepted = client.post(
            path + "/confirm",
            headers=headers("approve-http-confirm"),
            json={"proof": original["outcome"]["proof"]},
        )
        assert accepted.json()["outcome"]["state"] == "queued" and counts(s) == (0, 0)
        assert graph.worker.run_once().state == "succeeded"
        result = client.get(path + "/review", headers=headers()).json()["outcome"]
        assert result["state"] == "succeeded" and counts(s) == (1, 1)
        choice = {
            "source": parameters["source"],
            "expected_source_fingerprint": parameters["expected_source_fingerprint"],
        }
        page = client.post("/internal/policy/approvals/query", headers=headers(), json=choice)
        assert page.status_code == 200 and page.json()["items"][0]["approved_by"] == str(
            s.actor.actor_id
        )
        receipt = result["result"]
        item = {**choice, "id": receipt["id"], "expected_fingerprint": receipt["fingerprint"]}
        saved = client.post("/internal/policy/approvals/detail", headers=headers(), json=item)
        assert saved.status_code == 200 and saved.headers["cache-control"] == "no-store"
        assert (
            saved.json()["record"]["preview"]["request"] == parameters
            and not saved.json()["record"]["activation_performed"]
        )
        assert (
            client.post(
                "/internal/policy/approvals/detail",
                headers=headers(),
                json={**item, "expected_fingerprint": "f" * 64},
            ).status_code
            == 409
        )
        assert client.get(path + "/review", headers=headers()).json()[
            "outcome"
        ] == result and counts(s) == (1, 1)
        original_record = saved.json()["record"]
        revoke = {
            **choice,
            "approval_id": receipt["id"],
            "expected_approval_fingerprint": receipt["fingerprint"],
            "reason": "Retract my synthetic approval",
        }
        preview = client.post(
            "/internal/policy/approvals/revoke/validate", headers=headers(), json=revoke
        )
        assert preview.status_code == 200 and preview.json()["revocation_only"]
        requested = client.post(
            "/internal/commands",
            headers=headers("revoke-http"),
            json={"command": "policy.revoke_source_approval", "parameters": revoke},
        ).json()["outcome"]
        revoke_path = "/internal/commands/" + requested["command_id"]
        checked = client.get(revoke_path + "/review", headers=headers()).json()
        assert (
            client.post(
                revoke_path + "/confirm",
                headers=headers("revoke-confirm"),
                json={"proof": checked["outcome"]["proof"]},
            ).json()["outcome"]["state"]
            == "queued"
        )
        assert graph.worker.run_once().state == "succeeded"
        status = client.post(
            "/internal/policy/approvals/detail", headers=headers(), json=item
        ).json()
        assert status["state"] == "revoked" and status["record"] == original_record
        assert (
            status["revocation"]["request"]["reason"] == revoke["reason"]
            and not status["publication_authorized"]
        )
        assert (
            client.post("/internal/policy/approvals/query", headers=headers(), json=choice).json()[
                "items"
            ][0]["revocation_id"]
            == status["revocation"]["id"]
        )
        with surface.catalog.sessions.begin() as db:
            db.execute(delete(RoleBinding).where(RoleBinding.id == s.binding))
        current = client.post("/internal/policy/session", headers=headers()).json()
        assert (
            current["source_approval_requirements"] is None
            and not current["source_approval_writing_available"]
        )
        assert (
            client.post(
                "/internal/policy/approvals/detail", headers=headers(), json=item
            ).status_code
            == 403
        )


def test_source_approval_routes_default_off_and_reads_do_not_enable_a_writer(
    approval_editor, settings
):
    surface, graph, s = approval_editor
    with TestClient(
        create_app(settings, SlackSettings(_env_file=None), policy_sources=s.recording.surface)
    ) as client:
        assert (
            client.post(
                "/internal/policy/approvals/validate",
                headers=headers(),
                json=s.request.model_dump(mode="json"),
            ).status_code
            == 404
        )
        assert (
            client.post("/internal/policy/approvals/query", headers=headers(), json={}).status_code
            == 404
        )
        assert (
            client.post("/internal/policy/session", headers=headers()).json()[
                "source_approval_requirements"
            ]
            is None
        )
    reader = PolicyApprovalService(
        surface.catalog, surface.historical_reviews, s.service.requirements
    )
    readonly = PolicyReadSurface(
        surface.catalog,
        surface.authenticator,
        surface.policy_names,
        historical_usage=surface.historical_usage,
        historical_reviews=surface.historical_reviews,
        historical_review_implementation_ref=surface.historical_review_implementation_ref,
        approvals=reader,
    )
    with TestClient(
        create_app(settings, SlackSettings(_env_file=None), policy_sources=readonly)
    ) as client:
        assert (
            client.post(
                "/internal/policy/approvals/validate",
                headers=headers(),
                json=s.request.model_dump(mode="json"),
            ).status_code
            == 404
        )
        assert not client.post("/internal/policy/session", headers=headers()).json()[
            "source_approval_writing_available"
        ]
        page = client.post(
            "/internal/policy/approvals/query",
            headers=headers(),
            json={
                "source": s.request.source.model_dump(),
                "expected_source_fingerprint": s.request.expected_source_fingerprint,
            },
        )
        assert page.status_code == 200 and page.json()["items"] == [] and counts(s) == (0, 0)
