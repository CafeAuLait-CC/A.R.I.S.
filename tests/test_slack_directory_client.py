import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError

from aris.core.contracts import DomainError
from aris.interfaces.identity.slack_directory import SlackDirectoryClient, SlackRosterQuery
from tests.test_slack import slack_settings as slack_settings

NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)


def member(**changes):
    return {
        "id": "U111",
        "team_id": "T111",
        "name": "slack-handle",
        "deleted": False,
        "is_bot": False,
        "is_app_user": False,
        "tz": "America/Vancouver",
        "profile": {
            "display_name": "Alex <script>unsafe</script>",
            "email": "private@example.invalid",
            "status_text": "Private status",
            "image_512": "https://private.invalid/avatar",
        },
        **changes,
    }


def client(settings, payload=None, *, auth_team="T111", response=None):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("auth.test"):
            return httpx.Response(200, json={"ok": True, "team_id": auth_team})
        return (
            response
            if response is not None
            else httpx.Response(
                200,
                json=payload
                or {
                    "ok": True,
                    "members": [member()],
                    "response_metadata": {"next_cursor": "opaque-next=="},
                },
            )
        )

    return SlackDirectoryClient(settings, httpx.MockTransport(handler), clock=lambda: NOW), calls


def test_directory_verifies_workspace_and_reads_one_bounded_page_without_private_profile(
    slack_settings,
):
    directory, calls = client(slack_settings)
    page = directory.page(SlackRosterQuery(cursor="opaque-start=="))
    assert [request.method for request in calls] == ["POST", "GET"]
    assert [request.url.path for request in calls] == ["/api/auth.test", "/api/users.list"]
    assert dict(calls[-1].url.params) == {
        "limit": "50",
        "team_id": "T111",
        "cursor": "opaque-start==",
    }
    assert all(
        request.headers["authorization"] == "Bearer " + slack_settings.bot_token.get_secret_value()
        for request in calls
    )
    assert page.team_id == "T111" and page.fetched_at == NOW and page.next_cursor == "opaque-next=="
    person = page.members[0]
    assert (
        person.display_name == "Alex <script>unsafe</script>"
        and person.timezone == "America/Vancouver"
    )
    assert person.deleted is False and person.is_bot is False
    serialized = page.model_dump_json()
    assert (
        "private@example" not in serialized
        and "Private status" not in serialized
        and "avatar" not in serialized
    )
    assert (
        slack_settings.bot_token.get_secret_value() not in serialized
        and "opaque-next" not in repr(page)
    )


def test_directory_rejects_another_workspaces_token_before_fetching_users(slack_settings):
    directory, calls = client(slack_settings, auth_team="TOTHER")
    with pytest.raises(DomainError) as refused:
        directory.page(SlackRosterQuery())
    assert refused.value.code == "SLACK_DIRECTORY_UNAVAILABLE" and len(calls) == 1


def test_absent_optional_details_remain_unknown_and_cross_workspace_member_is_data_only(
    slack_settings,
):
    directory, _ = client(
        slack_settings,
        {
            "ok": True,
            "members": [
                {"id": "W222", "team_id": "TEXTERNAL", "tz": "not-a-timezone", "profile": None}
            ],
        },
    )
    person = directory.page(SlackRosterQuery()).members[0]
    assert person.display_name == "W222" and person.timezone is None
    assert person.deleted is None and person.is_bot is None and person.home_team_id == "TEXTERNAL"


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True, "members": [member()] * 51},
        {"ok": True, "members": [member(), member()]},
        {"ok": True, "members": [member(deleted="false")]},
        {"ok": True, "members": [{}]},
        {"ok": True, "members": None},
        {"ok": True, "members": [], "response_metadata": "bad"},
        {"ok": True, "members": [], "response_metadata": {"next_cursor": "x" * 2049}},
        {"ok": True, "members": [member(profile="not-a-profile")]},
    ],
)
def test_invalid_or_unbounded_upstream_data_never_becomes_a_false_empty_roster(
    slack_settings, payload
):
    directory, calls = client(slack_settings, payload)
    with pytest.raises(DomainError) as unavailable:
        directory.page(SlackRosterQuery())
    assert unavailable.value.code == "SLACK_DIRECTORY_UNAVAILABLE" and len(calls) == 2


@pytest.mark.parametrize(
    "response,code,status",
    [
        (httpx.Response(429, headers={"retry-after": "60"}), "SLACK_DIRECTORY_RATE_LIMITED", 429),
        (
            httpx.Response(200, json={"ok": False, "error": "missing_scope"}),
            "SLACK_DIRECTORY_SCOPE_MISSING",
            503,
        ),
        (
            httpx.Response(200, json={"ok": False, "error": "invalid_cursor"}),
            "SLACK_DIRECTORY_CURSOR_INVALID",
            422,
        ),
        (
            httpx.Response(200, json={"ok": False, "error": "private arbitrary secret"}),
            "SLACK_DIRECTORY_UNAVAILABLE",
            503,
        ),
        (
            httpx.Response(302, headers={"location": "https://another.invalid/steal"}),
            "SLACK_DIRECTORY_UNAVAILABLE",
            503,
        ),
        (httpx.Response(503), "SLACK_DIRECTORY_UNAVAILABLE", 503),
        (httpx.Response(200, text="not-json"), "SLACK_DIRECTORY_UNAVAILABLE", 503),
        (
            httpx.Response(
                200,
                content=json.dumps({"ok": True, "members": [], "unused": "x" * 524288}).encode(),
            ),
            "SLACK_DIRECTORY_UNAVAILABLE",
            503,
        ),
    ],
)
def test_upstream_errors_are_sanitized_and_not_retried(slack_settings, response, code, status):
    directory, calls = client(slack_settings, response=response)
    with pytest.raises(DomainError) as rejected:
        directory.page(SlackRosterQuery())
    assert rejected.value.code == code and rejected.value.status == status
    assert "private arbitrary" not in str(rejected.value) and len(calls) == 2


@pytest.mark.parametrize("cursor", ["", "with whitespace", "x" * 2049])
def test_cursor_is_bounded_opaque_data(cursor):
    with pytest.raises(ValidationError):
        SlackRosterQuery(cursor=cursor)


def test_directory_has_a_total_body_budget_and_sanitizes_transport_failures(
    slack_settings, monkeypatch
):
    directory, calls = client(slack_settings)
    ticks = iter([0, 4])
    monkeypatch.setattr(
        "aris.interfaces.identity.slack_directory.time.monotonic", lambda: next(ticks, 4)
    )
    with pytest.raises(DomainError) as timed_out:
        directory.page(SlackRosterQuery())
    assert timed_out.value.code == "SLACK_DIRECTORY_UNAVAILABLE" and len(calls) == 1
    monkeypatch.undo()

    def failed(request):
        raise httpx.ReadTimeout("private transport details must not escape")

    directory = SlackDirectoryClient(slack_settings, httpx.MockTransport(failed))
    with pytest.raises(DomainError) as unavailable:
        directory.page(SlackRosterQuery())
    assert unavailable.value.code == "SLACK_DIRECTORY_UNAVAILABLE"
    assert "private transport" not in str(unavailable.value)
