import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete

from aris.apps.api.main import create_app
from aris.core.access.models import RoleBinding
from aris.interfaces.policy.history import PolicyHistoryReviewInput, PolicyHistoryReviewView
from aris.interfaces.policy.history_report import historical_review_report
from aris.interfaces.policy.service import PolicyReadSurface
from aris.interfaces.slack.config import SlackSettings
from aris.modules.lyra.quota_evidence_schemas import quota_evidence_fingerprint
from tests.test_policy_history_interface import (
    HEADERS,
    TOKEN,
    adjustments,
    admin_state,
    assessment,
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
    windows,
)

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
    "windows",
]


@pytest.fixture
def review_surface(history_surface):
    source, state = history_surface
    surface = PolicyReadSurface(
        source.catalog,
        source.authenticator,
        source.policy_names,
        historical_usage=source.historical_usage,
        historical_review_implementation_ref="fixture-historical-review-a",
    )
    return surface, state


def query(surface, state, **changes):
    return {
        **assessment(surface, state),
        "expected_implementation_ref": "fixture-historical-review-a",
        **changes,
    }


def test_historical_review_uses_one_authorized_fixed_evidence_read_and_preserves_scope(
    review_surface, monkeypatch
):
    surface, state = review_surface
    before = counts(state.windows.service)
    original = state.service.read
    calls = []

    def tracked(actor, selection):
        result = original(actor, selection)
        calls.append(result)
        return result

    monkeypatch.setattr(state.service, "read", tracked)
    value = surface.history_review(
        "Bearer " + TOKEN, "history-review", PolicyHistoryReviewInput(**query(surface, state))
    )
    assert len(calls) == 1 and value.review.evidence == calls[0]
    evidence = value.review.evidence
    assert value.review.assessment.evidence_fingerprint == evidence.fingerprint
    assert evidence.selection.owner_id == state.actor.actor_id
    assert evidence.source_ceiling == state.saved.window.source.highest_committed_seq
    assert str(state.other) not in value.model_dump_json()
    assert (
        not value.review.current_dependencies_assessed
        and not value.review.role_assignments_verified
    )
    assert not value.review.approval_performed and not value.review.publication_performed
    assert counts(state.windows.service) == before
    large = evidence.model_copy(update={"source_ceiling": 9_007_199_254_740_993})
    fingerprint = quota_evidence_fingerprint(large)
    domain = value.review.model_copy(
        update={
            "evidence": large.model_copy(update={"fingerprint": fingerprint}),
            "assessment": value.review.assessment.model_copy(
                update={"evidence_fingerprint": fingerprint}
            ),
        }
    )
    precise = PolicyHistoryReviewView.of(domain, value.implementation_ref)
    assert precise.evidence.source_ceiling == "9007199254740993"
    assert precise.review.evidence.source_ceiling == 9_007_199_254_740_993
    assert PolicyHistoryReviewView.model_validate_json(precise.model_dump_json()) == precise
    tampered = precise.model_dump(mode="json")
    tampered["evidence"]["source_ceiling"] = "9007199254740992"
    with pytest.raises(ValidationError):
        PolicyHistoryReviewView.model_validate(tampered)


def test_historical_review_report_is_opt_in_rechecks_current_access_and_binds_original_refs(
    review_surface, history_surface, settings
):
    surface, state = review_surface
    parameters = query(surface, state)
    original, _ = history_surface
    with TestClient(
        create_app(settings, SlackSettings(_env_file=None), policy_sources=original)
    ) as client:
        assert (
            client.post(
                "/internal/policy/history/review", headers=HEADERS, json=parameters
            ).status_code
            == 404
        )
    with TestClient(
        create_app(settings, SlackSettings(_env_file=None), policy_sources=surface)
    ) as client:
        assert (
            client.post("/internal/policy/session", headers=HEADERS).json()[
                "historical_review_implementation_ref"
            ]
            == "fixture-historical-review-a"
        )
        context = client.post("/internal/policy/history/review", headers=HEADERS, json=parameters)
        assert context.status_code == 200 and context.json()["assessment"]["selection"][
            "owner_id"
        ] == str(state.actor.actor_id)
        report = client.post("/internal/policy/history/report", headers=HEADERS, json=parameters)
        assert report.status_code == 200 and report.headers["Cache-Control"] == "no-store"
        assert state.saved.fingerprint in report.text and "<script" not in report.text
        assert "尚未批准" in report.text and "手动假设" in report.text
        for change, status in (
            ({"expected_implementation_ref": "changed"}, 409),
            ({"expected_snapshot_fingerprint": "f" * 64}, 409),
            ({"owner_id": str(state.other)}, 422),
        ):
            rejected = client.post(
                "/internal/policy/history/report", headers=HEADERS, json={**parameters, **change}
            )
            assert rejected.status_code == status
        with surface.catalog.sessions.begin() as db:
            db.execute(delete(RoleBinding).where(RoleBinding.user_id == str(state.actor.actor_id)))
        assert (
            client.post(
                "/internal/policy/history/report", headers=HEADERS, json=parameters
            ).status_code
            == 403
        )


def test_review_retains_method_mismatch_as_unknown_and_escapes_original_model_text(review_surface):
    surface, state = review_surface
    history = surface.historical_usage
    history.nodes = tuple(
        node.model_copy(update={"expected_configuration_fingerprint": "f" * 64})
        for node in history.nodes
    )
    value = surface.history_review(
        "Bearer " + TOKEN, "mismatched-method", PolicyHistoryReviewInput(**query(surface, state))
    )
    assert value.review.assessment.reason == "USAGE_METHOD_MISMATCH"
    assert value.review.assessment.used_gpu_seconds is None
    assert value.review.evidence.total_usage_microseconds == 30_000_000
    mutated = value.review.evidence.model_copy(
        update={
            "models": tuple(
                item.model_copy(update={"model": "<img src=x onerror=alert(1)>"})
                for item in value.review.evidence.models
            )
        }
    )
    fingerprint = quota_evidence_fingerprint(mutated)
    original = value.review.model_copy(
        update={
            "evidence": mutated.model_copy(update={"fingerprint": fingerprint}),
            "assessment": value.review.assessment.model_copy(
                update={"evidence_fingerprint": fingerprint}
            ),
        }
    )
    html = historical_review_report(PolicyHistoryReviewView.of(original, value.implementation_ref))
    assert "&lt;img" in html and "<img" not in html and "历史额度未评估" in html
