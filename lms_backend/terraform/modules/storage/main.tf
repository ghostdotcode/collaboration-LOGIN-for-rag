# Leave attachments (medical certificates and similar). These are the most
# sensitive objects in the system: special-category personal data uploaded
# straight from a browser via presigned PUT.

resource "aws_s3_bucket" "this" {
  bucket        = var.bucket_name
  force_destroy = var.force_destroy

  tags = merge(var.tags, { Name = var.bucket_name })
}

# Versioning is the only protection against an overwrite of an approved
# request's evidence, and it is a prerequisite for the noncurrent-version
# lifecycle rules below.
resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id

  versioning_configuration {
    status = "Enabled"
  }
}

# ACLs disabled: with BucketOwnerEnforced, object ACLs cannot be used at all,
# which removes the classic "presigned upload made the object world-readable"
# failure mode.
resource "aws_s3_bucket_ownership_controls" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket = aws_s3_bucket.this.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }

    # S3 Bucket Keys cut KMS API calls (and cost) by ~99% for a bucket that
    # writes many small objects under one key.
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_cors_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  cors_rule {
    # PUT is the presigned upload; GET/HEAD are the presigned download the
    # approver's browser follows. POST is not used and is therefore absent.
    allowed_methods = ["PUT", "GET", "HEAD"]
    allowed_origins = var.cors_allowed_origins
    # A presigned PUT sends Content-Type and the x-amz-* signing headers;
    # blocking them turns every upload into an opaque CORS failure.
    allowed_headers = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = var.cors_max_age_seconds
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  # Attachments are read intensively during approval and then almost never
  # again, but HR retention means they are not deleted. Current versions
  # therefore age into colder storage instead of expiring.
  rule {
    id     = "attachment-tiering"
    status = "Enabled"

    filter {}

    transition {
      days          = var.ia_transition_days
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = var.archive_transition_days
      storage_class = "GLACIER_IR"
    }
  }

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_expiry_days
    }
  }

  # A browser that abandons a multipart upload leaves billable parts behind
  # forever unless they are reaped.
  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = var.abort_multipart_days
    }
  }

  rule {
    id     = "clean-expired-delete-markers"
    status = "Enabled"

    filter {}

    expiration {
      expired_object_delete_marker = true
    }
  }

  depends_on = [aws_s3_bucket_versioning.this]
}

resource "aws_s3_bucket_logging" "this" {
  count = var.access_log_bucket_id != null ? 1 : 0

  bucket        = aws_s3_bucket.this.id
  target_bucket = var.access_log_bucket_id
  target_prefix = "${var.bucket_name}/"
}

data "aws_iam_policy_document" "bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.this.arn, "${aws_s3_bucket.this.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid       = "DenyOutdatedTLS"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.this.arn, "${aws_s3_bucket.this.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "NumericLessThan"
      variable = "s3:TlsVersion"
      values   = [var.min_tls_version]
    }
  }

  # Deny an upload that explicitly asks for the wrong encryption, but allow
  # one that asks for nothing: a browser following a presigned PUT sends no
  # encryption header, and bucket default encryption applies SSE-KMS for it.
  # A blanket "header must be present" deny would break every upload.
  statement {
    sid       = "DenyWrongEncryptionHeader"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.this.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["aws:kms"]
    }

    condition {
      test     = "Null"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["false"]
    }
  }

  statement {
    sid       = "DenyWrongKmsKey"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.this.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = [var.kms_key_arn]
    }

    condition {
      test     = "Null"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "this" {
  bucket = aws_s3_bucket.this.id
  policy = data.aws_iam_policy_document.bucket.json

  # The public access block must exist first, otherwise S3 can briefly reject
  # (or worse, accept) a policy on an unprotected bucket.
  depends_on = [aws_s3_bucket_public_access_block.this]
}
