variable "project" {
  type    = string
  default = "procureguard"
}

variable "environment" {
  type        = string
  description = "dev, staging or prod"
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be dev, staging or prod."
  }
}

variable "region" {
  type    = string
  default = "us-west-2"
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "availability_zones" {
  type    = list(string)
  default = ["us-west-2a", "us-west-2b", "us-west-2c"]
}

variable "single_nat_gateway" {
  type        = bool
  description = "One NAT gateway instead of one per AZ. Cheaper, less resilient."
  default     = false
}

variable "image" {
  type        = string
  description = "Container image URI, immutably tagged (never :latest in prod)"
}

variable "certificate_arn" {
  type        = string
  description = "ACM certificate for the ALB HTTPS listener"
}

variable "ingress_cidrs" {
  type        = list(string)
  description = "Who may reach the ALB. The approval UI is internal-facing."
  default     = ["10.0.0.0/8"]
}

variable "api_cpu" {
  type    = number
  default = 1024
}

variable "api_memory" {
  type    = number
  default = 2048
}

variable "api_desired_count" {
  type    = number
  default = 2
}

variable "worker_cpu" {
  type    = number
  default = 2048
}

variable "worker_memory" {
  type    = number
  default = 4096
}

variable "worker_desired_count" {
  type    = number
  default = 2
}

variable "temporal_address" {
  type        = string
  description = "Temporal Cloud endpoint, for example your-ns.tmprl.cloud:7233"
}

variable "temporal_namespace" {
  type = string
}

variable "bedrock_model_arns" {
  type        = list(string)
  description = "Exact model ARNs the task may invoke. No wildcards."
}

variable "email_from_address" {
  type        = string
  description = "Verified SES sender for supplier correspondence"
}

variable "email_reply_to_domain" {
  type        = string
  description = "Subdomain receiving supplier replies, for example rfq.example.com"
}

variable "allow_automated_email_send" {
  type        = bool
  description = "Leave false until the release-by-human process has been observed"
  default     = false
}

variable "document_retention_days" {
  type        = number
  description = "S3 Object Lock retention. Procurement records are kept years, not days."
  default     = 2555
}

variable "log_retention_days" {
  type    = number
  default = 365
}
