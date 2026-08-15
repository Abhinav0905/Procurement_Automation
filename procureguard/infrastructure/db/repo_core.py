"""Case-file repositories: cases, approvals, decisions, audit, idempotency."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, delete, desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from procureguard.config import get_settings
from procureguard.domain.entities import Approval, SourcingCase
from procureguard.domain.enums import ApprovalDecision, ApprovalType, CaseState
from procureguard.domain.errors import ConflictError, NotFoundError
from procureguard.observability import logger

from .models import (
    ApprovalModel,
    AuditLogModel,
    DecisionEvidenceModel,
    DecisionModel,
    IdempotencyKeyModel,
    ScheduledReminderModel,
    SourcingCaseModel,
    UserModel,
    utcnow,
)

log = logger(__name__)


class TenantScopedRepository:
    """Base class. Every query is filtered by tenant; nothing is global."""

    def __init__(self, session: Session, tenant_id: str | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id or get_settings().default_tenant_id

    def _scoped(self, stmt: Select, model: Any) -> Select:
        return stmt.where(model.tenant_id == self.tenant_id)


class SqlSourcingCaseRepository(TenantScopedRepository):
    def get(self, case_id: str) -> SourcingCase | None:
        row = self.session.get(SourcingCaseModel, case_id)
        if row is None or row.tenant_id != self.tenant_id:
            return None
        return self._to_domain(row)

    def require(self, case_id: str) -> SourcingCase:
        case = self.get(case_id)
        if case is None:
            raise NotFoundError(f"Sourcing case {case_id} not found", case_id=case_id)
        return case

    def get_model(self, case_id: str) -> SourcingCaseModel | None:
        row = self.session.get(SourcingCaseModel, case_id)
        return row if row and row.tenant_id == self.tenant_id else None

    def get_by_pr_number(self, pr_number: str) -> SourcingCase | None:
        row = self.session.scalars(
            self._scoped(select(SourcingCaseModel), SourcingCaseModel).where(
                SourcingCaseModel.pr_number == pr_number
            )
        ).first()
        return self._to_domain(row) if row else None

    def save(self, case: SourcingCase, **extra: Any) -> None:
        row = self.session.get(SourcingCaseModel, case.case_id)
        if row is None:
            row = SourcingCaseModel(
                id=case.case_id,
                tenant_id=case.tenant_id or self.tenant_id,
                pr_number=case.pr_number,
                state=case.state.value,
                commercial_unlocked=case.commercial_unlocked,
                reminder_counts=dict(case.reminder_count_by_supplier),
                created_at=case.created_at,
                updated_at=case.updated_at,
                version=case.version,
            )
            self.session.add(row)
        else:
            if row.tenant_id != self.tenant_id:
                raise NotFoundError(f"Sourcing case {case.case_id} not found")
            # Optimistic concurrency: refuse to clobber a newer write.
            if case.version < row.version:
                raise ConflictError(
                    f"Case {case.case_id} was modified concurrently "
                    f"(loaded v{case.version}, stored v{row.version})",
                    case_id=case.case_id,
                )
            row.state = case.state.value
            row.commercial_unlocked = case.commercial_unlocked
            row.reminder_counts = dict(case.reminder_count_by_supplier)
            row.updated_at = case.updated_at
            row.version = max(case.version, row.version + 1)

        row.technical_approved_at = case.technical_approved_at
        row.award_approved_at = case.award_approved_at
        row.awarded_supplier_id = case.awarded_supplier_id
        row.negotiation_round = case.negotiation_round
        row.estimated_value_base = case.estimated_value_base
        row.base_currency = case.base_currency
        row.state_history = list(case.state_history)[-200:]
        row.cancellation_reason = case.cancellation_reason
        row.failure_reason = case.failure_reason
        for key, value in extra.items():
            if hasattr(row, key):
                setattr(row, key, value)
        self.session.flush()

    def list_by_state(
        self, states: Sequence[CaseState] | None = None, *, limit: int = 100, offset: int = 0
    ) -> list[SourcingCaseModel]:
        stmt = self._scoped(select(SourcingCaseModel), SourcingCaseModel)
        if states:
            stmt = stmt.where(SourcingCaseModel.state.in_([s.value for s in states]))
        stmt = stmt.order_by(desc(SourcingCaseModel.updated_at)).limit(limit).offset(offset)
        return list(self.session.scalars(stmt).all())

    def search(
        self,
        *,
        state: str | None = None,
        buyer_id: str | None = None,
        plant_code: str | None = None,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[SourcingCaseModel], int]:
        stmt = self._scoped(select(SourcingCaseModel), SourcingCaseModel)
        count_stmt = self._scoped(
            select(func.count()).select_from(SourcingCaseModel), SourcingCaseModel
        )
        for condition in _case_filters(state, buyer_id, plant_code, query):
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)
        total = int(self.session.scalar(count_stmt) or 0)
        rows = list(
            self.session.scalars(
                stmt.order_by(desc(SourcingCaseModel.updated_at)).limit(limit).offset(offset)
            ).all()
        )
        return rows, total

    def counts_by_state(self) -> dict[str, int]:
        rows = self.session.execute(
            self._scoped(
                select(SourcingCaseModel.state, func.count()).group_by(SourcingCaseModel.state),
                SourcingCaseModel,
            )
        ).all()
        return {str(state): int(count) for state, count in rows}

    @staticmethod
    def _to_domain(row: SourcingCaseModel) -> SourcingCase:
        return SourcingCase(
            case_id=row.id,
            pr_number=row.pr_number,
            state=CaseState(row.state),
            tenant_id=row.tenant_id,
            commercial_unlocked=bool(row.commercial_unlocked),
            technical_approved_at=row.technical_approved_at,
            award_approved_at=row.award_approved_at,
            reminder_count_by_supplier=dict(row.reminder_counts or {}),
            negotiation_round=int(row.negotiation_round or 0),
            awarded_supplier_id=row.awarded_supplier_id or "",
            estimated_value_base=Decimal(str(row.estimated_value_base or 0)),
            base_currency=row.base_currency or "USD",
            created_at=row.created_at,
            updated_at=row.updated_at,
            version=int(row.version or 1),
            state_history=list(row.state_history or []),
            cancellation_reason=row.cancellation_reason or "",
            failure_reason=row.failure_reason or "",
        )


def _case_filters(
    state: str | None, buyer_id: str | None, plant_code: str | None, query: str | None
) -> list[Any]:
    conditions: list[Any] = []
    if state:
        conditions.append(SourcingCaseModel.state == state)
    if buyer_id:
        conditions.append(SourcingCaseModel.buyer_id == buyer_id)
    if plant_code:
        conditions.append(SourcingCaseModel.plant_code == plant_code)
    if query:
        pattern = f"%{query.strip()}%"
        conditions.append(
            SourcingCaseModel.pr_number.ilike(pattern)
            | SourcingCaseModel.id.ilike(pattern)
            | SourcingCaseModel.title.ilike(pattern)
        )
    return conditions


class SqlApprovalRepository(TenantScopedRepository):
    def add(self, approval: Approval, *, ip_address: str = "", user_agent: str = "") -> None:
        self.session.add(
            ApprovalModel(
                id=approval.approval_id,
                tenant_id=self.tenant_id,
                case_id=approval.case_id,
                approval_type=approval.approval_type.value,
                decision=approval.decision.value,
                actor_id=approval.actor_id,
                actor_roles=list(approval.actor_roles),
                reason=approval.reason,
                subject_ref=approval.subject_ref,
                conditions=list(approval.conditions),
                payload=dict(approval.payload),
                signature=approval.signature,
                ip_address=ip_address,
                user_agent=user_agent[:500],
                created_at=approval.created_at,
            )
        )
        self.session.flush()

    def list_for_case(
        self, case_id: str, approval_type: ApprovalType | None = None
    ) -> list[Approval]:
        stmt = self._scoped(select(ApprovalModel), ApprovalModel).where(
            ApprovalModel.case_id == case_id
        )
        if approval_type:
            stmt = stmt.where(ApprovalModel.approval_type == approval_type.value)
        rows = self.session.scalars(stmt.order_by(ApprovalModel.created_at)).all()
        return [self._to_domain(row) for row in rows]

    def has_positive(self, case_id: str, approval_type: ApprovalType) -> bool:
        return any(a.is_positive for a in self.list_for_case(case_id, approval_type))

    @staticmethod
    def _to_domain(row: ApprovalModel) -> Approval:
        return Approval(
            approval_id=row.id,
            case_id=row.case_id,
            approval_type=ApprovalType(row.approval_type),
            decision=ApprovalDecision(row.decision),
            actor_id=row.actor_id,
            reason=row.reason,
            actor_roles=tuple(row.actor_roles or ()),
            subject_ref=row.subject_ref or "",
            conditions=tuple(row.conditions or ()),
            payload=dict(row.payload or {}),
            created_at=row.created_at,
            signature=row.signature or "",
        )


class SqlDecisionRepository(TenantScopedRepository):
    """Persists agent recommendations together with their supporting evidence."""

    def record(
        self,
        *,
        case_id: str,
        decision_type: str,
        recommendation: dict[str, Any],
        rationale: str,
        confidence: Decimal | float = 0,
        model_metadata: dict[str, Any] | None = None,
        evidence: Sequence[dict[str, Any]] = (),
        input_digest: str = "",
    ) -> DecisionModel:
        sequence = (
            int(
                self.session.scalar(
                    self._scoped(
                        select(func.count()).select_from(DecisionModel), DecisionModel
                    ).where(
                        DecisionModel.case_id == case_id,
                        DecisionModel.decision_type == decision_type,
                    )
                )
                or 0
            )
            + 1
        )
        row = DecisionModel(
            tenant_id=self.tenant_id,
            case_id=case_id,
            decision_type=decision_type,
            sequence=sequence,
            recommendation=recommendation,
            rationale=rationale,
            confidence=Decimal(str(confidence)),
            model_metadata=model_metadata or {},
            input_digest=input_digest or digest_of(recommendation),
        )
        self.session.add(row)
        self.session.flush()

        # Evidence is keyed by (decision, type, id, version). Callers routinely
        # cite the same document for many findings - forty requirements from one
        # specification, say - so collapse duplicates here rather than making
        # every caller remember to.
        seen: set[tuple[str, str, str]] = set()
        for item in evidence:
            evidence_type = str(item.get("evidence_type", "UNKNOWN"))[:48]
            evidence_id = str(item.get("evidence_id", ""))[:120]
            evidence_version = str(item.get("evidence_version", ""))[:120]
            if not evidence_id:
                continue
            key = (evidence_type, evidence_id, evidence_version)
            if key in seen:
                continue
            seen.add(key)
            self.session.add(
                DecisionEvidenceModel(
                    decision_id=row.id,
                    evidence_type=evidence_type,
                    evidence_id=evidence_id,
                    evidence_version=evidence_version,
                    role=str(item.get("role", "SUPPORTS"))[:32],
                    excerpt=str(item.get("excerpt", ""))[:4000],
                    weight=Decimal(str(item.get("weight", 1))),
                )
            )
        self.session.flush()
        return row

    def latest(self, case_id: str, decision_type: str) -> DecisionModel | None:
        return self.session.scalars(
            self._scoped(select(DecisionModel), DecisionModel)
            .where(DecisionModel.case_id == case_id, DecisionModel.decision_type == decision_type)
            .order_by(desc(DecisionModel.sequence))
            .limit(1)
        ).first()

    def list_for_case(self, case_id: str, limit: int = 200) -> list[DecisionModel]:
        return list(
            self.session.scalars(
                self._scoped(select(DecisionModel), DecisionModel)
                .where(DecisionModel.case_id == case_id)
                .order_by(DecisionModel.created_at)
                .limit(limit)
            ).all()
        )

    def evidence_for(self, decision_id: str) -> list[DecisionEvidenceModel]:
        return list(
            self.session.scalars(
                select(DecisionEvidenceModel).where(
                    DecisionEvidenceModel.decision_id == decision_id
                )
            ).all()
        )


class SqlAuditRepository(TenantScopedRepository):
    """Append-only. There is deliberately no update or delete method."""

    def record(
        self,
        *,
        entity_type: str,
        action: str,
        entity_id: str = "",
        case_id: str = "",
        actor_id: str = "SYSTEM",
        actor_type: str = "SYSTEM",
        actor_roles: Sequence[str] = (),
        before_state: dict[str, Any] | None = None,
        after_state: dict[str, Any] | None = None,
        detail: str = "",
        correlation_id: str = "",
        workflow_id: str = "",
        ip_address: str = "",
    ) -> None:
        self.session.add(
            AuditLogModel(
                tenant_id=self.tenant_id,
                case_id=case_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action=action,
                actor_id=actor_id,
                actor_type=actor_type,
                actor_roles=list(actor_roles),
                before_state=_jsonable(before_state or {}),
                after_state=_jsonable(after_state or {}),
                detail=detail[:8000],
                correlation_id=correlation_id,
                workflow_id=workflow_id,
                ip_address=ip_address,
            )
        )
        self.session.flush()

    def list_for_case(self, case_id: str, limit: int = 500) -> list[AuditLogModel]:
        return list(
            self.session.scalars(
                self._scoped(select(AuditLogModel), AuditLogModel)
                .where(AuditLogModel.case_id == case_id)
                .order_by(AuditLogModel.created_at)
                .limit(limit)
            ).all()
        )

    def search(
        self,
        *,
        actor_id: str | None = None,
        action: str | None = None,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[AuditLogModel]:
        stmt = self._scoped(select(AuditLogModel), AuditLogModel)
        if actor_id:
            stmt = stmt.where(AuditLogModel.actor_id == actor_id)
        if action:
            stmt = stmt.where(AuditLogModel.action == action)
        if since:
            stmt = stmt.where(AuditLogModel.created_at >= since)
        return list(
            self.session.scalars(
                stmt.order_by(desc(AuditLogModel.created_at)).limit(limit)
            ).all()
        )


class SqlIdempotencyRepository(TenantScopedRepository):
    """Makes at-least-once activity delivery safe for real side effects.

    `claim` inserts a row and returns True only for the caller that wins the
    race; every retry gets False plus the original result.
    """

    def claim(
        self,
        key: str,
        *,
        scope: str,
        ttl_hours: int = 720,
        result_payload: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        existing = self.session.get(IdempotencyKeyModel, key)
        if existing is not None:
            return False, dict(existing.result_payload or {})
        row = IdempotencyKeyModel(
            key=key,
            tenant_id=self.tenant_id,
            scope=scope,
            result_payload=result_payload or {},
            expires_at=utcnow() + timedelta(hours=ttl_hours),
        )
        self.session.add(row)
        try:
            self.session.flush()
        except IntegrityError:
            self.session.rollback()
            existing = self.session.get(IdempotencyKeyModel, key)
            return False, dict(existing.result_payload or {}) if existing else (False, {})
        return True, {}

    def complete(self, key: str, *, result_ref: str = "", payload: dict[str, Any] | None = None) -> None:
        row = self.session.get(IdempotencyKeyModel, key)
        if row is None:
            return
        row.result_ref = result_ref
        row.result_payload = _jsonable(payload or {})
        self.session.flush()

    def release(self, key: str) -> None:
        """Undo a claim when the guarded side effect failed permanently."""
        self.session.execute(delete(IdempotencyKeyModel).where(IdempotencyKeyModel.key == key))
        self.session.flush()

    def purge_expired(self, limit: int = 5_000) -> int:
        stale = self.session.scalars(
            select(IdempotencyKeyModel.key)
            .where(IdempotencyKeyModel.expires_at < utcnow())
            .limit(limit)
        ).all()
        if not stale:
            return 0
        self.session.execute(
            delete(IdempotencyKeyModel).where(IdempotencyKeyModel.key.in_(list(stale)))
        )
        self.session.flush()
        return len(stale)


class SqlReminderRepository(TenantScopedRepository):
    def schedule(
        self,
        *,
        case_id: str,
        reminder_type: str,
        due_at: datetime,
        vendor_id: str = "",
        subject_ref: str = "",
        max_attempts: int = 3,
        payload: dict[str, Any] | None = None,
    ) -> ScheduledReminderModel:
        row = ScheduledReminderModel(
            tenant_id=self.tenant_id,
            case_id=case_id,
            reminder_type=reminder_type,
            vendor_id=vendor_id,
            subject_ref=subject_ref,
            due_at=due_at,
            max_attempts=max_attempts,
            payload=payload or {},
        )
        self.session.add(row)
        self.session.flush()
        return row

    def due(self, *, now: datetime | None = None, limit: int = 200) -> list[ScheduledReminderModel]:
        now = now or datetime.now(UTC)
        return list(
            self.session.scalars(
                self._scoped(select(ScheduledReminderModel), ScheduledReminderModel)
                .where(
                    ScheduledReminderModel.status == "SCHEDULED",
                    ScheduledReminderModel.due_at <= now,
                )
                .order_by(ScheduledReminderModel.due_at)
                .limit(limit)
            ).all()
        )

    def mark_sent(self, reminder_id: str, *, escalated: bool = False) -> None:
        row = self.session.get(ScheduledReminderModel, reminder_id)
        if row is None:
            return
        row.sent_at = utcnow()
        row.attempt += 1
        row.status = "SENT"
        if escalated:
            row.escalation_level += 1
        self.session.flush()

    def cancel_for_case(self, case_id: str, reminder_type: str | None = None) -> int:
        stmt = self._scoped(select(ScheduledReminderModel), ScheduledReminderModel).where(
            ScheduledReminderModel.case_id == case_id,
            ScheduledReminderModel.status == "SCHEDULED",
        )
        if reminder_type:
            stmt = stmt.where(ScheduledReminderModel.reminder_type == reminder_type)
        rows = list(self.session.scalars(stmt).all())
        for row in rows:
            row.status = "CANCELLED"
        self.session.flush()
        return len(rows)

    def list_for_case(self, case_id: str) -> list[ScheduledReminderModel]:
        return list(
            self.session.scalars(
                self._scoped(select(ScheduledReminderModel), ScheduledReminderModel)
                .where(ScheduledReminderModel.case_id == case_id)
                .order_by(ScheduledReminderModel.due_at)
            ).all()
        )


class SqlUserRepository(TenantScopedRepository):
    def get_by_actor(self, actor_id: str) -> UserModel | None:
        return self.session.scalars(
            self._scoped(select(UserModel), UserModel).where(UserModel.actor_id == actor_id)
        ).first()

    def get_by_email(self, email: str) -> UserModel | None:
        return self.session.scalars(
            self._scoped(select(UserModel), UserModel).where(
                func.lower(UserModel.email) == email.strip().lower()
            )
        ).first()

    def get_by_api_key(self, raw_key: str) -> UserModel | None:
        digest = hashlib.sha256(raw_key.encode()).hexdigest()
        return self.session.scalars(
            self._scoped(select(UserModel), UserModel).where(
                UserModel.api_key_hash == digest, UserModel.active.is_(True)
            )
        ).first()

    def get_by_subject(self, subject: str) -> UserModel | None:
        return self.session.scalars(
            self._scoped(select(UserModel), UserModel).where(
                UserModel.external_subject == subject
            )
        ).first()

    def upsert(
        self,
        *,
        actor_id: str,
        email: str,
        display_name: str = "",
        roles: Sequence[str] = (),
        department: str = "",
        approval_limit_base: Decimal | float = 0,
        external_subject: str = "",
        api_key: str = "",
    ) -> UserModel:
        row = self.get_by_actor(actor_id)
        if row is None:
            row = UserModel(tenant_id=self.tenant_id, actor_id=actor_id, email=email)
            self.session.add(row)
        row.email = email
        row.display_name = display_name or row.display_name
        row.roles = list(roles) or list(row.roles or [])
        row.department = department or row.department
        row.approval_limit_base = Decimal(str(approval_limit_base))
        if external_subject:
            row.external_subject = external_subject
        if api_key:
            row.api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        self.session.flush()
        return row

    def list_by_role(self, role: str, limit: int = 100) -> list[UserModel]:
        rows = self.session.scalars(
            self._scoped(select(UserModel), UserModel)
            .where(UserModel.active.is_(True))
            .limit(500)
        ).all()
        return [r for r in rows if role in (r.roles or [])][:limit]


def digest_of(payload: Any) -> str:
    """Stable hash of a payload, used to tie decisions to their exact inputs."""
    return hashlib.sha256(
        json.dumps(_jsonable(payload), sort_keys=True, default=str).encode()
    ).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value
