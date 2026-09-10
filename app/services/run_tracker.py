# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""
Bookkeeping for runs the Forum started but never saw finish.

The Forum marks a run in flight on the session document before it calls the
engine and clears the mark when the call returns. A mark that is still there
on the next turn means the previous run never came back — the Cloud Run
instance was reclaimed mid-stream, the A2A `wait_for` timed out, or the
stream broke. That is the moment a `function_call` can be left in the
session with no result (see `app.services.session_healer`).

Two things come out of this, and they are worth separating. The **log** is
unconditional and useful on its own: how often runs get cut off, on which
agent, and what tool was in flight is the data that decides whether the
timeouts need tuning. The **healing** is optional, opt-in, and downstream.
"""
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Optional

from app.utils.datetime_helpers import to_aware_utc

logger = logging.getLogger(__name__)

# Why a run stopped, as recorded on the marker and quoted back to the agent
# in a synthetic tool result. Kept short: this text ends up in an agent's
# conversation history.
CUT_OFF_VANISHED = "the run was cut off"
CUT_OFF_TIMEOUT = "the Forum timed out waiting"
CUT_OFF_STREAM_BROKE = "the agent's stream broke mid-flight"


@dataclass(frozen=True)
class CutOffRun:
    """A run the Forum started and never saw finish."""

    started_at: datetime
    elapsed_seconds: float
    reason: str
    last_tool: Optional[str] = None


def active_run_marker(reason: str = CUT_OFF_VANISHED) -> dict[str, Any]:
    """The value written to a session doc while a run is in flight.

    `reason` is what we will assume happened if the run never clears the
    marker. Callers that *catch* their own failure overwrite it with
    something more specific before giving up.
    """
    return {"started_at": datetime.now(UTC), "reason": reason}


def describe_cut_off(
    marker: Optional[dict[str, Any]],
    *,
    stale_after_seconds: float,
    now: Optional[datetime] = None,
) -> Optional[CutOffRun]:
    """
    Decide whether an in-flight marker means a run was cut off.

    A *fresh* marker is not evidence of anything: the Forum handles turns
    concurrently, and a second message arriving while the first is still
    streaming would find one. Only a marker older than `stale_after_seconds`
    — the same grace the healer uses before it will touch a session — says
    the run is never coming back.

    Returns None when there is no marker, when it is unreadable, or when it
    is too young to draw a conclusion from.
    """
    if not marker:
        return None

    started_at = marker.get("started_at")
    if not started_at:
        return None
    try:
        started_at = to_aware_utc(started_at)
    except Exception:
        logger.debug(f"Unreadable active_run marker, ignoring: {marker!r}")
        return None

    elapsed = ((now or datetime.now(UTC)) - started_at).total_seconds()
    if elapsed < stale_after_seconds:
        return None

    return CutOffRun(
        started_at=started_at,
        elapsed_seconds=elapsed,
        reason=marker.get("reason") or CUT_OFF_VANISHED,
        last_tool=marker.get("last_tool"),
    )


def log_run_cut_off(
    run: CutOffRun,
    *,
    agent_id: str,
    session_id: str,
    platform: Optional[str] = None,
) -> None:
    """
    Record that a run never finished.

    The `json_fields` names are a log-query contract the same way the
    `message_processed` fields are — keep them stable.
    """
    logger.warning(
        f"Run cut off on agent {agent_id} session {session_id}: "
        f"{run.reason} after {run.elapsed_seconds:.0f}s "
        f"(last tool: {run.last_tool or 'unknown'})",
        extra={
            "json_fields": {
                "event": "run_cut_off",
                "agent_id": agent_id,
                "session_id": session_id,
                "platform": platform,
                "cutoff_reason": run.reason,
                "elapsed_seconds": round(run.elapsed_seconds, 1),
                "last_tool": run.last_tool,
            }
        },
    )
