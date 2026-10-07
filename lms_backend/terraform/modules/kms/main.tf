# A single symmetric CMK plus alias. Instantiated once per data store so each
# store has its own key, rotation schedule and CloudTrail trail.

data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "key" {
  # Without this statement the key becomes unmanageable: KMS requires the
  # account root to be able to administer the key, and it is also what makes
  # IAM identity policies (e.g. the IRSA role) effective on this key.
  statement {
    sid       = "EnableIAMUserPermissions"
    effect    = "Allow"
    actions   = ["kms:*"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }

  # Service principals get data-plane use only - never key administration -
  # and only when acting for this account (CreateGrant is needed by RDS and
  # ElastiCache to decrypt volumes/snapshots on your behalf).
  dynamic "statement" {
    for_each = length(var.service_principals) > 0 ? [1] : []

    content {
      sid    = "AllowServiceUse"
      effect = "Allow"

      actions = [
        "kms:Encrypt",
        "kms:Decrypt",
        "kms:ReEncrypt*",
        "kms:GenerateDataKey*",
        "kms:CreateGrant",
        "kms:DescribeKey",
      ]

      resources = ["*"]

      principals {
        type        = "Service"
        identifiers = var.service_principals
      }

      condition {
        test     = "StringEquals"
        variable = "aws:SourceAccount"
        values   = [data.aws_caller_identity.current.account_id]
      }
    }
  }
}

resource "aws_kms_key" "this" {
  description              = var.description
  deletion_window_in_days  = var.deletion_window_days
  enable_key_rotation      = var.enable_rotation
  key_usage                = "ENCRYPT_DECRYPT"
  customer_master_key_spec = "SYMMETRIC_DEFAULT"
  multi_region             = false
  policy                   = data.aws_iam_policy_document.key.json

  tags = merge(var.tags, { Name = var.alias })
}

resource "aws_kms_alias" "this" {
  name          = "alias/${var.alias}"
  target_key_id = aws_kms_key.this.key_id
}
