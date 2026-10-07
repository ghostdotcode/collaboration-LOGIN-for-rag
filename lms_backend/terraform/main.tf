data "aws_caller_identity" "current" {}

# Advisory rather than a hard variable validation: reusing the cluster's VPC
# is a valid mode, but it needs the subnet IDs supplied as well.
check "vpc_reuse_inputs" {
  assert {
    condition = var.create_vpc || (
      var.existing_vpc_id != "" &&
      length(var.existing_private_app_subnet_ids) >= 2 &&
      length(var.existing_private_data_subnet_ids) >= 2
    )
    error_message = "create_vpc = false requires existing_vpc_id plus at least two app and two data subnet IDs (Multi-AZ RDS and a Redis replication group both need two AZs)."
  }
}

# ─────────────────────────────────────────────────────────────────────────
# KMS
#
# One customer-managed key per data store instead of the shared AWS-managed
# `aws/rds`-style keys. Three reasons that matter here:
#   1. AWS-managed keys cannot be rotated on demand, cannot carry a key
#      policy, and cannot be audited per workload - all three are required
#      to evidence "PII encrypted at rest with AES-256" (spec section 6).
#   2. Separate keys give per-store blast radius: revoking the attachments
#      key does not lock anyone out of the database.
#   3. Only a CMK can be shared with another account later (e.g. a
#      cross-account backup vault for HR retention).
# ─────────────────────────────────────────────────────────────────────────

module "kms_rds" {
  source = "./modules/kms"

  alias                = "${local.name_prefix}-rds"
  description          = "LMS PostgreSQL storage, snapshots and Performance Insights encryption"
  deletion_window_days = var.kms_deletion_window_days
  service_principals   = ["rds.amazonaws.com", "monitoring.rds.amazonaws.com"]
  tags                 = local.common_tags
}

module "kms_redis" {
  source = "./modules/kms"

  alias                = "${local.name_prefix}-redis"
  description          = "LMS ElastiCache Redis at-rest encryption"
  deletion_window_days = var.kms_deletion_window_days
  service_principals   = ["elasticache.amazonaws.com"]
  tags                 = local.common_tags
}

module "kms_s3" {
  source = "./modules/kms"

  alias                = "${local.name_prefix}-s3-attachments"
  description          = "LMS leave attachment (medical certificate) encryption"
  deletion_window_days = var.kms_deletion_window_days
  service_principals   = ["s3.amazonaws.com"]
  tags                 = local.common_tags
}

module "kms_secrets" {
  source = "./modules/kms"

  alias                = "${local.name_prefix}-secrets"
  description          = "LMS Secrets Manager envelope encryption (DB, Redis, broker, app secrets)"
  deletion_window_days = var.kms_deletion_window_days
  service_principals   = ["secretsmanager.amazonaws.com"]
  tags                 = local.common_tags
}

module "kms_mq" {
  source = "./modules/kms"

  alias                = "${local.name_prefix}-mq"
  description          = "LMS Amazon MQ (RabbitMQ) at-rest encryption"
  deletion_window_days = var.kms_deletion_window_days
  service_principals   = ["mq.amazonaws.com"]
  tags                 = local.common_tags
}

# ─────────────────────────────────────────────────────────────────────────
# Networking
# ─────────────────────────────────────────────────────────────────────────

module "network" {
  source = "./modules/network"

  name_prefix             = local.name_prefix
  create_vpc              = var.create_vpc
  existing_vpc_id         = var.existing_vpc_id
  existing_public_subnets = var.existing_public_subnet_ids
  existing_app_subnets    = var.existing_private_app_subnet_ids
  existing_data_subnets   = var.existing_private_data_subnet_ids

  vpc_cidr                = var.vpc_cidr
  availability_zone_count = var.availability_zone_count
  single_nat_gateway      = var.single_nat_gateway
  enable_flow_logs        = var.enable_vpc_flow_logs
  flow_log_retention_days = var.flow_log_retention_days

  eks_cluster_name      = var.eks_cluster_name
  app_container_port    = var.app_container_port
  alb_security_group_id = var.alb_security_group_id

  tags = local.common_tags
}

# ─────────────────────────────────────────────────────────────────────────
# Data stores
# ─────────────────────────────────────────────────────────────────────────

module "database" {
  source = "./modules/database"

  name_prefix        = local.name_prefix
  subnet_ids         = module.network.private_data_subnet_ids
  security_group_ids = [module.network.rds_security_group_id]

  engine_version            = var.db_engine_version
  instance_class            = var.db_instance_class
  allocated_storage         = var.db_allocated_storage
  max_allocated_storage     = var.db_max_allocated_storage
  db_name                   = var.db_name
  master_username           = var.db_master_username
  multi_az                  = var.db_multi_az
  backup_retention_days     = var.db_backup_retention_days
  deletion_protection       = var.db_deletion_protection
  monitoring_interval       = var.db_monitoring_interval
  performance_insights_days = var.db_performance_insights_retention_days

  kms_key_arn                 = module.kms_rds.key_arn
  secrets_kms_key_arn         = module.kms_secrets.key_arn
  secret_name                 = "${local.secret_prefix}/rds/master"
  secret_recovery_window_days = var.secret_recovery_window_days

  tags = local.common_tags
}

module "cache" {
  source = "./modules/cache"

  name_prefix        = local.name_prefix
  subnet_ids         = module.network.private_data_subnet_ids
  security_group_ids = [module.network.redis_security_group_id]

  engine_version           = var.redis_engine_version
  node_type                = var.redis_node_type
  num_cache_clusters       = var.redis_num_cache_clusters
  snapshot_retention_limit = var.redis_snapshot_retention_days

