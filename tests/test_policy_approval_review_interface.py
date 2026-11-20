import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from aris.apps.api.main import create_app
from aris.apps.composition.policy import build_policy_draft_components
from aris.apps.ops import policy_ui
from aris.core.access.models import RoleBinding, User
from aris.interfaces.policy.history import PolicyHistoricalUsage
from aris.interfaces.policy.service import PolicyReadSurface
from aris.interfaces.slack.config import SlackSettings
from aris.modules.nova.policy_approval_review_service import PolicyApprovalReviewService
from tests.test_policy_approval_review import (
    adjustments,
    admin_state,
    approval,
    approval_review,
    catalog_state,
    closed,
    command_state,
    coverage,
    derivations,
    endorse,
    evidence,
    history_state,
    history_surface,
    ledger,
    recording,
    review_surface,
    windows,
)
from tests.test_policy_draft_editor import draft_runtime
from tests.test_policy_history_review import TOKEN

__all__ = [
    "adjustments",
    "admin_state",
    "approval",
    "approval_review",
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
HEADERS = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def approval_review_surface(approval_review):
    r = approval_review
    s = r.state
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
    summary = PolicyApprovalReviewService(graph.approvals, r.service.requirements)
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
        approval_summary=summary,
    )
    return surface, graph, r


def test_cross_signer_review_and_report_expose_metadata_but_not_others_usage_or_reasons(
    approval_review_surface, settings
):
    surface, graph, r = approval_review_surface
    s = r.state
    with s.service.sessions.begin() as db:
        db.get(User, str(r.other.actor_id)).display_name = "<img src=x onerror=alert(1)>"
    endorse(r)
    second = endorse(r, "second")
    with TestClient(
        create_app(
            settings,
            SlackSettings(_env_file=None),
            policy_sources=surface,
            user_commands=graph.user_api,
        )
    ) as client:
        session = client.post("/internal/policy/session", headers=HEADERS).json()
        assert (
            session["source_approval_review_available"]
            and session["source_approval_review_minimum_distinct_approvers"] == 2
        )
        assert client.get("/internal/policy/ui/assets/policy-approval-review.js").status_code == 200
        query = r.query.model_dump(mode="json")
        response = client.post("/internal/policy/approvals/review", headers=HEADERS, json=query)
        assert response.status_code == 200, response.text
        value = response.json()
        assert (
            value["qualifying_signers"] == 2
            and value["outcome"] == "minimum_met"
            and not value["publication_authorized"]
        )
        assert all(
            private not in response.text
            for private in (
                "OTHER_PRIVATE_APPROVAL_REASON",
                "OTHER_PRIVATE_REVIEW_REASON",
                "quota_state",
                "models",
            )
        )
        assert (
            client.post(
                "/internal/policy/approvals/review",
                headers=HEADERS,
                json={**query, "minimum_distinct_approvers": 1},
            ).status_code
            == 422
        )
        report_input = {**query, "expected_result_fingerprint": value["fingerprint"]}
        report = client.post(
            "/internal/policy/approvals/review/report", headers=HEADERS, json=report_input
        )
        assert report.status_code == 200 and report.headers["cache-control"] == "no-store"
        assert (
            "&lt;img src=x onerror=alert(1)&gt;" in report.text
            and "<img" not in report.text
            and "<script" not in report.text
        )
        assert value["fingerprint"] in report.text and "策略尚未发布" in report.text
        assert "OTHER_PRIVATE_APPROVAL_REASON" not in report.text
        # Learning a review ID does not grant access to its owner's original context.
        other = {
            "source": s.request.source.model_dump(mode="json"),
            "expected_source_fingerprint": s.request.expected_source_fingerprint,
            "id": second.result["id"],
            "expected_fingerprint": second.result["fingerprint"],
        }
        assert (
            client.post(
                "/internal/policy/approvals/detail", headers=HEADERS, json=other
            ).status_code
            == 404
        )
        with s.service.sessions.begin() as db:
            db.execute(delete(RoleBinding).where(RoleBinding.id == r.other_binding))
        assert (
            client.post(
                "/internal/policy/approvals/review/report", headers=HEADERS, json=report_input
            ).status_code
            == 409
        )
        assert (
            client.post("/internal/policy/approvals/review", headers=HEADERS, json=query).json()[
                "qualifying_signers"
            ]
            == 1
        )
        with s.service.sessions.begin() as db:
            db.execute(delete(RoleBinding).where(RoleBinding.id == s.binding))
        assert (
            client.post(
                "/internal/policy/approvals/review", headers=HEADERS, json=query
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/internal/policy/approvals/review/report", headers=HEADERS, json=report_input
            ).status_code
            == 403
        )
        assert not client.post("/internal/policy/session", headers=HEADERS).json()[
            "source_approval_review_available"
        ]


def test_approval_review_is_opt_in_and_its_private_configuration_validates_without_database(
    approval_review_surface, settings, tmp_path, monkeypatch, capsys
):
    _, _, r = approval_review_surface
    s = r.state
    with TestClient(
        create_app(settings, SlackSettings(_env_file=None), policy_sources=s.recording.surface)
    ) as client:
        assert (
            client.post(
                "/internal/policy/approvals/review",
                headers=HEADERS,
                json=r.query.model_dump(mode="json"),
            ).status_code
            == 404
        )
        assert not client.post("/internal/policy/session", headers=HEADERS).json()[
            "source_approval_review_available"
        ]
    config = {
        "environment": "test",
        "database_url": "sqlite+pysqlite:///" + str(tmp_path / "unavailable.db"),
        "read_token": "fixture-only-review-config-read-credential-123456789",
        "user_tokens": [
            {
                "subject": {"provider": "slack", "namespace": "TFIXTURE", "external_id": "UFIRST"},
                "token": TOKEN,
            }
        ],
        "policy_names": list(s.recording.surface.policy_names),
        "historical_usage": [
            item.model_dump(mode="json") for item in s.service.history.configuration.nodes
        ],
        "historical_review_implementation_ref": s.service.history.configuration.implementation_ref,
        "historical_review_records_read": True,
        "approval_requirements": s.service.requirements.model_dump(mode="json"),
        "approval_review": r.service.requirements.model_dump(mode="json"),
    }
    path = tmp_path / "private-review.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)

    def no_database(*args, **kwargs):
        raise AssertionError("configuration validation must not open a database")

    monkeypatch.setattr(policy_ui, "build_engine", no_database)
    assert policy_ui.main(["--config", str(path), "--validate"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert (
        summary["approval_review_read"]
        and summary["minimum_distinct_approvers"] == 2
        and not summary["database_accessed"]
    )
    assert not (tmp_path / "unavailable.db").exists()
