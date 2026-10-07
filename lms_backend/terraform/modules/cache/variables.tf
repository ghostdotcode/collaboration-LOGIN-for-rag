variable "name_prefix" {
  description = "Prefix for every resource name in this module."
  type        = string
}

variable "subnet_ids" {
  description = "Private data-tier subnets for the cache subnet group."
  type        = list(string)
}

variable "security_group_ids" {
  description = "Security groups attached to the replication group."
  type        = list(string)
}

variable "engine_version" {
  description = "Redis engine version."
  type        = string
  default     = "7.1"
}

variable "node_type" {
  description = "Cache node type."
  type        = string
  default     = "cache.m7g.large"
}

variable "num_cache_clusters" {
  description = "Total nodes (1 primary + N replicas)."
  type        = number
  default     = 3
}

variable "snapshot_retention_limit" {
  description = "Days of daily snapshots to keep."
  type        = number
  default     = 7
}

variable "snapshot_window" {
  description = "Daily snapshot window (UTC)."
  type        = string
  default     = "03:00-04:00"
}

variable "maintenance_window" {
  description = "Weekly maintenance window (UTC)."
  type        = string
  default     = "sun:05:00-sun:06:00"
}

variable "maxmemory_policy" {
  description = "Eviction policy. volatile-lru only evicts keys that carry a TTL, so rate-limit windows and cached hierarchies go first and Celery results without a TTL are never dropped."
  type        = string
  default     = "volatile-lru"
}

variable "apply_immediately" {
  description = "Apply modifications at once instead of during the maintenance window."
  type        = bool
  default     = false
}

variable "log_retention_days" {
  description = "CloudWatch retention for the Redis slow log."
  type        = number
  default     = 14
}

variable "kms_key_arn" {
  description = "CMK for at-rest encryption."
  type        = string
}

variable "secrets_kms_key_arn" {
  description = "CMK used to encrypt the auth token secret."
  type        = string
}

variable "secret_name" {
  description = "Secrets Manager path for the auth token."
  type        = string
}

variable "secret_recovery_window_days" {
  description = "Secrets Manager recovery window in days."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Common tags."
  type        = map(string)
  default     = {}
}
