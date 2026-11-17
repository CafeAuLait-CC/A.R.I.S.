import time
from contextlib import contextmanager
from datetime import timedelta
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import event

from aris.apps.composition.m2 import build_m2_components
from aris.core.commands.gateway_schemas import ExternalCommandActor
from aris.interfaces.commands.auth import UserTokenBinding
from aris.interfaces.slack.config import SlackSettings
from aris.modules.nova.reservation_schemas import MineQuery
from tests.test_availability import RULES, calendar, query
from tests.test_command_intake import NAMESPACE, subject
from tests.test_home_reservations import NS, SUBJECT
from tests.test_home_reservations import home_state as home_state
from tests.test_m2_composition import config
from tests.test_reservation_service import booking as booking
from tests.test_reservation_service import create
from tests.test_user_commands import ALICE_TOKEN
from tests.test_user_reservations import mine_api as mine_api


@contextmanager
def remote_round_trips(engine):
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL transaction timeout across remote reservation reads")
    calls = []

    def latency(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            calls.append(statement)
            time.sleep(0.055)

    event.listen(engine, "after_cursor_execute", latency)
    started = time.monotonic()
    try:
        yield
    finally:
        event.remove(engine, "after_cursor_execute", latency)
    assert len(calls) >= 5 and time.monotonic() - started > 0.25


@pytest.mark.parametrize("operation", ["mine", "detail", "choices", "availability"])
def test_composed_reservation_reads_tolerate_remote_round_trips_without_slowing_gateway(
    mine_api, settings, operation
):
    state = mine_api[2]
    created = create(state, start=state[4]["now"] + timedelta(hours=4)).result["reservation"]
    slack = SlackSettings(
        _env_file=None,
        team_id=NAMESPACE.namespace,
        bot_token="fixture-latency-bot",
        signing_secret="fixture-latency-signing",
    )
    cfg = config(
        commands=("reservation.cancel",),
        rules=RULES,
        maintenance=(calendar(),),
        user_tokens=(UserTokenBinding(subject=subject(), token=SecretStr(ALICE_TOKEN)),),
    )
    graph = build_m2_components(state[2], settings, slack, cfg, clock=lambda: state[4]["now"])
    context = ExternalCommandActor(subject=subject(), request_id="fixture-remote-read")
    queries = graph.user_reservations.queries
    assert graph.user_api.gateway.limits.transaction_timeout_ms <= 250
    with remote_round_trips(state[2].kw["bind"]):
        if operation == "mine":
            result = queries.mine_labeled(context, MineQuery())
            assert [str(item.id) for item in result.page.reservations] == [created["id"]]
        elif operation == "detail":
            result = queries.detail(context, UUID(created["id"]))
            assert str(result.reservation.id) == created["id"]
        elif operation == "choices":
            assert [item.gpu_uuid for item in queries.create_choices(context).labels] == [
                "GPU-target"
            ]
        else:
            assert queries.availability(context, query()).code == "AVAILABLE"


def test_composed_home_projection_tolerates_remote_reads_without_faking_unknown(
    home_state, settings
):
    _, snapshot, state, _ = home_state
    slack = SlackSettings(
        _env_file=None,
        team_id=NS.namespace,
        bot_token="fixture-latency-bot",
        signing_secret="fixture-latency-signing",
    )
    cfg = config(
        namespace=NS,
        commands=("reservation.cancel",),
        rules=None,
        maintenance=(),
        reservation_home=True,
    )
    graph = build_m2_components(state[2], settings, slack, cfg, clock=lambda: state[4]["now"])
    with remote_round_trips(state[2].kw["bind"]):
        assert (
            graph.home_reservations.snapshot(SUBJECT, snapshot).state_for("node-a", "GPU-target")
            == "occupied"
        )
