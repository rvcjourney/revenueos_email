"""Rate-policy fixtures for send-path unit tests."""

from __future__ import annotations

import uuid

from app.modules.rate_limit.schemas import RatePolicySpec, RateScopeKind


def mailbox_daily_cap() -> RatePolicySpec:
    """What every connected mailbox has: 50 per rolling 24 hours, 60 s apart.

    The send path refuses to send a campaign email from a mailbox without a daily
    cap, so a test that mocks the resolved policies must include one.
    """
    return RatePolicySpec(
        kind=RateScopeKind.MAILBOX,
        source_id=str(uuid.uuid4()),
        scope_key="mailbox-1",
        unit="MESSAGE",
        window_seconds=86_400,
        limit_value=50,
        min_spacing_seconds=60,
    )
