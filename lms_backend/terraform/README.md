# LMS Backend — AWS Infrastructure (Terraform)

Infrastructure for the multi-tenant Leave Management System backend: network,
PostgreSQL 16, Redis, RabbitMQ, the attachments bucket, the container
registry, the WAF and the pod identity role.

**This stack does not create the EKS cluster.** The cluster, its node groups
and its IAM OIDC provider are assumed to exist already and are passed in as
variables (`eks_cluster_name`, `eks_oidc_provider_arn`,
`eks_oidc_provider_url`). The VPC can either be created here (`create_vpc =
true`, the default) or reused from the cluster (`create_vpc = false` plus the
`existing_*_subnet_ids` variables).

## What gets created

| Module             | Resources |
| ------------------ | --------- |
| `modules/network`  | VPC, public/private-app/private-data subnets across 3 AZs, IGW, per-AZ NAT gateways, route tables, S3 gateway endpoint, REJECT flow logs, and the app/RDS/Redis/MQ security groups |
| `modules/kms`      | One symmetric CMK + alias with annual rotation. Instantiated five times: RDS, Redis, S3, Secrets Manager, Amazon MQ |
| `modules/database` | RDS PostgreSQL 16 (Multi-AZ, gp3, encrypted, 30-day backups, deletion protection, Performance Insights, parameter group) plus the generated master password in Secrets Manager |
| `modules/cache`    | ElastiCache Redis replication group (3 nodes, Multi-AZ, encryption at rest + in transit, AUTH token in Secrets Manager) |
| `modules/broker`   | Amazon MQ for RabbitMQ (cluster, private subnets, CMK-encrypted) plus credentials in Secrets Manager |
| `modules/storage`  | Attachments bucket: versioning, SSE-KMS with a dedicated CMK, all four public-access-block flags, lifecycle tiering, TLS-only policy, CORS for presigned PUT |
| `modules/security` | WAFv2 REGIONAL web ACL (IP reputation, common, known bad inputs, SQLi, rate-based) and the IRSA role for the LMS ServiceAccount |
| root               | ECR repository (scan on push, keep last 30 images) and the application secret consumed by Kubernetes |

Why RabbitMQ and not SQS: the spec mandates it, and
`app/tasks/celery_app.py` depends on broker semantics SQS does not offer —
`task_acks_late` with `task_reject_on_worker_lost` for redelivering an
interrupted accrual chunk, and per-queue routing of the `accrual` / `email`
fan-out. Amazon MQ is used instead of self-hosted RabbitMQ on EKS so broker
failover and patching are not in the critical path of the midnight accrual
run.

## Prerequisites

- Terraform >= 1.6, AWS provider ~> 5.0 (pinned in `versions.tf`)
- An existing EKS cluster with an IAM OIDC provider
- AWS credentials for a role that can create VPC, RDS, ElastiCache, MQ, S3,
  KMS, ECR, WAF and IAM resources
- For remote state: an S3 bucket (versioned, SSE-KMS) and a DynamoDB lock
  table. The `backend "s3"` block in `versions.tf` is commented out so
  `terraform init` works in a fresh clone

## Init / plan / apply

```bash
cd lms_backend/terraform
cp terraform.tfvars.example terraform.tfvars   # then edit

# Local state (review only)
terraform init

# Remote state (CI / shared environments): uncomment the backend block in
# versions.tf, then
terraform init -backend-config=backend.hcl

terraform fmt -recursive
terraform validate
terraform plan  -var-file=terraform.tfvars -out=tfplan
terraform apply tfplan
```

Secrets (`shared_jwt_secret_key`, `smtp_password`, `stripe_api_key`,
`stripe_webhook_secret`) should be supplied as `TF_VAR_*` environment
variables from the CI secret store, not written into `terraform.tfvars`.

State contains the generated RDS password, Redis AUTH token and RabbitMQ
password in plaintext — that is inherent to `random_password`. Treat the state
bucket as a secret store: encrypted, versioned, no public access, read
restricted to the deploy role. No root output exposes a credential; the
`terraform output` surface is only infrastructure metadata and Secrets
Manager pointers.

## Order of operations relative to the EKS cluster

1. **EKS cluster exists** (separate stack) with its IAM OIDC provider
   registered. Note the cluster name, OIDC provider ARN and issuer URL.
2. **Apply this stack** with `eks_oidc_provider_arn` / `_url` set and
   `alb_arn` left `null`. Everything except the WAF association is created.
   If `create_vpc = true`, the new VPC's subnets are tagged for the cluster
   name you passed, so a node group can be moved into them.
3. **Annotate the ServiceAccount.** `k8s/serviceaccount.yaml` creates the
   `lms` namespace and the `lms-backend` ServiceAccount that every pod spec
   already references; only the role ARN is a placeholder:

   ```bash
   terraform output -raw irsa_role_arn   # paste into eks.amazonaws.com/role-arn
   ```

   The namespace and ServiceAccount name are baked into the role's trust
   policy, so renaming either leaves the pods without AWS credentials.
4. **Attach the app security group** (`terraform output -raw
   app_security_group_id`) to the node group / pod ENIs. It is the only
   source the RDS, Redis and MQ security groups accept.
