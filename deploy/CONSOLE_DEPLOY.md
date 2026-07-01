# AWS 网页控制台部署步骤（Spot 抢占式 + S3）

不用命令行、纯 AWS 管理控制台点击完成部署。等价于 `deploy/terraform/` 的手工版——想要可复现的 IaC 走 Terraform，想手点走本文。

架构：单实例 ELB + Spot 抢占式（被回收由 ASG 自动补机）+ S3（产物 + Litestream 备份）+ 飞书 OAuth 鉴权。SQLite 靠 Litestream 复制到 S3，跨回收不丢 API Key/账本。

## 前提（先备好）

- **域名**：飞书 OAuth 必须真实 https 回调，不能用裸 ALB DNS。例：`android-packages.example.com`。
- **部署镜像仓公开**：user-data 开机 `git clone` 走 HTTPS，公开仓库免鉴权。本文用 `https://github.com/amber2024d/android-package-service.git`，分支 `feat/cloud-migration`。
- **飞书自建应用**：拿到 App ID / App Secret，开启「网页应用 / OAuth 登录」。
- **全程同一区域**（下例 `us-west-2`）；S3 桶、ACM 证书、ALB、实例都在这个区域。
- 右上角能看到你的 **12 位账号 ID**（IAM 策略要用）。

> 约定占位：`REGION`=区域、`ACCOUNT_ID`=账号 ID、`BUCKET`=S3 桶名、`<域名>`=你的域名。

---

## 1. S3 桶（产物 + Litestream 备份）

**S3 → Create bucket**：
- Name：`android-packages-prod`（=下文 `BUCKET`），Region：`us-west-2`。
- **Block all public access 保持勾选**（私有；客户端靠服务签发的 signed URL 取产物，不需要桶公开）。
- Create。

## 2. IAM 角色（实例用）

