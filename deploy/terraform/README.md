# AWS Spot 部署基础设施（Terraform 参考骨架）

把 android-package-service 跑在 **AWS Spot 抢占式实例 + S3** 上所需的云资源模板。**这是可参考的骨架、不是开箱即用**：带 `<PLACEHOLDER>` 的变量必须换成你环境里的真实值，且假设了一些既有资源（VPC/子网/证书/S3 桶）。

它和应用的边界：应用侧「机器上怎么跑起来」由仓库根的 `docker-compose.cloud.yml` + `deploy/litestream.yml` 负责；本目录负责「机器本身怎么被造出来、被 Spot 抢了谁补」。

## 创建什么

| 资源 | 说明 |
| --- | --- |
| IAM 角色 + 实例配置 | 给实例挂 S3 读写（产物 + litestream 备份）+ SSM 读配置 + SSM Session Manager 运维 |
| 安全组 ×2 | ALB（对外 443/80）；实例（仅 ALB 可达 8080） |
| ALB + 目标组 + 监听 | HTTPS(443, ACM 证书) 转发到实例 8080，健康检查 `/health`；80→443 重定向 |
| Launch Template | AMI/机型/IAM/安全组 + **IMDS hop-limit=2** + user-data 开机脚本 |
| Auto Scaling Group | Spot 混合策略、多机型、`capacity_rebalance`、`health_check_type=ELB`；维持 `desired`，实例被抢即自动补 |
| SSM SecureString 参数 | 存 `.env.cloud` 全文（密钥带外填入，terraform 不覆盖） |

**自愈链路**：Spot 回收 → ASG 发现实例少了 → 按 Launch Template 造新机 → user-data 装 docker + `docker compose up` → litestream 从 S3 恢复 SQLite → 服务恢复（API Key/账本还在）。

## 先决条件（本骨架不创建、需你已有）

- 一个 **VPC** + 公网子网（ALB 用）+ 实例子网（可私网），跨 ≥2 AZ。
- **S3 桶**（产物 + litestream 备份共用；`artifacts/` 与 `litestream/` 前缀分开）。
- **ACM 证书**（你的域名，用于 ALB HTTPS）。
- **域名**（Route53 或其它 DNS），apply 后指向 ALB。
- 应用代码仓库可被实例 `git clone`（公开，或在 AMI/脚本里配好凭据）。
- 实例出口：装 docker/拉代码/访问 S3、上游 provider、飞书 OAuth 都需外网。私网子网建议用 **NAT 网关**（最省事、全覆盖）；若走 VPC Endpoint 则 S3 Gateway Endpoint 只解决 S3，其余（GitHub/ECR/飞书等）仍需 NAT 或对应 Interface Endpoint。也可直接用公网子网 + 公网 IP。

## 需要你填的占位（`terraform.tfvars`）

| 变量 | 填什么 |
| --- | --- |
| `vpc_id` | 你的 VPC ID |
| `public_subnet_ids` | ALB 的公网子网（≥2 AZ） |
| `instance_subnet_ids` | 实例子网（≥2 AZ） |
| `ami_id` | Amazon Linux 2023 x86_64 AMI（或你烘好的自定义 AMI） |
| `artifacts_bucket` | 已存在的 S3 桶名 |
| `acm_certificate_arn` | 你域名的 ACM 证书 ARN（须与 `aws_region` 同区） |
| `app_repo_url` | 应用代码仓库 URL |
| `ingress_cidrs` | 允许访问 ALB 的来源（建议收窄） |
| `instance_types` / `on_demand_*` | Spot 机型与保底比例（默认全 Spot、多机型） |

## 部署步骤

```sh
cd deploy/terraform
cp terraform.tfvars.example terraform.tfvars   # 填占位
terraform init
terraform plan
terraform apply

# apply 后：把 .env.cloud 全文写进 SSM 参数（terraform 只建了占位、不含密钥）
#   .env.cloud 内容参考仓库根 .env.cloud.example；PUBLIC_BASE_URL 用你的 https 域名、
#   STORAGE_BACKEND=s3 + S3_BUCKET=<artifacts_bucket> + S3_REGION=<aws_region>、
#   S3_ACCESS_KEY_ID/SECRET 留空（走实例角色）、FEISHU_APP_ID/SECRET 填飞书自建应用凭据。
# --value file://<绝对路径>（file:// + 绝对路径；如当前目录下用 file://$(pwd)/.env.cloud）
aws ssm put-parameter --region <aws_region> \
  --name "$(terraform output -raw config_ssm_parameter)" \
  --type SecureString --overwrite --value "file://$(pwd)/.env.cloud"

# 把域名 DNS 指到 ALB
terraform output alb_dns_name    # 在 Route53 建 alias/CNAME 指向它
```

首次上线后：浏览器访问 `https://<域名>/` → 飞书 OAuth 登录（**第一个登录者即管理员**）→ 到 `/admin` 创建 API Key → 用它跑 `API_KEY=... BASE_URL=https://<域名> ../../scripts/smoke.sh`。

## 重要说明与取舍

- **单实例架构**：`asg_desired=1`。SQLite + litestream 是单写者模型，**不支持多实例并发写**——勿把 desired 调 >1 做 HA（会让 auth/catalog 分叉）。要多实例高可用需先把状态层换成托管 DB（见云迁移设计 §5.3）。滚动更新（instance_refresh）短暂起第二台，靠 litestream 保证新机状态一致、旧机停前刷 WAL。
- **中断窗口**：Spot 被抢到新机就绪有约 1–几分钟不可用，期间请求失败——服务的 `/discover` 已告知调用方按退避重试；下载任务幂等，会在新机续下。
- **开机构建耗时**：user-data 用 `docker compose up --build` 在实例上现构建镜像（`pip install '.[s3,gcs]'` + playwright 基镜像，较慢）。`health_check_grace_period=300s` 已留余量；**生产建议改为预烘镜像**（把镜像推到 ECR、compose 用 `image:` 拉取而非 `build:`），缩短新机就绪时间、去掉开机对 pip/网络的依赖。
- **无重叠单写者**：`capacity_rebalance=false` + `max_size=1` 确保 Spot 回收/滚动更新时**绝不两台同时跑**（否则两个 litestream 会同时写同一 S3 副本、破坏单写者）。代价是回收时先停再起、有短暂停机。
- **PUBLIC_BASE_URL / 飞书回调**：必须与 ACM 证书域名一致，并在飞书开放平台「安全设置 → 重定向 URL」登记 `https://<域名>/auth/callback`。
- **凭据**：实例 IAM 角色供 app(boto3) 与 litestream 用 S3，`.env.cloud` 里 S3 key 留空即可；IMDS hop-limit=2 已在 launch template 配好，容器才能取到角色凭据。
- **state**：骨架默认本地 state；生产请在 `versions.tf` 改远端 state（S3 + DynamoDB 锁）。
- **成本/清理**：ALB + NAT + Spot 会产生费用；`terraform destroy` 可拆除本骨架创建的资源（不含你既有的 VPC/桶/证书）。