  kms_key_arn                 = module.kms_redis.key_arn
  secrets_kms_key_arn         = module.kms_secrets.key_arn
  secret_name                 = "${local.secret_prefix}/redis/auth-token"
  secret_recovery_window_days = var.secret_recovery_window_days

  tags = local.common_tags
}

module "broker" {
  source = "./modules/broker"

  name_prefix        = local.name_prefix
  subnet_ids         = module.network.private_data_subnet_ids
  security_group_ids = [module.network.mq_security_group_id]

  engine_version     = var.mq_engine_version
  host_instance_type = var.mq_host_instance_type
  deployment_mode    = var.mq_deployment_mode
  username           = var.mq_username

  kms_key_arn                 = module.kms_mq.key_arn
  secrets_kms_key_arn         = module.kms_secrets.key_arn
  secret_name                 = "${local.secret_prefix}/rabbitmq/app-user"
  secret_recovery_window_days = var.secret_recovery_window_days

  tags = local.common_tags
}

module "storage" {
  source = "./modules/storage"

  bucket_name          = local.attachments_bucket_name
  kms_key_arn          = module.kms_s3.key_arn
  cors_allowed_origins = var.attachments_cors_allowed_origins

  ia_transition_days      = var.attachments_ia_transition_days
  archive_transition_days = var.attachments_archive_transition_days
  noncurrent_expiry_days  = var.attachments_noncurrent_expiry_days
  access_log_bucket_id    = var.attachments_access_log_bucket_id
  force_destroy           = var.attachments_force_destroy

  tags = local.common_tags
}

# ─────────────────────────────────────────────────────────────────────────
# Edge protection + workload identity
# ─────────────────────────────────────────────────────────────────────────

module "security" {
  source = "./modules/security"

  name_prefix = local.name_prefix

  # WAF
  alb_arn                = var.alb_arn
  rate_limit_per_5min    = var.waf_rate_limit_per_5min
  enable_waf_logging     = var.waf_enable_logging
  waf_log_retention_days = var.waf_log_retention_days
  count_only_rules       = var.waf_count_only_rules

  # IRSA
  enable_irsa              = local.enable_irsa
  oidc_provider_arn        = var.eks_oidc_provider_arn
  oidc_provider_url        = var.eks_oidc_provider_url
  k8s_namespace            = var.k8s_namespace
  k8s_service_account_name = var.k8s_service_account_name

  attachments_bucket_arn  = module.storage.bucket_arn
  attachments_kms_key_arn = module.kms_s3.key_arn
  secrets_kms_key_arn     = module.kms_secrets.key_arn

  app_secret_arns = [
    aws_secretsmanager_secret.app.arn,
    module.database.secret_arn,
    module.cache.secret_arn,
    module.broker.secret_arn,
  ]

  tags = local.common_tags
}

# ─────────────────────────────────────────────────────────────────────────
# Container registry
# ─────────────────────────────────────────────────────────────────────────

resource "aws_ecr_repository" "backend" {
  name                 = var.ecr_repository_name
  image_tag_mutability = var.ecr_image_tag_mutability
  force_delete         = false

  image_scanning_configuration {
    # Basic scanning on push. Enhanced (Inspector) scanning is an account-level
    # registry setting, deliberately not managed here so this stack stays
    # scoped to the LMS.
    scan_on_push = true
  }

  encryption_configuration {
    # Image layers carry application code, not customer PII, so the
    # S3-managed key is sufficient and avoids a KMS request per layer pull
    # on every pod start (which is real money at 50 replicas).
    encryption_type = "AES256"
  }

  tags = merge(local.common_tags, { Name = var.ecr_repository_name })
}

resource "aws_ecr_lifecycle_policy" "backend" {
  repository = aws_ecr_repository.backend.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep only the ${var.ecr_image_retention_count} most recent images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = var.ecr_image_retention_count
        }
        action = { type = "expire" }
      },
    ]
  })
}

data "aws_iam_policy_document" "ecr_pull" {
  count = length(var.ecr_pull_principal_arns) > 0 ? 1 : 0

  statement {
    sid    = "AllowPull"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = var.ecr_pull_principal_arns
    }

    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
  }
}

resource "aws_ecr_repository_policy" "backend" {
  count = length(var.ecr_pull_principal_arns) > 0 ? 1 : 0

  repository = aws_ecr_repository.backend.name
  policy     = data.aws_iam_policy_document.ecr_pull[0].json
}

# ─────────────────────────────────────────────────────────────────────────
# Application secret
#
# A single JSON document whose keys are exactly the keys of the
# `lms-backend-secrets` Secret in k8s/secret.example.yaml, so the External
# Secrets Operator can project it 1:1 with no per-key mapping.
# ─────────────────────────────────────────────────────────────────────────

resource "random_password" "jwt_secret_key" {
  # Only generated when no shared value is supplied. In any environment that
  # already runs the RAG chatbot this must be the chatbot's JWT_SECRET_KEY.
  count = var.shared_jwt_secret_key == null ? 1 : 0

  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "app" {
  name                    = "${local.secret_prefix}/app"
  description             = "LMS backend application environment secrets (projected into the lms-backend-secrets k8s Secret)"
  kms_key_id              = module.kms_secrets.key_arn
  recovery_window_in_days = var.secret_recovery_window_days

  tags = merge(local.common_tags, { Name = "${local.secret_prefix}/app" })
}

resource "aws_secretsmanager_secret_version" "app" {
  secret_id     = aws_secretsmanager_secret.app.id
  secret_string = jsonencode(local.app_secret_payload)
}
