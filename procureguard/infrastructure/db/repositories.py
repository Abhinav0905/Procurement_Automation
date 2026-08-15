"""Repository facade.

Callers import from here; the implementations live in `repo_core`,
`repo_enterprise`, `repo_evidence` and `repo_sourcing` so no single module has
to hold forty tables' worth of query logic.
"""

from __future__ import annotations

from .repo_core import (
    SqlApprovalRepository,
    SqlAuditRepository,
    SqlDecisionRepository,
    SqlIdempotencyRepository,
    SqlReminderRepository,
    SqlSourcingCaseRepository,
    SqlUserRepository,
    TenantScopedRepository,
    digest_of,
)
from .repo_enterprise import (
    SqlContractRepository,
    SqlEnterpriseHistoryRepository,
    SqlFreightRepository,
    SqlFxRepository,
    SqlInfoRecordRepository,
    SqlMaterialRepository,
    SqlVendorRepository,
)
from .repo_evidence import (
    SqlChunkRepository,
    SqlClaimRepository,
    SqlDocumentRepository,
    SqlSecurityFindingRepository,
)
from .repo_sourcing import (
    SqlBidRankingRepository,
    SqlCommunicationRepository,
    SqlComplianceRepository,
    SqlInfoRecordProposalRepository,
    SqlNegotiationRepository,
    SqlNormalizedOfferRepository,
    SqlPoRecommendationRepository,
    SqlQuotationRepository,
    SqlRequirementRepository,
    SqlRequisitionRepository,
    SqlRfqRepository,
    SqlSupplierCandidateRepository,
)

__all__ = [
    "SqlApprovalRepository",
    "SqlAuditRepository",
    "SqlBidRankingRepository",
    "SqlChunkRepository",
    "SqlClaimRepository",
    "SqlCommunicationRepository",
    "SqlComplianceRepository",
    "SqlContractRepository",
    "SqlDecisionRepository",
    "SqlDocumentRepository",
    "SqlEnterpriseHistoryRepository",
    "SqlFreightRepository",
    "SqlFxRepository",
    "SqlIdempotencyRepository",
    "SqlInfoRecordProposalRepository",
    "SqlInfoRecordRepository",
    "SqlMaterialRepository",
    "SqlNegotiationRepository",
    "SqlNormalizedOfferRepository",
    "SqlPoRecommendationRepository",
    "SqlQuotationRepository",
    "SqlReminderRepository",
    "SqlRequirementRepository",
    "SqlRequisitionRepository",
    "SqlRfqRepository",
    "SqlSecurityFindingRepository",
    "SqlSourcingCaseRepository",
    "SqlSupplierCandidateRepository",
    "SqlUserRepository",
    "SqlVendorRepository",
    "TenantScopedRepository",
    "digest_of",
]
