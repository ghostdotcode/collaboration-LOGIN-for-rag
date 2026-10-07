variable "name_prefix" {
  description = "Prefix for every resource name in this module."
  type        = string
}

variable "subnet_ids" {
  description = "Private data-tier subnets. CLUSTER_MULTI_AZ uses one subnet per AZ; SINGLE_INSTANCE takes only the first."
  type        = list(string)
}

variable "security_group_ids" {
  description = "Security groups attached to the broker."
  type        = list(string)
}

variable "engine_version" {
  description = "RabbitMQ engine version offered by Amazon MQ."
  type        = string
  default     = "3.13"
}

variable "host_instance_type" {
  description = "Broker host instance type."
  type        = string
  default     = "mq.m5.large"
}

variable "deployment_mode" {
  description = "SINGLE_INSTANCE or CLUSTER_MULTI_AZ."
  type        = string
  default     = "CLUSTER_MULTI_AZ"
}

variable "username" {
  description = "RabbitMQ application username used by Celery."
  type        = string
  default     = "lms_celery"
}

variable "maintenance_day" {
  description = "Day of week for the Amazon MQ maintenance window."
  type        = string
  default     = "SUNDAY"
}

variable "maintenance_time" {
  description = "Start time (HH:MM) of the maintenance window."
  type        = string
  default     = "04:00"
}

variable "maintenance_timezone" {
  description = "Timezone of the maintenance window."
  type        = string
  default     = "UTC"
}

variable "apply_immediately" {
  description = "Apply modifications at once instead of during the maintenance window."
  type        = bool
  default     = false
}

variable "kms_key_arn" {
  description = "CMK for broker at-rest encryption."
  type        = string
}

variable "secrets_kms_key_arn" {
  description = "CMK used to encrypt the broker credentials secret."
  type        = string
}

variable "secret_name" {
  description = "Secrets Manager path for the broker credentials."
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
