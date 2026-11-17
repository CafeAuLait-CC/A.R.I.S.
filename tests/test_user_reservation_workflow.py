from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, func, select

from aris.apps.api.main import create_app
from aris.core.access.models import ExternalIdentity, RoleBinding
from aris.core.commands.gateway import UserCommandGateway
from aris.core.commands.intake import CommandIntakeService
from aris.core.commands.worker import CommandWorker
from aris.core.notifications.dispatch import NotificationDispatcher
from aris.core.notifications.models import NotificationOutbox
from aris.core.notifications.service import NotificationService
from aris.core.relay.models import ConsumerInbox
from aris.core.relay.service import EventRelay
from aris.interfaces.commands.auth import BoundSubjectTokens, UserTokenBinding
from aris.interfaces.commands.service import UserCommandSurface
from aris.interfaces.slack.config import SlackSettings
from aris.modules.nova.models import Reservation, ReservationEvent
from aris.modules.nova.notifications import (
    ReservationNoticeTemplate,
    ReservationNotificationConsumer,
)
from aris.modules.vega.target_service import VegaTargetService
from tests.test_command_intake import LIMITS as INTAKE_LIMITS
from tests.test_command_intake import NAMESPACE, WORKER_LIMITS, subject
from tests.test_event_relay import LIMITS as RELAY_LIMITS
from tests.test_notifications import LIMITS as NOTIFICATION_LIMITS
from tests.test_notifications import FixtureTransport
from tests.test_reservation_service import booking as booking
from tests.test_reservation_service import request as reservation_request
from tests.test_user_commands import ALICE_TOKEN, BOB_TOKEN, LIMITS, headers


@pytest.mark.parametrize("change", ["none", "grant_revoked", "facts_stale", "rules_withdrawn"])
def test_http_identity_confirmation_real_reservation_outbox_and_mock_notice_form_one_flow(
    booking, settings, change
):
    reservations, commands, sessions, ids, clock, _ = booking
    with sessions.begin() as session:
        for user in ("alice", "bob"):
            session.add(
                ExternalIdentity(id=str(uuid4()), user_id=ids[user], **subject(user).model_dump())
            )
    intake = CommandIntakeService(
        commands, NAMESPACE, INTAKE_LIMITS, confirmation_key_id="fixture-reservation-key"
    )
    auth = BoundSubjectTokens(
        [
            UserTokenBinding(subject=subject(), token=SecretStr(ALICE_TOKEN)),
            UserTokenBinding(subject=subject("bob"), token=SecretStr(BOB_TOKEN)),
        ],
        reserved_tokens=[settings.read_token],
    )
    surface = UserCommandSurface(UserCommandGateway(intake, ["reservation.create"], LIMITS), auth)
    notifications = NotificationService(
        sessions, NAMESPACE, [ReservationNoticeTemplate()], clock=lambda: clock["now"]
    )
    transport = FixtureTransport(sessions)
    dispatch = NotificationDispatcher(notifications, NOTIFICATION_LIMITS, transport)

    relay = EventRelay(
        sessions,
        [ReservationNotificationConsumer(notifications, VegaTargetService(sessions, settings))],
        RELAY_LIMITS,
        clock=lambda: clock["now"],
    )
    with TestClient(
        create_app(settings, SlackSettings(_env_file=None), user_commands=surface),
        raise_server_exceptions=False,
    ) as client:
        body = {"command": "reservation.create", "parameters": reservation_request().parameters}
        submitted = client.post("/internal/commands", headers=headers(), json=body)
        assert submitted.status_code == 202, submitted.text
        pending = submitted.json()["outcome"]
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(Reservation)) == 0
        confirmed = client.post(
            f"/internal/commands/{pending['command_id']}/confirm",
            headers=headers(),
            json={"proof": pending["proof"]},
        )
        assert confirmed.status_code == 202 and confirmed.json()["outcome"]["state"] == "queued"
        assert not transport.sent and relay.publish_once().published == 0
        if change == "grant_revoked":
            with sessions.begin() as session:
                session.execute(delete(RoleBinding).where(RoleBinding.user_id == ids["alice"]))
        elif change == "facts_stale":
            clock["now"] += timedelta(seconds=settings.stale_after_seconds)
        elif change == "rules_withdrawn":
            reservations.availability.rules = None
        result = CommandWorker(intake, WORKER_LIMITS).run_once()
        if change != "none":
            assert result.state == "failed"
            with sessions() as session:
                assert session.scalar(select(func.count()).select_from(Reservation)) == 0
                assert session.scalar(select(func.count()).select_from(ReservationEvent)) == 0
            assert relay.publish_once().published == 0 and dispatch.run_once() is None
            assert not transport.sent
            return
        assert result.state == "succeeded"
        status = client.get(f"/internal/commands/{pending['command_id']}", headers=headers())
        assert status.status_code == 200 and status.json()["state"] == "succeeded"
        assert status.json()["result"]["reservation"]["status"] == "confirmed"
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(Reservation)) == 1
            assert session.scalar(select(func.count()).select_from(ReservationEvent)) == 1
        other = client.get(
            f"/internal/commands/{pending['command_id']}", headers=headers(token=BOB_TOKEN)
        )
        assert other.status_code == 403 and "Private fixture purpose" not in other.text
        assert relay.publish_once().published == 1
        assert relay.run_once().state == "processed"
        assert not transport.sent
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(ConsumerInbox)) == 1
            assert session.scalar(select(func.count()).select_from(NotificationOutbox)) == 1
        assert dispatch.run_once().state == "delivered" and len(transport.sent) == 1
        assert transport.sent[0].target == subject()
        assert dispatch.run_once() is None and relay.run_once() is None
