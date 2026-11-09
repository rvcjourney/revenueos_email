from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

from app.api.deps import WorkspaceContext
from app.modules.campaigns.audience_service import AudienceService
from app.modules.campaigns.schemas import AudienceSelectIn

WS = uuid.uuid4()
CAMPAIGN = uuid.uuid4()
LEAD = uuid.uuid4()
CTX = WorkspaceContext(workspace_id=WS, user_id=uuid.uuid4(), role_code="MEMBER")


def test_capture_task_is_dispatched_only_after_the_request_commits() -> None:
    """A worker that picks the task up before the commit finds no job, drops the
    task, and the capture (and its list gate) stays held until someone abandons it."""
    events: list[str] = []
    session = MagicMock()
    session.commit.side_effect = lambda: events.append("commit")
    service = AudienceService(session)

    repo = MagicMock()
    repo.get_campaign.return_value = {"status": "DRAFT"}
    repo.get_existing_list_ids.return_value = {}
    repo.get_existing_lead_ids.return_value = {LEAD}
    repo.insert_campaign_audience.return_value = {
        "id": uuid.uuid4(),
        "campaign_id": CAMPAIGN,
        "revision": 1,
        "status": "CAPTURING",
        "started_at": datetime.now(UTC),
        "completed_at": None,
    }
    repo.count_candidate_leads.return_value = 1
    service.repo = repo
    service._dispatch_capture_task = (  # type: ignore[method-assign]
        lambda **kw: events.append("dispatch")
    )

    payload = AudienceSelectIn(list_ids=[], lead_ids=[LEAD])
    service.select_audience(CTX, CAMPAIGN, payload)

    assert events == ["commit", "dispatch"]


def test_audience_out_reports_what_it_was_captured_from() -> None:
    """A client adds to an audience by re-capturing the union, so it needs the
    selection."""
    list_a, list_b, lead = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    service = AudienceService(MagicMock())
    repo = MagicMock()
    repo.get_capture_job_for_audience.return_value = None
    service.repo = repo

    out = service._to_full_out(
        CTX,
        {"draft_audience_id": None},
        {
            "id": uuid.uuid4(),
            "campaign_id": CAMPAIGN,
            "revision": 2,
            "status": "CAPTURING",
            "started_at": datetime.now(UTC),
            "completed_at": None,
            "selection_manifest": {
                "version": 1,
                "lists": [str(list_a), str(list_b)],
                "leads": [str(lead)],
            },
        },
    )

    assert out.selected_list_ids == [list_a, list_b]
    assert out.selected_lead_ids == [lead]
