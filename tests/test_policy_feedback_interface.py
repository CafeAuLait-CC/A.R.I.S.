import pytest
from sqlalchemy import delete, func, select

from aris.apps.composition.policy import build_policy_draft_components
from aris.core.access.models import RoleBinding
from aris.core.commands.gateway_schemas import UserCommandInput
from aris.core.commands.models import CommandRecord
from aris.interfaces.policy.service import PolicyReadSurface
from aris.modules.nova.models import PolicyReviewFeedbackRow
from aris.modules.nova.policy_feedback_service import PolicyFeedbackService
from tests.test_commands import command_state
from tests.test_policy_catalog import catalog_state
from tests.test_policy_draft_editor import client_for, draft_runtime, headers
from tests.test_policy_feedback import request as domain_request
from tests.test_policy_read_interface import TOKEN, policy_surface, seed

__all__ = ["command_state", "catalog_state", "policy_surface"]


@pytest.fixture
def feedback_editor(policy_surface):
    read, state = policy_surface
    seed(state, 1, 2)
    graph = build_policy_draft_components(
        state[2],
        read.authenticator,
        read.policy_names,
        draft_runtime(feedback_writing=True),
        clock=read.catalog.clock,
    )
    service = PolicyReadSurface(
        read.catalog,
        read.authenticator,
        read.policy_names,
        draft_commands=graph.user_api,
        feedback=PolicyFeedbackService(read.catalog),
    )
    return service, graph, state


def parameters(editor):
    return domain_request((None, editor[0].feedback, editor[2])).parameters


def directory(params):
    return {
        "source": params["source"],
        "expected_source_fingerprint": params["expected_source_fingerprint"],
    }


def test_feedback_preview_confirm_worker_and_original_id_recovery_share_the_same_request(
    feedback_editor, settings
):
    editor = feedback_editor
    params = parameters(editor)
    with client_for(editor, settings) as client:
        session = client.post("/internal/policy/session", headers=headers()).json()
        assert session["feedback_available"] and session["feedback_writing_available"]
        with editor[2][2]() as db:
            before = db.scalar(select(func.count()).select_from(CommandRecord))
        preview = client.post("/internal/policy/feedback/validate", headers=headers(), json=params)
        assert preview.status_code == 200 and preview.json()["request"] == params
        with editor[2][2]() as db:
            assert db.scalar(select(func.count()).select_from(CommandRecord)) == before
        accepted = client.post(
            "/internal/commands",
            headers=headers("feedback-ui"),
            json={"command": "policy.record_feedback", "parameters": params},
        )
        pending = accepted.json()["outcome"]
        path = "/internal/commands/" + pending["command_id"]
        review = client.get(path + "/review", headers=headers()).json()
        assert review["request"]["parameters"] == params
        queued = client.post(
            path + "/confirm",
            headers=headers("feedback-confirm"),
            json={"proof": review["outcome"]["proof"]},
        )
        assert queued.json()["outcome"]["state"] == "queued"
        assert editor[1].worker.run_once().state == "succeeded"
        recovered = client.get(path + "/review", headers=headers()).json()
        assert recovered["outcome"]["state"] == "succeeded"
        assert recovered["outcome"]["result"]["feedback_only"]
        page = client.post("/internal/policy/feedback", headers=headers(), json=directory(params))
        assert page.status_code == 200 and page.headers["Cache-Control"] == "no-store"
        assert len(page.json()["items"]) == 1
        assert page.json()["items"][0]["request"] == params
        replay = editor[1].user_api.submit(
            "Bearer " + TOKEN,
            "recovered-feedback",
            "feedback-ui",
            UserCommandInput(command="policy.record_feedback", parameters=params),
        )
        assert replay.outcome.result == recovered["outcome"]["result"]


def test_feedback_routes_are_opt_in_and_read_mode_does_not_admit_feedback_commands(
    feedback_editor, policy_surface, settings
):
    params = parameters(feedback_editor)
    read, state = policy_surface
    default_graph = build_policy_draft_components(
        state[2], read.authenticator, read.policy_names, draft_runtime(), clock=read.catalog.clock
    )
    with client_for((read, default_graph, state), settings) as client:
        assert (
            client.post(
                "/internal/policy/feedback", headers=headers(), json=directory(params)
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/internal/policy/feedback/validate", headers=headers(), json=params
            ).status_code
            == 404
        )
    reader = PolicyReadSurface(
        read.catalog,
        read.authenticator,
        read.policy_names,
        feedback=PolicyFeedbackService(read.catalog),
    )
    with client_for((reader, default_graph, state), settings) as client:
        connected = client.post("/internal/policy/session", headers=headers()).json()
        assert connected["feedback_available"] and not connected["feedback_writing_available"]
        assert (
            client.post(
                "/internal/policy/feedback", headers=headers(), json=directory(params)
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/internal/policy/feedback/validate", headers=headers(), json=params
            ).status_code
            == 404
        )
        forbidden = client.post(
            "/internal/commands",
            headers=headers("read-feedback"),
            json={"command": "policy.record_feedback", "parameters": params},
        )
        assert forbidden.status_code == 403


def test_feedback_name_fingerprint_author_and_current_access_boundaries_are_server_checked(
    feedback_editor, settings
):
    editor, params = feedback_editor, parameters(feedback_editor)
    with client_for(editor, settings) as client:
        for changes, status in (
            ({"expected_source_fingerprint": "f" * 64}, 409),
            ({"source": {"name": "other-policy", "version": 1}}, 403),
            ({"created_by": "forged-client-author"}, 422),
            ({"body": "   \n"}, 422),
        ):
            response = client.post(
                "/internal/policy/feedback/validate", headers=headers(), json={**params, **changes}
            )
            assert response.status_code == status
        assert client.post("/internal/policy/feedback", json=directory(params)).status_code == 401
        with editor[2][2].begin() as db:
            db.execute(delete(RoleBinding).where(RoleBinding.user_id == editor[2][3]["alice"]))
        assert (
            client.post(
                "/internal/policy/feedback", headers=headers(), json=directory(params)
            ).status_code
            == 403
        )
        with editor[2][2]() as db:
            assert db.scalar(select(func.count()).select_from(PolicyReviewFeedbackRow)) == 0
