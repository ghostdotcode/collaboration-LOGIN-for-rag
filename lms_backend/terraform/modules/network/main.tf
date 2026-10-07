data "aws_region" "current" {}

data "aws_availability_zones" "available" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  azs          = slice(data.aws_availability_zones.available.names, 0, var.availability_zone_count)
  subnet_count = var.create_vpc ? var.availability_zone_count : 0
  nat_count    = var.create_vpc ? (var.single_nat_gateway ? 1 : var.availability_zone_count) : 0

  # /16 split into /20s: 0-2 public, 4-6 app, 8-10 data. The gaps leave room
  # for a fourth AZ or an extra tier later without renumbering anything.
  public_cidrs = [for i in range(var.availability_zone_count) : cidrsubnet(var.vpc_cidr, 4, i)]
  app_cidrs    = [for i in range(var.availability_zone_count) : cidrsubnet(var.vpc_cidr, 4, i + 4)]
  data_cidrs   = [for i in range(var.availability_zone_count) : cidrsubnet(var.vpc_cidr, 4, i + 8)]

  vpc_id = var.create_vpc ? aws_vpc.this[0].id : var.existing_vpc_id

  public_subnet_ids = var.create_vpc ? aws_subnet.public[*].id : var.existing_public_subnets
  app_subnet_ids    = var.create_vpc ? aws_subnet.app[*].id : var.existing_app_subnets
  data_subnet_ids   = var.create_vpc ? aws_subnet.data[*].id : var.existing_data_subnets

  # The AWS Load Balancer Controller discovers subnets by tag; without these,
  # creating an Ingress fails with a subnet discovery error rather than
  # anything that points at the VPC.
  eks_cluster_tag  = { "kubernetes.io/cluster/${var.eks_cluster_name}" = "shared" }
  public_k8s_tags  = merge(local.eks_cluster_tag, { "kubernetes.io/role/elb" = "1" })
  private_k8s_tags = merge(local.eks_cluster_tag, { "kubernetes.io/role/internal-elb" = "1" })
}

# Read the CIDR back from the API rather than from var.vpc_cidr: when
# create_vpc is false that variable does not describe the VPC actually in use.
data "aws_vpc" "selected" {
  id = local.vpc_id
}

# ─────────────────────────────────────────────────────────────────────────
# VPC, subnets, gateways
# ─────────────────────────────────────────────────────────────────────────

# DNS support and hostnames are both required for pods to resolve the RDS,
# ElastiCache and Amazon MQ endpoints.
resource "aws_vpc" "this" {
  count = var.create_vpc ? 1 : 0

  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(var.tags, { Name = "${var.name_prefix}-vpc" })
}

# The default security group allows all traffic between its members. Managing
# it here with no rule blocks is the only way to make it deny-all, which
# covers any ENI that lands on it by accident.
resource "aws_default_security_group" "this" {
  count = var.create_vpc ? 1 : 0

  vpc_id = aws_vpc.this[0].id

  tags = merge(var.tags, { Name = "${var.name_prefix}-default-do-not-use" })
}

resource "aws_internet_gateway" "this" {
  count = var.create_vpc ? 1 : 0

  vpc_id = aws_vpc.this[0].id

  tags = merge(var.tags, { Name = "${var.name_prefix}-igw" })
}

resource "aws_subnet" "public" {
  count = local.subnet_count

  vpc_id                  = aws_vpc.this[0].id
  availability_zone       = local.azs[count.index]
  cidr_block              = local.public_cidrs[count.index]
  map_public_ip_on_launch = false

  tags = merge(var.tags, local.public_k8s_tags, {
    Name = "${var.name_prefix}-public-${local.azs[count.index]}"
    Tier = "public"
  })
}

resource "aws_subnet" "app" {
  count = local.subnet_count

  vpc_id            = aws_vpc.this[0].id
  availability_zone = local.azs[count.index]
  cidr_block        = local.app_cidrs[count.index]

  tags = merge(var.tags, local.private_k8s_tags, {
    Name = "${var.name_prefix}-app-${local.azs[count.index]}"
    Tier = "private-app"
  })
}

