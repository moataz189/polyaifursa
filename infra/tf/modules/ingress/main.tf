data "aws_route53_zone" "this" {
  name         = var.route53_zone_name
  private_zone = false
}

locals {
  # AWS caps ALB and Target Group names at 32 characters, which project_name
  # alone can already approach. Derive a short, deterministic prefix instead
  # of concatenating the full project_name onto these two resources.
  short_name_prefix = substr(var.project_name, 0, min(20, length(var.project_name)))
}

# =========================================================
# ALB Security Group
# =========================================================

resource "aws_security_group" "alb" {
  name        = "${var.project_name}-alb-sg"
  description = "Security group for the internet-facing ALB"
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTPS from the internet"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "Allow all outbound traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-alb-sg"
  }
}

# Allow the ALB to reach ingress-nginx's fixed HTTP NodePort on every worker.
resource "aws_security_group_rule" "workers_from_alb_http_node_port" {
  type                     = "ingress"
  description              = "ingress-nginx HTTP NodePort from the ALB"
  from_port                = var.worker_http_node_port
  to_port                  = var.worker_http_node_port
  protocol                 = "tcp"
  security_group_id        = var.worker_security_group_id
  source_security_group_id = aws_security_group.alb.id
}

# =========================================================
# ACM Certificate (DNS validated in the looked-up zone)
# =========================================================

resource "aws_acm_certificate" "this" {
  domain_name               = var.hostnames[0]
  subject_alternative_names = slice(var.hostnames, 1, length(var.hostnames))
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Name = "${var.project_name}-alb-cert"
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.this.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      type   = dvo.resource_record_type
      record = dvo.resource_record_value
    }
  }

  zone_id         = data.aws_route53_zone.this.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "this" {
  certificate_arn         = aws_acm_certificate.this.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# =========================================================
# Application Load Balancer
# =========================================================

resource "aws_lb" "this" {
  name               = "${local.short_name_prefix}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids

  tags = {
    Name = "${var.project_name}-alb"
  }
}

resource "aws_lb_target_group" "http_node_port" {
  name        = "${local.short_name_prefix}-ingress-tg"
  port        = var.worker_http_node_port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "instance"

  # Health check hits the ingress-nginx data-plane port directly (not the
  # operator-only 10254 metrics port). ALB health checks never send a Host
  # header matching one of our configured Ingress hosts, so the request
  # deterministically falls through to nginx's built-in default backend,
  # which always returns exactly 404 whenever nginx itself is alive and
  # serving. Matcher is exact (404), not a guessed range.
  health_check {
    protocol            = "HTTP"
    port                = "traffic-port"
    path                = "/"
    matcher             = "404"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }

  tags = {
    Name = "${var.project_name}-ingress-tg"
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.this.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.http_node_port.arn
  }
}

resource "aws_autoscaling_attachment" "workers" {
  autoscaling_group_name = var.worker_asg_name
  lb_target_group_arn    = aws_lb_target_group.http_node_port.arn
}

# =========================================================
# Route53 alias records -> ALB (one per public hostname)
# =========================================================

resource "aws_route53_record" "alb_alias" {
  for_each = toset(var.hostnames)

  zone_id = data.aws_route53_zone.this.zone_id
  name    = each.value
  type    = "A"

  alias {
    name                   = aws_lb.this.dns_name
    zone_id                = aws_lb.this.zone_id
    evaluate_target_health = true
  }
}
