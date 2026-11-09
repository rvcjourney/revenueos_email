from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.modules.scheduler.schemas import (
    ClaimResult,
    DueMessageCandidate,
    SchedulerIterationSummary,
    SendTaskPayload,
)
from app.modules.scheduler.service import OutboxPublisherService, SchedulerService


# ---------------------------------------------------------------------------
# SendTaskPayload: Schema & Strict Forbidden-Field Validation
# ---------------------------------------------------------------------------


def test_send_task_payload_valid() -> None:
    work_id = str(uuid.uuid4())
    delivery_id = str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    workspace_id = str(uuid.uuid4())

    payload = SendTaskPayload(
        schema_version=1,
        work_id=work_id,
        delivery_id=delivery_id,
        message_id=message_id,
        dispatch_id=delivery_id,
        resource_id=message_id,
        workspace_id=workspace_id,
        dispatch_generation=1,
        correlation_id="corr-123",
    )

    data = payload.model_dump()
    assert data["work_id"] == work_id
    assert data["delivery_id"] == delivery_id
    assert data["message_id"] == message_id
    assert data["dispatch_generation"] == 1
    assert data["correlation_id"] == "corr-123"


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "body",
        "html_body",
        "text_body",
        "subject",
        "access_token",
        "refresh_token",
        "token",
        "password",
        "secret",
        "api_key",
        "lead_name",
        "lead_email",
        "email",
        "to_address",
    ],
)
def test_send_task_payload_rejects_sensitive_fields(forbidden_key: str) -> None:
    kwargs = {
        "schema_version": 1,
        "work_id": str(uuid.uuid4()),
        "delivery_id": str(uuid.uuid4()),
        "message_id": str(uuid.uuid4()),
        "dispatch_id": str(uuid.uuid4()),
        "resource_id": str(uuid.uuid4()),
        "workspace_id": str(uuid.uuid4()),
        "dispatch_generation": 1,
        forbidden_key: "sensitive_data",
    }
    with pytest.raises(ValidationError):
        SendTaskPayload(**kwargs)


def test_send_task_payload_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        SendTaskPayload(
            schema_version=1,
            work_id=str(uuid.uuid4()),
            delivery_id=str(uuid.uuid4()),
            message_id=str(uuid.uuid4()),
            dispatch_id=str(uuid.uuid4()),
            resource_id=str(uuid.uuid4()),
            workspace_id=str(uuid.uuid4()),
            dispatch_generation=1,
            some_arbitrary_field="value",
        )


def test_send_task_payload_rejects_non_positive_generation() -> None:
    with pytest.raises(ValidationError):
        SendTaskPayload(
            schema_version=1,
            work_id=str(uuid.uuid4()),
            delivery_id=str(uuid.uuid4()),
            message_id=str(uuid.uuid4()),
            dispatch_id=str(uuid.uuid4()),
            resource_id=str(uuid.uuid4()),
            workspace_id=str(uuid.uuid4()),
            dispatch_generation=0,
        )


# ---------------------------------------------------------------------------
# Candidate & Result Schemas
# ---------------------------------------------------------------------------


def test_due_message_candidate_schema() -> None:
    cand = DueMessageCandidate(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        campaign_id=uuid.uuid4(),
        mailbox_id=uuid.uuid4(),
        due_at=datetime.now(UTC),
        purpose="FIRST_SEND",
        schedule_generation=1,
        status="SCHEDULED",
        dispatch_generation=0,
    )
    assert cand.purpose == "FIRST_SEND"
    assert cand.status == "SCHEDULED"


def test_claim_result_schema() -> None:
    res = ClaimResult(
        claimed=True,
        message_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        dispatch_generation=2,
    )
    assert res.claimed is True
    assert res.dispatch_generation == 2
    assert res.reason is None


