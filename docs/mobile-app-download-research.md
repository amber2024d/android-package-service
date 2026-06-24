# mobile-app-download Android 实现调研

## 摘要

`mobile-app-download` 是当前工作区里的独立 Skill/CLI：

```text
mobile-app-download/
```

本文只调研 Android 下载实现。

Android 核心策略：

1. 默认先走 Aurora 匿名 Google Play 协议。
2. Aurora 失败后自动降级 APKPure。
3. 支持强制指定来源：

   ```text
   --source aurora
   --source apkpure
   ```

和前两份调研的关系：

- `docs/gplayapi-download-research.md` 研究的是 Aurora Store 里 Android/Kotlin 版
  `com.auroraoss:gplayapi`。
- `docs/aptoide-mcp-download-research.md` 研究的是 Aptoide V7 API，详情响应里直接给
  `file.path`、`aab.splits` 等下载字段。
- 本文研究的 `mobile-app-download` Android 主路是 Python `gpapi` + Aurora dispenser，
  本质仍然是 Google Play 下载；APKPure 是兜底来源。当前没有使用 Aptoide。

## 项目结构

关键文件：

```text
mobile-app-download/SKILL.md
mobile-app-download/README.md
mobile-app-download/scripts/cli.py
mobile-app-download/scripts/android/orchestrator.py
mobile-app-download/scripts/android/aurora.py
mobile-app-download/scripts/android/_dispenser.py
mobile-app-download/scripts/android/_aurora_headers.py
mobile-app-download/scripts/android/apkpure.py
mobile-app-download/scripts/android/_metadata.py
mobile-app-download/scripts/core/config.py
mobile-app-download/scripts/core/exceptions.py
mobile-app-download/scripts/core/naming.py
mobile-app-download/references/android-aurora.md
mobile-app-download/references/android-apkpure.md
```

Android 依赖：

```text
gpapi>=0.4.4
pytest>=8.0
```

## CLI 入口

入口文件：

```text
mobile-app-download/scripts/cli.py
```

Android 调用：

```text
python3 cli.py android --package <pkg> [--version V] [--source aurora|apkpure] [--no-fallback]
```

示例：

```text
python3 cli.py android --package org.fdroid.fdroid
python3 cli.py android --package org.fdroid.fdroid --source apkpure
python3 cli.py android --package com.discord --source aurora --no-fallback
```

## 输出与命名

输出目录由 `--output-base` 决定，默认是当前工作目录：

```text
<base>/output/apks/
<base>/.cache/
```

文件名规则在 `core/naming.py`：

```text
产品名_包名_V版本号_方案名.{apk|xapk}
```

示例：

```text
Telegram_org.telegram.messenger_V10.4.0_aurora.apk
Discord_com.discord_V214.4_apkpure.xapk
```

写文件时使用 `.part` 临时文件，下载成功后 rename 到最终文件名。

## Android 编排逻辑

文件：

```text
mobile-app-download/scripts/android/orchestrator.py
```

默认流程：

1. 调用 `aurora.download()`。
2. 如果抛 `NotFoundError` 或 `NetworkError`，且没有关闭 fallback，则调用
   `apkpure.download()`。
3. 如果抛 `AuthError` 或 `ConfigError`，不 fallback。

fallback 触发条件：

```text
NotFoundError
NetworkError
```

不触发 fallback：

```text
AuthError
ConfigError
```

强制单源：

```text
--source aurora
--source apkpure
```

关闭 fallback：

```text
--no-fallback
```

## Aurora 路径

相关文件：

```text
mobile-app-download/scripts/android/aurora.py
mobile-app-download/scripts/android/_dispenser.py
mobile-app-download/scripts/android/_aurora_headers.py
mobile-app-download/references/android-aurora.md
```

### 基本链路

Aurora 路径使用 Python `gpapi`：

```python
from gpapi.googleplay import GooglePlayAPI
```

该依赖来自 PyPI 包 `gpapi>=0.4.4`，对应上游仓库：

```text
https://github.com/NoMore201/googleplay-api
```

流程：

1. 从 Aurora 公共 dispenser 获取匿名账号：

   ```text
   https://auroraoss.com/api/auth/
   ```

2. dispenser 返回：

   ```json
   {
     "email": "...",
     "auth": "..."
   }
   ```

3. 用 `auth` 做 checkin 和 Play API token：

   ```python
   api.gsfId = api.checkin(email, token)
   api.setAuthSubToken(token)
   ```

