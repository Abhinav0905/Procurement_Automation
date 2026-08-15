"""Composition root.

One place decides which adapter each port gets, and one object carries the
repositories bound to a single database session. Application services take a
`ServiceContext` and never construct infrastructure themselves, which is what
keeps them testable with in-memory doubles.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from sqlalchemy.orm import Session

from procureguard.config import Settings, get_settings
from procureguard.domain.policies import ProcurementPolicy
from procureguard.infrastructure.db.repositories import (
    SqlApprovalRepository,
    SqlAuditRepository,
    SqlBidRankingRepository,
    SqlChunkRepository,
    SqlClaimRepository,
    SqlCommunicationRepository,
    SqlComplianceRepository,
    SqlContractRepository,
    SqlDecisionRepository,
    SqlDocumentRepository,
    SqlEnterpriseHistoryRepository,
    SqlFreightRepository,
    SqlFxRepository,
    SqlIdempotencyRepository,
    SqlInfoRecordProposalRepository,
    SqlInfoRecordRepository,
    SqlMaterialRepository,
    SqlNegotiationRepository,
    SqlNormalizedOfferRepository,
    SqlPoRecommendationRepository,
    SqlQuotationRepository,
    SqlReminderRepository,
    SqlRequirementRepository,
    SqlRequisitionRepository,
    SqlRfqRepository,
    SqlSecurityFindingRepository,
    SqlSourcingCaseRepository,
    SqlSupplierCandidateRepository,
    SqlUserRepository,
    SqlVendorRepository,
)
from procureguard.observability import logger
from procureguard.security.document_firewall import SupplierDocumentFirewall

log = logger(__name__)


# Adapters are process-wide and stateless, so they are built once. Repositories
# are per-session and built per request.
@lru_cache(maxsize=1)
def get_object_store(_key: str = "") -> Any:
    from procureguard.infrastructure.storage.object_store import build_object_store

    return build_object_store(get_settings())


@lru_cache(maxsize=1)
def get_language_model(_key: str = "") -> Any:
    settings = get_settings()
    if settings.llm_backend == "bedrock":
        from procureguard.infrastructure.llm.bedrock import BedrockLanguageModel

        return BedrockLanguageModel(settings)
    from procureguard.infrastructure.llm.deterministic import DeterministicLanguageModel

    return DeterministicLanguageModel(settings)


@lru_cache(maxsize=1)
def get_embedding_model(_key: str = "") -> Any:
    settings = get_settings()
    if settings.embedding_backend == "bedrock":
        from procureguard.infrastructure.llm.bedrock import BedrockEmbeddingModel

        return BedrockEmbeddingModel(settings)
    from procureguard.infrastructure.llm.deterministic import HashingEmbeddingModel

    return HashingEmbeddingModel(settings.embedding_dimensions)


@lru_cache(maxsize=1)
def get_mailer(_key: str = "") -> Any:
    from procureguard.infrastructure.email.mailer import build_mailer

    return build_mailer(get_settings())


@lru_cache(maxsize=1)
def get_mail_receiver(_key: str = "") -> Any:
    from procureguard.infrastructure.email.receiver import build_mail_receiver

    return build_mail_receiver(get_settings())


@lru_cache(maxsize=1)
def get_encryptor(_key: str = "") -> Any:
    from procureguard.security.crypto import build_encryptor

    return build_encryptor(get_settings())


@lru_cache(maxsize=1)
def get_firewall(_key: str = "") -> SupplierDocumentFirewall:
    return SupplierDocumentFirewall()


def reset_adapter_cache() -> None:
    """Test helper: rebuild adapters after changing settings."""
    for builder in (
        get_object_store,
        get_language_model,
        get_embedding_model,
        get_mailer,
        get_mail_receiver,
        get_encryptor,
        get_firewall,
    ):
        builder.cache_clear()


@dataclass(slots=True)
class Repositories:
    """Every repository, bound to one session and one tenant."""

    cases: SqlSourcingCaseRepository
    approvals: SqlApprovalRepository
    decisions: SqlDecisionRepository
    audit: SqlAuditRepository
    idempotency: SqlIdempotencyRepository
    reminders: SqlReminderRepository
    users: SqlUserRepository
    materials: SqlMaterialRepository
    vendors: SqlVendorRepository
    history: SqlEnterpriseHistoryRepository
    info_records: SqlInfoRecordRepository
    contracts: SqlContractRepository
    fx: SqlFxRepository
    freight: SqlFreightRepository
    documents: SqlDocumentRepository
    chunks: SqlChunkRepository
    claims: SqlClaimRepository
    findings: SqlSecurityFindingRepository
    requisitions: SqlRequisitionRepository
    requirements: SqlRequirementRepository
    candidates: SqlSupplierCandidateRepository
    rfqs: SqlRfqRepository
    quotations: SqlQuotationRepository
    compliance: SqlComplianceRepository
    normalized_offers: SqlNormalizedOfferRepository
    rankings: SqlBidRankingRepository
    negotiations: SqlNegotiationRepository
    po_recommendations: SqlPoRecommendationRepository
    info_record_proposals: SqlInfoRecordProposalRepository
    communications: SqlCommunicationRepository

    @classmethod
    def build(cls, session: Session, tenant_id: str) -> Repositories:
        return cls(
            cases=SqlSourcingCaseRepository(session, tenant_id),
            approvals=SqlApprovalRepository(session, tenant_id),
            decisions=SqlDecisionRepository(session, tenant_id),
            audit=SqlAuditRepository(session, tenant_id),
            idempotency=SqlIdempotencyRepository(session, tenant_id),
            reminders=SqlReminderRepository(session, tenant_id),
            users=SqlUserRepository(session, tenant_id),
            materials=SqlMaterialRepository(session, tenant_id),
            vendors=SqlVendorRepository(session, tenant_id),
            history=SqlEnterpriseHistoryRepository(session, tenant_id),
            info_records=SqlInfoRecordRepository(session, tenant_id),
            contracts=SqlContractRepository(session, tenant_id),
            fx=SqlFxRepository(session, tenant_id),
            freight=SqlFreightRepository(session, tenant_id),
            documents=SqlDocumentRepository(session, tenant_id),
            chunks=SqlChunkRepository(session, tenant_id),
            claims=SqlClaimRepository(session, tenant_id),
            findings=SqlSecurityFindingRepository(session, tenant_id),
            requisitions=SqlRequisitionRepository(session, tenant_id),
            requirements=SqlRequirementRepository(session, tenant_id),
            candidates=SqlSupplierCandidateRepository(session, tenant_id),
            rfqs=SqlRfqRepository(session, tenant_id),
            quotations=SqlQuotationRepository(session, tenant_id),
            compliance=SqlComplianceRepository(session, tenant_id),
            normalized_offers=SqlNormalizedOfferRepository(session, tenant_id),
            rankings=SqlBidRankingRepository(session, tenant_id),
            negotiations=SqlNegotiationRepository(session, tenant_id),
            po_recommendations=SqlPoRecommendationRepository(session, tenant_id),
            info_record_proposals=SqlInfoRecordProposalRepository(session, tenant_id),
            communications=SqlCommunicationRepository(session, tenant_id),
        )


@dataclass(slots=True)
class ServiceContext:
    """What every application service receives.

    Carrying the session explicitly (rather than hiding it in a scoped registry)
    is deliberate: it makes the transaction boundary visible at every call site,
    which is how the "never hold a transaction open across a remote call" rule
    stays enforceable by reading the code.
    """

    session: Session
    settings: Settings
    tenant_id: str
    repos: Repositories
    policy: ProcurementPolicy
    object_store: Any
    model: Any
    embedder: Any
    mailer: Any
    encryptor: Any
    firewall: SupplierDocumentFirewall
    actor_id: str = "SYSTEM"
    actor_roles: tuple[str, ...] = ()
    correlation_id: str = ""

    @classmethod
    def build(
        cls,
        session: Session,
        *,
        settings: Settings | None = None,
        tenant_id: str = "",
        actor_id: str = "SYSTEM",
        actor_roles: tuple[str, ...] = (),
        correlation_id: str = "",
    ) -> ServiceContext:
        settings = settings or get_settings()
        tenant = tenant_id or settings.default_tenant_id
        return cls(
            session=session,
            settings=settings,
            tenant_id=tenant,
            repos=Repositories.build(session, tenant),
            policy=ProcurementPolicy.from_settings(settings),
            object_store=get_object_store(),
            model=get_language_model(),
            embedder=get_embedding_model(),
            mailer=get_mailer(),
            encryptor=get_encryptor(),
            firewall=get_firewall(),
            actor_id=actor_id,
            actor_roles=actor_roles,
            correlation_id=correlation_id,
        )

    def with_actor(self, actor_id: str, roles: tuple[str, ...] = ()) -> ServiceContext:
        self.actor_id = actor_id
        self.actor_roles = roles
        return self

    def audit(self, **kwargs: Any) -> None:
        """Shorthand that stamps the current actor and correlation id."""
        kwargs.setdefault("actor_id", self.actor_id)
        kwargs.setdefault("actor_roles", self.actor_roles)
        kwargs.setdefault("correlation_id", self.correlation_id)
        kwargs.setdefault(
            "actor_type", "SYSTEM" if self.actor_id in ("SYSTEM", "AGENT") else "HUMAN"
        )
        self.repos.audit.record(**kwargs)
