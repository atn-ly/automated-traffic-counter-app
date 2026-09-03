from __future__ import annotations


def format_clock(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def estimate_remaining(elapsed: float, current: int, total: int) -> float | None:
    if total <= 0 or current <= 0 or elapsed <= 0:
        return None
    if current >= total:
        return 0.0
    return elapsed * (total - current) / current


def progress_time_text(started_at: float, current: int, total: int, now: float) -> str:
    elapsed = max(0.0, now - started_at)
    remaining = estimate_remaining(elapsed, current, total)
    remaining_text = format_clock(remaining) if remaining is not None else "—"
    return f"Elapsed {format_clock(elapsed)} · Remaining {remaining_text}"