4. 尽力上传设备配置：

   ```python
   api.uploadDeviceConfig()
   ```

5. 未指定版本时，先查详情拿最新版 versionCode：

   ```python
   details = api.details(package)
   versionCode = details["details"]["appDetails"]["versionCode"]
   ```

6. 下载：

   ```python
   api.download(package, versionCode=versionCode)
   ```

7. 从返回值读取：

   ```python
   result["file"]["data"]
   ```

   并把 chunk 顺序写入单个 APK。

### dispenser 验证

本次轻量验证：

```text
HEAD https://auroraoss.com/api/auth/
```

结果：

```text
HTTP/2 200
content-type: application/json; charset=utf-8
content-length: 465
```

说明 dispenser 当前可访问。

### token 缓存

token 缓存在：

```text
<base>/.cache/aurora_token.json
```

配置默认 TTL 是 7 天：

```python
token_ttl_seconds = 60 * 60 * 24 * 7
```

但实际读取时取更保守的上限：

```python
SAFE_TTL_SECONDS = 30 * 60
ttl = min(config.aurora.token_ttl_seconds, SAFE_TTL_SECONDS)
```

也就是最多缓存 30 分钟，避免使用过期 token。

### Google Play header monkey-patch

`gpapi 0.4.4` 的默认 header 太旧，会被现代 Google Play 拒绝，典型错误是：

```text
DF-DFERH-01
```

`_aurora_headers.py` 会 monkey-patch `GooglePlayAPI.getHeaders`，注入 Aurora/GPlayApi
使用的现代 header：

```text
Authorization: Bearer <authSubToken>
X-DFE-Encoded-Targets
X-DFE-Phenotype
X-Limit-Ad-Tracking-Enabled: false
X-Ad-Id:
X-DFE-UserLanguages: en_US
User-Agent: Android-Finsky/40.7.20-29 ...
```

这是 Aurora 路径能否工作的关键。

### protobuf 兼容

`gpapi 0.4.x` 的 protobuf stub 较老，在 protobuf 4/5 上容易 import 崩溃。
`aurora.py` 在导入 `gpapi.googleplay` 前设置：

```python
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
```

让 protobuf 使用 pure-python 实现，牺牲一点速度换兼容性。

### gpapi 下载返回结构

上游 `NoMore201/googleplay-api` 的 `GooglePlayAPI.download()` 本身不会把 split APK、
OBB 或 patch 合并进一个 APK。它的流程是：

1. `download()` 先调用 `/fdfe/purchase` 拿 `downloadToken`。
2. 再调用 `delivery(packageName, versionCode, offerType, dlToken, expansion_files=...)`。
3. `delivery()` 解析 Google Play 的 `appDeliveryData`，分别返回 base APK、split APK
   和可选 expansion files。

源码结构可概括为：

```python
result["file"] = self._deliver_data(downloadUrl, cookies)

for split in appDeliveryData.split:
    result["splits"].append({
        "name": split.name,
        "file": self._deliver_data(split.downloadUrl, None),
    })

if expansion_files:
    for obb in appDeliveryData.additionalFile:
        result["additionalData"].append({
            "type": "main" or "patch",
            "versionCode": obb.versionCode,
            "file": self._deliver_data(obb.downloadUrl, None),
        })
```

也就是说：

- `result["file"]` 是 base APK 的下载流。
- `result["splits"]` 是 Google Play 返回的 split APK 列表。
- `result["additionalData"]` 是 OBB expansion file，`fileType == 0` 表示 main，
  `fileType == 1` 表示 patch；只有调用时传 `expansion_files=True` 才会填充。

这些文件只是被分开放在返回字典里，`gpapi` 没有把它们打包成 `.xapk`、`.apks`、
`.apkm`，也没有把它们合并回 base APK。

### 版本能力

Aurora 路径只接受整数 `versionCode`：

```text
--version 123456
```

如果传字符串版本号，例如：

```text
--version 1.18.0
```

会抛 `NotFoundError`，提示改用 APKPure。

这个能力边界和 GPlayAPI 调研一致：Google Play 可以用已知 versionCode 请求下载，
但没有公开接口枚举历史 versionCode。

### 下载形态

当前 Aurora 实现只把 `result["file"]["data"]` 写成单个 `.apk`。

实际代码没有遍历 `result["splits"]`，也没有给 `api.download()` 传
`expansion_files=True` 来获取 `result["additionalData"]`，因此当前输出一定只是
base APK。

