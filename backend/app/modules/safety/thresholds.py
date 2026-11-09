"""Deliverability thresholds, in one place.

The dashboard warns at these numbers and automatic protection acts on them, so
they must be the same numbers. Mailbox providers start throttling or suspending a
sender when bounces pass a few percent or spam complaints pass about one in a
thousand.
"""

from __future__ import annotations

from typing import Literal

# Bounce rate (% of emails sent), judged only once enough have been sent for the
# rate to mean something.
BOUNCE_WARNING_PERCENT = 2.0
BOUNCE_CRITICAL_PERCENT = 5.0
BOUNCE_MIN_SENDS = 20

# Spam complaints (% of emails sent).
COMPLAINT_CRITICAL_PERCENT = 0.1
COMPLAINT_MIN_SENDS = 50

Level = Literal["CRITICAL", "WARNING"]


def rate_percent(count: int, sent: int) -> float:
    """`count` as a percentage of `sent`, to two decimals (0.0 if none sent)."""
    return round((count / sent) * 100, 2) if sent > 0 else 0.0


def bounce_level(bounce_rate_percent: float, sent: int) -> Level | None:
    """CRITICAL above 5%, WARNING above 2%, once at least 20 emails were sent."""
    if sent < BOUNCE_MIN_SENDS:
        return None
    if bounce_rate_percent > BOUNCE_CRITICAL_PERCENT:
        return "CRITICAL"
    if bounce_rate_percent > BOUNCE_WARNING_PERCENT:
        return "WARNING"
    return None


def complaints_critical(complaint_rate_percent: float, sent: int) -> bool:
    """Above 0.1%, once at least 50 emails were sent."""
    return (
        sent >= COMPLAINT_MIN_SENDS
        and complaint_rate_percent > COMPLAINT_CRITICAL_PERCENT
    )
