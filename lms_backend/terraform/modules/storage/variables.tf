variable "bucket_name" {
  description = "Globally unique bucket name for leave attachments."
  type        = string
}

variable "kms_key_arn" {
  description = "Dedicated CMK for SSE-KMS on this bucket."
  type        = string
}

variable "cors_allowed_origins" {
  description = "Origins permitted to perform presigned PUT/GET against the bucket from a browser."
  type        = list(string)
  default     = []
}

variable "cors_max_age_seconds" {
  description = "How long a browser may cache the CORS preflight response."
  type        = number
  default     = 3000
}

variable "ia_transition_days" {
  description = "Days before an attachment moves to STANDARD_IA."
  type        = number
  default     = 90
}

variable "archive_transition_days" {
  description = "Days before an attachment moves to GLACIER_IR."
  type        = number
  default     = 365
}

variable "noncurrent_expiry_days" {
  description = "Days a non-current version is retained after being overwritten."
  type        = number
  default     = 90
}

variable "abort_multipart_days" {
  description = "Days before an incomplete multipart upload is aborted and its parts deleted."
  type        = number
  default     = 7
}

variable "access_log_bucket_id" {
  description = "Bucket that receives S3 server access logs; null disables access logging."
  type        = string
  default     = null
}

variable "force_destroy" {
  description = "Permit Terraform to empty and delete the bucket."
  type        = bool
  default     = false
}

variable "min_tls_version" {
  description = "Minimum TLS version accepted by the bucket policy."
  type        = string
  default     = "1.2"
}

variable "tags" {
  description = "Common tags."
  type        = map(string)
  default     = {}
}
