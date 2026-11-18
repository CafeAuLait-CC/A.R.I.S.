from dataclasses import replace

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, event, select, text

from aris.core.access.models import RoleBinding
from aris.core.contracts import DomainError
from aris.modules.nova.models import PolicySourceVersion
from aris.modules.nova.policy_catalog_repo import PolicyCatalogRepository
from aris.modules.nova.policy_catalog_schemas import PolicySourceListQuery
from tests.test_commands import command_state
from tests.test_policy_catalog import actor, catalog_state, confirmed, reference, source_counts
from tests.test_policy_source import policy

__all__ = ["command_state", "catalog_state"]


def seed(state, versions=(9, 1, 3, 4, 2), name=None):
    for version in versions:
        raw = policy()
        raw["metadata"]["version"] = version
        if name is not None:
            raw["metadata"]["name"] = name
        confirmed(state, key="version-" + str(version) + (name or ""), source=raw)


def query(**changes):
    return PolicySourceListQuery(name=reference().name, **changes)


def test_source_directory_uses_version_keyset_pages_with_no_full_source_or_approval_claim(
    catalog_state,
):
    _, catalog, _, ids, _ = catalog_state
    seed(catalog_state)
    seed(catalog_state, versions=(2,), name="another-policy")
    versions = []
    after = 0
    while True:
        page = catalog.sources(actor(ids), query(after_version=after, limit=2))
        assert len(page.versions) <= 2 and page.name == reference().name
        for item in page.versions:
            assert item.summary_only and item.catalog_entry_only
            assert item.reference.name == reference().name
            assert all(
                key not in item.model_dump() for key in ("source", "reason", "created_by", "status")
            )
        versions.extend(item.reference.version for item in page.versions)
        if page.next_after_version is None:
            break
        assert page.next_after_version > after
        after = page.next_after_version
    assert versions == [1, 2, 3, 4, 9]
    assert catalog.sources(actor(ids), query(after_version=9)).versions == ()


def test_empty_matching_directory_does_not_write_or_infer_any_published_policy(catalog_state):
    _, catalog, sessions, ids, _ = catalog_state
    before = source_counts(sessions)
    page = catalog.sources(actor(ids), query())
    assert page.versions == () and page.next_after_version is None
    assert source_counts(sessions) == before
    assert set(page.model_dump()) == {"name", "versions", "next_after_version"}


def test_directory_does_not_read_source_bodies_or_private_creation_evidence(catalog_state):
    _, catalog, sessions, ids, _ = catalog_state
    seed(catalog_state, versions=(1, 2, 3))
    statements = []
    engine = sessions.kw["bind"]

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        page = catalog.sources(actor(ids), query(limit=1))
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    selects = [item for item in statements if "policy_versions" in item]
    assert len(selects) == 1 and len(page.versions) == 1 and page.next_after_version == 1
    assert all(value not in selects[0] for value in ("canonical_json", "reason", "created_by"))
    with sessions() as session:
        assert len(PolicyCatalogRepository(session).page(query(limit=1))) == 2


@pytest.mark.parametrize("name", [reference().name, "not-present"])
def test_permission_denial_does_not_reveal_whether_a_policy_name_exists(catalog_state, name):
    _, catalog, sessions, ids, _ = catalog_state
    seed(catalog_state, versions=(1,))
    with sessions.begin() as session:
        session.execute(delete(RoleBinding).where(RoleBinding.user_id == ids["alice"]))
    with pytest.raises(DomainError) as error:
        catalog.sources(actor(ids), PolicySourceListQuery(name=name))
    assert error.value.code == "PERMISSION_DENIED"


def test_permission_is_checked_again_after_reading_the_directory(catalog_state, monkeypatch):
    _, catalog, sessions, ids, _ = catalog_state
    seed(catalog_state, versions=(1,))
    original = PolicyCatalogRepository.page

    def revoke(repo, selection):
        result = original(repo, selection)
        repo.session.execute(delete(RoleBinding).where(RoleBinding.user_id == ids["alice"]))
        return result

    monkeypatch.setattr(PolicyCatalogRepository, "page", revoke)
    with sessions() as session:
        with pytest.raises(DomainError) as error:
            catalog.sources_in_transaction(session, actor(ids), query())
        assert error.value.code == "PERMISSION_DENIED"
        session.rollback()


def test_directory_is_not_polluted_by_an_uncommitted_tracked_source_object(catalog_state):
    _, catalog, sessions, ids, _ = catalog_state
    seed(catalog_state, versions=(1,))
    expected = catalog.sources(actor(ids), query())
    with sessions() as session:
        record = session.scalars(select(PolicySourceVersion)).one()
        record.fingerprint = "f" * 64
        assert catalog.sources_in_transaction(session, actor(ids), query()) == expected
        assert record.fingerprint == "f" * 64
        session.rollback()


@pytest.mark.parametrize("case", ["fingerprint", "reference", "id"])
def test_invalid_metadata_is_unavailable_instead_of_silently_omitting_a_version(
    catalog_state, monkeypatch, case
):
    _, catalog, sessions, ids, _ = catalog_state
    seed(catalog_state, versions=(1,))
    with sessions() as session:
        records = PolicyCatalogRepository(session).page(query())
    changes = {
        "fingerprint": {"fingerprint": "invalid"},
        "reference": {"name": "another-policy"},
        "id": {"id": "invalid-uuid"},
    }
    monkeypatch.setattr(
        PolicyCatalogRepository,
        "page",
        lambda self, selection: (replace(records[0], **changes[case]),),
    )
    with pytest.raises(DomainError) as error:
        catalog.sources(actor(ids), query())
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"


@pytest.mark.parametrize(
    "changes",
    [
        {"limit": 0},
        {"limit": 51},
        {"limit": True},
        {"after_version": -1},
        {"after_version": True},
        {"name": "*"},
        {"published": True},
    ],
)
def test_directory_inputs_are_bounded_without_boolean_numeric_coercion(changes):
    with pytest.raises(ValidationError):
        PolicySourceListQuery.model_validate({"name": reference().name, **changes})


def test_unchecked_model_copy_cannot_bypass_the_directory_page_size_budget(catalog_state):
    _, catalog, _, ids, _ = catalog_state
    invalid = query().model_copy(update={"limit": 10000})
    with pytest.raises(ValidationError):
        catalog.sources(actor(ids), invalid)


def test_postgresql_directory_is_read_only_and_leaves_source_rows_unchanged(catalog_state):
    _, catalog, sessions, ids, _ = catalog_state
    if sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL source directory READ ONLY contract")
    seed(catalog_state, versions=(1, 2))
    before = source_counts(sessions)
    with sessions.begin() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        page = catalog.sources_in_transaction(session, actor(ids), query(limit=1))
    assert len(page.versions) == 1 and page.next_after_version == 1
    assert source_counts(sessions) == before
