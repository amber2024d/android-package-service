# 无 default 的变量 = 必须填（见 terraform.tfvars.example 的 <PLACEHOLDER>）。

variable "aws_region" {
  description = "AWS 区域"
  type        = string
  default     = "us-west-2"
}

variable "name_prefix" {
  description = "资源命名前缀"
  type        = string
  default     = "android-package-service"
}

# ---- 网络（引用你 VPC 里的现成资源）----
variable "vpc_id" {
  description = "现有 VPC ID"
  type        = string
}

variable "public_subnet_ids" {
  description = "ALB 用的公网子网（跨 ≥2 AZ）"
  type        = list(string)
}

variable "instance_subnet_ids" {
  description = "应用实例用的子网（可私网，跨 ≥2 AZ 分散 Spot 被抢概率）"
  type        = list(string)
}

variable "ingress_cidrs" {
  description = "允许访问 ALB 443/80 的来源 CIDR；建议按需收窄"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

# ---- 实例 ----
variable "ami_id" {
  description = "基础 AMI（Amazon Linux 2023 x86_64，或你烘好的自定义 AMI）"
  type        = string
}

variable "instance_types" {
  description = "候选机型（Spot 多机型分散被抢概率）；第一个也作 launch template 默认机型"
  type        = list(string)
  default     = ["m6i.large", "m5.large", "m5a.large"]
}

variable "key_name" {
  description = "SSH key 名（可选，null 表示不配）"
  type        = string
  default     = null
}

# ---- Spot / 容量 ----
variable "on_demand_base" {
  description = "On-Demand 保底实例数（0 = 全 Spot，最省钱、可容忍中断窗口；调 1 = 保底一台不被抢，换更高可用，成本↑）"
  type        = number
  default     = 0
}

variable "on_demand_percentage" {
  description = "超出 base 后 On-Demand 占比（0 = 全 Spot）"
  type        = number
  default     = 0
}

# 单实例架构：SQLite + litestream 是单写者，两台同时跑会破坏一致性。desired=min=max=1 保证任何时刻至多一台，
# 回收/滚动更新都是「先终止再补机」（有短暂停机，无重叠）。**勿把 max 调 >1 做 HA**（需先换托管 DB 状态层）。
variable "asg_desired" {
  type    = number
  default = 1
}

variable "asg_min" {
  type    = number
  default = 1
}

variable "asg_max" {
  type    = number
  default = 1
}

# ---- 存储 ----
variable "artifacts_bucket" {
  description = "S3 桶名（产物 <storage_prefix>/ + litestream/ 备份共用，须已存在）"
  type        = string
}

variable "storage_prefix" {
  description = "桶内产物根前缀，须与应用 STORAGE_PREFIX（.env.cloud）一致"
  type        = string
  default     = "artifacts"
}

# ---- HTTPS ----
variable "acm_certificate_arn" {
  description = "ACM 证书 ARN（ALB 443 监听用）"
  type        = string
}

# ---- 应用代码来源（user-data 里 git clone）----
variable "app_repo_url" {
  description = "应用代码仓库 URL（user-data 开机 clone）"
  type        = string
}

variable "app_repo_ref" {
  description = "分支/标签"
  type        = string
  default     = "main"
}

# ---- 配置参数 ----
variable "config_ssm_prefix" {
  description = ".env.cloud 全文所存的 SSM SecureString 参数前缀（实际参数为 <prefix>/env）"
  type        = string
  default     = "/android-package-service"
}
