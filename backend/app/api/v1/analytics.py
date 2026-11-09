from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext, get_db
from app.core.permissions import require_permission
from app.modules.analytics.activity import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    CampaignActivityService,
)
from app.modules.analytics.schemas import (
    CampaignActivityPage,
    CampaignActivityStatus,
    CampaignActivityType,
    CampaignAnalytics,
    CampaignSequenceAnalytics,
    DeliverabilityOverview,
    WorkspaceOverviewAnalytics,
)
from app.modules.analytics.service import AnalyticsService

router = APIRouter()


@router.get("/overview", response_model=WorkspaceOverviewAnalytics)
def get_workspace_overview(
    start_date: str | None = Query(
        default=None,
        description="Filter window start in YYYY-MM-DD format (default: 30 days ago)",
    ),
    end_date: str | None = Query(
        default=None,
        description="Filter window end in YYYY-MM-DD format (default: today)",
    ),
    timezone: str | None = Query(
        default="UTC",
        description="Timezone for grouping calendar dates (default: UTC)",
    ),
    campaign_id: UUID | None = Query(
        default=None,
        description="Optional filter by campaign ID",
    ),
    mailbox_id: UUID | None = Query(
        default=None,
        description="Optional filter by mailbox ID",
    ),
    context: WorkspaceContext = Depends(require_permission("product.read")),
    db: Session = Depends(get_db),
) -> WorkspaceOverviewAnalytics:
    """Fetch aggregated workspace outreach performance, daily trend, and top entities."""
    return AnalyticsService(db).get_workspace_overview(
        context,
        start_date=start_date,
        end_date=end_date,
        timezone=timezone,
        campaign_id=campaign_id,
        mailbox_id=mailbox_id,
    )


@router.get("/campaigns/{campaign_id}", response_model=CampaignAnalytics)
def get_campaign_analytics(
    campaign_id: UUID,
    context: WorkspaceContext = Depends(require_permission("product.read")),
    db: Session = Depends(get_db),
) -> CampaignAnalytics:
    """Fetch authoritative campaign-level performance metrics and rates."""
    return AnalyticsService(db).get_campaign_analytics(context, campaign_id)


@router.get("/campaigns/{campaign_id}/activity", response_model=CampaignActivityPage)
def get_campaign_activity(
    campaign_id: UUID,
    type: CampaignActivityType | None = Query(
        default=None, description="Only this kind of activity"
    ),
    status: CampaignActivityStatus | None = Query(
        default=None, description="Only activity with this outcome status"
    ),
    start_date: str | None = Query(
        default=None, description="Window start in YYYY-MM-DD format"
    ),
    end_date: str | None = Query(
        default=None, description="Window end in YYYY-MM-DD format"
    ),
    q: str | None = Query(
        default=None,
        max_length=100,
        description="Search recipient email or subject",
    ),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(default=None, max_length=512),
    context: WorkspaceContext = Depends(require_permission("product.read")),
    db: Session = Depends(get_db),
) -> CampaignActivityPage:
    """Newest-first history of what happened in one campaign."""
    return CampaignActivityService(db).get_activity(
        context,
        campaign_id,
        activity_type=type,
        status=status,
        start_date=start_date,
        end_date=end_date,
        q=q,
        limit=limit,
        cursor=cursor,
    )


@router.get(
    "/campaigns/{campaign_id}/sequence", response_model=CampaignSequenceAnalytics
)
def get_campaign_sequence_analytics(
    campaign_id: UUID,
    context: WorkspaceContext = Depends(require_permission("product.read")),
    db: Session = Depends(get_db),
) -> CampaignSequenceAnalytics:
    """Fetch step-level sequence analytics with authoritative reply attribution."""
    return AnalyticsService(db).get_sequence_analytics(context, campaign_id)


@router.get("/deliverability", response_model=DeliverabilityOverview)
def get_deliverability_overview(
    start_date: str | None = Query(
        default=None,
        description="Filter window start in YYYY-MM-DD format (default: 30 days ago)",
    ),
    end_date: str | None = Query(
        default=None,
        description="Filter window end in YYYY-MM-DD format (default: today)",
    ),
    context: WorkspaceContext = Depends(require_permission("product.read")),
    db: Session = Depends(get_db),
) -> DeliverabilityOverview:
    """Fetch operational deliverability metrics, mailbox health states, warnings, and error breakdown."""
    return AnalyticsService(db).get_deliverability_overview(
        context,
        start_date=start_date,
        end_date=end_date,
    )
