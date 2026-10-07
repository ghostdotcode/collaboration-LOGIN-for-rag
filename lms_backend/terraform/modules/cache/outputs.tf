output "replication_group_id" {
  description = "ElastiCache replication group ID."
  value       = aws_elasticache_replication_group.this.id
}

output "primary_endpoint_address" {
  description = "Write endpoint (primary node)."
  value       = aws_elasticache_replication_group.this.primary_endpoint_address
}

output "reader_endpoint_address" {
  description = "Read-only endpoint spanning the replicas."
  value       = aws_elasticache_replication_group.this.reader_endpoint_address
}

output "port" {
  description = "Redis port."
  value       = 6379
}

output "secret_arn" {
  description = "ARN of the auth token secret."
  value       = aws_secretsmanager_secret.auth_token.arn
}

output "secret_name" {
  description = "Name/path of the auth token secret."
  value       = aws_secretsmanager_secret.auth_token.name
}

# `rediss://` (double s) is what tells redis-py to negotiate TLS; with
# transit encryption enabled a plain `redis://` URL fails to connect.
# The empty username before ':' is the Redis AUTH convention for a
# password-only credential.
output "redis_url" {
  description = "REDIS_URL for the app (db 0: rate limiting, sessions, caches)."
  value       = "rediss://:${urlencode(random_password.auth_token.result)}@${aws_elasticache_replication_group.this.primary_endpoint_address}:6379/0"
  sensitive   = true
}

# `ssl_cert_reqs` is not optional here: Celery refuses to start with a
# `rediss://` result backend that does not specify it.
output "celery_result_backend_url" {
  description = "CELERY_RESULT_BACKEND for the app (db 1, kept apart from the cache keyspace)."
  value       = "rediss://:${urlencode(random_password.auth_token.result)}@${aws_elasticache_replication_group.this.primary_endpoint_address}:6379/1?ssl_cert_reqs=required"
  sensitive   = true
}
