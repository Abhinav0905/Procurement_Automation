"""Temporal worker.

Runs the workflow and every activity. Activities do blocking database and
network I/O, so they get a bounded thread pool; the workflow itself runs on the
event loop under Temporal's determinism sandbox.
"""

from __future__ import annotations

import asyncio
import signal
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from temporalio.client import Client, TLSConfig
from temporalio.worker import Worker

from procureguard.config import Settings, get_settings
from procureguard.observability import configure_logging, configure_tracing, logger
from procureguard.workflows.activities import ALL_ACTIVITIES
from procureguard.workflows.procurement import ProcurementWorkflow

log = logger(__name__)


async def build_client(settings: Settings | None = None) -> Client:
    settings = settings or get_settings()
    tls: TLSConfig | bool = False
    if settings.temporal_tls_cert_path and settings.temporal_tls_key_path:
        with open(settings.temporal_tls_cert_path, "rb") as cert, open(
            settings.temporal_tls_key_path, "rb"
        ) as key:
            tls = TLSConfig(client_cert=cert.read(), client_private_key=key.read())

    kwargs: dict[str, Any] = {"namespace": settings.temporal_namespace}
    if tls:
        kwargs["tls"] = tls
    if settings.temporal_api_key:
        kwargs["api_key"] = settings.temporal_api_key
        kwargs["tls"] = True
    return await Client.connect(settings.temporal_address, **kwargs)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    configure_tracing(settings)

    client = await build_client(settings)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            pass

    with ThreadPoolExecutor(
        max_workers=settings.temporal_max_concurrent_activities,
        thread_name_prefix="pg-activity",
    ) as executor:
        worker = Worker(
            client,
            task_queue=settings.temporal_task_queue,
            workflows=[ProcurementWorkflow],
            activities=ALL_ACTIVITIES,
            activity_executor=executor,
            max_concurrent_activities=settings.temporal_max_concurrent_activities,
        )
        log.info(
            "worker_starting",
            task_queue=settings.temporal_task_queue,
            namespace=settings.temporal_namespace,
            address=settings.temporal_address,
            activities=len(ALL_ACTIVITIES),
        )
        async with worker:
            await stop.wait()
        log.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
