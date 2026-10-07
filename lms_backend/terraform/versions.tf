terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Remote state for this stack contains the generated RDS master password,
  # the Redis auth token and the RabbitMQ password in plaintext (that is
  # unavoidable with `random_password`), so the state bucket must be
  # versioned, SSE-KMS encrypted, and readable only by the deploy role.
  #
  # Left commented so `terraform init` succeeds in a fresh clone with no AWS
  # account attached. In CI, enable it and pass values at init time instead
  # of hardcoding them here:
  #
  #   terraform init -backend-config=backend.hcl
  #
  # backend "s3" {
  #   bucket         = "meritech-tfstate"
  #   key            = "lms-backend/production/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "meritech-tfstate-locks"
  #   encrypt        = true
  #   kms_key_id     = "alias/meritech-tfstate"
  # }
}
