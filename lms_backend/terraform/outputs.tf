# ─────────────────────────────────────────────────────────────────────────
# Networking
# ─────────────────────────────────────────────────────────────────────────

output "vpc_id" {
  description = "VPC the LMS data plane runs in."
  value       = module.network.vpc_id
}

output "public_subnet_ids" {
  description = "Public subnets for the internet-facing ALB."
  value       = module.network.public_subnet_ids
}

output "private_app_subnet_ids" {
  description = "Private subnets for EKS node groups running the LMS."
  value       = module.network.private_app_subnet_ids
}

output "private_data_subnet_ids" {
  description = "Private subnets holding RDS, ElastiCache and Amazon MQ."
  value       = module.network.private_data_subnet_ids
}

output "app_security_group_id" {
  description = "Attach this SG to the EKS nodes/pods; it is the only source the data stores accept."
  value       = module.network.app_security_group_id
}

# ─────────────────────────────────────────────────────────────────────────
# Data stores
# ─────────────────────────────────────────────────────────────────────────

output "rds_endpoint" {
  description = "PostgreSQL endpoint (host:port)."
  value       = module.database.endpoint
}

output "rds_database_name" {
  description = "Database name. The LMS owns the `lms` schema inside it (DB_SCHEMA)."
  value       = module.database.db_name
}

output "rds_master_secret_arn" {
  description = "Secrets Manager ARN of the RDS master credentials."
  value       = module.database.secret_arn
}

output "redis_primary_endpoint" {
  description = "Redis write endpoint."
  value       = module.cache.primary_endpoint_address
}

output "redis_reader_endpoint" {
  description = "Redis read-only endpoint."
  value       = module.cache.reader_endpoint_address
}

output "redis_secret_arn" {
  description = "Secrets Manager ARN of the Redis auth token."
  value       = module.cache.secret_arn
}

output "mq_amqps_endpoint" {
  description = "RabbitMQ AMQPS endpoint used by Celery."
  value       = module.broker.amqps_endpoint
}

output "mq_console_url" {
  description = "RabbitMQ management console URL (VPC-internal only)."
  value       = module.broker.console_url
}

output "mq_secret_arn" {
  description = "Secrets Manager ARN of the RabbitMQ credentials."
  value       = module.broker.secret_arn
}

# ─────────────────────────────────────────────────────────────────────────
# Storage, registry, edge, identity
# ─────────────────────────────────────────────────────────────────────────

output "attachments_bucket_name" {
  description = "Value for the S3_BUCKET env var."
  value       = module.storage.bucket_id
}

output "attachments_bucket_arn" {
  description = "ARN of the attachments bucket."
  value       = module.storage.bucket_arn
}

output "ecr_repository_url" {
  description = "Push/pull URL for the lms-backend image."
  value       = aws_ecr_repository.backend.repository_url
}

output "waf_web_acl_arn" {
  description = "ARN of the regional web ACL."
  value       = module.security.web_acl_arn
}

output "irsa_role_arn" {
  description = "Role ARN for the ServiceAccount annotation; null until the OIDC provider variables are set."
  value       = module.security.irsa_role_arn
}

output "service_account_annotations" {
  description = "Annotations to add to the LMS ServiceAccount so pods receive AWS credentials."
  value = {
    "eks.amazonaws.com/role-arn" = module.security.irsa_role_arn
  }
}

output "kms_key_arns" {
  description = "Customer-managed keys created by this stack, by purpose."
  value = {
    rds     = module.kms_rds.key_arn
    redis   = module.kms_redis.key_arn
    s3      = module.kms_s3.key_arn
    secrets = module.kms_secrets.key_arn
    mq      = module.kms_mq.key_arn
  }
}

# ─────────────────────────────────────────────────────────────────────────
# Kubernetes wiring
# ─────────────────────────────────────────────────────────────────────────

output "app_secret_arn" {
  description = "Secrets Manager ARN of the JSON document that backs the lms-backend-secrets Secret."
  value       = aws_secretsmanager_secret.app.arn
}

output "app_secret_name" {
  description = "Secrets Manager path of the app secret; use this as remoteRef.key in an ExternalSecret."
  value       = aws_secretsmanager_secret.app.name
}

# No plaintext credential is exported. Every value below is either public
# infrastructure metadata or a pointer into Secrets Manager; the secrets
# themselves are read at runtime by the External Secrets Operator using the
# IRSA role, which keeps them out of CI logs and out of `terraform output`.
output "k8s_secret_key_sources" {
  description = "Each key of the lms-backend-secrets Secret and the Secrets Manager JSON property it comes from."
  value = {
    SECRET_KEY            = "${aws_secretsmanager_secret.app.name}:SECRET_KEY"
    DATABASE_URL          = "${aws_secretsmanager_secret.app.name}:DATABASE_URL"
    REDIS_URL             = "${aws_secretsmanager_secret.app.name}:REDIS_URL"
    CELERY_BROKER_URL     = "${aws_secretsmanager_secret.app.name}:CELERY_BROKER_URL"
    CELERY_RESULT_BACKEND = "${aws_secretsmanager_secret.app.name}:CELERY_RESULT_BACKEND"
    SMTP_HOST             = "${aws_secretsmanager_secret.app.name}:SMTP_HOST"
    SMTP_USER             = "${aws_secretsmanager_secret.app.name}:SMTP_USER"
    SMTP_PASSWORD         = "${aws_secretsmanager_secret.app.name}:SMTP_PASSWORD"
    STRIPE_API_KEY        = "${aws_secretsmanager_secret.app.name}:STRIPE_API_KEY"
    STRIPE_WEBHOOK_SECRET = "${aws_secretsmanager_secret.app.name}:STRIPE_WEBHOOK_SECRET"
  }
}

# S3_BUCKET and S3_REGION are not secrets, so they belong in
# k8s/configmap.yaml rather than the Secret. Neither key is present in that
# file yet - attachments stay disabled until they are added.
output "k8s_configmap_additions" {
  description = "Non-secret keys to add to the lms-backend-config ConfigMap."
  value = {
    S3_BUCKET = module.storage.bucket_id
    S3_REGION = module.storage.bucket_region
  }
}