注意：Google Play 现代应用经常有 split APK、OBB、patch 等多文件形态。Aurora Store
的 Kotlin `gplayapi` 会返回 `PlayFile` 列表，而这里的 Python `gpapi.download()` 封装
当前只处理单个 `file.data`。所以它更像“单 APK 下载器”，不等价于 Aurora Store 完整
安装包下载链路。

如果某次通过 Aurora 路径下载到的 `.apk` 文件很大，不能据此推断 `gpapi` 已经把
split APK、OBB 或 patch 合并进去了。更可能的解释是：

- 应用本身就是单 APK 分发，资源直接放在 base APK 内。
- Google Play 针对当前伪装设备返回的是体积较大的设备专用 base APK。
- 应用虽然存在 split，但主要资源仍在 base APK，split 只承担 ABI、density 或语言配置。
- 游戏资源没有走 OBB / Play Asset Delivery，而是直接打进 APK 的 `assets` 或 `lib`。

要完整保存现代 Google Play 安装包，需要额外处理：

1. 下载 `result["file"]` 作为 base APK。
2. 遍历并下载 `result["splits"]`。
3. 需要 OBB 时以 `expansion_files=True` 调用，并下载 `result["additionalData"]`。
4. 输出为目录、`.apks`、`.xapk` 或 `.apkm` 这类多文件安装包格式，而不是单个 `.apk`。

## APKPure 路径

相关文件：

```text
mobile-app-download/scripts/android/apkpure.py
mobile-app-download/scripts/android/_metadata.py
mobile-app-download/references/android-apkpure.md
```

### 基本链路

APKPure 路径直连 APKPure mobile API：

```text
GET https://api.pureapk.com/m/v3/cms/app_version?hl=en-US&package_name=<pkg>
```

请求头：

```text
User-Agent: APKPure/3.20.42 (Linux; U; Android 14; en_US)
x-cv: 3172501
x-sv: 29
x-abis: arm64-v8a,armeabi-v7a,armeabi,x86,x86_64
x-gp: 1
```

不带这些 header 时，API 会返回类似 `INVALID_COMMAND` 或被 403。

返回是二进制 protobuf。当前实现没有 protobuf schema，而是：

1. 用 `errors="replace"` 做 UTF-8 lossy decode。
2. 用正则提取版本列表、下载 URL、标题。

关键正则：

```python
_VERSION_RE = re.compile(r"([0-9A-Za-z\.-]+):\(([0-9a-fA-F]{40,})")
_DOWNLOAD_RE = re.compile(r"(X?APKJ)..(https?://...)")
```

`APKJ` 表示单 APK，保存为 `.apk`。

`XAPKJ` 表示 XAPK，保存为 `.xapk`，不解包。

### 版本能力

APKPure 路径支持字符串版本号：

```text
--version 1.23.2
```

实现方式：

1. 先解析版本列表，假定 newest-first。
2. 如果用户指定版本，检查版本是否在列表中。
3. 定位该版本的 hash anchor。
4. 从 anchor 后向后找第一个下载 URL。

### APKPure 实测

本地 Python 3.11 默认 CA 配置失效时，`urllib` 报：

```text
CERTIFICATE_VERIFY_FAILED
```

设置证书路径后可正常访问：

```bash
SSL_CERT_FILE=/etc/ssl/cert.pem PYTHONPATH=mobile-app-download/scripts python3 - <<'PY'
from android import apkpure
print(apkpure.list_versions("org.fdroid.fdroid"))
PY
```

结果：

```text
['1.23.2', '1.23.1', '1.23.0', '1.23.0-alpha0', '1.22.0',
 '1.21.1', '1.20.0', '1.19.0', '1.17.0', '1.16.3']
```

同样测试 `com.oakever.arrows`：

```text
[]
```

说明 APKPure fallback 不能覆盖所有包；`com.oakever.arrows` 在 Aptoide 调研里可由 Aptoide
找到，但 APKPure 这路没有版本列表。

### 标题 fallback

如果 APKPure protobuf 中没有解析出标题，会调用 Play Store 网页：

```text
https://play.google.com/store/apps/details?id=<pkg>&hl=en&gl=US
```

通过 `<title>` 提取展示名。失败则使用包名。

## 错误码

统一异常在：

```text
mobile-app-download/scripts/core/exceptions.py
```

Android 相关退出码：

