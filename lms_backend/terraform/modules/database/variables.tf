variable "name_prefix" {
  description = "Prefix for every resource name in this module."
  type        = string
}

variable "subnet_ids" {
  description = "Private data-tier subnets for the DB subnet group (>= 2 AZs required for Multi-AZ)."
  type        = list(string)
}

variable "security_group_ids" {
  description = "Security groups attached to the instance."
  type        = list(string)
}

variable "engine_version" {
  description = "PostgreSQL version. A major-only value lets RDS select the latest supported minor."
  type        = string
  default     = "16"
}

variable "instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.m6g.large"
}

variable "allocated_storage" {
  description = "Initial storage in GiB."
  type        = number
  default     = 100
}

variable "max_allocated_storage" {
  description = "Storage autoscaling ceiling in GiB; 0 disables autoscaling."
  type        = number
  default     = 1000
}

variable "db_name" {
  description = "Initial database name."
  type        = string
  default     = "meritech_db"
}

variable "master_username" {
  description = "Master username."
  type        = string
  default     = "lms_admin"
}

variable "multi_az" {
  description = "Provision a synchronous standby in another AZ."
  type        = bool
  default     = true
}

variable "backup_retention_days" {
  description = "Automated backup retention in days."
  type        = number
  default     = 30
}

variable "deletion_protection" {
  description = "Refuse deletion of the instance until explicitly disabled."
  type        = bool
  default     = true
}

variable "monitoring_interval" {
  description = "Enhanced Monitoring interval in seconds; 0 disables it (and the IAM role)."
  type        = number
  default     = 60
}

variable "performance_insights_days" {
  description = "Performance Insights retention in days."
  type        = number
  default     = 7
}

variable "backup_window" {
  description = "Daily backup window (UTC). Placed before the accrual CronJob at 00:15 UTC."
  type        = string
  default     = "22:00-23:00"
}

variable "maintenance_window" {
  description = "Weekly maintenance window (UTC), outside the Monday 09:00 login peak."
  type        = string
  default     = "sun:23:30-mon:00:30"
}

variable "ca_cert_identifier" {
  description = "RDS CA bundle for the server certificate. Pinned so a CA rotation is a deliberate, reviewed change."
  type        = string
  default     = "rds-ca-rsa2048-g1"
}

variable "apply_immediately" {
  description = "Apply modifications at once instead of during the maintenance window."
  type        = bool
  default     = false
}

variable "parameters" {
  description = "Extra DB parameter group entries, merged over the defaults set by this module."
  type = list(object({
    name         = string
    value        = string
    apply_method = optional(string, "immediate")
  }))
  default = []
}

variable "kms_key_arn" {
  description = "CMK for storage, snapshot and Performance Insights encryption."
  type        = string
}

variable "secrets_kms_key_arn" {
  description = "CMK used to encrypt the master credentials secret."
  type        = string
}

variable "secret_name" {
  description = "Secrets Manager path for the master credentials."
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
