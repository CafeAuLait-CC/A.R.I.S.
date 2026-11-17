import hashlib
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.schemas import AccessRequest, AccessScope, UserPrincipal
from aris.core.access.service import AccessService
from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.commands.schemas import CommandExecution
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction, transaction_savepoint
from aris.core.events import DomainEvent
from aris.core.relay.models import ConsumerInbox, EventDelivery, EventPublication
from aris.core.relay.repo import EventRelayRepository
from aris.core.relay.schemas import (
    ConsumerDefinition,
    DeliveryClaim,
    DeliveryResult,
    EventEnvelope,
    EventSelector,
    PublicationResult,
    RelayLimits,
    ReplayDelivery,
)
from aris.core.time import utc


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=True).encode()
    ).hexdigest()


def event_data(source: DomainEvent) -> dict[str, Any]:
    return {
        "event_id": source.event_id,
        "event_type": source.event_type,
        "event_version": source.event_version,
        "aggregate_type": source.aggregate_type,
        "aggregate_id": source.aggregate_id,
        "aggregate_version": source.aggregate_version,
        "occurred_at": utc(source.occurred_at).isoformat(),
        "recorded_at": utc(source.recorded_at).isoformat(),
        "actor": source.actor,
        "correlation_id": source.correlation_id,
        "causation_id": source.causation_id,
        "payload": source.payload,
    }


class DatabaseEventConsumer(ABC):
    """Trusted database handler, without external I/O or transaction ownership."""

    definition: ConsumerDefinition

    @abstractmethod
    def consume(self, session: Session, envelope: EventEnvelope) -> None:
        """Write only in the relay-owned transaction; a return is not a commit."""


