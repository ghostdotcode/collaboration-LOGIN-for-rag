output "bucket_id" {
  description = "Bucket name; this is the value for the S3_BUCKET env var."
  value       = aws_s3_bucket.this.id
}

output "bucket_arn" {
  description = "Bucket ARN."
  value       = aws_s3_bucket.this.arn
}

output "bucket_regional_domain_name" {
  description = "Regional domain name used by presigned URLs."
  value       = aws_s3_bucket.this.bucket_regional_domain_name
}

output "bucket_region" {
  description = "Region the bucket lives in; this is the value for S3_REGION."
  value       = aws_s3_bucket.this.region
}
