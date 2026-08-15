# ProcureGuard AWS infrastructure.
#
# Provisions the AWS side: networking, ECS services for the API and the Temporal
# worker, S3 for documents and inbound email, KMS for sealed bids, Secrets
# Manager, SES, and least-privilege task roles.
#
# CockroachDB Cloud and Temporal Cloud are managed externally; their connection
# details arrive through Secrets Manager. That boundary is deliberate - this
# module never holds a database credential in state.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }
}

provider "aws" {
  region = var.region
}

locals {
  name = "${var.project}-${var.environment}"
  tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
    Component   = "procureguard"
  }
}

data "aws_caller_identity" "current" {}

# ── networking ───────────────────────────────────────────────────────────────
# Tasks run in private subnets. Egress to CockroachDB Cloud, Temporal Cloud and
# Bedrock leaves through NAT; S3 and KMS use VPC endpoints so that traffic never
# reaches the public internet.

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(local.tags, { Name = local.name })
}

resource "aws_subnet" "public" {
  count                   = length(var.availability_zones)
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 4, count.index)
  availability_zone       = var.availability_zones[count.index]
  map_public_ip_on_launch = true
  tags                    = merge(local.tags, { Name = "${local.name}-public-${count.index}" })
}

resource "aws_subnet" "private" {
  count             = length(var.availability_zones)
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index + length(var.availability_zones))
  availability_zone = var.availability_zones[count.index]
  tags              = merge(local.tags, { Name = "${local.name}-private-${count.index}" })
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = local.tags
}

resource "aws_eip" "nat" {
  count  = var.single_nat_gateway ? 1 : length(var.availability_zones)
  domain = "vpc"
  tags   = local.tags
}

resource "aws_nat_gateway" "main" {
  count         = var.single_nat_gateway ? 1 : length(var.availability_zones)
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id
  depends_on    = [aws_internet_gateway.main]
  tags          = local.tags
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = local.tags
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  count  = length(var.availability_zones)
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main[var.single_nat_gateway ? 0 : count.index].id
  }
  tags = local.tags
}

resource "aws_route_table_association" "private" {
  count          = length(aws_subnet.private)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.private[*].id
  tags              = local.tags
}

# ── storage ──────────────────────────────────────────────────────────────────
# Versioning plus Object Lock: a document version cited by an evaluation must not
# be alterable afterwards.

resource "aws_kms_key" "main" {
  description             = "${local.name} documents and sealed commercial bids"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = local.tags
}

resource "aws_kms_alias" "main" {
  name          = "alias/${local.name}"
  target_key_id = aws_kms_key.main.key_id
}

resource "aws_s3_bucket" "documents" {
  bucket              = "${local.name}-documents-${data.aws_caller_identity.current.account_id}"
  object_lock_enabled = true
  tags                = local.tags
}

resource "aws_s3_bucket_versioning" "documents" {
  bucket = aws_s3_bucket.documents.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id
  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.main.arn
      sse_algorithm     = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "documents" {
  bucket                  = aws_s3_bucket.documents.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_object_lock_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id
  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = var.document_retention_days
    }
  }
}

resource "aws_s3_bucket" "inbound_email" {
  bucket = "${local.name}-inbound-email-${data.aws_caller_identity.current.account_id}"
  tags   = local.tags
}

resource "aws_s3_bucket_public_access_block" "inbound_email" {
  bucket                  = aws_s3_bucket.inbound_email.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# SES must be able to deposit raw inbound mail.
resource "aws_s3_bucket_policy" "inbound_email" {
  bucket = aws_s3_bucket.inbound_email.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowSESPuts"
      Effect    = "Allow"
      Principal = { Service = "ses.amazonaws.com" }
      Action    = "s3:PutObject"
      Resource  = "${aws_s3_bucket.inbound_email.arn}/*"
      Condition = {
        StringEquals = { "aws:Referer" = data.aws_caller_identity.current.account_id }
      }
    }]
  })
}

# ── secrets ──────────────────────────────────────────────────────────────────

resource "aws_secretsmanager_secret" "app" {
  name        = "${local.name}/app"
  description = "CockroachDB URL, Temporal credentials and session secret"
  kms_key_id  = aws_kms_key.main.arn
  tags        = local.tags
}

# ── task roles ───────────────────────────────────────────────────────────────

data "aws_iam_policy_document" "task_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task_execution" {
  name               = "${local.name}-task-execution"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "task_execution" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "task_execution_secrets" {
  role = aws_iam_role.task_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue", "kms:Decrypt"]
      Resource = [aws_secretsmanager_secret.app.arn, aws_kms_key.main.arn]
    }]
  })
}

resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
  tags               = local.tags
}

