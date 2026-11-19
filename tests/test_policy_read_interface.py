from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, text

from aris.apps.api.main import create_app
from aris.core.access.models import ExternalIdentity, RoleBinding
from aris.core.access.schemas import ExternalSubject
from aris.core.contracts import DomainError
from aris.interfaces.commands.auth import BoundSubjectTokens, UserTokenBinding
from aris.interfaces.policy.service import PolicyReadSurface
from aris.interfaces.slack.config import SlackSettings
from aris.modules.nova.policy_catalog_schemas import (
    PolicySourceComparisonQuery,
    PolicySourceListQuery,
)
from tests.test_commands import command_state
from tests.test_policy_catalog import actor, catalog_state, confirmed, reference, source_counts
from tests.test_policy_simulation import scenario
from tests.test_policy_source import policy

__all__ = ["command_state", "catalog_state"]
TOKEN = "fixture-only-read-credential-123456789"


@pytest.fixture
def policy_surface(catalog_state, settings):
    _, catalog, sessions, ids, _ = catalog_state
    subject = ExternalSubject(provider="slack", namespace="TFIXTURE", external_id="UALICE")
    with sessions.begin() as db:
        db.add(ExternalIdentity(id=str(uuid4()), user_id=ids["alice"], **subject.model_dump()))
    auth = BoundSubjectTokens(
        [UserTokenBinding(subject=subject, token=SecretStr(TOKEN))],
        reserved_tokens=[settings.read_token],
    )
    return PolicyReadSurface(catalog, auth, ("fixture-only", "empty-policy")), catalog_state


def seed(state, version, concurrency):
    raw = policy()
    raw["metadata"]["version"] = version
    raw["spec"]["reservation"]["max_concurrent_gpus"] = concurrency
    confirmed(state, key="fixture-version-" + str(version), source=raw)


def test_policy_browser_routes_page_versions_show_verified_details_and_compare_without_writes(
    policy_surface, settings
):
    surface, state = policy_surface
    for version in (1, 2, 3):
        seed(state, version, version)
    before = source_counts(state[2])
    headers = {"Authorization": "Bearer " + TOKEN}
    with TestClient(
        create_app(settings, SlackSettings(_env_file=None), policy_sources=surface)
    ) as client:
        info = client.post("/internal/policy/session", headers=headers).json()
        assert info["actor"]["user_id"] == state[3]["alice"]
        assert info["policy_names"] == ["fixture-only", "empty-policy"]
        assert not info["draft_editing_available"]
        assert (
            client.post("/internal/policy/drafts/validate", headers=headers, json={}).status_code
            == 404
        )
        first = client.post(
            "/internal/policy/sources", headers=headers, json={"name": "fixture-only", "limit": 2}
        ).json()
        assert [v["reference"]["version"] for v in first["versions"]] == [1, 2]
        assert first["next_after_version"] == 2
        last = client.post(
            "/internal/policy/sources",
            headers=headers,
            json={"name": "fixture-only", "after_version": 2, "limit": 2},
        ).json()
        assert [v["reference"]["version"] for v in last["versions"]] == [3]
        old, new = first["versions"]
        detail = client.post(
            "/internal/policy/detail",
            headers=headers,
            json={"reference": old["reference"], "expected_fingerprint": old["fingerprint"]},
        )
        assert (
            detail.status_code == 200
            and detail.json()["source"]["spec"]["reservation"]["max_concurrent_gpus"] == 1
        )
        compared = client.post(
            "/internal/policy/compare",
            headers=headers,
            json={
                "before": old["reference"],
                "after": new["reference"],
                "expected_before_fingerprint": old["fingerprint"],
                "expected_after_fingerprint": new["fingerprint"],
            },
        )
        assert compared.status_code == 200 and compared.json()["comparison_only"]
        changes = {row["path"]: row for row in compared.json()["difference"]["changes"]}
        assert changes["/spec/reservation/max_concurrent_gpus"]["before"] == 1
        assert changes["/spec/reservation/max_concurrent_gpus"]["after"] == 2
        assumption = scenario(include_usage=False, projected_concurrent_gpus=2).model_dump(
            mode="json"
        )
        for comparison_target, expected in ((old, "WOULD_DENY"), (new, "NOT_EVALUATED")):
            simulated = client.post(
                "/internal/policy/compare",
                headers=headers,
                json={
                    "before": old["reference"],
                    "after": comparison_target["reference"],
                    "expected_before_fingerprint": old["fingerprint"],
                    "expected_after_fingerprint": comparison_target["fingerprint"],
                    "scenarios": [assumption],
                },
            )
            assert simulated.status_code == 200 and simulated.json()["comparison_only"]
            case = simulated.json()["cases"][0]
            assert case["before"]["decision"] == "WOULD_DENY"
            assert case["after"]["decision"] == expected
            assert all(
                result["simulation_only"]
                and result["assumed_used_gpu_seconds"] is None
                and next(
                    rule for rule in result["results"] if rule["rule_id"] == "quota.actual_usage"
                )["reason"]
                == "USAGE_UNKNOWN"
                for result in (case["before"], case["after"])
            )
        assert client.post("/internal/commands", headers=headers, json={}).status_code == 404
        assert "no-store" in detail.headers["cache-control"]
        assert client.get("/internal/policy/ui").status_code == 200
        assert client.get("/internal/policy/ui/assets/policy.js").status_code == 200
        asset = client.get("/internal/policy/ui/assets/policy-scenarios.js")
        assert asset.status_code == 200 and "javascript" in asset.headers["content-type"]
        assert client.get("/internal/policy/ui/assets/anything").status_code == 422
    assert source_counts(state[2]) == before


