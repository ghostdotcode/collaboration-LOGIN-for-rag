output "instance_identifier" {
  description = "RDS instance identifier."
  value       = aws_db_instance.this.identifier
}

output "instance_arn" {
  description = "RDS instance ARN."
  value       = aws_db_instance.this.arn
}

output "endpoint" {
  description = "Instance endpoint (host:port)."
  value       = aws_db_instance.this.endpoint
}

output "address" {
  description = "Instance hostname."
  value       = aws_db_instance.this.address
}

output "port" {
  description = "Instance port."
  value       = aws_db_instance.this.port
}

output "db_name" {
  description = "Initial database name."
  value       = aws_db_instance.this.db_name
}

output "master_username" {
  description = "Master username (the password is only in Secrets Manager)."
  value       = aws_db_instance.this.username
}

output "secret_arn" {
  description = "ARN of the master credentials secret."
  value       = aws_secretsmanager_secret.master.arn
}

output "secret_name" {
  description = "Name/path of the master credentials secret."
  value       = aws_secretsmanager_secret.master.name
}

# No sslmode/ssl query parameter on purpose: asyncpg and psycopg2 disagree on
# the parameter name, and the app derives both driver URLs from this one
# string. TLS is instead mandatory server-side via rds.force_ssl=1.
output "database_url" {
  description = "SQLAlchemy DATABASE_URL for the master user (bootstrap/migrations)."
  value       = "postgresql+asyncpg://${var.master_username}:${urlencode(random_password.master.result)}@${aws_db_instance.this.address}:${aws_db_instance.this.port}/${var.db_name}"
  sensitive   = true
}
