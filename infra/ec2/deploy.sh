#!/usr/bin/env bash
# Deploy the ProcureGuard demo box: one t4g.micro running the API and approval UI
# against the CockroachDB Cloud cluster.
#
# Deliberately minimal — no ALB, no NAT gateway, no Fargate task definitions. A
# public demo for a handful of visitors does not need $99/month of plumbing. The
# production topology lives in infra/terraform and is intentionally not applied.
#
# The instance carries NO AWS credentials: it runs LLM_BACKEND=deterministic, so
# the only secret it needs is the database URL. Bedrock is exercised from the
# development environment, where the IAM key already lives.
#
# Usage:  ./infra/ec2/deploy.sh
#         ./infra/ec2/deploy.sh --terminate

set -euo pipefail

REGION="${REGION:-us-east-1}"
# t4g.small, not micro: the host runs Temporal, its Postgres, the API and a
# worker, so the demo exercises the real durable-orchestration path rather than
# a simulated one. 1 GiB cannot hold that set.
INSTANCE_TYPE="${INSTANCE_TYPE:-t4g.small}"
NAME="${NAME:-procureguard-demo}"
SG_NAME="${SG_NAME:-procureguard-demo-sg}"
CLUSTER_ID="${CLUSTER_ID:-f369276d-9167-41dd-9833-10d7ba7e3fe0}"
REPO="${REPO:-https://github.com/Abhinav0905/Procurement_Automation.git}"

cd "$(dirname "$0")/../.."

# ── teardown ────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--terminate" ]]; then
  ids=$(aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text)
  if [[ -n "$ids" ]]; then
    echo "==> Terminating: $ids"
    aws ec2 terminate-instances --region "$REGION" --instance-ids $ids >/dev/null
    echo "    Billing stops when the instance reaches 'terminated'."
  else
    echo "==> No instance tagged $NAME found."
  fi
  exit 0
fi

# ── inputs ──────────────────────────────────────────────────────────────────
DATABASE_URL="$(grep -E '^DATABASE_URL=' .env | tail -1 | cut -d= -f2-)"
if [[ -z "$DATABASE_URL" || "$DATABASE_URL" == *"localhost"* ]]; then
  echo "ERROR: .env DATABASE_URL must point at the CockroachDB Cloud cluster." >&2
  exit 1
fi

echo "==> Region $REGION, instance $INSTANCE_TYPE"

# Latest Amazon Linux 2023 for arm64, resolved rather than hardcoded.
AMI="$(aws ssm get-parameter --region "$REGION" \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
  --query 'Parameter.Value' --output text)"
echo "    AMI: $AMI"

VPC="$(aws ec2 describe-vpcs --region "$REGION" --filters Name=is-default,Values=true \
  --query 'Vpcs[0].VpcId' --output text)"
SUBNET="$(aws ec2 describe-subnets --region "$REGION" --filters "Name=vpc-id,Values=$VPC" \
  --query 'Subnets[0].SubnetId' --output text)"
echo "    VPC: $VPC   Subnet: $SUBNET"

# ── security group: port 80 only, no SSH ────────────────────────────────────
# No inbound SSH: the box is configured entirely by user-data, so there is no
# administrative surface to protect.
SG="$(aws ec2 describe-security-groups --region "$REGION" \
  --filters "Name=group-name,Values=$SG_NAME" "Name=vpc-id,Values=$VPC" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")"

if [[ "$SG" == "None" || -z "$SG" ]]; then
  SG="$(aws ec2 create-security-group --region "$REGION" \
    --group-name "$SG_NAME" --description "ProcureGuard demo, HTTP only" \
    --vpc-id "$VPC" --query 'GroupId' --output text)"
  echo "    Created SG $SG"
else
  echo "    Reusing SG $SG"
fi

# 80 for the app, 8088 for the Temporal UI so workflow executions are visible.
# Both idempotent: an existing rule returns Duplicate and is not an error here.
for port in 80 8088; do
  aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SG" \
    --protocol tcp --port "$port" --cidr 0.0.0.0/0 >/dev/null 2>&1 \
    && echo "    Opened tcp/$port" || echo "    tcp/$port already open"
done

# ── user-data ───────────────────────────────────────────────────────────────
# Swap first: the Docker build compiles wheels and 1 GiB alone is marginal.
USERDATA="$(cat <<EOF
#!/bin/bash
set -eux
exec > >(tee /var/log/procureguard-boot.log) 2>&1

dd if=/dev/zero of=/swapfile bs=1M count=2048
chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile

dnf install -y docker git
systemctl enable --now docker

# CA certificate for sslmode=verify-full against CockroachDB Cloud.
mkdir -p /opt/certs
curl -sS -o /opt/certs/root.crt "https://cockroachlabs.cloud/clusters/${CLUSTER_ID}/cert"

dnf install -y docker-compose-plugin || true

git clone --depth 1 "${REPO}" /opt/app

# Temporal, its Postgres, the API and a worker. The database stays remote — this
# host holds workflow state only.
cd /opt/app/infra/ec2
DATABASE_URL='${DATABASE_URL}' docker compose -f docker-compose.demo.yml up -d --build
EOF
)"

# ── launch ──────────────────────────────────────────────────────────────────
ID="$(aws ec2 run-instances --region "$REGION" \
  --image-id "$AMI" --instance-type "$INSTANCE_TYPE" \
  --subnet-id "$SUBNET" --security-group-ids "$SG" \
  --associate-public-ip-address \
  --user-data "$USERDATA" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME},{Key=Project,Value=procureguard}]" \
  --metadata-options "HttpTokens=required" \
  --query 'Instances[0].InstanceId' --output text)"
echo "==> Launched $ID"

aws ec2 wait instance-running --region "$REGION" --instance-ids "$ID"
IP="$(aws ec2 describe-instances --region "$REGION" --instance-ids "$ID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)"
DNS="$(aws ec2 describe-instances --region "$REGION" --instance-ids "$ID" \
  --query 'Reservations[0].Instances[0].PublicDnsName' --output text)"

cat <<DONE

==> Instance running: $ID
    Demo URL:     http://$DNS
    Temporal UI:  http://$DNS:8088
    IP:           $IP

Four containers build and start on first boot (Temporal, Postgres, API, worker),
so allow roughly ten minutes. Poll until the API answers:

    until curl -sf "http://$DNS/api/v1/health"; do sleep 20; done

Then confirm Temporal is connected, not merely running:

    curl -s "http://$DNS/api/v1/health/ready" | grep -o '"temporal":{[^}]*}'

Teardown when judging is over (billing stops at 'terminated'):

    ./infra/ec2/deploy.sh --terminate

DONE
