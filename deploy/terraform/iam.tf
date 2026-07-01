data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name               = "${var.name_prefix}-instance"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "instance" {
  # S3 按前缀最小权限（同一个桶）：
  # - artifacts/：应用读写产物（上传 + 复用探测 head/get；下发是客户端拿 signed URL 直取，不经实例）。无需 Delete。
  # - litestream/：litestream 需读写 + 删（滚动清理旧 WAL/快照）。
  statement {
    sid       = "S3ArtifactsReadWrite"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["arn:aws:s3:::${var.artifacts_bucket}/${var.storage_prefix}/*"]
  }
  statement {
    sid       = "S3LitestreamReadWrite"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::${var.artifacts_bucket}/litestream/*"]
  }
  statement {
    sid       = "S3BucketList"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["arn:aws:s3:::${var.artifacts_bucket}"]
  }
  # SSM：读取 .env.cloud 配置参数（SecureString）。
  statement {
    sid       = "SsmReadConfig"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.env.arn]
  }
  # SecureString 解密：调用方（实例角色）用 --with-decryption 时会直接向 KMS 发 Decrypt，
  # 即使用默认托管密钥 alias/aws/ssm 也需显式 kms:Decrypt（用 ViaService 约束到仅经 SSM）。
  # 若改用自建 CMK，把 resources 换成该 key ARN 即可。
  statement {
    sid       = "SsmKmsDecrypt"
    actions   = ["kms:Decrypt"]
    resources = ["arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:key/*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.aws_region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "instance" {
  name   = "${var.name_prefix}-instance"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.instance.json
}

# 便于用 SSM Session Manager 远程运维（免开 22 端口）。
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "instance" {
  name = "${var.name_prefix}-instance"
  role = aws_iam_role.instance.name
}
