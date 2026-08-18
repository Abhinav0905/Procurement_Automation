"""Temporal client access for the API process.

Temporal being unavailable must not take the read API down: buyers still need to
see cases, evidence and bid tabulations during an orchestrator outage. Signals
and workflow starts fail loudly; queries degrade to "unavailable".
"""

from __future__ import annotations

from typing import Any

from temporalio.client import Client

from procureguard.config import get_settings
from procureguard.domain.errors import ExternalServiceError
from procureguard.observability import logger

log = logger(__name__)

_client: Client | None = None


async def get_temporal_client() -> Client:
    global _client
    if _client is None:
        from procureguard.workflows.worker import build_client

        try:
            _client = await build_client(get_settings())
        except Exception as exc:
            raise ExternalServiceError(
                "Temporal is unavailable", detail=str(exc)[:300], retryable=True
            ) from exc
    return _client


async def try_get_temporal_client() -> Client | None:
    try:
        return await get_temporal_client()
    except Exception as exc:
        log.info("temporal_unavailable", detail=str(exc)[:200])
        return None


async def signal_workflow(case_id: str, signal: str, *args: Any) -> bool:
    settings = get_settings()
    client = await get_temporal_client()
    handle = client.get_workflow_handle(settings.temporal_workflow_id(case_id))
    await handle.signal(signal, *args)
    log.info("workflow_signalled", case_id=case_id, signal=signal)
    return True


async def try_signal_workflow(case_id: str, signal: str, *args: Any) -> dict[str, Any]:
    """Signal, but report failure rather than aborting the human's action.

    An approval is recorded in CockroachDB first and is authoritative. If the
    signal fails, the approval still stands and the workflow can be resynced.
    """
    try:
        await signal_workflow(case_id, signal, *args)
        return {"signalled": True}
    except Exception as exc:
        log.warning("workflow_signal_failed", case_id=case_id, signal=signal, detail=str(exc)[:300])
        return {"signalled": False, "detail": str(exc)[:300]}


async def query_workflow_state(case_id: str) -> dict[str, Any] | None:
    settings = get_settings()
    client = await try_get_temporal_client()
    if client is None:
        return None
    try:
        handle = client.get_workflow_handle(settings.temporal_workflow_id(case_id))
        state = await handle.query("get_state")
        pending = await handle.query("pending_suppliers")
    except Exception as exc:
        log.info("workflow_query_failed", case_id=case_id, detail=str(exc)[:200])
        return None
    if state is None:
        return None
    payload = state if isinstance(state, dict) else vars(state)
    payload["pending_suppliers"] = pending or []
    return payload


# Event types worth showing a human. The full history includes task scheduling and
# completion for every workflow task, which is noise for anyone asking "what has
# the orchestrator actually done".
_INTERESTING_EVENTS = (
    "WorkflowExecutionStarted",
    "WorkflowExecutionCompleted",
    "WorkflowExecutionFailed",
    "WorkflowExecutionContinuedAsNew",
    "ActivityTaskScheduled",
    "ActivityTaskStarted",
    "ActivityTaskCompleted",
    "ActivityTaskFailed",
    "ActivityTaskTimedOut",
    "TimerStarted",
    "TimerFired",
    "WorkflowExecutionSignaled",
)


def _event_detail(event: Any) -> str:
    """Pull the one human-meaningful string out of a history event."""
    which = event.WhichOneof("attributes")
    if not which:
        return ""
    attrs = getattr(event, which)
    for path in (
        ("activity_type", "name"),   # activity scheduled
        ("workflow_type", "name"),   # execution started
    ):
        obj: Any = attrs
        for part in path:
            obj = getattr(obj, part, None)
            if obj is None:
                break
        if isinstance(obj, str) and obj:
            return obj
    for field in ("signal_name", "timer_id", "activity_id"):
        value = getattr(attrs, field, "")
        if value:
            return str(value)
    # Activity completion carries no type of its own; it references the scheduling
    # event, which the caller resolves.
    scheduled = getattr(attrs, "scheduled_event_id", 0)
    return f"#{scheduled}" if scheduled else ""


async def fetch_workflow_history(case_id: str, *, limit: int = 200) -> dict[str, Any]:
    """Real Temporal event history for a case, condensed for display.

    This exists so orchestration is verifiable from inside the application rather
    than only from the Temporal Web UI: the events below are read from Temporal's
    own history store, not reconstructed from our database.
    """
    settings = get_settings()
    client = await try_get_temporal_client()
    if client is None:
        return {"available": False, "detail": "Temporal is unavailable", "events": []}

    workflow_id = settings.temporal_workflow_id(case_id)
    try:
        handle = client.get_workflow_handle(workflow_id)
        description = await handle.describe()
        raw = [event async for event in handle.fetch_history_events()]
    except Exception as exc:
        log.info("workflow_history_unavailable", case_id=case_id, detail=str(exc)[:200])
        return {"available": False, "detail": str(exc)[:300], "events": []}

    # Activity completions name no activity, so resolve them through the
    # scheduling event they point back at.
    scheduled_names: dict[int, str] = {}
    events: list[dict[str, Any]] = []
    for event in raw:
        type_name = event.event_type.name.removeprefix("EVENT_TYPE_")
        pretty = "".join(part.capitalize() for part in type_name.split("_"))
        detail = _event_detail(event)
        if pretty == "ActivityTaskScheduled":
            scheduled_names[event.event_id] = detail
        elif detail.startswith("#"):
            detail = scheduled_names.get(int(detail[1:]), "")
        if pretty not in _INTERESTING_EVENTS:
            continue
        events.append(
            {
                "event_id": event.event_id,
                "event_type": pretty,
                "detail": detail,
                "timestamp": event.event_time.ToDatetime().isoformat() + "Z",
            }
        )

    return {
        "available": True,
        "workflow_id": workflow_id,
        "run_id": description.run_id,
        "status": description.status.name if description.status else "UNKNOWN",
        "task_queue": settings.temporal_task_queue,
        "started_at": description.start_time.isoformat() if description.start_time else "",
        "event_count": len(raw),
        "events": events[-limit:],
    }


async def start_procurement_workflow(
    *,
    case_id: str,
    tenant_id: str,
    pr_artifact_uri: str = "",
    correlation_id: str = "",
    quote_window_days: int = 10,
    enable_negotiation: bool = True,
    auto_release_rfq: bool = False,
) -> dict[str, Any]:
    from procureguard.workflows.procurement import ProcurementWorkflow, ProcurementWorkflowInput

    settings = get_settings()
    client = await get_temporal_client()
    workflow_id = settings.temporal_workflow_id(case_id)
    handle = await client.start_workflow(
        ProcurementWorkflow.run,
        ProcurementWorkflowInput(
            case_id=case_id,
            pr_artifact_uri=pr_artifact_uri,
            tenant_id=tenant_id,
            correlation_id=correlation_id,
            quote_window_days=quote_window_days,
            reminder_interval_hours=settings.reminder_interval_hours,
            max_reminders=settings.max_rfq_reminders,
            enable_negotiation=enable_negotiation,
            auto_release_rfq=auto_release_rfq,
        ),
        id=workflow_id,
        task_queue=settings.temporal_task_queue,
        execution_timeout=None,
    )
    log.info("workflow_started", case_id=case_id, workflow_id=workflow_id, run_id=handle.result_run_id)
    return {"workflow_id": workflow_id, "run_id": handle.result_run_id}