class EventRelay:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        consumers: Sequence[DatabaseEventConsumer],
        limits: RelayLimits,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions, self.limits = sessions, limits
        self.clock = clock or (lambda: datetime.now(UTC))
        if len(consumers) > 32:
            raise ValueError("consumer registration budget exceeded")
        self.access = AccessService(sessions)
        self.registry: dict[str, tuple[ConsumerDefinition, DatabaseEventConsumer]] = {}
        for consumer in consumers:
            definition = ConsumerDefinition.model_validate(consumer.definition.model_dump())
            if definition.consumer_id in self.registry:
                raise ValueError("duplicate consumer registration")
            self.registry[definition.consumer_id] = definition, consumer
        self.registry_fingerprint = fingerprint(
            [self.registry[key][0].model_dump(mode="json") for key in sorted(self.registry)]
        )

    def _now(self) -> datetime:
        return utc(self.clock())

    def _budget(self, session: Session) -> None:
        transaction_budget(
            session,
            self.limits.lock_timeout_ms,
            self.limits.statement_timeout_ms,
            self.limits.transaction_timeout_ms,
        )

    def _audit(
        self, session: Session, delivery: EventDelivery, action: str, code: str | None = None
    ) -> None:
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor="event-relay",
                action=action,
                target=delivery.id,
                request_id=delivery.event_id,
                recorded_at=self._now(),
                changes={
                    "event_id": delivery.event_id,
                    "consumer_id": delivery.consumer_id,
                    "attempt": delivery.attempts,
                    "replay_count": delivery.replay_count,
                    "code": code,
                },
            )
        )

    def publish_once(self) -> PublicationResult:
        selectors = {
            (item.event_type, item.event_version)
            for definition, _ in self.registry.values()
            for item in definition.events
        }
        if not selectors:
            return PublicationResult(published=0, deliveries_queued=0)
        try:
            with self.sessions.begin() as session:
                self._budget(session)
                repo = EventRelayRepository(session)
                pending = repo.unpublished(
                    [
                        EventSelector(event_type=name, event_version=version)
                        for name, version in sorted(selectors)
                    ],
                    self.limits.batch_size,
                )
                queued = 0
                for outbox, source in pending:
                    definitions = [
                        definition
                        for definition, _ in self.registry.values()
                        if any(
                            (item.event_type, item.event_version)
                            == (source.event_type, source.event_version)
                            for item in definition.events
                        )
                    ]
                    if repo.publication(source.event_id) is not None:
                        raise DomainError("CONFLICT", "Event publication metadata differs", 409)
                    now = self._now()
                    session.add(
                        EventPublication(
                            event_id=source.event_id,
                            event_fingerprint=fingerprint(event_data(source)),
                            registry_fingerprint=self.registry_fingerprint,
                            subscriber_count=len(definitions),
                            created_at=now,
                        )
                    )
                    session.flush()
                    for definition in definitions:
                        delivery = EventDelivery(
                            id=str(uuid4()),
                            event_id=source.event_id,
                            consumer_id=definition.consumer_id,
                            definition_fingerprint=fingerprint(definition.model_dump(mode="json")),
                            state="queued",
                            attempts=0,
                            replay_count=0,
                            available_at=now,
                            lease_token=None,
                            lease_expires_at=None,
                            last_error_code=None,
                            created_at=now,
                            completed_at=None,
                        )
                        session.add(delivery)
                        self._audit(session, delivery, "event.delivery.queued")
                        queued += 1
                    outbox.published_at = now
                return PublicationResult(published=len(pending), deliveries_queued=queued)
        except DBAPIError:
            raise DomainError("RELAY_UNAVAILABLE", "Publication outcome unavailable", 503) from None

    def _finish(
        self,
        session: Session,
        delivery: EventDelivery,
        state: Literal["processed", "dead"],
        code: str | None = None,
    ) -> DeliveryResult:
        delivery.state, delivery.completed_at = state, self._now()
        delivery.lease_token, delivery.lease_expires_at = None, None
        delivery.last_error_code = code
        self._audit(session, delivery, "event.delivery." + state, code)
        return DeliveryResult(delivery_id=delivery.id, state=state, code=code)

    def claim(self) -> DeliveryClaim | DeliveryResult | None:
        try:
            with self.sessions.begin() as session:
                self._budget(session)
                delivery = EventRelayRepository(session).next_delivery(
                    self._now(), sorted(self.registry)
                )
                if delivery is None:
                    return None
                if delivery.attempts >= self.limits.max_attempts:
                    return self._finish(session, delivery, "dead", "ATTEMPTS_EXHAUSTED")
                delivery.state, delivery.attempts = "running", delivery.attempts + 1
                delivery.lease_token = str(uuid4())
                delivery.lease_expires_at = self._now() + timedelta(
                    seconds=self.limits.lease_seconds
                )
                self._audit(session, delivery, "event.delivery.claimed")
                return DeliveryClaim(
                    delivery_id=delivery.id,
                    event_id=delivery.event_id,
                    consumer_id=delivery.consumer_id,
                    lease_token=delivery.lease_token,
                    lease_expires_at=delivery.lease_expires_at,
                    attempt=delivery.attempts,
                    replay_count=delivery.replay_count,
                )
        except DBAPIError:
            raise DomainError(
                "RELAY_UNAVAILABLE", "Delivery claim outcome unavailable", 503
            ) from None

    @staticmethod
    def _lost(claim: DeliveryClaim) -> DeliveryResult:
        return DeliveryResult(delivery_id=claim.delivery_id, state="lost_claim")

    def _owned(self, delivery: EventDelivery, claim: DeliveryClaim) -> bool:
        return (
            delivery.event_id == str(claim.event_id)
            and delivery.consumer_id == claim.consumer_id
            and delivery.state == "running"
            and delivery.lease_token == str(claim.lease_token)
            and delivery.attempts == claim.attempt
            and delivery.replay_count == claim.replay_count
            and delivery.lease_expires_at is not None
            and utc(delivery.lease_expires_at) == utc(claim.lease_expires_at)
            and utc(delivery.lease_expires_at) > self._now()
        )

    def _envelope(self, repo: EventRelayRepository, delivery: EventDelivery) -> EventEnvelope:
        source, publication = repo.event(delivery.event_id), repo.publication(delivery.event_id)
        if source is None or publication is None:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Event publication is missing", 503)
        data = event_data(source)
        if fingerprint(data) != publication.event_fingerprint:
            raise DomainError("EVENT_CHANGED", "Published event content differs", 409)
        try:
            envelope = EventEnvelope.model_validate(data)
            encoded = json.dumps(envelope.model_dump(mode="json"), allow_nan=False).encode()
            if len(encoded) > 131072:
                raise ValueError("event budget exceeded")
            return envelope
        except (ValidationError, ValueError, TypeError, OverflowError):
            raise DomainError("EVENT_INVALID", "Event contract is invalid", 422) from None

    def execute(self, claim: DeliveryClaim) -> DeliveryResult:
        try:
            with self.sessions.begin() as session:
                self._budget(session)
                repo = EventRelayRepository(session)
                delivery = repo.delivery(str(claim.delivery_id), lock=True)
                if delivery is None:
                    return self._lost(claim)
                if delivery.state in {"processed", "dead"}:
                    return DeliveryResult(
                        delivery_id=delivery.id, state=delivery.state, code=delivery.last_error_code
                    )
                if not self._owned(delivery, claim):
                    return self._lost(claim)
                envelope = self._envelope(repo, delivery)
                inbox = repo.inbox(delivery)
                if inbox is not None:
                    publication = repo.publication(delivery.event_id)
                    if (
                        publication is None
                        or inbox.event_fingerprint != publication.event_fingerprint
                    ):
                        raise DomainError("EVENT_CHANGED", "Processed event differs", 409)
                    return self._finish(session, delivery, "processed")
                registration = self.registry.get(delivery.consumer_id)
                if registration is None:
                    raise DomainError("CONSUMER_UNAVAILABLE", "Consumer is not registered", 503)
                definition, consumer = registration
                if (
                    fingerprint(definition.model_dump(mode="json"))
                    != delivery.definition_fingerprint
                ):
                    raise DomainError("CONSUMER_CHANGED", "Consumer definition differs", 409)
                if not any(
                    item.event_type == envelope.event_type
                    and item.event_version == envelope.event_version
                    for item in definition.events
                ):
                    raise DomainError("EVENT_UNSUPPORTED", "Consumer does not accept event", 409)

                def check_lease(active: Session) -> None:
                    if self._now() >= utc(claim.lease_expires_at):
                        raise DomainError("LEASE_EXPIRED", "Consumer lease expired", 409)

                def connection_commit(active: Connection) -> None:
                    check_lease(session)

                def deny_commit(active: Session) -> None:
                    if active.get_nested_transaction() is None:
                        raise DomainError("INTERNAL_ERROR", "Consumer cannot commit", 500)

                event.listen(session, "before_commit", deny_commit)
                try:
                    with transaction_savepoint(session):
                        consumer.consume(session, envelope)
                finally:
                    event.remove(session, "before_commit", deny_commit)
                check_lease(session)
                publication = repo.publication(delivery.event_id)
                assert publication is not None
                session.add(
                    ConsumerInbox(
                        consumer_id=delivery.consumer_id,
                        event_id=delivery.event_id,
                        delivery_id=delivery.id,
                        event_fingerprint=publication.event_fingerprint,
                        definition_fingerprint=delivery.definition_fingerprint,
                        processed_at=self._now(),
                    )
                )
                result = self._finish(session, delivery, "processed")
                session.flush()
                check_lease(session)
                event.listen(session, "before_commit", check_lease)
                connection = session.connection()
                event.listen(connection, "commit", connection_commit)
                try:
                    commit_owned_transaction(session)
                finally:
                    event.remove(session, "before_commit", check_lease)
                    event.remove(connection, "commit", connection_commit)
                return result
        except DBAPIError as error:
            return self._recover(claim, database_failure(error))
        except DomainError as error:
            code = (
                error.code
                if re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", error.code)
                else "INTERNAL_ERROR"
            )
            return self._recover(claim, code)
        except Exception:
            return self._recover(claim, "INTERNAL_ERROR")

    def _recover(self, claim: DeliveryClaim, code: str) -> DeliveryResult:
        try:
            with self.sessions.begin() as session:
                self._budget(session)
                delivery = EventRelayRepository(session).delivery(str(claim.delivery_id), lock=True)
                if delivery is None:
                    return self._lost(claim)
                if delivery.state in {"processed", "dead"}:
                    return DeliveryResult(
                        delivery_id=delivery.id, state=delivery.state, code=delivery.last_error_code
                    )
                if not self._owned(delivery, claim):
                    return self._lost(claim)
                delay = min(60, self.limits.retry_base_seconds * 2 ** (delivery.attempts - 1))
                if (
                    code == "RETRYABLE_DATABASE_ERROR"
                    and delivery.attempts < self.limits.max_attempts
                ):
                    delivery.state, delivery.available_at = (
                        "queued",
                        self._now() + timedelta(seconds=delay),
                    )
                    delivery.lease_token, delivery.lease_expires_at = None, None
                    delivery.last_error_code = code
                    self._audit(session, delivery, "event.delivery.retry", code)
                    return DeliveryResult(delivery_id=delivery.id, state="retry", code=code)
                return self._finish(session, delivery, "dead", code)
        except DBAPIError:
            raise DomainError("RELAY_UNAVAILABLE", "Consumer outcome unavailable", 503) from None

    def run_once(self) -> DeliveryResult | None:
        claimed = self.claim()
        return self.execute(claimed) if isinstance(claimed, DeliveryClaim) else claimed

    def replay(
        self,
        session: Session,
        execution: CommandExecution,
        principal: UserPrincipal,
        request: ReplayDelivery,
    ) -> dict[str, Any]:
        scope = AccessScope(type="global", id="*")
        if execution.actor_id != principal.user_id or execution.scope != scope:
            raise DomainError("PERMISSION_DENIED", "Replay execution scope differs", 403)
        access = AccessRequest(user_id=execution.actor_id, permission="events.replay", scope=scope)
        with transaction_savepoint(session):
            self.access.require_in_transaction(session, access)
            repo = EventRelayRepository(session)
            delivery = repo.delivery(str(request.delivery_id), lock=True)
            if delivery is None:
                raise DomainError("CONFLICT", "Delivery is not available", 409)
            if delivery.state != "dead" or delivery.replay_count != request.expected_replay_count:
                raise DomainError("CONFLICT", "Delivery state or replay count changed", 409)
            envelope = self._envelope(repo, delivery)
            if repo.inbox(delivery) is not None:
                raise DomainError("CONFLICT", "Processed consumer cannot be replayed", 409)
            registration = self.registry.get(delivery.consumer_id)
            if registration is None:
                raise DomainError("CONSUMER_UNAVAILABLE", "Consumer is not registered", 503)
            if not any(
                item.event_type == envelope.event_type
                and item.event_version == envelope.event_version
                for item in registration[0].events
            ):
                raise DomainError("EVENT_UNSUPPORTED", "Consumer does not accept event", 409)
            delivery.definition_fingerprint = fingerprint(registration[0].model_dump(mode="json"))
            original_code = delivery.last_error_code
            delivery.state, delivery.attempts = "queued", 0
            delivery.replay_count += 1
            delivery.available_at, delivery.completed_at = self._now(), None
            delivery.lease_token, delivery.lease_expires_at, delivery.last_error_code = (
                None,
                None,
                None,
            )
            session.add(
                AuditEntry(
                    id=str(uuid4()),
                    actor=str(execution.actor_id),
                    action="event.delivery.replayed",
                    target=delivery.id,
                    request_id=execution.request_id,
                    recorded_at=self._now(),
                    changes={
                        "event_id": delivery.event_id,
                        "consumer_id": delivery.consumer_id,
                        "previous_code": original_code,
                        "replay_count": delivery.replay_count,
                        "reason": request.reason,
                        "command_id": str(execution.command_id),
                    },
                )
            )
            session.flush()
            self.access.require_in_transaction(session, access)
            return {
                "delivery_id": delivery.id,
                "state": delivery.state,
                "replay_count": delivery.replay_count,
            }
