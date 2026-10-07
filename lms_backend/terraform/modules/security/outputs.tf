output "web_acl_arn" {
  description = "ARN of the regional web ACL."
  value       = aws_wafv2_web_acl.this.arn
}

output "web_acl_id" {
  description = "ID of the regional web ACL."
  value       = aws_wafv2_web_acl.this.id
}

output "web_acl_name" {
  description = "Name of the regional web ACL."
  value       = aws_wafv2_web_acl.this.name
}

output "waf_log_group_name" {
  description = "CloudWatch log group receiving WAF logs, if logging is enabled."
  value       = try(aws_cloudwatch_log_group.waf[0].name, null)
}

output "irsa_role_arn" {
  description = "ARN to put in the ServiceAccount's eks.amazonaws.com/role-arn annotation."
  value       = try(aws_iam_role.irsa[0].arn, null)
}

output "irsa_role_name" {
  description = "Name of the IRSA role."
  value       = try(aws_iam_role.irsa[0].name, null)
}