5. **Deploy the workload** (`kubectl apply -k k8s/`). Point the `images:`
   entry in `k8s/kustomization.yaml` at `terraform output -raw
   ecr_repository_url` first — that is the single place the registry is
   configured. The Ingress creates the ALB.
6. **Re-apply with `alb_arn`** (and optionally `alb_security_group_id`) set to
   the ALB the ingress controller created. This associates the web ACL and
   narrows app-SG ingress to the ALB. Splitting this into a second apply is
   deliberate: the ALB is owned by the cluster's ingress controller, not by
   Terraform.
7. **Run migrations.** The `migrate` initContainer runs `alembic upgrade head`
   using the master DSN. Afterwards, create the least-privilege `lms_app`
   role and Postgres RLS policies (spec section 6 — not managed by
   Terraform), then set `database_url_override` and re-apply so the app stops
   connecting as master.

## Which values feed `k8s/secret.example.yaml`

Terraform writes one JSON document to Secrets Manager at
`<project>/<environment>/app` (`terraform output -raw app_secret_name`) whose
properties are exactly the keys of the `lms-backend-secrets` Secret, so the
External Secrets Operator can project it 1:1 with no per-key mapping:

| Secret key in `lms-backend-secrets` | Source |
| ----------------------------------- | ------ |
| `SECRET_KEY`                        | `var.shared_jwt_secret_key` — **must equal the RAG chatbot's `JWT_SECRET_KEY`**; a random value is generated only if you leave it null, which breaks SSO in any environment that already runs the chatbot |
| `DATABASE_URL`                      | Composed from the RDS endpoint + the generated master password (or `var.database_url_override`) |
| `REDIS_URL`                         | Composed from the Redis primary endpoint + AUTH token, `rediss://` scheme, db `0` |
| `CELERY_BROKER_URL`                 | Composed from the Amazon MQ AMQPS endpoint + broker credentials, default vhost `/` |
| `CELERY_RESULT_BACKEND`             | Same Redis cluster as `REDIS_URL`, db `1`, with `?ssl_cert_reqs=required` — Celery refuses to start on a `rediss://` backend without it |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` | Passed through from `var.smtp_*` |
| `STRIPE_API_KEY` / `STRIPE_WEBHOOK_SECRET`  | Passed through from `var.stripe_*` |

Example `ExternalSecret` (keys are copied verbatim, so
`dataFrom` is enough):

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: lms-backend-secrets
  namespace: lms
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: aws-secretsmanager
    kind: SecretStore
  target:
    name: lms-backend-secrets
    creationPolicy: Owner
  dataFrom:
    - extract:
        key: lms/production/app
```

The two remaining app settings are **not** secrets and live in
`k8s/configmap.yaml`, where `S3_BUCKET` ships empty. Attachment upload stays
disabled until it is filled in (`S3_BUCKET` is `Optional[str] = None` in
`app/core/config.py`, and the presign endpoint refuses rather than failing
mid-upload):

```bash
terraform output k8s_configmap_additions   # S3_BUCKET, S3_REGION
```

The per-store secrets (`rds_master_secret_arn`, `redis_secret_arn`,
`mq_secret_arn`) exist for operators and for future native rotation. The
application only needs the single `app` secret above.

## Notable decisions

- **Customer-managed KMS keys, one per store.** The AWS-managed `aws/rds`
  style keys cannot be rotated on demand, cannot carry a key policy, and
  cannot be shared cross-account for a backup vault. Per-store keys also mean
  revoking the attachments key does not lock anyone out of the database.
- **RDS Multi-AZ, not a read replica.** A leave approval is a
  financial-equivalent write; the synchronous standby is what makes a
  failover lossless. Read replicas are asynchronous and would drop committed
  transactions.
- **Deletion protection + `skip_final_snapshot = false`.** One instance holds
  every tenant's HR data. An accidental destroy is unrecoverable for all of
  them at once.
- **No `sslmode`/`ssl` query parameter in `DATABASE_URL`.** asyncpg and
  psycopg2 spell that parameter differently and the app derives both driver
  URLs from the one string. TLS is enforced server-side instead, with
  `rds.force_ssl = 1` in the parameter group.
- **`DenyWrongEncryptionHeader` allows a *missing* header.** A browser
  following a presigned PUT sends no encryption header; bucket default
  encryption supplies SSE-KMS. Only an explicitly wrong value is denied — a
  blanket "header required" deny would break every upload.
- **One NAT gateway per AZ by default.** With a single shared NAT, one zonal
  outage cuts SMTP, Stripe and STS egress for pods in every AZ.
- **WAF rate limit above the app's own limiter.** Redis already enforces
  `RATE_LIMIT_PER_IP` / `RATE_LIMIT_PER_TENANT`; the WAF rule is there to drop
  floods before they consume a pod and a database connection.
- **`DeleteObject` is not granted to the pods.** Attachments are evidence for
  an approved request; removal is a lifecycle-policy decision, not an
  application one.
- **Tags come from `local.common_tags`, not provider `default_tags`.** Using
  both produces perpetual `tags_all` diffs on resources that normalise tags
  server-side.

## Not managed here

EKS cluster and node groups · Kubernetes objects (Deployments, Ingress,
ServiceAccount, External Secrets Operator) · Route53/ACM · Postgres
roles, schemas and RLS policies · SES/SMTP identity · Stripe configuration ·
the remote-state bucket and lock table.