# The data tier carries no kubernetes.io tags on purpose: neither a node group
# nor a load balancer should ever be placed alongside the databases.
resource "aws_subnet" "data" {
  count = local.subnet_count

  vpc_id            = aws_vpc.this[0].id
  availability_zone = local.azs[count.index]
  cidr_block        = local.data_cidrs[count.index]

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-data-${local.azs[count.index]}"
    Tier = "private-data"
  })
}

resource "aws_eip" "nat" {
  count = local.nat_count

  domain = "vpc"

  tags = merge(var.tags, { Name = "${var.name_prefix}-nat-${count.index}" })
}

# One NAT gateway per AZ by default: with a single shared NAT, a zonal outage
# cuts SMTP, Stripe and STS egress for pods in every AZ at once.
resource "aws_nat_gateway" "this" {
  count = local.nat_count

  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = merge(var.tags, { Name = "${var.name_prefix}-nat-${count.index}" })

  depends_on = [aws_internet_gateway.this]
}

# ─────────────────────────────────────────────────────────────────────────
# Routing
# ─────────────────────────────────────────────────────────────────────────

resource "aws_route_table" "public" {
  count = var.create_vpc ? 1 : 0

  vpc_id = aws_vpc.this[0].id

  tags = merge(var.tags, { Name = "${var.name_prefix}-public" })
}

resource "aws_route" "public_default" {
  count = var.create_vpc ? 1 : 0

  route_table_id         = aws_route_table.public[0].id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this[0].id
}

resource "aws_route_table_association" "public" {
  count = local.subnet_count

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public[0].id
}

# One private route table per AZ so each AZ can egress through its own NAT.
resource "aws_route_table" "private" {
  count = local.subnet_count

  vpc_id = aws_vpc.this[0].id

  tags = merge(var.tags, { Name = "${var.name_prefix}-private-${local.azs[count.index]}" })
}

resource "aws_route" "private_default" {
  count = local.subnet_count

  route_table_id         = aws_route_table.private[count.index].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = var.single_nat_gateway ? aws_nat_gateway.this[0].id : aws_nat_gateway.this[count.index].id
}

