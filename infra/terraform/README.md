# Infrastructure skeleton

Production IaC should provision or connect: VPC/private subnets, ECS/EKS API, Temporal workers, S3, Lambda/EventBridge ingestion trigger, Secrets Manager, KMS, logging/metrics and least-privilege IAM. CockroachDB Cloud and Temporal Cloud may be external managed services.
