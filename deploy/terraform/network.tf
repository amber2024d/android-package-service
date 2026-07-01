# 两个安全组互相引用（ALB↔实例），故规则用独立 aws_vpc_security_group_*_rule 资源（先建空 SG 再挂规则），
# 避免 inline 交叉引用造成的循环依赖，也避免 inline 与独立规则混用的冲突。

resource "aws_security_group" "alb" {
  name_prefix = "${var.name_prefix}-alb-"
  description = "ALB (443/80 from clients; egress only to app on 8080)"
  vpc_id      = var.vpc_id
  tags        = { Name = "${var.name_prefix}-alb" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "instance" {
  name_prefix = "${var.name_prefix}-instance-"
  description = "App instances (8080 from ALB only; egress all)"
  vpc_id      = var.vpc_id
  tags        = { Name = "${var.name_prefix}-instance" }

  lifecycle {
    create_before_destroy = true
  }
}

# ---- ALB 入站：443/80 from clients ----
resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  for_each          = toset(var.ingress_cidrs)
  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "HTTPS from clients"
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  for_each          = toset(var.ingress_cidrs)
  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  description       = "HTTP (redirects to HTTPS)"
}

# ---- ALB 出站：仅转发到实例 8080 ----
resource "aws_vpc_security_group_egress_rule" "alb_to_instance" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.instance.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
  description                  = "Forward to app instances on 8080"
}

# ---- 实例入站：8080 仅来自 ALB ----
resource "aws_vpc_security_group_ingress_rule" "instance_from_alb" {
  security_group_id            = aws_security_group.instance.id
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
  description                  = "App port from ALB only"
}

# ---- 实例出站：全放开（拉镜像/代码、S3、上游 provider、飞书 OAuth、IMDS）----
resource "aws_vpc_security_group_egress_rule" "instance_all" {
  security_group_id = aws_security_group.instance.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
  description       = "All outbound"
}
