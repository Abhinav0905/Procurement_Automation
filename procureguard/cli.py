"""ProcureGuard command line.

    procureguard db init                 create the schema
    procureguard db check                connectivity and capability report
    procureguard seed --scale medium     load the synthetic enterprise
    procureguard demo                    seed plus an end-to-end sourcing case
    procureguard pipeline CASE-ID        drive one case through every stage
    procureguard mail poll               process the inbound mailbox
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from procureguard.config import get_settings
from procureguard.observability import configure_logging, logger

log = logger(__name__)


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


# ────────────────────────────────────────────────────────────────────── db

def cmd_db_init(args: argparse.Namespace) -> int:
    from procureguard.infrastructure.db.session import create_all, get_engine
    from procureguard.infrastructure.db.vector import native_vector_enabled

    engine = get_engine()
    create_all(engine)
    _print(
        {
            "status": "created",
            "database_url": _redact(get_settings().database_url),
            "vector_backend": "native" if native_vector_enabled() else "json",
        }
    )
    return 0


def cmd_db_check(args: argparse.Namespace) -> int:
    from procureguard.infrastructure.db.session import healthcheck

    result = healthcheck()
    _print(result)
    return 0 if result.get("status") == "ok" else 1


def cmd_db_stats(args: argparse.Namespace) -> int:
    from sqlalchemy import func, select

    from procureguard.infrastructure.db import models
    from procureguard.infrastructure.db.session import read_session

    interesting = [
        models.MaterialModel, models.MaterialPlantModel, models.VendorModel,
        models.VendorContactModel, models.PurchaseHistoryModel,
        models.GoodsReceiptHistoryModel, models.InfoRecordModel, models.SourceListModel,
        models.ContractModel, models.FxRateModel, models.FreightRateModel,
        models.SourcingCaseModel, models.PurchaseRequisitionModel, models.RequirementModel,
        models.RfqModel, models.QuotationModel, models.DocumentVersionModel,
        models.DocumentChunkModel, models.ApprovalModel, models.AuditLogModel,
    ]
    counts: dict[str, int] = {}
    with read_session() as session:
        for model in interesting:
            counts[model.__tablename__] = int(
                session.scalar(select(func.count()).select_from(model)) or 0
            )
    _print({"row_counts": counts, "total": sum(counts.values())})
    return 0


# ──────────────────────────────────────────────────────────────────── seed

def cmd_seed(args: argparse.Namespace) -> int:
    from procureguard.seed.runner import seed_database

    report = seed_database(
        scale=args.scale,
        seed=args.seed,
        reset=args.reset,
        embed=not args.no_embeddings,
    )
    _print(report.to_dict())
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from procureguard.seed.scenarios import build_demo_scenarios

    result = build_demo_scenarios(
        scale=args.scale, reset=args.reset, run_pipeline=not args.no_pipeline
    )
    _print(result)
    return 0


def cmd_pipeline(args: argparse.Namespace) -> int:
    from procureguard.seed.scenarios import run_pipeline_for_case

    result = run_pipeline_for_case(
        args.case_id,
        approver=args.approver,
        auto_approve=args.auto_approve,
        simulate_quotes=not args.no_quotes,
    )
    _print(result)
    return 0


# ──────────────────────────────────────────────────────────────────── mail

def cmd_mail_poll(args: argparse.Namespace) -> int:
    from procureguard.application.mailroom import MailroomService
    from procureguard.application.quotation_ingestion import QuotationIngestionService
    from procureguard.domain.enums import CommunicationType
    from procureguard.infrastructure.db.session import session_scope
    from procureguard.infrastructure.factory import ServiceContext

    with session_scope() as session:
        ctx = ServiceContext.build(session, actor_id="cli")
        mailroom = MailroomService(ctx)
        ingestion = QuotationIngestionService(ctx)
        results = []
        for outcome in mailroom.poll(limit=args.limit):
            entry = outcome.to_dict()
            if outcome.case_id and not outcome.quarantined and outcome.classification in (
                CommunicationType.QUOTATION_RECEIPT.value,
                CommunicationType.NEGOTIATION_RESPONSE.value,
            ):
                try:
                    entry["quotation"] = ingestion.ingest_from_communication(
                        outcome.communication_id
                    ).to_dict()
                except Exception as exc:
                    entry["quotation_error"] = str(exc)[:300]
            results.append(entry)
    _print({"processed": len(results), "results": results})
    return 0


def cmd_mail_release(args: argparse.Namespace) -> int:
    from procureguard.application.mailroom import MailroomService
    from procureguard.infrastructure.db.session import session_scope
    from procureguard.infrastructure.factory import ServiceContext

    with session_scope() as session:
        ctx = ServiceContext.build(session, actor_id=args.actor)
        outcome = MailroomService(ctx).release_held(args.communication_id, actor_id=args.actor)
    _print(outcome.to_dict())
    return 0


def cmd_mail_pending(args: argparse.Namespace) -> int:
    from procureguard.infrastructure.db.session import read_session
    from procureguard.infrastructure.factory import ServiceContext

    with read_session() as session:
        ctx = ServiceContext.build(session, actor_id="cli")
        pending = [
            {
                "communication_id": c.id,
                "case_id": c.case_id,
                "vendor_id": c.vendor_id,
                "type": c.communication_type,
                "to": c.to_addresses,
                "subject": c.subject,
                "status": c.status,
            }
            for c in ctx.repos.communications.list_pending_release()
        ]
    _print({"pending": len(pending), "messages": pending})
    return 0


# ────────────────────────────────────────────────────────────────── parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="procureguard", description=__doc__)
    parser.add_argument("--log-level", default="")
    subparsers = parser.add_subparsers(dest="command", required=True)

    db = subparsers.add_parser("db", help="database operations")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    db_sub.add_parser("init", help="create the schema").set_defaults(func=cmd_db_init)
    db_sub.add_parser("check", help="connectivity report").set_defaults(func=cmd_db_check)
    db_sub.add_parser("stats", help="row counts").set_defaults(func=cmd_db_stats)

    seed = subparsers.add_parser("seed", help="load synthetic enterprise data")
    seed.add_argument("--scale", default="medium",
                      choices=["tiny", "small", "medium", "large", "xlarge"])
    seed.add_argument("--seed", type=int, default=None)
    seed.add_argument("--reset", action="store_true", help="delete existing rows first")
    seed.add_argument("--no-embeddings", action="store_true")
    seed.set_defaults(func=cmd_seed)

    demo = subparsers.add_parser("demo", help="seed plus end-to-end demo cases")
    demo.add_argument("--scale", default="small",
                      choices=["tiny", "small", "medium", "large", "xlarge"])
    demo.add_argument("--reset", action="store_true")
    demo.add_argument("--no-pipeline", action="store_true", help="create cases but do not run them")
    demo.set_defaults(func=cmd_demo)

    pipeline = subparsers.add_parser("pipeline", help="drive one case through every stage")
    pipeline.add_argument("case_id")
    pipeline.add_argument("--approver", default="jordan.head")
    pipeline.add_argument("--auto-approve", action="store_true", default=True)
    pipeline.add_argument("--no-quotes", action="store_true", help="do not simulate supplier replies")
    pipeline.set_defaults(func=cmd_pipeline)

    mail = subparsers.add_parser("mail", help="mailroom operations")
    mail_sub = mail.add_subparsers(dest="mail_command", required=True)
    poll = mail_sub.add_parser("poll", help="process the inbound mailbox")
    poll.add_argument("--limit", type=int, default=50)
    poll.set_defaults(func=cmd_mail_poll)
    mail_sub.add_parser("pending", help="list held outbound messages").set_defaults(
        func=cmd_mail_pending
    )
    release = mail_sub.add_parser("release", help="release a held outbound message")
    release.add_argument("communication_id")
    release.add_argument("--actor", default="sam.senior")
    release.set_defaults(func=cmd_mail_release)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(args.log_level or settings.log_level, settings.log_format)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        log.error("command_failed", command=args.command, detail=str(exc))
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 1


def _redact(url: str) -> str:
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.partition("@")
    return f"{scheme}://***@{host}"


if __name__ == "__main__":
    raise SystemExit(main())