**IAM → Roles → Create role**：
- Trusted entity = **AWS service**，Use case = **EC2** → Next → 直接创建，名字 `android-package-service-instance`。
- 进该角色 **Add permissions → Attach policies** → 勾 **AmazonSSMManagedInstanceCore**（用于 Session Manager 免密登录运维）。
- **Add permissions → Create inline policy → JSON**，粘下面（把 `BUCKET`/`REGION`/`ACCOUNT_ID` 换掉）：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "S3Artifacts", "Effect": "Allow", "Action": ["s3:GetObject","s3:PutObject"], "Resource": "arn:aws:s3:::BUCKET/artifacts/*" },
    { "Sid": "S3Litestream", "Effect": "Allow", "Action": ["s3:GetObject","s3:PutObject","s3:DeleteObject"], "Resource": "arn:aws:s3:::BUCKET/litestream/*" },
    { "Sid": "S3List", "Effect": "Allow", "Action": ["s3:ListBucket","s3:GetBucketLocation"], "Resource": "arn:aws:s3:::BUCKET" },
    { "Sid": "SsmRead", "Effect": "Allow", "Action": ["ssm:GetParameter"], "Resource": "arn:aws:ssm:REGION:ACCOUNT_ID:parameter/android-package-service/env" },
    { "Sid": "SsmKmsDecrypt", "Effect": "Allow", "Action": ["kms:Decrypt"], "Resource": "arn:aws:kms:REGION:ACCOUNT_ID:key/*", "Condition": { "StringEquals": { "kms:ViaService": "ssm.REGION.amazonaws.com" } } }
  ]
}
```

（EC2 角色会自动生成同名 instance profile，Launch Template 里直接选这个角色即可。）

## 3. ACM 证书（先启动，DNS 验证要等）

**Certificate Manager（确认区域 = us-west-2）→ Request → Request a public certificate**：
- 域名填 `<域名>`，Validation = **DNS validated** → Request。
- 进证书详情，把它给的 CNAME 记录加到你的 DNS（Route53 可一键「Create records」）→ 等状态变 **Issued**。
- 记下证书 **ARN**（第 7 步用）。

## 4. SSM 参数（存 .env.cloud 全文）

**Systems Manager → Parameter Store → Create parameter**：
- Name：`/android-package-service/env`
- Type：**SecureString**（KMS key 用默认 `alias/aws/ssm`）
- Value：粘贴你的 `.env.cloud` **全文**（以仓库 `.env.cloud.example` 为模板），关键行：

```
APP_ENV=production
PUBLIC_BASE_URL=https://<域名>
AUTH_ENABLED=true
AUTH_API_KEY_ENABLED=true
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
STORAGE_BACKEND=s3
STORAGE_PREFIX=artifacts
SIGNED_URL_TTL_SECONDS=3600
S3_BUCKET=android-packages-prod
S3_REGION=us-west-2
S3_ACCESS_KEY_ID=
S3_SECRET_ACCESS_KEY=
DOWNLOAD_JOB_LEASE_SECONDS=600
PROVIDER_GOOGLE_PLAY_ENABLED=true
```

> `S3_ACCESS_KEY_ID/SECRET` **留空** → 走实例 IAM 角色（第 2 步）。`PROVIDER_*_ENABLED` 按需开启来源。

## 5. 两个安全组

**EC2 → Security Groups → Create security group** ×2（选你的 VPC）：
- **SG-ALB**（对外）：Inbound 加 `HTTPS 443` 来源 `0.0.0.0/0`、`HTTP 80` 来源 `0.0.0.0/0`。Outbound 默认全放。
- **SG-APP**（实例）：Inbound 加 **Custom TCP 8080**，来源选 **SG-ALB**（在来源框里选另一个安全组）。Outbound 保持全放（拉镜像/代码、S3、飞书、上游 provider 都要出网）。

## 6. 目标组（Target Group）

**EC2 → Target Groups → Create target group**：
- Type = **Instances**，Protocol **HTTP**，Port **8080**，选你的 VPC。
- **Health checks**：Path = `/health`；Advanced → Success codes = `200`。
- 创建（先不注册实例，ASG 会自动挂）。

## 7. 应用负载均衡器（ALB）

**EC2 → Load Balancers → Create → Application Load Balancer**：
- Scheme **Internet-facing**；Mappings 选 **≥2 个公网子网**；Security groups = **SG-ALB**。
- Listeners：
  - **HTTPS : 443** → Default action **Forward to** 第 6 步目标组 → Default SSL/TLS certificate 选第 3 步 **ACM 证书**（Security policy 选 TLS13 系列）。
  - 建好后进 ALB → Listeners → **Add listener**：**HTTP : 80** → action **Redirect to** URL，端口 443、协议 HTTPS、状态码 301。
- 创建。记下 ALB 的 **DNS name**（第 10 步用）。

## 8. 启动模板（Launch Template）

**EC2 → Launch Templates → Create launch template**：
- AMI：**Amazon Linux 2023**（x86_64）。
- Instance type：如 `m6i.large`（Spot 在 ASG 里配，这里先不勾）。
- Key pair：可不选（用 Session Manager 登录）。
- **Network settings → Security groups**：选 **SG-APP**。
- **Advanced details**：
  - **IAM instance profile** = 第 2 步的角色。
  - **Metadata version** = **V2 only (token required)**；**Metadata response hop limit** = **2**（关键！容器要靠它拿实例角色凭据；填 1 会导致 S3/Litestream 全挂）。
  - **User data**：粘下面（把 `us-west-2` 换成你的区域；仓库地址/分支已填好）：

```bash
#!/bin/bash
set -euxo pipefail
exec > >(tee -a /var/log/user-data.log) 2>&1
dnf install -y docker git awscli
systemctl enable --now docker
install -d /usr/local/lib/docker/cli-plugins
curl -fsSL "https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-$(uname -m)" -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
install -d /opt/app
git clone --branch feat/cloud-migration https://github.com/amber2024d/android-package-service.git /opt/app
cd /opt/app
aws ssm get-parameter --region us-west-2 --name /android-package-service/env --with-decryption --query 'Parameter.Value' --output text > /opt/app/.env.cloud
chmod 600 /opt/app/.env.cloud
docker compose -f docker-compose.yml -f docker-compose.cloud.yml --env-file .env.cloud up -d --build
```

- 创建。

## 9. 自动伸缩组（ASG）——Spot + 自愈

**EC2 → Auto Scaling Groups → Create Auto Scaling group**：
- 名字 `android-package-service-asg`，选第 8 步 **Launch template**。
- Instance type requirements：开 **Override launch template / Use multiple instance types**，加 `m6i.large`/`m5.large`/`m5a.large`；购买选项 **Spot**，分配策略 **price-capacity-optimized**，On-Demand base = 0（全 Spot）。
- Network：选**实例子网**（≥2 AZ）。
- **Load balancing → Attach to an existing load balancer** → 选第 6 步的目标组。
- **Health checks**：勾 **Turn on Elastic Load Balancing health checks**；**Health check grace period = 300**（首启要现构建镜像 + Litestream 恢复，给足时间）。
- Group size：**Desired 1 / Min 1 / Max 1**（单实例架构，**不要调大**：SQLite + Litestream 是单写者，多实例会破坏一致性）。
- 创建。ASG 会自动起一台实例并跑 user-data。

## 10. DNS + 飞书回调

- 你的 DNS：把 `<域名>` 用 **Route53 Alias**（或 CNAME）指到第 7 步 ALB 的 DNS name。
- 飞书开放平台「安全设置 → 重定向 URL」登记 `https://<域名>/auth/callback`（**逐字一致**，含协议/域名/路径）。

