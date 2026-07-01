output "alb_dns_name" {
  description = "ALB 域名——把你的 DNS（Route53 alias / CNAME）指到它，PUBLIC_BASE_URL 用该域名的 https"
  value       = aws_lb.this.dns_name
}

output "asg_name" {
  description = "Auto Scaling Group 名"
  value       = aws_autoscaling_group.this.name
}

output "instance_role_arn" {
  description = "实例 IAM 角色 ARN（S3 读写 + SSM）"
  value       = aws_iam_role.instance.arn
}

output "config_ssm_parameter" {
  description = ".env.cloud 全文所存的 SSM 参数名——apply 后用 put-parameter 填真实内容"
  value       = aws_ssm_parameter.env.name
}
