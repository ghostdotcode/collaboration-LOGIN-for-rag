# ─────────────────────────────────────────────────────────────────────────
# Account / naming
# ─────────────────────────────────────────────────────────────────────────

variable "aws_region" {
  description = "AWS region for every resource in this stack. Must match S3_REGION in the app config."
  type        = string
  default     = "us-east-1"
}

variable "allowed_account_ids" {
  description = "Account IDs the provider is permitted to operate in. Empty list disables the guard."
  type        = list(string)
  default     = []
}

variable "project" {
  description = "Short project slug used as the prefix of every resource name."
  type        = string
  default     = "lms"
}

variable "environment" {
  description = "Deployment environment. Mirrors the ENVIRONMENT env var consumed by app/core/config.py."
  type        = string
  default     = "production"

  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "environment must be one of: dev, staging, production."
  }
}

variable "cost_center" {
  description = "Finance cost-allocation tag applied to every resource."
  type        = string
  default     = "HR-PLATFORM"
}

variable "additional_tags" {
  description = "Extra tags merged into local.common_tags for every resource."
  type        = map(string)
  default     = {}
}

# ─────────────────────────────────────────────────────────────────────────
# Existing EKS cluster (NOT created by this stack)
# ─────────────────────────────────────────────────────────────────────────

variable "eks_cluster_name" {
  description = "Name of the pre-existing EKS cluster. Used only for subnet discovery tags and resource naming."
  type        = string
  default     = "meritech-eks"
}

variable "eks_oidc_provider_arn" {
  description = "ARN of the cluster's IAM OIDC provider. Empty string skips IRSA role creation."
  type        = string
  default     = ""
}

variable "eks_oidc_provider_url" {
  description = "Issuer URL of the cluster's OIDC provider, with or without the https:// prefix."
  type        = string
  default     = ""
}

variable "k8s_namespace" {
  description = "Namespace the LMS workloads run in. Must match the namespace the k8s/ manifests are applied to, or IRSA will not authenticate."
  type        = string
  default     = "lms"
}

variable "k8s_service_account_name" {
  description = "ServiceAccount the API pods and Celery workers run as; bound to the IRSA role."
  type        = string
  default     = "lms-backend"
}

# ─────────────────────────────────────────────────────────────────────────
# Networking
# ─────────────────────────────────────────────────────────────────────────

variable "create_vpc" {
  description = "Create a new VPC. Set false to reuse the VPC the EKS cluster already lives in (see existing_* variables)."
  type        = bool
  default     = true
}

variable "existing_vpc_id" {
  description = "VPC to reuse when create_vpc is false."
  type        = string
  default     = ""
}

variable "existing_public_subnet_ids" {
  description = "Public subnets to reuse when create_vpc is false."
  type        = list(string)
  default     = []
}

variable "existing_private_app_subnet_ids" {
  description = "Private application/node subnets to reuse when create_vpc is false."
  type        = list(string)
  default     = []
}

variable "existing_private_data_subnet_ids" {
  description = "Private data-tier subnets (RDS/Redis/MQ) to reuse when create_vpc is false."
  type        = list(string)
  default     = []
}

variable "vpc_cidr" {
  description = "CIDR block for the new VPC. /16 leaves room for 16 x /20 subnets across 3 AZs plus growth."
  type        = string
  default     = "10.40.0.0/16"
}

variable "availability_zone_count" {
  description = "Number of AZs to spread subnets across. The spec requires 3 for RDS Multi-AZ plus a 3-node Redis and RabbitMQ cluster."
  type        = number
  default     = 3

  validation {
    condition     = var.availability_zone_count >= 2 && var.availability_zone_count <= 4
    error_message = "availability_zone_count must be between 2 and 4."
  }
}

variable "single_nat_gateway" {
  description = "Route all private egress through one NAT gateway. Cheaper, but a single AZ failure cuts egress for every pod - only for dev/staging."
  type        = bool
  default     = false
}

variable "enable_vpc_flow_logs" {
  description = "Capture VPC flow logs to CloudWatch (AuditLog/forensics requirement in spec section 6)."
  type        = bool
  default     = true
}

variable "flow_log_retention_days" {
  description = "CloudWatch retention for VPC flow logs."
  type        = number
  default     = 90
}

variable "app_container_port" {
  description = "Port uvicorn listens on inside the pod; matches containerPort in k8s/deployment.yaml."
  type        = number
  default     = 8000
}

# ─────────────────────────────────────────────────────────────────────────
# RDS PostgreSQL
# ─────────────────────────────────────────────────────────────────────────

variable "db_engine_version" {
  description = "PostgreSQL major version. Spec mandates 16; major-only lets RDS pick the newest supported minor."
  type        = string
  default     = "16"
}

variable "db_instance_class" {
  description = "RDS instance class. Graviton (m6g) is the cheapest class that still supports Multi-AZ and Performance Insights."
  type        = string
  default     = "db.m6g.large"
}

