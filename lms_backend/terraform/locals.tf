locals {
  name_prefix = "${var.project}-${var.environment}"

  # Applied to every taggable resource in the stack (root + child modules).
  common_tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      CostCenter  = var.cost_center
      Component   = "lms-backend"
      Repository  = "Meritech/lms_backend"
    },
    var.additional_tags,
  )

  account_id = data.aws_caller_identity.current.account_id

  attachments_bucket_name = coalesce(
    var.attachments_bucket_name,
    "${local.name_prefix}-leave-attachments-${local.account_id}",
  )

  # Secrets Manager path convention: <project>/<environment>/<purpose>.
  secret_prefix = "${var.project}/${var.environment}"

  # IRSA is only wired up once the cluster's OIDC provider is known. Before
  # then the rest of the stack still plans and applies cleanly.
  enable_irsa = var.eks_oidc_provider_arn != "" && var.eks_oidc_provider_url != ""

  # SECRET_KEY has to match the RAG chatbot's JWT_SECRET_KEY for SSO, so a
  # generated value is a fallback for greenfield environments only.
  jwt_secret_key = coalesce(var.shared_jwt_secret_key, one(random_password.jwt_secret_key[*].result))

  # Exactly the keys in k8s/secret.example.yaml, so External Secrets can do a
  # 1:1 projection of this JSON document into the `lms-backend-secrets` Secret.
  app_secret_payload = {
    SECRET_KEY            = local.jwt_secret_key
    DATABASE_URL          = coalesce(var.database_url_override, module.database.database_url)
    REDIS_URL             = module.cache.redis_url
    CELERY_BROKER_URL     = module.broker.celery_broker_url
    CELERY_RESULT_BACKEND = module.cache.celery_result_backend_url
    SMTP_HOST             = var.smtp_host
    SMTP_USER             = var.smtp_user
    SMTP_PASSWORD         = var.smtp_password
    STRIPE_API_KEY        = var.stripe_api_key
    STRIPE_WEBHOOK_SECRET = var.stripe_webhook_secret
  }
}