resource "aws_route_table_association" "app" {
  count = local.subnet_count

  subnet_id      = aws_subnet.app[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

resource "aws_route_table_association" "data" {
  count = local.subnet_count

  subnet_id      = aws_subnet.data[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# Gateway endpoint rather than an interface endpoint: attachment traffic then
# never crosses the NAT gateway, which removes both the per-GB NAT charge and
# a public egress path for medical certificates.
resource "aws_vpc_endpoint" "s3" {
  count = var.create_vpc ? 1 : 0

  vpc_id            = aws_vpc.this[0].id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.private[*].id

  tags = merge(var.tags, { Name = "${var.name_prefix}-s3-endpoint" })
}

# ─────────────────────────────────────────────────────────────────────────
# Flow logs
# ─────────────────────────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "flow_logs" {
  count = var.create_vpc && var.enable_flow_logs ? 1 : 0

  name              = "/aws/vpc/${var.name_prefix}/flow-logs"
  retention_in_days = var.flow_log_retention_days

  tags = var.tags
}

data "aws_iam_policy_document" "flow_logs_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "flow_logs" {
  statement {
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]

    resources = ["arn:aws:logs:*:*:log-group:/aws/vpc/${var.name_prefix}/*"]
  }
}

resource "aws_iam_role" "flow_logs" {
  count = var.create_vpc && var.enable_flow_logs ? 1 : 0

  name               = "${var.name_prefix}-vpc-flow-logs"
  assume_role_policy = data.aws_iam_policy_document.flow_logs_assume.json

  tags = var.tags
}

resource "aws_iam_role_policy" "flow_logs" {
  count = var.create_vpc && var.enable_flow_logs ? 1 : 0

  name   = "write-flow-logs"
  role   = aws_iam_role.flow_logs[0].id
  policy = data.aws_iam_policy_document.flow_logs.json
}

# REJECT-only: accepted traffic is already visible in ALB/application logs,
# and rejects are what matter for spotting lateral movement without paying to
# store every packet flow in a 50-pod deployment.
resource "aws_flow_log" "this" {
  count = var.create_vpc && var.enable_flow_logs ? 1 : 0

  vpc_id                   = aws_vpc.this[0].id
  traffic_type             = "REJECT"
  log_destination_type     = "cloud-watch-logs"
  log_destination          = aws_cloudwatch_log_group.flow_logs[0].arn
  iam_role_arn             = aws_iam_role.flow_logs[0].arn
  max_aggregation_interval = 60

  tags = merge(var.tags, { Name = "${var.name_prefix}-flow-logs" })
}

# ─────────────────────────────────────────────────────────────────────────
# Security groups
#
# Strictly tiered: only the app SG may reach a data store, and each store
# exposes exactly one port. Rules are separate resources rather than inline
# blocks so groups can reference each other without a dependency cycle.
# ─────────────────────────────────────────────────────────────────────────

resource "aws_security_group" "app" {
  name        = "${var.name_prefix}-app"
  description = "LMS API pods, Celery workers and accrual CronJob"
  vpc_id      = local.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-app" })
}

resource "aws_vpc_security_group_ingress_rule" "app_from_alb_sg" {
  count = var.alb_security_group_id != "" ? 1 : 0

  security_group_id            = aws_security_group.app.id
  description                  = "ALB to uvicorn"
  ip_protocol                  = "tcp"
  from_port                    = var.app_container_port
  to_port                      = var.app_container_port
  referenced_security_group_id = var.alb_security_group_id

  tags = var.tags
}

resource "aws_vpc_security_group_ingress_rule" "app_from_vpc" {
  count = var.alb_security_group_id == "" ? 1 : 0

  security_group_id = aws_security_group.app.id
  description       = "In-VPC ingress to uvicorn (ALB SG not yet known)"
  ip_protocol       = "tcp"
  from_port         = var.app_container_port
  to_port           = var.app_container_port
  cidr_ipv4         = data.aws_vpc.selected.cidr_block

  tags = var.tags
}

# Pods need broad egress: Postgres, Redis, AMQPS, S3, STS, the SMTP relay and
# Stripe. Pinning that to CIDRs is not maintainable against SaaS endpoints, so
# the enforcement point is the ingress side of each data-store SG instead.
resource "aws_vpc_security_group_egress_rule" "app_all" {
  security_group_id = aws_security_group.app.id
  description       = "All egress"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"

  tags = var.tags
}

resource "aws_security_group" "rds" {
  name        = "${var.name_prefix}-rds"
  description = "PostgreSQL 16 - reachable only from the LMS app SG"
  vpc_id      = local.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-rds" })
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_app" {
  security_group_id            = aws_security_group.rds.id
  description                  = "asyncpg / psycopg2 from API, workers and migrations"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.app.id

  tags = var.tags
}

resource "aws_security_group" "redis" {
  name        = "${var.name_prefix}-redis"
  description = "ElastiCache Redis - reachable only from the LMS app SG"
  vpc_id      = local.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-redis" })
}

resource "aws_vpc_security_group_ingress_rule" "redis_from_app" {
  security_group_id            = aws_security_group.redis.id
  description                  = "Rate limiter, session cache and Celery result backend"
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  referenced_security_group_id = aws_security_group.app.id

  tags = var.tags
}

resource "aws_security_group" "mq" {
  name        = "${var.name_prefix}-mq"
  description = "Amazon MQ RabbitMQ - reachable only from the LMS app SG"
  vpc_id      = local.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-mq" })
}

resource "aws_vpc_security_group_ingress_rule" "mq_amqps_from_app" {
  security_group_id            = aws_security_group.mq.id
  description                  = "Celery producers and consumers over AMQPS"
  ip_protocol                  = "tcp"
  from_port                    = 5671
  to_port                      = 5671
  referenced_security_group_id = aws_security_group.app.id

  tags = var.tags
}

# Amazon MQ serves the RabbitMQ management console on 443. The broker is not
# publicly accessible, so operators reach it by port-forward from a pod.
resource "aws_vpc_security_group_ingress_rule" "mq_console_from_app" {
  security_group_id            = aws_security_group.mq.id
  description                  = "RabbitMQ management console"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.app.id

  tags = var.tags
}