def test_policy_empty_source_and_invalid_reference_are_distinct_from_an_effective_policy(
    policy_surface, settings
):
    surface, state = policy_surface
    seed(state, 1, 2)
    headers = {"Authorization": "Bearer " + TOKEN}
    with TestClient(
        create_app(settings, SlackSettings(_env_file=None), policy_sources=surface)
    ) as client:
        empty = client.post(
            "/internal/policy/sources", headers=headers, json={"name": "empty-policy"}
        ).json()
        assert empty["versions"] == [] and "status" not in empty
        wrong = client.post(
            "/internal/policy/detail",
            headers=headers,
            json={"reference": reference().model_dump(), "expected_fingerprint": "0" * 64},
        )
        assert wrong.status_code == 409
        forged = client.post(
            "/internal/policy/sources",
            headers=headers,
            json={"name": "fixture-only", "actor_id": state[3]["alice"]},
        )
        assert forged.status_code == 422


def test_policy_requires_the_bound_current_global_draft_permission(policy_surface, settings):
    surface, state = policy_surface
    with pytest.raises(DomainError) as denied:
        surface.session("Bearer " + settings.read_token.get_secret_value(), "fixture-read")
    assert denied.value.status == 401
    with state[2].begin() as db:
        db.execute(delete(RoleBinding).where(RoleBinding.user_id == state[3]["alice"]))
    for name in ("fixture-only", "outside-scope"):
        with pytest.raises(DomainError) as denied:
            surface.sources("Bearer " + TOKEN, "fixture-read", PolicySourceListQuery(name=name))
        assert denied.value.code == "PERMISSION_DENIED"


def test_external_identity_is_rechecked_after_catalog_work(policy_surface, monkeypatch):
    surface, state = policy_surface
    original = surface.catalog.sources_in_transaction

    def remove(session, actor, query):
        result = original(session, actor, query)
        session.execute(
            delete(ExternalIdentity).where(ExternalIdentity.user_id == str(actor.actor_id))
        )
        return result

    monkeypatch.setattr(surface.catalog, "sources_in_transaction", remove)
    with pytest.raises(DomainError) as denied:
        surface.sources(
            "Bearer " + TOKEN, "fixture-read", PolicySourceListQuery(name="fixture-only")
        )
    assert denied.value.code == "PERMISSION_DENIED"


def test_default_api_does_not_enable_policy_sources(settings):
    with TestClient(create_app(settings, SlackSettings(_env_file=None))) as client:
        assert client.get("/internal/policy/ui").status_code == 404


def test_postgresql_policy_comparison_runs_in_a_read_only_transaction(policy_surface):
    surface, state = policy_surface
    if state[2].kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL READ ONLY contract")
    seed(state, 1, 1)
    seed(state, 2, 2)
    old = surface.catalog.exact(actor(state[3]), reference())
    new = surface.catalog.exact(
        actor(state[3]),
        reference(policy() | {"metadata": {"name": "fixture-only", "version": 2}}),
    )
    query = PolicySourceComparisonQuery(
        before=reference(),
        after=reference(new.source.model_dump()),
        expected_before_fingerprint=old.fingerprint,
        expected_after_fingerprint=new.fingerprint,
    )
    before = source_counts(state[2])
    with state[2]() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        result = surface.catalog.compare_sources_in_transaction(session, actor(state[3]), query)
    assert result.difference.changes and source_counts(state[2]) == before
