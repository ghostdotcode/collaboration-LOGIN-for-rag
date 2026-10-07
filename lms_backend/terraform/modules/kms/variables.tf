variable "alias" {
  description = "Alias name for the key, without the `alias/` prefix."
  type        = string
}

variable "description" {
  description = "What this key protects. Surfaced in CloudTrail and the console."
  type        = string
}

variable "deletion_window_days" {
  description = "Waiting period before a scheduled key deletion completes."
  type        = number
  default     = 30
}

variable "enable_rotation" {
  description = "Enable automatic annual rotation of the key material. Old material is retained so existing ciphertexts stay readable."
  type        = bool
  default     = true
}

variable "service_principals" {
  description = "AWS service principals allowed to use the key on behalf of this account (e.g. rds.amazonaws.com)."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Tags applied to the key."
  type        = map(string)
  default     = {}
}
