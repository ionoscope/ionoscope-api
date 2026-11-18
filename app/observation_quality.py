from datetime import UTC, datetime, timedelta

from .schemas import ObservationQuality


def observation_quality(points: dict[datetime, float], *, now: datetime) -> ObservationQuality:
    """Coverage of 96 completed quarter-hour intervals before calculation time.

    A qualified aggregate counts once per interval, not once per raw measurement.
    Empty intervals, including the tail after the last observation, remain gaps.
    """
    now = now.astimezone(UTC)
    end = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)
    start = end - timedelta(hours=24)
    available = longest = run = 0
    for i in range(96):
        if start + timedelta(minutes=15*i) in points:
            available += 1
            run = 0
        else:
            run += 1
            longest = max(longest, run)
    return ObservationQuality(
        window_start=start, window_end=end, expected_intervals=96,
        available_intervals=available, missing_intervals=96-available,
        coverage_percent=round(available / 96 * 100, 2),
        longest_gap_minutes=longest*15, trailing_gap_minutes=run*15,
    )
