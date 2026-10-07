output "vpc_id" {
  description = "ID of the VPC in use (created or reused)."
  value       = local.vpc_id
}

output "vpc_cidr_block" {
  description = "CIDR block of the VPC in use."
  value       = data.aws_vpc.selected.cidr_block
}

output "availability_zones" {
  description = "AZs the subnets are spread across."
  value       = local.azs
}

output "public_subnet_ids" {
  description = "Public subnets - internet-facing ALB and NAT gateways."
  value       = local.public_subnet_ids
}

output "private_app_subnet_ids" {
  description = "Private subnets for EKS node groups and pods."
  value       = local.app_subnet_ids
}

output "private_data_subnet_ids" {
  description = "Private subnets for RDS, ElastiCache and Amazon MQ."
  value       = local.data_subnet_ids
}

output "app_security_group_id" {
  description = "Security group to attach to the EKS nodes/pods running the LMS."
  value       = aws_security_group.app.id
}

output "rds_security_group_id" {
  description = "Security group for the RDS instance."
  value       = aws_security_group.rds.id
}

output "redis_security_group_id" {
  description = "Security group for the ElastiCache replication group."
  value       = aws_security_group.redis.id
}

output "mq_security_group_id" {
  description = "Security group for the Amazon MQ broker."
  value       = aws_security_group.mq.id
}

output "s3_vpc_endpoint_id" {
  description = "Gateway endpoint keeping S3 attachment traffic off the NAT gateway."
  value       = try(aws_vpc_endpoint.s3[0].id, null)
}