```text
0  成功
10 鉴权失败
11 应用或版本未找到
13 网络或 IO 错误
14 平台歧义，需要用户确认
20 依赖缺失或配置问题
1  其他错误
```

## 本次验证结果

测试日期：

```text
2026-06-24
```

已验证：

- CLI `--help` 正常。
- CLI `ask com.oakever.arrows` 正常输出平台确认问题，退出码 14。
- Aurora dispenser `https://auroraoss.com/api/auth/` 返回 200。
- APKPure mobile API 可访问；在修正 Python CA 后，`org.fdroid.fdroid` 能解析版本列表。
- APKPure 对 `com.oakever.arrows` 返回空版本列表。

未端到端验证：

- Aurora Google Play 下载：当前系统 Python 没安装 `gpapi`。
- 大文件完整下载和 hash 校验：当前实现本身没有对 APKPure/Aurora 下载结果做 hash 校验。

环境发现：

- 当前系统 Python 3.11 默认 OpenSSL cafile 指向：

  ```text
  /Library/Frameworks/Python.framework/Versions/3.11/etc/openssl/cert.pem
  ```

  该路径不存在或不可用时，`urllib` 会报 `CERTIFICATE_VERIFY_FAILED`。

- 使用：

  ```text
  SSL_CERT_FILE=/etc/ssl/cert.pem
  ```

  后 APKPure 可正常访问。

## 能力边界

Aurora：

- 优点：直接走 Google Play，覆盖 Play 上的包；无需用户 Google 账号。
- 优点：支持已知整数 `versionCode`。
- 缺点：依赖 Aurora dispenser 可用性和匿名账号状态。
- 缺点：依赖 gpapi monkey-patched headers，Google Play 协议变动会影响稳定性。
- 缺点：当前实现只写单个 APK，没有完整处理 split APK / OBB / patch 文件。

APKPure：

- 优点：无需账号，无外部二进制。
- 优点：支持字符串版本号和历史版本列表。
- 优点：可保存 APK 或 XAPK。
- 缺点：protobuf 用正则解析，字段格式变动会坏。
- 缺点：覆盖范围有限，例如 `com.oakever.arrows` 返回空版本列表。
- 缺点：没有 hash 校验。

## 与 Aptoide 方案对比

`mobile-app-download` 当前没有使用 Aptoide。Android 来源只有：

```text
Google Play via Aurora/gpapi
APKPure mobile API
```

前一份 Aptoide 调研中，`com.oakever.arrows` 可以通过 Aptoide 拿到：

```text
base APK
aab.splits
md5
历史版本 app_id / apk_md5sum
```

而本次 APKPure 实测 `com.oakever.arrows` 返回空版本列表。说明如果目标是“尽量按包名拿到
可下载包”，Aptoide 可以作为 `mobile-app-download` 的第三路 Android 来源：

```text
Aurora(Google Play) -> APKPure -> Aptoide
```

或者直接增加：

```text
--source aptoide
```

但接入 Aptoide 时必须处理 AAB/split，而不能只下载 base APK。

## 改造建议

1. 把 Aptoide 增加为 Android 第三来源

   新增：

   ```text
   scripts/android/aptoide.py
   --source aptoide
   ```

   输入 `package`，先调：

   ```text
   /api/7/app/get/package_name={package}/aab=1
   ```

   解析 base APK、`aab.splits`、`obb`，保存为 `.xapk` 或目录结构。

2. 给 Android 下载增加 hash 校验

   Aurora 如果能拿到 hash，校验 hash。

   APKPure 如果响应里可提取 sha1/sha256，也应保存和校验。

   Aptoide 可直接校验 `file.md5sum` 和 split `md5sum`。

3. 统一多文件安装包格式

   当前 Aurora 输出 `.apk`，APKPure 可输出 `.xapk`，未来 Aptoide AAB/split 应输出 `.xapk`
   或 `.apkm`，避免 base APK 单独不可安装。

4. 修复 Python CA 证书体验

   在 README 或启动检查里提示：

   ```text
   SSL_CERT_FILE=/etc/ssl/cert.pem
   ```

   或在 macOS setup 中引导安装/修复 Python certificates。

5. 安装依赖后再做 Android 端到端验证

   需要：

   ```text
   pip install -r mobile-app-download/scripts/requirements.txt
   ```

   然后分别验证：

   ```text
   python3 scripts/cli.py android --package org.fdroid.fdroid --source apkpure
   python3 scripts/cli.py android --package <Play 包名> --source aurora
   ```