def test_scheduler_iteration_summary() -> None:
    summary = SchedulerIterationSummary(
        due_discovered=10,
        messages_claimed=5,
        deliveries_published=5,
        claims_recovered=1,
        leases_recovered=0,
        oldest_due_lag_seconds=12.5,
    )
    assert summary.messages_claimed == 5
    assert summary.oldest_due_lag_seconds == 12.5


# ---------------------------------------------------------------------------
# Fairness & Round-Robin Logic in SchedulerService
# ---------------------------------------------------------------------------


def test_scheduler_service_round_robin_fairness() -> None:
    session = MagicMock()
    service = SchedulerService(session)

    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    now = datetime.now(UTC)

    # ws_a has 5 messages, ws_b has 2 messages
    candidates = [
        DueMessageCandidate(
            id=uuid.uuid4(),
            workspace_id=ws_a,
            campaign_id=uuid.uuid4(),
            mailbox_id=uuid.uuid4(),
            due_at=now,
            purpose="FIRST_SEND",
            schedule_generation=1,
            status="SCHEDULED",
            dispatch_generation=0,
        )
        for _ in range(5)
    ] + [
        DueMessageCandidate(
            id=uuid.uuid4(),
            workspace_id=ws_b,
            campaign_id=uuid.uuid4(),
            mailbox_id=uuid.uuid4(),
            due_at=now,
            purpose="FIRST_SEND",
            schedule_generation=1,
            status="SCHEDULED",
            dispatch_generation=0,
        )
        for _ in range(2)
    ]

    service.repository.find_due_messages = MagicMock(return_value=candidates)
    claimed_order: list[uuid.UUID] = []

    def mock_claim(workspace_id: uuid.UUID, message_id: uuid.UUID, **kwargs: object) -> ClaimResult:
        claimed_order.append(workspace_id)
        return ClaimResult(
            claimed=True,
            message_id=message_id,
            workspace_id=workspace_id,
            dispatch_generation=1,
        )

    service.repository.claim_due_message = MagicMock(side_effect=mock_claim)

    disc, claimed = service.discover_and_claim_due_work(batch_size=4)

    assert disc == 7
    assert claimed == 4
    # Round-robin between ws_a and ws_b:
    # 1: ws_a, 2: ws_b, 3: ws_a, 4: ws_b
    assert claimed_order == [ws_a, ws_b, ws_a, ws_b]


# ---------------------------------------------------------------------------
# Backpressure: never claim more than the send workers can take
# ---------------------------------------------------------------------------


def _candidates(count: int) -> list[DueMessageCandidate]:
    now = datetime.now(UTC)
    ws = uuid.uuid4()
    return [
        DueMessageCandidate(
            id=uuid.uuid4(),
            workspace_id=ws,
            campaign_id=uuid.uuid4(),
            mailbox_id=uuid.uuid4(),
            due_at=now,
            purpose="CAMPAIGN",
            schedule_generation=1,
            status="SCHEDULED",
            dispatch_generation=0,
        )
        for _ in range(count)
    ]


def _backpressure_service(outstanding: int, max_outstanding: int):
    from app.core.config import Settings

    settings = Settings.current().model_copy(
        update={"scheduler_max_outstanding_claims": max_outstanding}
    )
    service = SchedulerService(MagicMock(), settings=settings)
    service.repository.count_outstanding_claims = MagicMock(return_value=outstanding)
    service.repository.find_due_messages = MagicMock(return_value=_candidates(50))
    service.repository.claim_due_message = MagicMock(
        side_effect=lambda workspace_id, message_id, **_: ClaimResult(
            claimed=True,
            message_id=message_id,
            workspace_id=workspace_id,
            dispatch_generation=1,
        )
    )
    return service


def test_scheduler_claims_nothing_while_the_queue_is_full() -> None:
    service = _backpressure_service(outstanding=200, max_outstanding=200)

    assert service.discover_and_claim_due_work() == (0, 0)

    service.repository.find_due_messages.assert_not_called()
    service.repository.claim_due_message.assert_not_called()


