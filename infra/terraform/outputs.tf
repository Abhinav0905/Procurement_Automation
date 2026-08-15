output "alb_dns_name" {
  description = "Point your internal DNS record at this"
  value       = aws_lb.main.dns_name
}

output "documents_bucket" {
  value = aws_s3_bucket.documents.id
}

output "inbound_email_bucket" {
  value = aws_s3_bucket.inbound_email.id
}

output "kms_key_arn" {
  description = "Key protecting documents and sealed commercial bids"
  value       = aws_kms_key.main.arn
}

output "app_secret_arn" {
  description = "Populate with database_url, session_secret, temporal_api_key and the OIDC keys"
  value       = aws_secretsmanager_secret.app.arn
}

output "inbound_email_topic_arn" {
  description = "Subscribe the API's /api/v1/mail/inbound webhook to this"
  value       = aws_sns_topic.inbound_email.arn
}

output "ecs_cluster" {
  value = aws_ecs_cluster.main.name
}

output "task_role_arn" {
  description = "Grant CockroachDB Cloud IAM access to this role if using IAM auth"
  value       = aws_iam_role.task.arn
}
