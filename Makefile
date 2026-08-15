.DEFAULT_GOAL := help
PY ?= python3
VENV := .venv
BIN := $(VENV)/bin
COMPOSE ?= docker compose

# Export .env into the process environment before running application code.
# Settings reads .env directly, but boto3 does not: it walks its own credential
# chain and would otherwise pick up ~/.aws/credentials from an unrelated
# project. Prefixed onto every target that can reach an AWS adapter.
ENVLOAD := set -a; [ -f .env ] && . ./.env; set +a;

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ── environment ──────────────────────────────────────────────────────────────
$(VENV):
	$(PY) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip setuptools wheel

.PHONY: install
install: $(VENV) ## Create the virtualenv and install the package
	$(BIN)/python -m pip install -e ".[dev]"

.PHONY: install-all
install-all: $(VENV) ## Install with every optional extra (PDF, OIDC, OTel)
	$(BIN)/python -m pip install -e ".[all]"

# ── infrastructure ───────────────────────────────────────────────────────────
.PHONY: up
up: ## Start CockroachDB, Temporal, MinIO and MailHog
	$(COMPOSE) up -d cockroach temporal temporal-ui minio mailhog
	@echo "Waiting for CockroachDB..."
	@until $(COMPOSE) exec -T cockroach ./cockroach sql --insecure -e "SELECT 1" >/dev/null 2>&1; do sleep 1; done
	@$(COMPOSE) exec -T cockroach ./cockroach sql --insecure -e "CREATE DATABASE IF NOT EXISTS procureguard;"
	@echo "CockroachDB   http://localhost:8081"
	@echo "Temporal UI   http://localhost:8088"
	@echo "MinIO         http://localhost:9001  (procureguard / procureguard)"
	@echo "MailHog       http://localhost:8025"

.PHONY: down
down: ## Stop all containers
	$(COMPOSE) down

.PHONY: clean
clean: ## Stop containers and delete all volumes and local artifacts
	$(COMPOSE) down -v
	rm -rf var/ .pytest_cache .ruff_cache

# ── database ─────────────────────────────────────────────────────────────────
.PHONY: migrate
migrate: ## Apply database migrations
	$(BIN)/alembic upgrade head

.PHONY: db-check
db-check: ## Report database connectivity and vector capability
	$(BIN)/procureguard db check

.PHONY: db-stats
db-stats: ## Show row counts per table
	$(BIN)/procureguard db stats

# ── data ─────────────────────────────────────────────────────────────────────
.PHONY: seed
seed: ## Seed the synthetic enterprise (SCALE=tiny|small|medium|large|xlarge)
	@$(ENVLOAD) $(BIN)/procureguard seed --scale $(or $(SCALE),medium) --reset

.PHONY: demo
demo: ## Seed and drive demo cases through all fifteen stages
	@$(ENVLOAD) $(BIN)/procureguard demo --scale $(or $(SCALE),small) --reset

# ── run ──────────────────────────────────────────────────────────────────────
.PHONY: run
run: ## Start the API and approval UI on http://localhost:8000
	@$(ENVLOAD) $(BIN)/python -m uvicorn procureguard.api.main:create_app --factory --reload --port 8000

.PHONY: worker
worker: ## Start the Temporal worker
	@$(ENVLOAD) $(BIN)/python -m procureguard.workflows.worker

# ── quality ──────────────────────────────────────────────────────────────────
.PHONY: test
test: ## Run the unit test suite
	$(BIN)/pytest

.PHONY: test-integration
test-integration: ## Run tests including those needing CockroachDB
	PROCUREGUARD_TEST_DB=1 $(BIN)/pytest -m "integration or not integration"

.PHONY: lint
lint: ## Lint and compile-check
	$(BIN)/ruff check procureguard tests
	$(BIN)/python -m compileall -q procureguard

.PHONY: format
format: ## Apply safe lint fixes
	$(BIN)/ruff check --fix procureguard tests

.PHONY: typecheck
typecheck: ## Run mypy
	$(BIN)/mypy procureguard

.PHONY: check
check: lint test ## Lint and test

.PHONY: bootstrap
bootstrap: install up migrate demo ## Full first-run setup, end to end
	@echo ""
	@echo "Ready. Start the API with 'make run' and open http://localhost:8000"
