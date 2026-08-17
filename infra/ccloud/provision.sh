#!/usr/bin/env bash
# Provision the CockroachDB Cloud cluster that backs ProcureGuard's memory layer.
#
# These are the exact commands used to create the deployed cluster, kept as a
# script so the control plane is reproducible rather than a sequence of console
# clicks. Requires the ccloud CLI:
#
#   brew install cockroachdb/tap/ccloud
#   ccloud auth login
#
# The cluster is BASIC on AWS so that the memory layer sits in the same cloud —
# and the same region — as the Bedrock endpoint the agent calls.

set -euo pipefail

CLUSTER="${CLUSTER:-daring-mink}"
DATABASE="${DATABASE:-procureguard}"
CLOUD="${CLOUD:-AWS}"
REGION="${REGION:-us-east-1}"

echo "==> Authenticated as:"
ccloud auth whoami

# Create the cluster. Idempotent by intent: if it already exists, ccloud errors
# and the script stops rather than silently provisioning a second one.
if ! ccloud cluster list -q | awk '{print $1}' | grep -qx "$CLUSTER"; then
  echo "==> Creating BASIC cluster '$CLUSTER' on $CLOUD/$REGION"
  ccloud cluster create BASIC "$CLUSTER" --cloud "$CLOUD" "$REGION" --wait
else
  echo "==> Cluster '$CLUSTER' already exists, skipping create"
fi

echo "==> Creating database '$DATABASE'"
ccloud cluster database create "$CLUSTER" "$DATABASE" || \
  echo "    (already exists)"

echo "==> Cluster details"
ccloud cluster info "$CLUSTER"

echo "==> Connection string"
ccloud cluster connection-string "$CLUSTER"

# The CA certificate is required for sslmode=verify-full. Downloaded once per
# machine; the cluster id comes from `ccloud cluster list`.
CLUSTER_ID="$(ccloud cluster list -q | awk -v c="$CLUSTER" '$1==c {print $2}')"
echo "==> Downloading CA cert for cluster $CLUSTER_ID"
curl -sS --create-dirs -o "$HOME/.postgresql/root.crt" \
  "https://cockroachlabs.cloud/clusters/${CLUSTER_ID}/cert"

cat <<'NEXT'

Next steps
----------
1. Put the connection string in .env, with two edits the console output does not
   make for you:

     DATABASE_URL=cockroachdb+psycopg://USER:PASSWORD@HOST:26257/procureguard?sslmode=verify-full

   - the scheme must be cockroachdb+psycopg, not postgresql: the stock
     PostgreSQL dialect cannot parse CockroachDB's version() string
   - the database is procureguard, not defaultdb

2. Apply the schema and confirm the cluster serves native vectors:

     make migrate
     procureguard db check      # expect {"vector_backend": "native"}

3. Seed. Over a network connection to a BASIC cluster, prefer a smaller scale:

     make seed SCALE=small

NEXT
