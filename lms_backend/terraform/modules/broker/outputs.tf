locals {
  # e.g. amqps://b-1234.mq.us-east-1.amazonaws.com:5671
  amqps_endpoint = aws_mq_broker.this.instances[0].endpoints[0]
  amqps_host     = replace(local.amqps_endpoint, "amqps://", "")
}

output "broker_id" {
  description = "Amazon MQ broker ID."
  value       = aws_mq_broker.this.id
}

output "broker_arn" {
  description = "Amazon MQ broker ARN."
  value       = aws_mq_broker.this.arn
}

output "amqps_endpoint" {
  description = "AMQPS endpoint of the broker."
  value       = local.amqps_endpoint
}

output "console_url" {
  description = "RabbitMQ management console URL (reachable only inside the VPC)."
  value       = aws_mq_broker.this.instances[0].console_url
}

output "secret_arn" {
  description = "ARN of the broker credentials secret."
  value       = aws_secretsmanager_secret.broker.arn
}

output "secret_name" {
  description = "Name/path of the broker credentials secret."
  value       = aws_secretsmanager_secret.broker.name
}

# The trailing '//' is the default vhost '/', matching the CELERY_BROKER_URL
# shape in .env.example. The `amqps` scheme is what makes kombu negotiate TLS;
# set Celery's broker_use_ssl explicitly if strict certificate verification is
# required on top of that.
output "celery_broker_url" {
  description = "CELERY_BROKER_URL for the app and the Celery workers."
  value       = "amqps://${var.username}:${urlencode(random_password.broker.result)}@${local.amqps_host}//"
  sensitive   = true
}