variable "db_allocated_storage" {
  description = "Initial gp3 storage in GiB."
  type        = number
  default     = 100
}

variable "db_max_allocated_storage" {
  description = "Upper bound for RDS storage autoscaling in GiB. 0 disables autoscaling."
  type        = number
  default     = 1000
}

variable "db_name" {
  description = "Initial database name. The LMS owns the `lms` schema inside it (DB_SCHEMA), sharing the database with the RAG chatbot's public schema."
  type        = string
  default     = "meritech_db"
}

variable "db_master_username" {
  description = "RDS master username. NOT the runtime role: the app connects as the least-privilege `lms_app` role created by a DBA/migration."
  type        = string
  default     = "lms_admin"
}

variable "db_backup_retention_days" {
  description = "Automated backup retention. 30 days is the spec floor for HR records."
  type        = number
  default     = 30

  validation {
    condition     = var.db_backup_retention_days >= 7
    error_message = "HR/leave data requires at least 7 days of automated backups; the spec asks for 30."
  }
}

variable "db_multi_az" {
  description = "Run a synchronous standby in a second AZ. Leave-approval writes are financially material, so a failover must not lose committed transactions."
  type        = bool
  default     = true
}

variable "db_deletion_protection" {
  description = "Block `terraform destroy` / console deletion of the database."
  type        = bool
  default     = true
}

variable "db_performance_insights_retention_days" {
  description = "Performance Insights retention. 7 is the free tier; 465/731 are billable long-term tiers."
  type        = number
  default     = 7

  validation {
    condition     = contains([7, 31, 93, 155, 186, 217, 248, 279, 310, 341, 372, 403, 434, 465, 731], var.db_performance_insights_retention_days)
    error_message = "Must be 7, 731, or a multiple of 31 up to 465."
  }
}

variable "db_monitoring_interval" {
  description = "Enhanced Monitoring granularity in seconds (0 disables). 60s is enough to see the midnight accrual storm."
  type        = number
  default     = 60
}

variable "database_url_override" {
  description = "Explicit DATABASE_URL to publish in the app secret. Set this to the least-privilege `lms_app` DSN once that role exists; null publishes the master DSN for bootstrap/migrations."
  type        = string
  default     = null
  sensitive   = true
}

# ─────────────────────────────────────────────────────────────────────────
# ElastiCache Redis
# ─────────────────────────────────────────────────────────────────────────

variable "redis_engine_version" {
  description = "ElastiCache Redis engine version."
  type        = string
  default     = "7.1"
}

variable "redis_node_type" {
  description = "Cache node type. Sized for rate-limit counters plus hierarchy caches for 500+ employees per tenant."
  type        = string
  default     = "cache.m7g.large"
}

variable "redis_num_cache_clusters" {
  description = "Primary + replica count. 3 gives two failover targets across AZs."
  type        = number
  default     = 3

  validation {
    condition     = var.redis_num_cache_clusters >= 2
    error_message = "At least 2 nodes are required for automatic failover."
  }
}

variable "redis_snapshot_retention_days" {
  description = "Daily snapshot retention. Redis holds only derived state, so this is for fast warm-restore, not durability."
  type        = number
  default     = 7
}

# ─────────────────────────────────────────────────────────────────────────
# Amazon MQ (RabbitMQ) - Celery broker
# ─────────────────────────────────────────────────────────────────────────

variable "mq_engine_version" {
  description = "RabbitMQ engine version supported by Amazon MQ."
  type        = string
  default     = "3.13"
}

variable "mq_host_instance_type" {
  description = "Amazon MQ host instance type. mq.t3.micro is single-instance only and throttles under the accrual fan-out."
  type        = string
  default     = "mq.m5.large"
}

variable "mq_deployment_mode" {
  description = "SINGLE_INSTANCE or CLUSTER_MULTI_AZ. Cluster keeps the Celery broker available while an AZ is down."
  type        = string
  default     = "CLUSTER_MULTI_AZ"

  validation {
    condition     = contains(["SINGLE_INSTANCE", "CLUSTER_MULTI_AZ"], var.mq_deployment_mode)
    error_message = "mq_deployment_mode must be SINGLE_INSTANCE or CLUSTER_MULTI_AZ."
  }
}

variable "mq_username" {
  description = "RabbitMQ application user for Celery producers and workers."
  type        = string
  default     = "lms_celery"
}

# ─────────────────────────────────────────────────────────────────────────
# S3 attachments bucket
# ─────────────────────────────────────────────────────────────────────────

variable "attachments_bucket_name" {
  description = "Explicit bucket name for leave attachments. null derives `<project>-<env>-leave-attachments-<account_id>`."
  type        = string
  default     = null
}

variable "attachments_cors_allowed_origins" {
  description = "Origins allowed to issue presigned PUT uploads. Must include APP_BASE_URL and every origin in BACKEND_CORS_ORIGINS that can upload."
  type        = list(string)
  default = [
    "https://leave.meritech.example",
    "https://chat.meritech.example",
  ]
}

