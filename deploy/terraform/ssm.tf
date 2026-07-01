# .env.cloud 全文存为一个 SSM SecureString 参数；user-data 开机拉取写盘。
# 骨架用占位值创建；真实内容（含飞书密钥、S3 桶等）请在 apply 后带外填入，terraform 不覆盖（见 lifecycle）：
#   aws ssm put-parameter --name /android-package-service/env --type SecureString \
#     --value file://.env.cloud --overwrite
resource "aws_ssm_parameter" "env" {
  name        = "${var.config_ssm_prefix}/env"
  description = "android-package-service .env.cloud 全文"
  type        = "SecureString"
  value       = "PLACEHOLDER_SET_ME_VIA_CLI"

  lifecycle {
    ignore_changes = [value]
  }
}
