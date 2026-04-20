"""Latency telemetry endpoint.

Returns rolling signal→submit→fill percentile stats from the
in-memory ``LatencyTracker`` (populated during the tick loop).
Falls back to an empty response when the tracker hasn't been
created (feature disabled or no fills yet).
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["telemetry"])

_tracker = None


def set_tracker(tracker) -> None:
    global _tracker
    _tracker = tracker


def _stats_to_dict(s) -> dict:
    return {
        "n_samples": s.n_samples,
        "p50_ms": round(s.p50_ms, 2),
        "p90_ms": round(s.p90_ms, 2),
        "p95_ms": round(s.p95_ms, 2),
        "p99_ms": round(s.p99_ms, 2),
        "max_ms": round(s.max_ms, 2),
    }


@router.get("/latency")
def get_latency() -> dict:
    if _tracker is None:
        return {
            "enabled": False,
            "intervals": {},
            "latest_signal_to_fill_ms": None,
        }

    stats = _tracker.stats()
    latest = _tracker.latest_signal_to_fill_ms()

    return {
        "enabled": True,
        "intervals": {k: _stats_to_dict(v) for k, v in stats.items()},
        "latest_signal_to_fill_ms": round(latest, 2) if latest is not None else None,
    }
