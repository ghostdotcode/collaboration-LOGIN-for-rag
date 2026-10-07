locals {
  replication_group_id = "${var.name_prefix}-redis"
}

resource "aws_elasticache_subnet_group" "this" {
  name        = "${var.name_prefix}-redis"
  description = "LMS Redis subnet group (private data tier)"
  subnet_ids  = var.subnet_ids

  tags = merge(var.tags, { Name = "${var.name_prefix}-redis" })
}

resource "aws_elasticache_parameter_group" "this" {
  name        = "${var.name_prefix}-redis7"
  family      = "redis${split(".", var.engine_version)[0]}"
  description = "LMS Redis parameters"

  parameter {
    name  = "maxmemory-policy"
    value = var.maxmemory_policy
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-redis7" })
}

# ElastiCache rejects most punctuation in auth tokens, so this stays
# alphanumeric and buys its entropy from length instead.
resource "random_password" "auth_token" {
  length  = 64
  special = false
}

resource "aws_cloudwatch_log_group" "slow_log" {
  name              = "/aws/elasticache/${var.name_prefix}/slow-log"
  retention_in_days = var.log_retention_days

  tags = var.tags
}

resource "aws_cloudwatch_log_group" "engine_log" {
  name              = "/aws/elasticache/${var.name_prefix}/engine-log"
  retention_in_days = var.log_retention_days

  tags = var.tags
}

resource "aws_elasticache_replication_group" "this" {
  replication_group_id = local.replication_group_id
  description          = "LMS rate limiting, RAG-shared sessions and Celery result backend"

  engine               = "redis"
  engine_version       = var.engine_version
  node_type            = var.node_type
  port                 = 6379
  parameter_group_name = aws_elasticache_parameter_group.this.name
  subnet_group_name    = aws_elasticache_subnet_group.this.name
  security_group_ids   = var.security_group_ids

  # Replicas across AZs with automatic failover: losing the primary must not
  # take the API's rate limiter (and therefore every request) down with it.
  num_cache_clusters         = var.num_cache_clusters
  automatic_failover_enabled = true
  multi_az_enabled           = true

  # Sessions shared with the RAG chatbot travel over this connection, so both
  # legs are encrypted; the auth token is Redis AUTH, not a substitute for TLS.
  at_rest_encryption_enabled = true
  kms_key_id                 = var.kms_key_arn
  transit_encryption_enabled = true
  auth_token                 = random_password.auth_token.result
  auth_token_update_strategy = "ROTATE"

  snapshot_retention_limit = var.snapshot_retention_limit
  snapshot_window          = var.snapshot_window
  maintenance_window       = var.maintenance_window

  auto_minor_version_upgrade = true
  apply_immediately          = var.apply_immediately

  log_delivery_configuration {
    destination      = aws_cloudwatch_log_group.slow_log.name
    destination_type = "cloudwatch-logs"
    log_format       = "json"
    log_type         = "slow-log"
  }

  log_delivery_configuration {
    destination      = aws_cloudwatch_log_group.engine_log.name
    destination_type = "cloudwatch-logs"
    log_format       = "json"
    log_type         = "engine-log"
  }

  tags = merge(var.tags, { Name = local.replication_group_id })

  lifecycle {
    # Token rotation is a two-step ElastiCache operation driven outside
    # Terraform; re-planning must not try to reset it.
    ignore_changes = [auth_token]
  }
}

resource "aws_secretsmanager_secret" "auth_token" {
  name                    = var.secret_name
  description             = "LMS ElastiCache Redis AUTH token"
  kms_key_id              = var.secrets_kms_key_arn
  recovery_window_in_days = var.secret_recovery_window_days

  tags = merge(var.tags, { Name = var.secret_name })
}

resource "aws_secretsmanager_secret_version" "auth_token" {
  secret_id = aws_secretsmanager_secret.auth_token.id

  secret_string = jsonencode({
    host       = aws_elasticache_replication_group.this.primary_endpoint_address
    reader     = aws_elasticache_replication_group.this.reader_endpoint_address
    port       = 6379
    auth_token = random_password.auth_token.result
  })
}
