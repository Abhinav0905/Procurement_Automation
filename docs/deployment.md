# Deployment

## Topology

| Component | Service | Notes |
| --- | --- | --- |
| API + approval UI | ECS Fargate behind an ALB | stateless, scale on CPU |
| Temporal workers | ECS Fargate | scale on task-queue backlog |
| Orchestration | Temporal Cloud | mTLS or API key |
| Database | CockroachDB Cloud | multi-region; `VECTOR` needs 25.2+ |
| Documents | S3 | versioning, Object Lock, SSE-KMS |
| Inbound email | SES receipt rule → S3 → SNS → API webhook | raw MIME retained |
| Outbound email | SES v2 with a dedicated subdomain | SPF, DKIM, DMARC |
| Model | Bedrock with a Guardrail | private VPC endpoint |
| Secrets | Secrets Manager | rotation enabled |
| Encryption | KMS CMK | separate key for sealed bids |
| Observability | OTLP → your collector | traces, metrics, JSON logs |

Both containers come from the same image; only the command differs.

## Configuration

Production requires these, and `Settings` **refuses to start** without them:

```bash
APP_ENV=prod
AUTH_MODE=oidc              # dev mode is rejected in prod
OBJECT_STORE_BACKEND=s3
ENCRYPTION_BACKEND=kms
SESSION_SECRET=<from Secrets Manager>
```

Then:

```bash
DATABASE_URL=cockroachdb+psycopg://user:pass@host:26257/procureguard?sslmode=verify-full
TEMPORAL_ADDRESS=your-ns.tmprl.cloud:7233
TEMPORAL_NAMESPACE=your-ns
S3_BUCKET=... ; S3_KMS_KEY_ID=... ; KMS_KEY_ID=...
LLM_BACKEND=bedrock ; BEDROCK_MODEL_ID=... ; BEDROCK_GUARDRAIL_ID=...
EMBEDDING_BACKEND=bedrock ; EMBEDDING_DIMENSIONS=1024
EMAIL_BACKEND=ses ; EMAIL_FROM_ADDRESS=procurement@... ; EMAIL_REPLY_TO_DOMAIN=rfq...
OIDC_ISSUER=... ; OIDC_AUDIENCE=... ; OIDC_JWKS_URL=...
```

`EMBEDDING_DIMENSIONS` is part of the schema. Changing it requires re-embedding
every indexed chunk, so decide it before the first production load.

Leave `ALLOW_AUTOMATED_EMAIL_SEND` and `ALLOW_AUTOMATED_PO_CREATION` off until
the process has been observed with humans releasing each message.

## Database roles

Use three identities:

| Identity | Grants | Used by |
| --- | --- | --- |
| `procureguard_migrate` | DDL | migration job only |
| `procureguard_app` | DML on all tables, no DDL | API and workers |
| `procureguard_readonly` | SELECT | analytics, support |

The application must not hold DDL rights. Run migrations as a separate job before
rolling the service.

```bash
alembic upgrade head          # migration job
```

Baseline `0001` materialises the full declarative metadata in one step. Every
subsequent revision must be hand-written explicit DDL — baselining from metadata
is a one-time privilege, not a pattern.

## Rollout

1. Run the migration job to completion.
2. Deploy workers first. They are backward compatible with in-flight workflows
   and will pick up activities as soon as they start.
3. Deploy the API.
4. Verify `/api/v1/health/ready`: it reports database, Temporal and each backend
   separately, so a degraded dependency is visible rather than inferred.

Because Temporal owns workflow state, a worker deploy mid-case is safe: the case
resumes on the new worker at the same step, and email sends are
idempotency-keyed so nothing is re-sent.

## Scaling and cost

`purchase_history` dominates row count. At a million lines the covering index on
`(tenant_id, material_code, order_date)` keeps benchmark queries in a single key
span. Run `ANALYZE` after any bulk load — the seed loader does this — because
CockroachDB otherwise plans against stale statistics and a benchmark query that
should use the index can end up scanning.

Model spend concentrates in requirement extraction and compliance location. Both
run a deterministic parser first and call the model only for the remainder, and
both cap what they send. `LLM_BACKEND=deterministic` reduces model cost to zero
at the cost of recall on unusual prose.

## SAP integration

Import is idempotent by construction: the file is content-hashed, each normalised
row is row-hashed, overlapping exports deduplicate, and superseded rows are
closed with `valid_to` rather than deleted. Re-importing the same extract is a
no-op.

Export is a draft. ProcureGuard produces `sap_payload` shaped for the standard
purchase-order API; a human releases it. Wire it to your ERP integration only
after enabling `ALLOW_AUTOMATED_PO_CREATION`, and keep `PO_RELEASE` as a required
approval even then.

## Backup and recovery

CockroachDB Cloud manages backups; verify the retention window meets your audit
requirement, which for procurement records is usually years, not days. S3 holds
the raw artifacts — enable versioning and Object Lock so a document version
cannot be altered after an evaluation cited it.

Restore drill: restore the database to a point in time, confirm that
`document_versions.content_hash` still resolves in S3, and re-run a completed
case's bid ranking from stored `normalized_offers`. The figures must reproduce
exactly; they are all `Decimal` arithmetic over stored inputs.
