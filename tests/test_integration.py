"""End-to-end tests against a real CockroachDB.

These exercise the actual queries, the actual vector search and the actual
fifteen-stage pipeline. Run with:

    PROCUREGUARD_TEST_DB=1 pytest -m integration
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, requires_db]


# ── schema and capability ────────────────────────────────────────────────────

def test_schema_and_vector_capability(seeded_database):
    from procureguard.infrastructure.db.session import healthcheck

    health = healthcheck()
    assert health["status"] == "ok"
    assert health["vector_backend"] in ("native", "json")


def test_seed_produced_realistic_volumes(seeded_database):
    counts = seeded_database.counts
    assert counts["materials"] > 50
    assert counts["vendors"] > 10
    assert counts["purchase_history"] > 1_000
    assert counts["goods_receipt_history"] > 500
    assert counts["fx_rates"] > 100


# ── bounded enterprise queries ───────────────────────────────────────────────

def test_price_statistics_are_internally_consistent(seeded_database):
    from procureguard.infrastructure.db.session import read_session
    from procureguard.infrastructure.factory import ServiceContext

    with read_session() as session:
        ctx = ServiceContext.build(session)
        material = next(
            (
                m
                for m in ctx.repos.materials.search_by_group("MG-FAST", limit=40)
                if ctx.repos.history.get_price_statistics(m.material_code, months=72)["order_count"]
                >= 5
            ),
            None,
        )
        if material is None:
            pytest.skip("no material with enough history in this seed")

        stats = ctx.repos.history.get_price_statistics(material.material_code, months=72)
        assert stats["min_unit_price"] <= stats["median_unit_price"] <= stats["max_unit_price"]
        assert stats["p25_unit_price"] <= stats["median_unit_price"] <= stats["p75_unit_price"]
        assert stats["order_count"] > 0


def test_benchmark_produces_a_defensible_target(seeded_database):
    from procureguard.application.history_service import HistoricalProcurementService
    from procureguard.infrastructure.db.session import read_session
    from procureguard.infrastructure.factory import ServiceContext

    with read_session() as session:
        ctx = ServiceContext.build(session)
        for material in ctx.repos.materials.search_by_group("MG-BEARING", limit=40):
            benchmark = HistoricalProcurementService(ctx).build_benchmark(
                material.material_code, requested_quantity=Decimal(100), window_months=72
            )
            if not benchmark.has_history:
                continue
            assert benchmark.benchmark_unit_price is not None
            if benchmark.should_cost is not None and benchmark.target_price is not None:
                # A target below the demonstrated floor is not credible.
                assert benchmark.target_price >= benchmark.should_cost
            return
        pytest.skip("no bearing material with history in this seed")


def test_vendor_performance_is_derived_from_receipts(seeded_database):
    from procureguard.infrastructure.db.session import read_session
    from procureguard.infrastructure.factory import ServiceContext

    with read_session() as session:
        ctx = ServiceContext.build(session)
        vendors = ctx.repos.vendors.list_qualified(limit=20)
        assert vendors
        for vendor in vendors:
            performance = ctx.repos.history.get_vendor_performance(vendor.vendor_id, months=72)
            if performance["receipt_count"]:
                assert 0 <= performance["on_time_pct"] <= 100
                return
        pytest.skip("no vendor with goods receipts in this seed")


def test_vector_search_finds_semantically_similar_materials(seeded_database):
    from procureguard.infrastructure.db.session import read_session
    from procureguard.infrastructure.factory import ServiceContext

    with read_session() as session:
        ctx = ServiceContext.build(session)
        sample = ctx.repos.materials.search_by_group("MG-VALVE", limit=1)
        if not sample:
            pytest.skip("no valve materials in this seed")

        vector = ctx.embedder.embed(sample[0].description)
        hits = ctx.repos.materials.semantic_search(
            vector, top_k=5, dimensions=ctx.embedder.dimensions
        )
        assert hits, "vector search returned nothing"
        assert hits[0][0] == sample[0].material_code, "a material should be its own nearest neighbour"


# ── the full pipeline ────────────────────────────────────────────────────────

def test_full_fifteen_stage_pipeline(seeded_database):
    """Drive a real case from requisition to released purchase order."""
    from procureguard.seed.scenarios import SCENARIOS, _create_case, run_pipeline_for_case

    created = _create_case(SCENARIOS[1], 901)
    assert created["case_id"]

    result = run_pipeline_for_case(
        created["case_id"], approver="jordan.head", auto_approve=True, simulate_quotes=True
    )
    stages = [entry["stage"] for entry in result["trace"]]

    # Every stage up to the technical gate must have run.
    for expected in (
        "1-2 pr_and_material_validation",
        "4-5 evidence_and_requirements",
        "3 historical_benchmark",
        "6 supplier_shortlist",
        "7 rfq_generation",
        "8 email_integration",
        "9 quotation_ingestion",
        "10 technical_comparison",
    ):
        assert expected in stages, f"{expected} did not run: {stages}"

    # The case either completes, or halts for a legitimate, named reason.
    if not result.get("completed"):
        assert result["halted_at"] in (
            "no_qualified_bid", "shortlist", "validation", "no_qualified_bid_at_award",
            "award_chain_incomplete", "awaiting_technical_approval",
        )
        return

    assert result["final_state"] == "ORDER_PLACED"
    assert "15 po_release_and_info_records" in stages


def test_sealed_bids_are_unreadable_before_technical_approval(seeded_database):
    """The central guarantee, checked against the database rather than in memory."""
    from procureguard.infrastructure.db.session import read_session
    from procureguard.infrastructure.factory import ServiceContext
    from procureguard.seed.scenarios import SCENARIOS, _create_case, run_pipeline_for_case

    created = _create_case(SCENARIOS[1], 902)
    run_pipeline_for_case(
        created["case_id"], auto_approve=False, simulate_quotes=True
    )

    with read_session() as session:
        ctx = ServiceContext.build(session)
        case = ctx.repos.cases.require(created["case_id"])
        assert not case.commercial_unlocked

        quotations = ctx.repos.quotations.list_for_case(
            created["case_id"], commercial_unlocked=False
        )
        if not quotations:
            pytest.skip("no quotations were received in this run")

        for quotation in quotations:
            if not quotation.is_sealed:
                continue
            # Nothing priced may exist in plaintext while sealed.
            assert quotation.total_amount == Decimal(0)
            assert quotation.currency in ("", None)
            assert quotation.sealed_payload, "a sealed bid must have an encrypted payload"
            for line in quotation.lines:
                assert line.unit_price == Decimal(0)
            # But the lines still exist: sealing must not destroy data.
            assert quotation.lines


def test_audit_trail_records_every_human_decision(seeded_database):
    from procureguard.infrastructure.db.session import read_session
    from procureguard.infrastructure.factory import ServiceContext
    from procureguard.seed.scenarios import SCENARIOS, _create_case, run_pipeline_for_case

    created = _create_case(SCENARIOS[1], 903)
    run_pipeline_for_case(created["case_id"], auto_approve=True, simulate_quotes=True)

    with read_session() as session:
        ctx = ServiceContext.build(session)
        entries = ctx.repos.audit.list_for_case(created["case_id"])
        actions = {entry.action for entry in entries}
        assert "CASE_OPENED" in actions
        assert any("RFQ" in action for action in actions)
        # Decisions are explainable: each carries a rationale and evidence.
        decisions = ctx.repos.decisions.list_for_case(created["case_id"])
        assert decisions
        assert all(decision.rationale for decision in decisions)


def test_transaction_retry_helper_survives_contention(seeded_database):
    """Serialization failures are a protocol, not an error path."""
    from procureguard.infrastructure.db.retry import is_retryable, run_in_transaction
    from procureguard.infrastructure.db.session import get_session_factory

    attempts: list[int] = []

    def flaky(session):
        attempts.append(1)
        if len(attempts) < 3:
            from psycopg.errors import SerializationFailure
            from sqlalchemy.exc import OperationalError

            raise OperationalError("stmt", {}, SerializationFailure("restart transaction"))
        return "done"

    session = get_session_factory()()
    try:
        assert run_in_transaction(session, flaky, base_delay_ms=1) == "done"
        assert len(attempts) == 3
    finally:
        session.close()

    assert not is_retryable(ValueError("not a database error"))


# ── API ──────────────────────────────────────────────────────────────────────

def test_api_health_and_identity(api_client, buyer_headers):
    assert api_client.get("/api/v1/health").json()["status"] == "ok"

    ready = api_client.get("/api/v1/health/ready").json()
    assert ready["database"]["status"] == "ok"

    whoami = api_client.get("/api/v1/whoami", headers=buyer_headers).json()
    assert whoami["actor_id"] == "sam.senior"
    assert "RFQ_RELEASE" in whoami["permissions"]


def test_api_enforces_rbac_on_approval_routes(api_client, seeded_database):
    """A buyer must not be able to sign off a technical evaluation."""
    from procureguard.seed.scenarios import SCENARIOS, _create_case

    created = _create_case(SCENARIOS[1], 904)
    response = api_client.post(
        f"/api/v1/cases/{created['case_id']}/approvals/technical",
        json={"reason": "Looks fine to me"},
        headers={"X-Actor-Id": "dana.buyer", "X-Actor-Roles": "BUYER"},
    )
    assert response.status_code == 403
    assert response.json()["error"] == "FORBIDDEN"


def test_api_case_detail_hides_commercial_data_while_sealed(api_client, seeded_database):
    from procureguard.seed.scenarios import SCENARIOS, _create_case, run_pipeline_for_case

    created = _create_case(SCENARIOS[1], 905)
    run_pipeline_for_case(created["case_id"], auto_approve=False, simulate_quotes=True)

    detail = api_client.get(
        f"/api/v1/cases/{created['case_id']}",
        headers={"X-Actor-Id": "dana.buyer", "X-Actor-Roles": "BUYER"},
    ).json()

    for quotation in detail["quotations"]:
        if quotation["is_sealed"]:
            assert "total_amount" not in quotation
            assert "commercial_visibility" in quotation


def test_api_material_search_and_benchmark(api_client, seeded_database, buyer_headers):
    search = api_client.get(
        "/api/v1/data/materials/search", params={"q": "valve"}, headers=buyer_headers
    ).json()
    if not search["materials"]:
        pytest.skip("no valve materials in this seed")

    code = search["materials"][0]["material_code"]
    benchmark = api_client.get(
        f"/api/v1/data/materials/{code}/benchmark",
        params={"quantity": 100},
        headers=buyer_headers,
    ).json()
    assert benchmark["material_code"] == code


def test_api_rejects_unknown_case(api_client, buyer_headers):
    response = api_client.get("/api/v1/cases/PG-DOES-NOT-EXIST", headers=buyer_headers)
    assert response.status_code == 404
    assert response.json()["error"] == "NOT_FOUND"