# Deliberately narrow. No s3:DeleteObject on documents: evidence is immutable,
# and no wildcard Bedrock access.
resource "aws_iam_role_policy" "task" {
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DocumentsReadWriteNoDelete"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.documents.arn, "${aws_s3_bucket.documents.arn}/*"]
      },
      {
        Sid      = "InboundEmailRead"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.inbound_email.arn, "${aws_s3_bucket.inbound_email.arn}/*"]
      },
      {
        Sid      = "SealedBidEnvelopeKeys"
        Effect   = "Allow"
        Action   = ["kms:GenerateDataKey", "kms:Decrypt", "kms:DescribeKey"]
        Resource = [aws_kms_key.main.arn]
      },
      {
        Sid      = "BedrockInference"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:Converse"]
        Resource = var.bedrock_model_arns
      },
      {
        Sid      = "SendSupplierEmail"
        Effect   = "Allow"
        Action   = ["ses:SendEmail", "ses:SendRawEmail"]
        Resource = "*"
        Condition = {
          StringEquals = { "ses:FromAddress" = var.email_from_address }
        }
      },
      {
        Sid      = "Secrets"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [aws_secretsmanager_secret.app.arn]
      }
    ]
  })
}

# ── compute ──────────────────────────────────────────────────────────────────

resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
  tags = local.tags
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
  tags              = local.tags
}

resource "aws_security_group" "alb" {
  name   = "${local.name}-alb"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.ingress_cidrs
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = local.tags
}

resource "aws_security_group" "tasks" {
  name   = "${local.name}-tasks"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = local.tags
}

resource "aws_lb" "main" {
  name                       = local.name
  load_balancer_type         = "application"
  subnets                    = aws_subnet.public[*].id
  security_groups            = [aws_security_group.alb.id]
  drop_invalid_header_fields = true
  enable_deletion_protection = var.environment == "prod"
  tags                       = local.tags
}

resource "aws_lb_target_group" "api" {
  name        = "${local.name}-api"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"
  health_check {
    path                = "/api/v1/health"
    matcher             = "200"
    interval            = 15
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
  tags = local.tags
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

locals {
  # Both services run the same image; only the command differs.
  common_env = [
    { name = "APP_ENV", value = var.environment },
    { name = "AWS_REGION", value = var.region },
    { name = "AUTH_MODE", value = "oidc" },
    { name = "OBJECT_STORE_BACKEND", value = "s3" },
    { name = "ENCRYPTION_BACKEND", value = "kms" },
    { name = "S3_BUCKET", value = aws_s3_bucket.documents.id },
    { name = "S3_INBOUND_EMAIL_BUCKET", value = aws_s3_bucket.inbound_email.id },
    { name = "S3_KMS_KEY_ID", value = aws_kms_key.main.arn },
    { name = "KMS_KEY_ID", value = aws_kms_key.main.arn },
    { name = "LLM_BACKEND", value = "bedrock" },
    { name = "EMBEDDING_BACKEND", value = "bedrock" },
    { name = "EMAIL_BACKEND", value = "ses" },
    { name = "EMAIL_FROM_ADDRESS", value = var.email_from_address },
    { name = "EMAIL_REPLY_TO_DOMAIN", value = var.email_reply_to_domain },
    { name = "TEMPORAL_NAMESPACE", value = var.temporal_namespace },
    { name = "TEMPORAL_ADDRESS", value = var.temporal_address },
    { name = "OTEL_ENABLED", value = "true" },
    # Safety switches stay off until the process has been observed.
    { name = "ALLOW_AUTOMATED_EMAIL_SEND", value = tostring(var.allow_automated_email_send) },
    { name = "ALLOW_AUTOMATED_PO_CREATION", value = "false" },
  ]

  common_secrets = [
    { name = "DATABASE_URL", valueFrom = "${aws_secretsmanager_secret.app.arn}:database_url::" },
    { name = "SESSION_SECRET", valueFrom = "${aws_secretsmanager_secret.app.arn}:session_secret::" },
    { name = "TEMPORAL_API_KEY", valueFrom = "${aws_secretsmanager_secret.app.arn}:temporal_api_key::" },
    { name = "OIDC_ISSUER", valueFrom = "${aws_secretsmanager_secret.app.arn}:oidc_issuer::" },
    { name = "OIDC_AUDIENCE", valueFrom = "${aws_secretsmanager_secret.app.arn}:oidc_audience::" },
    { name = "OIDC_JWKS_URL", valueFrom = "${aws_secretsmanager_secret.app.arn}:oidc_jwks_url::" },
  ]
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name      = "api"
    image     = var.image
    essential = true
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]
    command   = ["uvicorn", "procureguard.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
    environment = local.common_env
    secrets     = local.common_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.app.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "api"
      }
    }
  }])
  tags = local.tags
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name        = "worker"
    image       = var.image
    essential   = true
    command     = ["python", "-m", "procureguard.workflows.worker"]
    environment = local.common_env
    secrets     = local.common_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.app.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "worker"
      }
    }
  }])
  tags = local.tags
}

resource "aws_ecs_service" "api" {
  name            = "${local.name}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.tasks.id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [aws_lb_listener.https]
  tags       = local.tags
}

resource "aws_ecs_service" "worker" {
  name            = "${local.name}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.tasks.id]
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  tags = local.tags
}

# ── inbound email ────────────────────────────────────────────────────────────
# SES receipt rule writes raw MIME to S3 and notifies; the API webhook parses it.
# The raw object is retained so a disputed quotation can be re-parsed from the
# original bytes.

resource "aws_sns_topic" "inbound_email" {
  name              = "${local.name}-inbound-email"
  kms_master_key_id = aws_kms_key.main.id
  tags              = local.tags
}

resource "aws_ses_receipt_rule_set" "main" {
  rule_set_name = local.name
}

resource "aws_ses_receipt_rule" "quotations" {
  name          = "${local.name}-quotations"
  rule_set_name = aws_ses_receipt_rule_set.main.rule_set_name
  recipients    = [var.email_reply_to_domain]
  enabled       = true
  scan_enabled  = true

  s3_action {
    bucket_name       = aws_s3_bucket.inbound_email.id
    object_key_prefix = "inbound/"
    topic_arn         = aws_sns_topic.inbound_email.arn
    position          = 1
  }

  depends_on = [aws_s3_bucket_policy.inbound_email]
}
