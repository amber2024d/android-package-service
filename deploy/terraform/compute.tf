resource "aws_launch_template" "this" {
  name_prefix   = "${var.name_prefix}-"
  image_id      = var.ami_id
  instance_type = var.instance_types[0] # ASG mixed policy 用 overrides 覆盖为多机型
  key_name      = var.key_name

  iam_instance_profile {
    arn = aws_iam_instance_profile.instance.arn
  }

  vpc_security_group_ids = [aws_security_group.instance.id]

  # 容器需经 IMDS 取实例角色凭证：hop-limit=2（容器多一跳），并强制 IMDSv2。
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  user_data = base64encode(templatefile("${path.module}/user_data.sh.tftpl", {
    aws_region      = var.aws_region
    config_ssm_name = aws_ssm_parameter.env.name
    app_repo_url    = var.app_repo_url
    app_repo_ref    = var.app_repo_ref
  }))

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = var.name_prefix }
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_autoscaling_group" "this" {
  name                = "${var.name_prefix}-asg"
  vpc_zone_identifier = var.instance_subnet_ids
  desired_capacity    = var.asg_desired
  min_size            = var.asg_min
  max_size            = var.asg_max
  target_group_arns   = [aws_lb_target_group.this.arn]
  health_check_type   = "ELB"
  # 覆盖开机 + user-data（装 docker/拉码/首次 compose build）+ litestream 恢复 + 起容器；build 较慢，给足 5min。
  health_check_grace_period = 300
  # 单写者约束：capacity_rebalance 会在 Spot 预警时**提前多起一台**，两台会同时写同一份 S3 上的
  # litestream 副本（litestream 无租约、单写者），故关闭；配合 max_size=1，回收后是「先终止再补机」，永不重叠。
  capacity_rebalance = false

  mixed_instances_policy {
    instances_distribution {
      on_demand_base_capacity                  = var.on_demand_base
      on_demand_percentage_above_base_capacity = var.on_demand_percentage
      spot_allocation_strategy                 = "price-capacity-optimized"
    }

    launch_template {
      launch_template_specification {
        launch_template_id = aws_launch_template.this.id
        version            = "$Latest"
      }

      dynamic "override" {
        for_each = var.instance_types
        content {
          instance_type = override.value
        }
      }
    }
  }

  # 改 launch template 后滚动替换实例。单实例（max_size=1）+ min_healthy_percentage=0：
  # ASG 先终止旧实例、再起新实例（有短暂停机，但绝不两台同时跑 → 不破坏 litestream 单写者）。
  instance_refresh {
    strategy = "Rolling"
    preferences {
      min_healthy_percentage = 0
    }
  }

  tag {
    key                 = "Name"
    value               = var.name_prefix
    propagate_at_launch = true
  }
}