## 11. 验证

- 首启现构建镜像，**等约 3–5 分钟**。**EC2 → Target Groups → 你的 TG → Targets** 里实例状态变 **healthy**。
- 浏览器 `https://<域名>/health` → `{"status":"ok"}`。
- 开 `https://<域名>/` → 飞书 OAuth 登录，**第一个登录者即管理员** → 到 `/admin` 建一个 API Key。
- 拿 Key 验证数据 API：`https://<域名>/api/v1/android/apps/org.fdroid.fdroid?provider=fake`，带请求头 `Authorization: Bearer <key>` → 200。
- （可选）本地跑 smoke：`API_KEY=<key> BASE_URL=https://<域名> scripts/smoke.sh`。

---

## 故障排查

实例起不来 / 目标组一直 unhealthy → **Systems Manager → Session Manager → Start session** 登进实例看日志：

```sh
sudo cat /var/log/user-data.log          # 开机脚本哪步失败
cd /opt/app && sudo docker compose ps    # 容器状态
sudo docker compose logs litestream      # Litestream 恢复/S3 权限/桶名
sudo docker compose logs android-package-service
```

最常见的坑：

| 症状 | 原因 / 处理 |
| --- | --- |
| clone 失败 | 部署镜像仓不是 public → 设为 public，或 user-data 加 GitHub token |
| S3/Litestream 全报权限/超时 | Launch Template 的 **IMDS hop limit 不是 2** → 容器拿不到实例角色凭据；或 IAM 策略桶名/区域填错 |
| 飞书登录回调 400 | 重定向 URL 没在飞书**逐字**登记，或 `PUBLIC_BASE_URL` 与域名不一致 |
| 目标组 unhealthy 反复重启 | grace period 太短（用 300）、或镜像构建/依赖拉取慢；看 user-data.log |
| 证书选不到 | ACM 证书不在 ALB 同区域，或还没 Issued |

## 与 Terraform 的关系

本文是手工版；`deploy/terraform/` 是等价的 IaC 版（`tofu apply` 一键建同一套资源）。两者二选一，别同时管理同一批资源。云迁移与抢占式适配的设计见 [部署设计 · 云上部署](../develop/android-package-service-deployment.md#云上部署aws-spot--s3--鉴权阶段-23) 与 [云迁移设计 §5](../develop/android-package-service-cloud-migration-design.md)。
