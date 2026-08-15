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
