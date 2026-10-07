# Amazon MQ for RabbitMQ - the Celery broker.
#
# The spec mandates RabbitMQ, and app/tasks/celery_app.py relies on broker
# behaviour that SQS cannot provide: `task_acks_late` with
# `task_reject_on_worker_lost` (redelivery of an interrupted accrual chunk)
# and per-queue routing of the accrual/email fan-out. Amazon MQ is used
# instead of self-hosting RabbitMQ on EKS so that broker storage, failover
# and patching are not the LMS team's problem during the midnight accrual run.

locals {
  broker_name = "${var.name_prefix}-rabbitmq"

  # Cluster deployments spread nodes across the supplied subnets (one per AZ);
  # a single-instance broker accepts exactly one subnet.
  broker_subnets = var.deployment_mode == "SINGLE_INSTANCE" ? slice(var.subnet_ids, 0, 1) : var.subnet_ids
}

# Amazon MQ rejects ',', ':' and '=' in RabbitMQ passwords and requires at
# least 12 characters with 4 unique ones.
resource "random_password" "broker" {
  length           = 40
  special          = true
  override_special = "!#$%^&*()-_+[]{}<>?."
  min_upper        = 2
  min_lower        = 2
  min_numeric      = 2
  min_special      = 2
}

resource "aws_mq_broker" "this" {
  broker_name        = local.broker_name
  engine_type        = "RabbitMQ"
  engine_version     = var.engine_version
  host_instance_type = var.host_instance_type
  deployment_mode    = var.deployment_mode

  # Private-only: the broker sits in the data tier with no public endpoint, so
  # the only route to it is from pods holding the app security group.
  publicly_accessible = false
  subnet_ids          = local.broker_subnets
  security_groups     = var.security_group_ids

  # Customer-managed key rather than the AWS-owned default so broker storage
  # falls under the same rotation and revocation story as RDS and S3.
  encryption_options {
    kms_key_id        = var.kms_key_arn
    use_aws_owned_key = false
  }

  # RabbitMQ on Amazon MQ supports the general log only (no audit log).
  logs {
    general = true
  }

  auto_minor_version_upgrade = true
  apply_immediately          = var.apply_immediately

  maintenance_window_start_time {
    day_of_week = var.maintenance_day
    time_of_day = var.maintenance_time
    time_zone   = var.maintenance_timezone
  }

  user {
    username = var.username
    password = random_password.broker.result
  }

  tags = merge(var.tags, { Name = local.broker_name })

  lifecycle {
    # Rotating the broker password is a console/CLI operation followed by a
    # secret update; a re-plan must not fight it.
    ignore_changes = [user]
  }
}

resource "aws_secretsmanager_secret" "broker" {
  name                    = var.secret_name
  description             = "LMS Amazon MQ (RabbitMQ) application credentials"
  kms_key_id              = var.secrets_kms_key_arn
  recovery_window_in_days = var.secret_recovery_window_days

  tags = merge(var.tags, { Name = var.secret_name })
}

resource "aws_secretsmanager_secret_version" "broker" {
  secret_id = aws_secretsmanager_secret.broker.id

  secret_string = jsonencode({
    username = var.username
    password = random_password.broker.result
    endpoint = aws_mq_broker.this.instances[0].endpoints[0]
    console  = aws_mq_broker.this.instances[0].console_url
  })
}
