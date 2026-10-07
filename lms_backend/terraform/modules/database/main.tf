locals {
  # Tuned for the LMS access pattern rather than left at defaults:
  #  - force_ssl: the spec requires TLS in transit; this rejects any plaintext
  #    connection at the server, so a misconfigured pod fails loudly instead
  #    of quietly shipping PII in the clear.
  #  - log_lock_waits + deadlock_timeout: the balance row is taken with
  #    SELECT ... FOR UPDATE, so contention has to be visible in the logs.
  #  - idle_in_transaction_session_timeout: a wedged pod holding a balance
  #    lock would otherwise block every other request for that user.
  default_parameters = [
    {
      name         = "rds.force_ssl"
      value        = "1"
      apply_method = "pending-reboot"
    },
    {
      name         = "shared_preload_libraries"
      value        = "pg_stat_statements"
      apply_method = "pending-reboot"
    },
    {
      name         = "log_min_duration_statement"
      value        = "500"
      apply_method = "immediate"
    },
    {
      name         = "log_lock_waits"
      value        = "1"
      apply_method = "immediate"
    },
    {
      name         = "deadlock_timeout"
      value        = "1000"
      apply_method = "immediate"
    },
    {
      name         = "idle_in_transaction_session_timeout"
      value        = "60000"
      apply_method = "immediate"
    },
    {
      name         = "log_connections"
      value        = "1"
      apply_method = "immediate"
    },
    {
      name         = "log_disconnections"
      value        = "1"
      apply_method = "immediate"
    },
  ]

  # Caller-supplied entries win over the defaults above.
  merged_parameters = values(merge(
    { for p in local.default_parameters : p.name => p },
    { for p in var.parameters : p.name => p },
  ))

  identifier = "${var.name_prefix}-postgres"
}

resource "aws_db_subnet_group" "this" {
  name        = "${var.name_prefix}-postgres"
  description = "LMS PostgreSQL subnet group (private data tier)"
  subnet_ids  = var.subnet_ids

  tags = merge(var.tags, { Name = "${var.name_prefix}-postgres" })
}

resource "aws_db_parameter_group" "this" {
  # name_prefix, not name: a family or parameter change that forces a new
  # group must be able to exist alongside the one the live instance still
  # references (see create_before_destroy below).
  name_prefix = "${var.name_prefix}-postgres16-"
  family      = "postgres${split(".", var.engine_version)[0]}"
  description = "LMS PostgreSQL 16 parameters"

  dynamic "parameter" {
    for_each = local.merged_parameters

    content {
      name         = parameter.value.name
      value        = parameter.value.value
      apply_method = parameter.value.apply_method
    }
  }

  lifecycle {
    create_before_destroy = true
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-postgres16" })
}

# Generated here rather than typed by a human, and never echoed as an output.
# RDS rejects '/', '@', '"' and space in master passwords, so the special
# character set is restricted instead of using random_password's default.
resource "random_password" "master" {
  length           = 40
  special          = true
  override_special = "!#$%^&*()-_=+[]{}<>:?"
  min_upper        = 2
  min_lower        = 2
  min_numeric      = 2
  min_special      = 2
}

# Enhanced Monitoring reads OS-level metrics through this role; without it the
# midnight accrual storm is invisible below the hypervisor.
data "aws_iam_policy_document" "monitoring_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["monitoring.rds.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "monitoring" {
  count = var.monitoring_interval > 0 ? 1 : 0

  name               = "${var.name_prefix}-rds-monitoring"
  assume_role_policy = data.aws_iam_policy_document.monitoring_assume.json

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "monitoring" {
  count = var.monitoring_interval > 0 ? 1 : 0

  role       = aws_iam_role.monitoring[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

resource "aws_db_instance" "this" {
  identifier     = local.identifier
  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.instance_class
  db_name        = var.db_name
  username       = var.master_username
  password       = random_password.master.result
  port           = 5432

  # gp3 gives a fixed IOPS/throughput baseline that does not depend on volume
  # size, so storage can grow without the burst-balance cliff of gp2.
  allocated_storage     = var.allocated_storage
  max_allocated_storage = var.max_allocated_storage > 0 ? var.max_allocated_storage : null
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = var.kms_key_arn

  db_subnet_group_name   = aws_db_subnet_group.this.name
  parameter_group_name   = aws_db_parameter_group.this.name
  vpc_security_group_ids = var.security_group_ids
  publicly_accessible    = false
  ca_cert_identifier     = var.ca_cert_identifier

  # Lets an operator or a future Lambda connect with a short-lived IAM token
  # instead of the master password.
  iam_database_authentication_enabled = true

  # Leave approvals are a financial-equivalent write path: a failover must not
  # lose a committed transaction, which rules out a read-replica-only setup.
  multi_az = var.multi_az

  backup_retention_period   = var.backup_retention_days
  backup_window             = var.backup_window
  copy_tags_to_snapshot     = true
  delete_automated_backups  = false
  skip_final_snapshot       = false
  final_snapshot_identifier = "${local.identifier}-final"

  # Multi-tenant HR data: an accidental `terraform destroy` of this instance
  # is unrecoverable for every tenant at once.
  deletion_protection = var.deletion_protection

  maintenance_window          = var.maintenance_window
  auto_minor_version_upgrade  = true
  allow_major_version_upgrade = false
  apply_immediately           = var.apply_immediately

  monitoring_interval                   = var.monitoring_interval
  monitoring_role_arn                   = var.monitoring_interval > 0 ? aws_iam_role.monitoring[0].arn : null
  performance_insights_enabled          = true
  performance_insights_kms_key_id       = var.kms_key_arn
  performance_insights_retention_period = var.performance_insights_days
  enabled_cloudwatch_logs_exports       = ["postgresql", "upgrade"]

  tags = merge(var.tags, { Name = local.identifier })

  lifecycle {
    # Rotating the master password is done in Secrets Manager, not by
    # re-running Terraform with a new random value.
    ignore_changes = [password]
  }
}

resource "aws_secretsmanager_secret" "master" {
  name                    = var.secret_name
  description             = "LMS PostgreSQL master credentials"
  kms_key_id              = var.secrets_kms_key_arn
  recovery_window_in_days = var.secret_recovery_window_days

  tags = merge(var.tags, { Name = var.secret_name })
}

# Stored in the shape AWS's own rotation Lambdas and the RDS console expect,
# so native rotation can be attached later without reshaping the document.
resource "aws_secretsmanager_secret_version" "master" {
  secret_id = aws_secretsmanager_secret.master.id

  secret_string = jsonencode({
    engine               = "postgres"
    host                 = aws_db_instance.this.address
    port                 = aws_db_instance.this.port
    username             = var.master_username
    password             = random_password.master.result
    dbname               = var.db_name
    dbInstanceIdentifier = aws_db_instance.this.identifier
  })
}