def test_scheduler_claims_only_what_there_is_room_for() -> None:
    service = _backpressure_service(outstanding=190, max_outstanding=200)

    _, claimed = service.discover_and_claim_due_work(batch_size=50)

    assert claimed == 10


def test_scheduler_claims_a_full_batch_when_there_is_room() -> None:
    service = _backpressure_service(outstanding=0, max_outstanding=200)

    _, claimed = service.discover_and_claim_due_work(batch_size=30)

    assert claimed == 30


def test_scheduler_holds_back_when_over_the_limit() -> None:
    service = _backpressure_service(outstanding=500, max_outstanding=200)

    assert service.discover_and_claim_due_work() == (0, 0)


# ---------------------------------------------------------------------------
# OutboxPublisherService: Publication & Failure Handling
# ---------------------------------------------------------------------------


def test_outbox_publisher_success_flow() -> None:
    session = MagicMock()
    celery_mock = MagicMock()
    publisher = OutboxPublisherService(session, celery_client=celery_mock, publisher_id="test-pub")

    delivery_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    work_id = uuid.uuid4()
    resource_id = uuid.uuid4()

    pending_row = {
        "delivery_id": delivery_id,
        "workspace_id": workspace_id,
        "work_id": work_id,
        "resource_id": resource_id,
        "semantic_key": f"{resource_id}:1",
        "attempts": 0,
        "correlation_id": "corr-test",
    }

    publisher.repository.find_pending_deliveries = MagicMock(return_value=[pending_row])
    publisher.repository.lease_delivery = MagicMock(
        return_value={"lease_generation": 1, "lease_owner": "test-pub"}
    )
    publisher.repository.mark_published = MagicMock()

    count = publisher.publish_pending_deliveries(batch_size=1)

    assert count == 1
    # Celery task published
    celery_mock.send_task.assert_called_once()
    call_args = celery_mock.send_task.call_args
    assert call_args[0][0] == "email.send"
    assert call_args[1]["queue"] == "email.send"
    payload = call_args[1]["kwargs"]["payload"]
    assert payload["work_id"] == str(work_id)
    assert payload["delivery_id"] == str(delivery_id)
    assert payload["dispatch_generation"] == 1

    # mark_published called with matching lease owner & generation
    publisher.repository.mark_published.assert_called_once_with(
        delivery_id=delivery_id,
        workspace_id=workspace_id,
        lease_owner="test-pub",
        lease_generation=1,
        authoritative_now=None,
    )


def test_outbox_publisher_redis_failure_retries_with_backoff() -> None:
    session = MagicMock()
    celery_mock = MagicMock()
    celery_mock.send_task.side_effect = RuntimeError("Redis connection refused")
    publisher = OutboxPublisherService(session, celery_client=celery_mock, publisher_id="test-pub")

    delivery_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    work_id = uuid.uuid4()
    resource_id = uuid.uuid4()

    pending_row = {
        "delivery_id": delivery_id,
        "workspace_id": workspace_id,
        "work_id": work_id,
        "resource_id": resource_id,
        "semantic_key": f"{resource_id}:1",
        "attempts": 2,
        "correlation_id": None,
    }

    publisher.repository.find_pending_deliveries = MagicMock(return_value=[pending_row])
    publisher.repository.lease_delivery = MagicMock(
        return_value={"lease_generation": 1, "lease_owner": "test-pub"}
    )
    publisher.repository.mark_retry = MagicMock()

    count = publisher.publish_pending_deliveries(batch_size=1)

    assert count == 0
    publisher.repository.mark_retry.assert_called_once()
    _, kwargs = publisher.repository.mark_retry.call_args
    assert kwargs["delivery_id"] == delivery_id
    assert kwargs["lease_generation"] == 1
    assert "Redis connection refused" in kwargs["safe_error"]
    assert kwargs["backoff_seconds"] >= 4.0  # 2^attempts is 4 + jitter
