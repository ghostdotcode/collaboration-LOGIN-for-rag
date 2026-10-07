variable "name_prefix" {
  description = "Prefix for every resource name in this module."
  type        = string
}

# ── WAF ──────────────────────────────────────────────────────────────────

variable "alb_arn" {
  description = "ALB to associate the web ACL with. null leaves the ACL unassociated."
  type        = string
  default     = null
}

variable "rate_limit_per_5min" {
  description = "Requests per source IP per 5 minutes before the rate-based rule blocks."
  type        = number
  default     = 3000
}

variable "enable_waf_logging" {
  description = "Ship WAF logs to CloudWatch Logs."
  type        = bool
  default     = true
}

variable "waf_log_retention_days" {
  description = "Retention for the WAF log group."
  type        = number
  default     = 30
}

variable "count_only_rules" {
  description = "Rules inside AWSManagedRulesCommonRuleSet to run in Count mode instead of Block."
  type        = list(string)
  default     = []
}

# ── IRSA ─────────────────────────────────────────────────────────────────

variable "enable_irsa" {
  description = "Create the IRSA role. False when the cluster's OIDC provider is not known yet."
  type        = bool
  default     = false
}

variable "oidc_provider_arn" {
  description = "IAM OIDC provider ARN of the existing EKS cluster."
  type        = string
  default     = ""
}

variable "oidc_provider_url" {
  description = "OIDC issuer URL of the existing EKS cluster (with or without https://)."
  type        = string
  default     = ""
}

variable "k8s_namespace" {
  description = "Namespace of the LMS ServiceAccount."
  type        = string
  default     = "lms"
}

variable "k8s_service_account_name" {
  description = "Name of the LMS ServiceAccount."
  type        = string
  default     = "lms-backend"
}

variable "attachments_bucket_arn" {
  description = "ARN of the attachments bucket the role may read and write."
  type        = string
}

variable "attachments_kms_key_arn" {
  description = "CMK protecting the attachments bucket."
  type        = string
}

variable "secrets_kms_key_arn" {
  description = "CMK protecting the application secrets."
  type        = string
}

variable "app_secret_arns" {
  description = "Secrets Manager ARNs the workload may read."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Common tags."
  type        = map(string)
  default     = {}
}
