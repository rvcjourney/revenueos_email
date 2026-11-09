from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.errors import AppError

# Generous safety bound for the gap-resolution scan below (a real DST gap is
# at most a couple of hours) and for the day-by-day window search (a
# pathological weekday_set/window combination should fail fast, not hang).
_MAX_GAP_SCAN_MINUTES = 1440
_DEFAULT_MAX_DAYS = 400


def _fold_offsets(
    naive: datetime, tz: ZoneInfo
) -> tuple[timedelta | None, timedelta | None]:
    return (
        naive.replace(tzinfo=tz, fold=0).utcoffset(),
        naive.replace(tzinfo=tz, fold=1).utcoffset(),
    )


def _resolve_local_to_utc(
    naive: datetime, tz: ZoneInfo, *, prefer_later: bool
) -> datetime:
    """Resolve one naive local datetime to UTC, handling both DST cases.

    Ambiguous (fall-back) times exist twice: fold=0 is the earlier UTC
    occurrence (larger/daylight offset), fold=1 is the later one
    (smaller/standard offset) -- distinguished here by off0 > off1.

    Nonexistent (spring-forward gap) times never occur on the wall clock at
    all -- distinguished by off1 > off0 (the post-transition offset is
    larger). Per the documented policy, these shift forward to the first
    valid local instant after the gap, found by scanning forward minute by
    minute until fold=0 and fold=1 agree again.
    """
    off0, off1 = _fold_offsets(naive, tz)
    if off0 == off1:
        return naive.replace(tzinfo=tz).astimezone(UTC)

    if off1 is not None and off0 is not None and off1 > off0:
        probe = naive
        for _ in range(_MAX_GAP_SCAN_MINUTES):
            probe += timedelta(minutes=1)
            p0, p1 = _fold_offsets(probe, tz)
            if p0 == p1:
                return probe.replace(tzinfo=tz).astimezone(UTC)
        raise AppError(
            "internal_error",
            f"Could not resolve a DST gap for timezone {tz.key}",
            status_code=500,
        )

    chosen_fold = 1 if prefer_later else 0
    return naive.replace(tzinfo=tz, fold=chosen_fold).astimezone(UTC)


def project_into_window(
    lower_bound_utc: datetime,
    *,
    timezone: str,
    weekday_set: int,
    window_start_local: time,
    window_end_local: time,
    max_days: int = _DEFAULT_MAX_DAYS,
) -> datetime:
    """Earliest UTC instant >= lower_bound_utc that falls inside an allowed
    weekday's sending window, per SCHEDULER.md.

    weekday_set uses the same bit order as
    campaign_settings_versions.weekday_set / settings_service.py's
    _weekdays_to_bitmask: bit 0 = Monday .. bit 6 = Sunday, matching Python's
    date.weekday() directly. A date whose local window collapses to
    end<=start under DST (fall-back shortening it past empty) is skipped
    entirely, not inverted.

    This is the entire scheduling surface Phase 8 needs: it is only ever
    called once per enrollment, for the first Email step, with
    lower_bound_utc = the campaign's activation start_at. The signature is
    written generically enough that a future sequence-progression phase can
    reuse it unchanged for later steps (lower_bound = prior message's
    accepted time + wait duration), but Phase 8 itself never plans past the
    first step.
    """
    tz = ZoneInfo(timezone)
    local_date: date = lower_bound_utc.astimezone(tz).date()

    for _ in range(max_days):
        if not (weekday_set >> local_date.weekday()) & 1:
            local_date += timedelta(days=1)
            continue

        start_utc = _resolve_local_to_utc(
            datetime.combine(local_date, window_start_local), tz, prefer_later=True
        )
        end_utc = _resolve_local_to_utc(
            datetime.combine(local_date, window_end_local), tz, prefer_later=False
        )

        if end_utc > start_utc:
            if start_utc <= lower_bound_utc < end_utc:
                return lower_bound_utc
            if lower_bound_utc < start_utc:
                return start_utc

        local_date += timedelta(days=1)

    raise AppError(
        "internal_error",
        "No eligible sending window found within the configured bound",
        status_code=500,
    )


# How far ahead the window calendar will look for the Nth window. A campaign this
# long cannot be planned day by day anyway; later messages share the last window
# and the send-time limiter paces them.
_CALENDAR_HORIZON_DAYS = 3650


class WindowCalendar:
    """A campaign's sending windows in order, counted from its start.

    Window 0 is the first window still open at `lower_bound_utc` (clipped to
    start there when the campaign starts mid-window); each following window is the
    next allowed weekday's local window. It uses the same weekday, timezone and DST
    rules as `project_into_window`, so a time picked inside a window here is one
    that function accepts unchanged.

    Windows are found lazily and remembered, so planning thousands of messages
    over one calendar costs a single scan, not one per message.
    """

    def __init__(
        self,
        lower_bound_utc: datetime,
        *,
        timezone: str,
        weekday_set: int,
        window_start_local: time,
        window_end_local: time,
    ) -> None:
        self._lower = lower_bound_utc
        self._tz = ZoneInfo(timezone)
        self._weekday_set = weekday_set
        self._start_local = window_start_local
        self._end_local = window_end_local
        self._next_date: date = lower_bound_utc.astimezone(self._tz).date()
        self._last_date = self._next_date + timedelta(days=_CALENDAR_HORIZON_DAYS)
        self._windows: list[tuple[datetime, datetime]] = []

    def _find_next(self) -> bool:
        while self._next_date <= self._last_date:
            local_date = self._next_date
            self._next_date += timedelta(days=1)
            if not (self._weekday_set >> local_date.weekday()) & 1:
                continue
            start_utc = _resolve_local_to_utc(
                datetime.combine(local_date, self._start_local),
                self._tz,
                prefer_later=True,
            )
            end_utc = _resolve_local_to_utc(
                datetime.combine(local_date, self._end_local),
                self._tz,
                prefer_later=False,
            )
            if end_utc <= start_utc or end_utc <= self._lower:
                continue
            self._windows.append((max(start_utc, self._lower), end_utc))
            return True
        return False

    def window(self, index: int) -> tuple[datetime, datetime]:
        """The `index`th (0-based) window as (start, end) in UTC.

        Past the horizon this returns the last window found rather than failing.
        """
        while len(self._windows) <= index:
            if not self._find_next():
                break
        if not self._windows:
            raise AppError(
                "internal_error",
                "No eligible sending window found within the configured bound",
                status_code=500,
            )
        return self._windows[min(index, len(self._windows) - 1)]
