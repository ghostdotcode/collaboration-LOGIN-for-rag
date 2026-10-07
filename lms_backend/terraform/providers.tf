provider "aws" {
  region = var.aws_region

  # Deliberately NOT using `default_tags`. Every module here tags resources
  # from `local.common_tags`, and combining both makes the provider emit
  # perpetual "tags/tags_all" diffs on resources that normalise tags
  # server-side (S3, ElastiCache). One tagging mechanism, one source of truth.

  # Guard-rail: refuse to touch an account other than the intended one.
  # Empty list disables the check so `plan` works for reviewers with
  # read-only credentials in a sandbox account.
  allowed_account_ids = var.allowed_account_ids
}
