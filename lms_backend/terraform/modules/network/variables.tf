variable "name_prefix" {
  description = "Prefix for every resource name in this module."
  type        = string
}

variable "create_vpc" {
  description = "Create the VPC and its subnets. False reuses the EKS cluster's existing VPC; only security groups are then created."
  type        = bool
  default     = true
}

variable "existing_vpc_id" {
  description = "VPC to reuse when create_vpc is false."
  type        = string
  default     = ""
}

variable "existing_public_subnets" {
  description = "Public subnet IDs to reuse when create_vpc is false."
  type        = list(string)
  default     = []
}

variable "existing_app_subnets" {
  description = "Private application/node subnet IDs to reuse when create_vpc is false."
  type        = list(string)
  default     = []
}

variable "existing_data_subnets" {
  description = "Private data-tier subnet IDs to reuse when create_vpc is false."
  type        = list(string)
  default     = []
}

variable "vpc_cidr" {
  description = "CIDR for the new VPC."
  type        = string
  default     = "10.40.0.0/16"
}

variable "availability_zone_count" {
  description = "How many AZs to spread the three subnet tiers across."
  type        = number
  default     = 3
}

variable "single_nat_gateway" {
  description = "Use one NAT gateway for all private subnets instead of one per AZ."
  type        = bool
  default     = false
}

variable "enable_flow_logs" {
  description = "Send VPC flow logs to CloudWatch Logs."
  type        = bool
  default     = true
}

variable "flow_log_retention_days" {
  description = "Retention for the flow log group."
  type        = number
  default     = 90
}

variable "eks_cluster_name" {
  description = "Existing EKS cluster name, used for the kubernetes.io/cluster subnet tags the AWS Load Balancer Controller needs."
  type        = string
}

variable "app_container_port" {
  description = "Port the API container listens on."
  type        = number
  default     = 8000
}

variable "alb_security_group_id" {
  description = "Security group of the ingress ALB. When set, only it may reach the app port; otherwise the whole VPC CIDR may."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Common tags."
  type        = map(string)
  default     = {}
}
