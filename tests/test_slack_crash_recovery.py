"""Kill only disposable Worker fixtures, using a fake publisher throughout."""

import json
import os
import selectors
import signal
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import aris.interfaces.slack.dispatch as dispatch_module
from aris.core.audit import AuditEntry
from aris.core.db import build_engine, session_factory
from aris.core.time import utc
from aris.interfaces.slack.dispatch import SlackDispatcher
from aris.interfaces.slack.models import SlackHomeJob, SlackReceipt
from aris.interfaces.slack.schemas import HomeJobClaim
from aris.modules.vega.service import VegaService
from tests.test_slack import (
    Publisher,
    home_event,
    send_event,
    slack_client,
    slack_settings,
)

# Register the shared fixtures imported above without duplicating their setup.
__all__ = ["slack_client", "slack_settings"]


@pytest.mark.parametrize("phase", ["before_publish", "after_publish"])
def test_abrupt_worker_exit_reclaims_committed_job_without_replaying_completed_work(
    slack_client, settings, slack_settings, monkeypatch, phase
):
    assert send_event(slack_client, home_event()).status_code == 200
    program = textwrap.dedent("""
        import json, os, sys
        from dataclasses import asdict
        from aris.core.config import Settings
        from aris.core.db import build_engine, session_factory
        from aris.interfaces.slack.config import SlackSettings
        from aris.interfaces.slack.dispatch import SlackDispatcher
        from aris.modules.vega.service import VegaService

        config = Settings(_env_file=None, environment='test',
                          database_url=os.environ['ARIS_CRASH_TEST_DATABASE_URL'])
        slack = SlackSettings(_env_file=None, signing_secret='fixture-secret',
                              bot_token='xoxb-fixture-only', team_id='T111')
        engine = build_engine(config.database_url)
        sessions = session_factory(engine)

        class FakePublisher:
            def publish_home(self, user_id, view):
                assert user_id == 'U111' and view['type'] == 'home'
                if sys.argv[1] == 'before_publish':
                    stop_at_crash_point(None, None)
                self.published = True

        publisher = FakePublisher()
        worker = SlackDispatcher(sessions, slack, VegaService(sessions, config), publisher)
        original_claim = worker.claim

        def capture_claim():
            claim = original_claim()
            worker.captured = claim
            return claim

        def stop_at_crash_point(claim, error):
            assert error is None
            captured = asdict(worker.captured)
            captured['lease_until'] = captured['lease_until'].isoformat()
            print(json.dumps({'claim': captured,
                              'fake_published': getattr(publisher, 'published', False)}),
                  flush=True)
            sys.stdin.read()
            raise AssertionError('fixture must be killed at its crash point')

        worker.claim = capture_claim
        worker.finish = stop_at_crash_point
        worker.run_once()
    """)
    env = {**os.environ, "ARIS_CRASH_TEST_DATABASE_URL": settings.database_url}
    child = subprocess.Popen(
        [sys.executable, "-c", program, phase],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(child.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=10), "fixture Worker did not reach its crash point"
        line = child.stdout.readline()
        assert line, "fixture Worker exited before reaching its crash point"
        marker = json.loads(line)
        assert marker["fake_published"] is (phase == "after_publish")
        claim_values = marker["claim"]
        claim_values["lease_until"] = datetime.fromisoformat(claim_values["lease_until"])
        first = HomeJobClaim(**claim_values)
        assert first.attempts == 1
        with Session(slack_client.app.state.engine) as session:
            job = session.get(SlackHomeJob, first.job_id)
            assert job.state == "processing" and job.lease_token == first.lease_token
            original_receipt = job.receipt_id
        child.kill()
        assert child.wait(timeout=5) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)

    # Advance only the fixture claim clock; there is no real wait or DB editing
    # to force expiry, and no running Worker or external Slack API is involved.
    now = first.lease_until - timedelta(seconds=1)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(dispatch_module, "datetime", Clock)
    engine = build_engine(settings.database_url)
    try:
        sessions = session_factory(engine)
        publisher = Publisher()
        restarted = SlackDispatcher(
            sessions, slack_settings, VegaService(sessions, settings), publisher
        )
        assert restarted.claim() is None
        now = first.lease_until + timedelta(seconds=1)
        assert restarted.finish(first, None).outcome == "lease_lost"
        assert restarted.run_once().outcome == "delivered"
        assert len(publisher.calls) == 1
        assert restarted.finish(first, None).outcome == "lease_lost"
        assert restarted.run_once().outcome == "idle"
        with sessions() as session:
            job = session.get(SlackHomeJob, first.job_id)
            assert job.state == "completed" and job.attempts == 2
            assert job.receipt_id == original_receipt and utc(job.completed_at) == now
            assert job.lease_token is None and job.lease_until is None
            assert job.last_error_code is None
            assert session.scalar(select(func.count()).select_from(SlackHomeJob)) == 1
            assert session.scalar(select(func.count()).select_from(SlackReceipt)) == 1
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AuditEntry)
                    .where(AuditEntry.action == "slack.home.delivered")
                )
                == 1
            )
        # A further clean restart cannot publish a completed task again. If the
        # first fake publish preceded the crash, two publishes were possible;
        # the contract is at least once, never exactly once across that gap.
        another = SlackDispatcher(
            sessions, slack_settings, VegaService(sessions, settings), publisher
        )
        assert another.run_once().outcome == "idle"
        assert len(publisher.calls) == 1
    finally:
        engine.dispose()
