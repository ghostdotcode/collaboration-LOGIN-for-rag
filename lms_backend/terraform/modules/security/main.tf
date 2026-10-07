data "aws_caller_identity" "current" {}

locals {
  waf_name = "${var.name_prefix}-web-acl"

  # Priority order is cost and noise order, not preference: IP reputation is
  # a cheap list lookup and drops scanners before the request is inspected by
  # the heavier signature groups.
  managed_rule_groups = [
    {
      name      = "AWSManagedRulesAmazonIpReputationList"
      priority  = 10
      overrides = []
    },
    # The common rule set is the only group large enough to need a staged
    # rollout, so it is the only one that honours count_only_rules.
    {
      name      = "AWSManagedRulesCommonRuleSet"
      priority  = 20
      overrides = var.count_only_rules
    },
    {
      name      = "AWSManagedRulesKnownBadInputsRuleSet"
      priority  = 30
      overrides = []
    },
    {
      name      = "AWSManagedRulesSQLiRuleSet"
      priority  = 40
      overrides = []
    },
  ]

  oidc_host = replace(var.oidc_provider_url, "https://", "")
}

# ─────────────────────────────────────────────────────────────────────────
# WAFv2
#
# REGIONAL scope because the entry point is an ALB in front of the EKS
# ingress. A CloudFront distribution would require scope = CLOUDFRONT and a
# provider aliased to us-east-1.
#
# The app already enforces per-IP and per-tenant limits in Redis
# (RATE_LIMIT_PER_IP / RATE_LIMIT_PER_TENANT). This ACL exists for the layer
# below that: payload signatures the app should never have to parse, and
# floods that must be dropped before they reach a pod and consume a DB
# connection from the pool.
# ─────────────────────────────────────────────────────────────────────────

resource "aws_wafv2_web_acl" "this" {
  name        = local.waf_name
  description = "LMS backend edge protection"
  scope       = "REGIONAL"

  default_action {
    allow {}
  }

  dynamic "rule" {
    for_each = local.managed_rule_groups

    content {
      name     = rule.value.name
      priority = rule.value.priority

      # override_action (not action): the managed group decides per-rule
      # whether to block; `none {}` means "use the group's own actions".
      override_action {
        none {}
      }

      statement {
        managed_rule_group_statement {
          name        = rule.value.name
          vendor_name = "AWS"

          dynamic "rule_action_override" {
            for_each = rule.value.overrides

            content {
              name = rule_action_override.value

              action_to_use {
                count {}
              }
            }
          }
        }
      }

      visibility_config {
        cloudwatch_metrics_enabled = true
        metric_name                = rule.value.name
        sampled_requests_enabled   = true
      }
    }
  }

  # Last in the chain: a flood of otherwise valid requests. The threshold sits
  # far above the app's own limiter so this only fires on genuine abuse.
  rule {
    name     = "rate-limit-per-ip"
    priority = 50

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = var.rate_limit_per_5min
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "rate-limit-per-ip"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = local.waf_name
    sampled_requests_enabled   = true
  }

  tags = merge(var.tags, { Name = local.waf_name })
}

resource "aws_wafv2_web_acl_association" "alb" {
  count = var.alb_arn != null ? 1 : 0

  resource_arn = var.alb_arn
  web_acl_arn  = aws_wafv2_web_acl.this.arn
}

# WAF will only deliver to a log group whose name starts with
# `aws-waf-logs-`; anything else fails at association time.
resource "aws_cloudwatch_log_group" "waf" {
  count = var.enable_waf_logging ? 1 : 0

  name              = "aws-waf-logs-${var.name_prefix}"
  retention_in_days = var.waf_log_retention_days

  tags = var.tags
}

data "aws_iam_policy_document" "waf_log_delivery" {
  statement {
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]

    principals {
      type        = "Service"
      identifiers = ["delivery.logs.amazonaws.com"]
    }

    resources = ["arn:aws:logs:*:${data.aws_caller_identity.current.account_id}:log-group:aws-waf-logs-${var.name_prefix}:*"]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_cloudwatch_log_resource_policy" "waf" {
  count = var.enable_waf_logging ? 1 : 0

  policy_name     = "${var.name_prefix}-waf-log-delivery"
  policy_document = data.aws_iam_policy_document.waf_log_delivery.json
}

resource "aws_wafv2_web_acl_logging_configuration" "this" {
  count = var.enable_waf_logging ? 1 : 0

  resource_arn            = aws_wafv2_web_acl.this.arn
  log_destination_configs = [aws_cloudwatch_log_group.waf[0].arn]

  # WAF logs whole headers. The refresh cookie and bearer token would
  # otherwise be retained in CloudWatch for anyone with logs:GetLogEvents.
  redacted_fields {
    single_header {
      name = "authorization"
    }
  }

  redacted_fields {
    single_header {
      name = "cookie"
    }
  }

  depends_on = [aws_cloudwatch_log_resource_policy.waf]
}

# ─────────────────────────────────────────────────────────────────────────
# IRSA
#
# Pod-level credentials via the cluster's OIDC provider, so no AWS key ever
# reaches a container env var and the node role stays minimal.
# ─────────────────────────────────────────────────────────────────────────

data "aws_iam_policy_document" "irsa_assume" {
  count = var.enable_irsa ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }

    # Both conditions matter: without the `sub` bound to one
    # namespace/ServiceAccount pair, any pod in the cluster could assume this
    # role; without `aud`, a token minted for another audience would work.
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = ["system:serviceaccount:${var.k8s_namespace}:${var.k8s_service_account_name}"]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "irsa" {
  count = var.enable_irsa ? 1 : 0

  # GetObject/PutObject only. DeleteObject is deliberately withheld: an
  # attachment is evidence attached to an approved leave request, and
  # retention is handled by the bucket lifecycle rules, not the application.
  statement {
    sid       = "AttachmentObjectAccess"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${var.attachments_bucket_arn}/*"]
  }

  # SSE-KMS means every presigned GET/PUT needs key access as well as object
  # access; without this the upload succeeds and the download 403s.
  statement {
    sid    = "AttachmentKeyAccess"
    effect = "Allow"

    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]

    resources = [var.attachments_kms_key_arn, var.secrets_kms_key_arn]
  }

  dynamic "statement" {
    for_each = length(var.app_secret_arns) > 0 ? [1] : []

    content {
      sid       = "ReadAppSecrets"
      effect    = "Allow"
      actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
      resources = var.app_secret_arns
    }
  }
}

resource "aws_iam_role" "irsa" {
  count = var.enable_irsa ? 1 : 0

  name                 = "${var.name_prefix}-irsa"
  description          = "IRSA role for the LMS backend ServiceAccount"
  assume_role_policy   = data.aws_iam_policy_document.irsa_assume[0].json
  max_session_duration = 3600

  tags = merge(var.tags, { Name = "${var.name_prefix}-irsa" })
}

resource "aws_iam_policy" "irsa" {
  count = var.enable_irsa ? 1 : 0

  name        = "${var.name_prefix}-irsa"
  description = "S3 attachments, KMS and Secrets Manager access for the LMS backend"
  policy      = data.aws_iam_policy_document.irsa[0].json

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "irsa" {
  count = var.enable_irsa ? 1 : 0

  role       = aws_iam_role.irsa[0].name
  policy_arn = aws_iam_policy.irsa[0].arn
}