variable "attachments_ia_transition_days" {
  description = "Days before an attachment moves to STANDARD_IA. Medical certificates are read during approval, then almost never again."
  type        = number
  default     = 90
}

variable "attachments_archive_transition_days" {
  description = "Days before an attachment moves to GLACIER_IR (still millisecond-retrievable for audits)."
  type        = number
  default     = 365
}

variable "attachments_noncurrent_expiry_days" {
  description = "Days a non-current object version is kept after being overwritten."
  type        = number
  default     = 90
}

variable "attachments_access_log_bucket_id" {
  description = "Existing bucket to receive S3 server access logs. null disables access logging."
  type        = string
  default     = null
}

variable "attachments_force_destroy" {
  description = "Allow Terraform to delete a non-empty attachments bucket. Never true in production - these are HR evidence documents."
  type        = bool
  default     = false
}

# ─────────────────────────────────────────────────────────────────────────
# ECR
# ─────────────────────────────────────────────────────────────────────────

variable "ecr_repository_name" {
  description = "ECR repository for the lms-backend image."
  type        = string
  default     = "lms-backend"
}

variable "ecr_image_retention_count" {
  description = "Number of most recent images to keep before expiry."
  type        = number
  default     = 30
}

variable "ecr_image_tag_mutability" {
  description = "IMMUTABLE forces deploys to reference a fixed digest, so a re-pushed `latest` cannot silently change what is running."
  type        = string
  default     = "IMMUTABLE"

  validation {
    condition     = contains(["MUTABLE", "IMMUTABLE"], var.ecr_image_tag_mutability)
    error_message = "ecr_image_tag_mutability must be MUTABLE or IMMUTABLE."
  }
}

variable "ecr_pull_principal_arns" {
  description = "Extra IAM principals (e.g. the EKS node role, a CI role) granted pull access via the repository policy."
  type        = list(string)
  default     = []
}

# ─────────────────────────────────────────────────────────────────────────
# WAF
# ─────────────────────────────────────────────────────────────────────────

variable "alb_arn" {
  description = "ARN of the ingress ALB to associate the web ACL with. null creates the ACL unassociated (useful before the ingress exists)."
  type        = string
  default     = null
}

variable "alb_security_group_id" {
  description = "Security group of the ingress ALB. When set, the app SG only accepts traffic from it instead of from the whole VPC CIDR."
  type        = string
  default     = ""
}

variable "waf_rate_limit_per_5min" {
  description = "WAFv2 rate-based rule threshold per source IP per 5 minutes. Sits well above the app's own RATE_LIMIT_PER_IP so WAF only catches floods."
  type        = number
  default     = 3000
}

variable "waf_enable_logging" {
  description = "Ship sampled WAF requests to CloudWatch Logs with auth headers redacted."
  type        = bool
  default     = true
}

variable "waf_log_retention_days" {
  description = "CloudWatch retention for WAF logs."
  type        = number
  default     = 30
}

variable "waf_count_only_rules" {
  description = "Managed rule names forced to Count instead of Block, for staged rollout of a noisy rule (e.g. SizeRestrictions_BODY)."
  type        = list(string)
  default     = []
}

# ─────────────────────────────────────────────────────────────────────────
# Application secrets (published to Secrets Manager, consumed by k8s)
# ─────────────────────────────────────────────────────────────────────────

variable "shared_jwt_secret_key" {
  description = "SECRET_KEY for the app. MUST be the same value the RAG chatbot signs its JWTs with, or SSO breaks. null generates a random 48-byte value, which is only correct for a greenfield environment."
  type        = string
  default     = null
  sensitive   = true
}

variable "smtp_host" {
  description = "SMTP_HOST for the Celery email worker."
  type        = string
  default     = ""
}

variable "smtp_user" {
  description = "SMTP_USER for the Celery email worker."
  type        = string
  default     = ""
}

variable "smtp_password" {
  description = "SMTP_PASSWORD for the Celery email worker."
  type        = string
  default     = ""
  sensitive   = true
}

variable "stripe_api_key" {
  description = "STRIPE_API_KEY for SaaS billing."
  type        = string
  default     = ""
  sensitive   = true
}

variable "stripe_webhook_secret" {
  description = "STRIPE_WEBHOOK_SECRET used to verify inbound Stripe webhooks."
  type        = string
  default     = ""
  sensitive   = true
}

variable "secret_recovery_window_days" {
  description = "Secrets Manager recovery window. 0 deletes immediately (dev only); 30 gives a month to undo a mistaken destroy."
  type        = number
  default     = 30
}

variable "kms_deletion_window_days" {
  description = "Waiting period before a scheduled KMS key deletion completes. Deleting a key makes every ciphertext under it permanently unreadable, so keep this long."
  type        = number
  default     = 30

  validation {
    condition     = var.kms_deletion_window_days >= 7 && var.kms_deletion_window_days <= 30
    error_message = "kms_deletion_window_days must be between 7 and 30."
  }
}
