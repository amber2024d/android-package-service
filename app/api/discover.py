import json
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.core.config import Settings, get_settings
from app.providers.factory import ProviderFactory

discover_router = APIRouter()

# 各来源的人读说明（与启用状态无关）；实际是否列出、优先级取自运行时已启用的 provider。
_PROVIDER_DESCRIPTIONS = {
    "google-play": "Google Play（gpapi 匿名），官方主力源",
    "apkpure-signed": "APKPure 签名 API",
    "aptoide": "Aptoide 商店",
    "apkpure-proto": "APKPure protobuf 接口",
    "apkpure-web": "APKPure 网页抓取",
    "apkmirror": "APKMirror 深历史源（.apkm 解包重建 XAPK），用作历史 fallback",
    "fake": "内置离线样例源，仅供联调/冒烟，不访问外网",
    "fake-failing": "内置必失败样例源，仅供测试 fallback 行为",
}

PROMPT_TEMPLATE = """我现在要使用 Android 安装包服务（Android Package Service）的功能。

请按下面的顺序帮我：

1. 用 curl 获取 {discover_url}，把整份 API 文档读完并理解。
2. 用通俗易懂的中文给我**简要介绍**通过这个 API 可以完成哪些事情（按功能分组列出，不要贴原始 endpoint 列表，要让我快速看懂能力边界）。
3. 介绍完后**问我接下来想做什么**，再根据我的回答去调用具体接口。"""

_HOME_HTML = (Path(__file__).parent / "home.html").read_text(encoding="utf-8")


@discover_router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    base_url = str(request.base_url).rstrip("/")
    prompt = PROMPT_TEMPLATE.format(discover_url=f"{base_url}/discover")
    html = _HOME_HTML.replace("__PROMPT_JSON__", json.dumps(prompt, ensure_ascii=False))
    return HTMLResponse(content=html)


@discover_router.get("/discover")
async def discover(request: Request, settings: Settings = Depends(get_settings)):
    base_url = str(request.base_url).rstrip("/")

    enabled_providers = sorted(
        ProviderFactory(settings).providers.values(),
        key=lambda provider: provider.priority,
        reverse=True,
    )
    providers_available = {
        provider.id: {
            "priority": provider.priority,
            "description": _PROVIDER_DESCRIPTIONS.get(provider.id, ""),
        }
        for provider in enabled_providers
    }

    return JSONResponse(content={
        "name": "Android Package Service",
        "version": "1.0",
        "description": "Android 安装包聚合服务。通过统一的 RESTful API 查询和下载 Android 应用的 APK/XAPK/APKS：多源 provider 自动聚合并按优先级 fallback，版本目录统一枚举每个包的可下载版本，下载层负责落盘、校验、artifact 复用与 XAPK 打包。服务对外无需鉴权，上游来源凭证与代理由服务端持有，主要面向 Agent 程序化调用。",
        "base_url": base_url,
        "auth": {
            "type": "none",
            "description": "本服务对外不需要任何鉴权，所有接口均可直接调用。Google Play / APKPure 等上游来源的凭证、代理由服务端持有，调用方无需关心。",
            "public_endpoints": ["/", "/dashboard", "/discover", "/health", "/api/v1/monitor/snapshot", "/api/v1/android/apps/{packageName}", "/api/v1/android/apps/{packageName}/files", "/api/v1/android/apps/{packageName}/versions", "/api/v1/android/apps/{packageName}/download", "/api/v1/android/downloads/{jobId}", "/api/v1/android/downloads/{jobId}/file"],
        },
        "concepts": {
            "description": "调用本系统前需要理解的核心概念。",
            "package_name": "应用唯一标识（Android 包名），如 com.tencent.mm、org.fdroid.fdroid。是所有 /apps 接口的主键，作为 URL 路径段传入。",
            "version_selection": "目标版本可三选一指定：versionCode（整数，权威、稳定，优先）> versionName（如 8.0.1，可能多个产物共用）；两者都不传 = 最新版（快路径，不触发版本目录采集）。",
            "provider": "上游来源。默认 auto：服务端按 priority 从大到小依次尝试并自动 fallback，首个成功即返回；可用 ?provider=<id> 强制指定单一来源（该源失败即报错，不再 fallback）。可用 id 见 providers 段。",
            "artifact_types": "产物有三类：APK（单文件，Content-Type application/vnd.android.package-archive）；XAPK / APKS（ZIP 容器，application/zip，内含 base.apk + split 配置 APK +（可选）OBB）。带 split/obb 的应用会被打包为 XAPK 后返回。",
            "version_catalog": "多源聚合的版本目录，统一枚举每个包的「可下载」（downloadable）版本。/versions 首次访问会阻塞采集一次再返回，之后直接读库；新鲜度由服务端后台定时刷新维护。",
            "download_semantics": "/download 命中已有 artifact 时直接返回文件流/302；未命中时返回 202 JSON 并创建下载任务，由独立 worker 下载、解压/打包和校验。调用方轮询 statusUrl，成功后访问 fileUrl 取文件。指定版本时会在后台异步补采版本目录，不阻塞入队响应。",
        },
        "endpoints": {
            "apps": {
                "get_info": {
                    "method": "GET",
                    "path": "/api/v1/android/apps/{packageName}",
                    "auth": "public",
                    "description": "查询应用元信息与（来源给出的）版本列表，不下载文件。用于确认包是否可获取、看最新版本号、看可选版本。",
                    "params": {
                        "path": {"packageName": "Android 包名"},
                        "query": {
                            "versionCode": {"type": "int", "required": False, "description": "按 versionCode 指定版本"},
                            "versionName": {"type": "string", "required": False, "description": "按 versionName 指定版本，如 8.0.1"},
                            "provider": {"type": "string", "required": False, "default": "auto", "description": "强制指定来源 id；省略或 auto = 按优先级自动 fallback"},
                        },
                    },
                    "response": {
                        "200": "{ packageName, appName, versionName, versionCode, provider, downloadUrl, versions: [{ versionName, versionCode, downloadUrl, providerVersionId }] }",
                        "404": "所有启用的 provider 都未找到该包/版本（error=NOT_FOUND）",
                        "400": "指定的 provider 未启用（error=UNSUPPORTED）",
                        "502": "上游解析失败",
                    },
                    "example_curl": f"curl '{base_url}/api/v1/android/apps/org.fdroid.fdroid'",
                },
                "list_files": {
                    "method": "GET",
                    "path": "/api/v1/android/apps/{packageName}/files",
                    "auth": "public",
                    "description": "获取下载计划：构成该版本的全部文件清单（base / split / obb 及各文件的大小与校验和）。用于在真正下载前了解产物结构、判断是单 APK 还是多文件 XAPK。",
                    "params": {
                        "path": {"packageName": "Android 包名"},
                        "query": {
                            "versionCode": {"type": "int", "required": False},
                            "versionName": {"type": "string", "required": False},
                            "provider": {"type": "string", "required": False, "default": "auto"},
                        },
                    },
                    "response": {
                        "200": "{ packageName, appName, versionName, versionCode, provider, files: [{ type, name, url, fallbackUrls, size, md5, sha1, sha256, splitName, splitType, metadata }] }。type ∈ BASE_APK/SPLIT_APK/OBB_MAIN/OBB_PATCH/XAPK/APKS/APKM",
                        "404": "未找到该包/版本",
                        "502": "上游解析失败",
                    },
                    "example_curl": f"curl '{base_url}/api/v1/android/apps/org.fdroid.fdroid/files'",
                },
                "list_versions": {
                    "method": "GET",
                    "path": "/api/v1/android/apps/{packageName}/versions",
                    "auth": "public",
                    "description": "列出版本目录中该包的可下载版本（downloadable，多源聚合去重后的结果）。首次访问会阻塞采集一次，之后读库返回。下载前若需让用户挑版本，先调它。",
                    "params": {
                        "path": {"packageName": "Android 包名"},
                    },
                    "response": {
                        "200": "{ packageName, versions: [{ versionName, versionCode, releaseDate }] }（releaseDate 为发布时间，以 AppMagic 为准、未采到则 null；按版本号降序，可能为空表示暂未采到可下载版本）",
                    },
                    "example_curl": f"curl '{base_url}/api/v1/android/apps/org.fdroid.fdroid/versions'",
                },
                "download": {
                    "method": "GET",
                    "path": "/api/v1/android/apps/{packageName}/download",
                    "auth": "public",
                    "description": "下载安装包。命中 artifact 复用则直接返回文件流/302；未命中则返回 202 下载任务，避免 Web worker 被大包下载、解压、压缩、校验占住。",
                    "params": {
                        "path": {"packageName": "Android 包名"},
                        "query": {
                            "versionCode": {"type": "int", "required": False, "description": "指定版本；省略 = 最新版"},
                            "versionName": {"type": "string", "required": False},
                            "provider": {"type": "string", "required": False, "default": "auto"},
                        },
                    },
                    "response": {
                        "200": "二进制文件流。Content-Type: application/vnd.android.package-archive（单 APK）或 application/zip（XAPK/APKS）；Content-Disposition 带 filename",
                        "302": "配置 NAS_PUBLIC_BASE_URL 且 artifact 在 NAS 挂载下时，Location 指向 NAS HTTP 直链；客户端应跟随重定向",
                        "202": "{ jobId, status, statusUrl, fileUrl, ... }。未命中缓存时入队，轮询 statusUrl；status=succeeded 后访问 fileUrl 取文件",
                        "404": "未找到该包/版本",
                        "502": "解析失败或产物校验和不匹配（error=VERIFY_FAILED）",
                    },
                    "example_curl": f"curl -i '{base_url}/api/v1/android/apps/org.fdroid.fdroid/download'",
                },
                "download_status": {
                    "method": "GET",
                    "path": "/api/v1/android/downloads/{jobId}",
                    "auth": "public",
                    "description": "查询异步下载任务状态。status 为 queued/running/succeeded/failed；成功后 fileUrl 可下载产物。",
                    "response": {
                        "200": "{ jobId, status, packageName, versionCode, versionName, provider, statusUrl, fileUrl, artifactPath, error, providerErrors, createdAt, updatedAt, startedAt, finishedAt }",
                        "404": "任务不存在",
                    },
                },
                "download_file": {
                    "method": "GET",
                    "path": "/api/v1/android/downloads/{jobId}/file",
                    "auth": "public",
                    "description": "获取已完成下载任务的文件。任务未成功完成时返回 409 NOT_READY。",
                    "response": {
                        "200": "二进制文件流或 302 NAS 直链",
                        "302": "配置 NAS_PUBLIC_BASE_URL 时重定向到 NAS HTTP 直链",
                        "409": "任务未完成或产物不可用",
                    },
                },
            },
            "system": {
                "health": {
                    "method": "GET",
                    "path": "/health",
                    "auth": "public",
                    "description": "健康检查",
                    "response": {"200": "{ status: \"ok\" }"},
                },
                "discover": {
                    "method": "GET",
                    "path": "/discover",
                    "auth": "public",
                    "description": "服务发现（API 自描述文档，即本文档）",
                },
                "home": {
                    "method": "GET",
                    "path": "/",
                    "auth": "public",
                    "description": "HTML 首页，展示面向 AI 的使用引导提示词",
                },
                "dashboard": {
                    "method": "GET",
                    "path": "/dashboard",
                    "auth": "public",
                    "description": "HTML 监控面板：实时任务状态（进行中/排队/失败/成功）、provider 流转、近 N 天耗时与成功率、收录规模。轮询 /api/v1/monitor/snapshot。",
                },
                "monitor_snapshot": {
                    "method": "GET",
                    "path": "/api/v1/monitor/snapshot",
                    "auth": "public",
                    "description": "监控面板的数据源：把任务状态、provider 流转、近 N 天分析、收录规模聚合成一份只读快照（JSON），可高频轮询。",
                    "params": {
                        "query": {
                            "days": {"type": "int", "required": False, "default": 7, "description": "统计窗口天数（1–90），影响 providers/analytics 段"},
                        },
                    },
                    "response": {
                        "200": "{ generatedAt, windowDays, overview, tasks, providers, analytics }",
                    },
                },
            },
        },
        "errors": {
            "format": "{ error: <code>, message: <str>, providerErrors: [{ provider, error, message }] }",
            "note": "多源聚合失败时，error 取首个 provider 的错误码，providerErrors 列出每个尝试过的来源各自的失败原因，便于定位是哪一源、因何失败。",
            "codes": {
                "NOT_FOUND": {"status": 404, "description": "所有启用的 provider 都未找到该包/版本"},
                "UNSUPPORTED": {"status": 400, "description": "指定的 provider 未启用，或该来源不支持此请求"},
                "VERIFY_FAILED": {"status": 502, "description": "下载产物校验和与上游声明不匹配"},
                "NETWORK_ERROR": {"status": 502, "description": "访问上游网络失败"},
                "AUTH_ERROR": {"status": 502, "description": "上游鉴权失败"},
                "BAD_RESPONSE": {"status": 502, "description": "上游返回无法解析"},
            },
        },
        "providers": {
            "description": "available 只列出本服务端**当前已启用**的来源，按 priority 从大到小（即 auto 的尝试顺序）排列。auto 模式按此顺序依次尝试，首个成功即返回；?provider=<id> 可强制单源（指定未在 available 中的源返回 UNSUPPORTED）。",
            "available": providers_available,
        },
        "config": {
            "description": "服务端运行参数，通过环境变量配置（影响行为与能力边界，调用方通常无需改动，列出以便理解系统能力）。",
            "providers_default": "真实来源默认关闭，按需开启：PROVIDER_APKPURE_SIGNED_ENABLED / PROVIDER_GOOGLE_PLAY_ENABLED / PROVIDER_APTOIDE_ENABLED / PROVIDER_APKPURE_PROTO_ENABLED / PROVIDER_APKPURE_WEB_ENABLED / PROVIDER_APKMIRROR_ENABLED",
            "variables": {
                "DOWNLOAD_MAX_FILE_BYTES": {"default": "5368709120", "description": "单文件最大下载大小（字节），默认 5GB"},
                "DOWNLOAD_READ_TIMEOUT_SECONDS": {"default": "900", "description": "下载读超时（秒）"},
                "DOWNLOAD_CONNECT_TIMEOUT_SECONDS": {"default": "60", "description": "下载连接超时（秒）"},
                "DOWNLOAD_ASYNC_ENABLED": {"default": "true", "description": "下载未命中缓存时是否入队交给独立 worker"},
                "DOWNLOAD_JOB_POLL_SECONDS": {"default": "2", "description": "下载 worker 空闲轮询间隔（秒）"},
                "DOWNLOAD_JOB_LEASE_SECONDS": {"default": "3600", "description": "下载任务运行租约（秒），worker 执行时续租，崩溃后超时可重抢"},
                "DOWNLOAD_WORKER_CONCURRENCY": {"default": "4", "description": "单个下载 worker 容器内并发执行的下载任务数"},
                "CATALOG_REFRESH_ENABLED": {"default": "true", "description": "版本目录后台定时刷新开关"},
                "CATALOG_REFRESH_INTERVAL_HOURS": {"default": "12", "description": "定时刷新间隔（小时）"},
                "ARCHIVE_ENABLED": {"default": "false", "description": "主动归档：发现新版本即自动下载入 NAS（默认关）"},
                "UPSTREAM_PROXY": {"default": "", "description": "APKPure / Google Play 系上游代理（HTTP/HTTPS，含鉴权，不支持 SOCKS5）"},
            },
        },
        "workflows": {
            "query_latest_info": {
                "description": "查询某应用最新版的元信息（不下载）",
                "steps": [
                    "1. GET /api/v1/android/apps/{包名}",
                    "2. 响应中的 versionName / versionCode 即最新版；provider 为命中的来源",
                    "3. 404 表示所有启用源都没有该包",
                ],
                "example_curl": f"curl '{base_url}/api/v1/android/apps/org.fdroid.fdroid'",
            },
            "list_downloadable_versions": {
                "description": "列出某应用所有可下载版本，供用户挑选",
                "steps": [
                    "1. GET /api/v1/android/apps/{包名}/versions",
                    "2. 首次访问会阻塞采集一次（稍慢），之后读库很快",
                    "3. 返回 versions 数组，每项含 versionName、（可选）versionCode、releaseDate（发布时间，以 AppMagic 为准、未采到则 null）",
                    "4. versions 为空表示版本目录暂未采到该包的可下载版本",
                ],
                "example_curl": f"curl '{base_url}/api/v1/android/apps/org.fdroid.fdroid/versions'",
            },
            "download_latest": {
                "description": "下载某应用最新版安装包",
                "steps": [
                    "1. GET /api/v1/android/apps/{包名}/download（不带版本参数 = 最新版，走快路径）",
                    "2. 若返回 200/302，说明命中已有 artifact，可直接保存文件或跟随 NAS 重定向",
                    "3. 若返回 202，轮询 statusUrl；status=succeeded 后请求 fileUrl",
                    "4. fileUrl 返回单 APK .apk 或带 split/obb 的 .xapk（也可能 302 到 NAS 直链）",
                ],
                "example_curl": f"curl -i '{base_url}/api/v1/android/apps/org.fdroid.fdroid/download'",
            },
            "download_specific_version": {
                "description": "下载指定版本（优先用 versionCode，更稳）",
                "steps": [
                    "1. 如不确定版本，先 GET .../versions 拿到候选 versionCode / versionName",
                    "2. GET /api/v1/android/apps/{包名}/download?versionCode={code}",
                    "3. 未缓存会返回 202，轮询 statusUrl，成功后访问 fileUrl",
                    "4. 也可用 ?versionName=8.0.1；versionCode 优先级更高、更权威",
                    "5. 指定版本会在后台异步补采版本目录，不阻塞入队响应",
                ],
                "example_curl": f"curl -i '{base_url}/api/v1/android/apps/org.fdroid.fdroid/download?versionCode=1021050'",
            },
            "inspect_before_download": {
                "description": "下载前先看产物文件清单（判断单 APK 还是多文件 XAPK、看大小与校验和）",
                "steps": [
                    "1. GET /api/v1/android/apps/{包名}/files 获取下载计划",
                    "2. 检查 files：只有一个 BASE_APK = 单 APK；含 SPLIT_APK / OBB_* = 会打包成 XAPK",
                    "3. 每个文件含 size 和 md5/sha1/sha256，可用于预估下载量或核对",
                    "4. 确认后再 GET .../download 真正下载",
                ],
                "example_curl": f"curl '{base_url}/api/v1/android/apps/org.fdroid.fdroid/files'",
            },
            "force_provider": {
                "description": "强制指定上游来源（例如只要 Google Play 官方产物，或排查某一源）",
                "steps": [
                    "1. 在任意 /apps 接口加 ?provider={id}（id 见 providers 段）",
                    "2. 该源失败即直接报错，不再 fallback 到其他源",
                    "3. 指定了未启用的源会返回 400 UNSUPPORTED",
                ],
                "example_curl": f"curl -i '{base_url}/api/v1/android/apps/org.fdroid.fdroid/download?provider=google-play'",
            },
            "pick_and_download": {
                "description": "典型 Agent 流程：列版本 → 让用户选 → 下载选中的版本",
                "steps": [
                    "1. GET /api/v1/android/apps/{包名}/versions 拿到可下载版本列表",
                    "2. 把版本列表呈现给用户，让其选择一个 versionCode / versionName",
                    "3. GET /api/v1/android/apps/{包名}/download?versionCode={选中的 code}",
                    "4. 未缓存返回 202 时轮询 statusUrl，成功后访问 fileUrl 下载",
                ],
                "example_curl": f"curl -i '{base_url}/api/v1/android/apps/org.fdroid.fdroid/download?versionCode=1021050'",
            },
            "poll_download_job": {
                "description": "处理 /download 返回 202 的标准轮询流程",
                "steps": [
                    "1. 保存 202 响应里的 jobId/statusUrl",
                    "2. GET statusUrl，直到 status 变为 succeeded 或 failed",
                    "3. succeeded 时读取 fileUrl 并下载；failed 时展示 error/providerErrors",
                    "4. fileUrl 在任务未完成前会返回 409 NOT_READY",
                ],
                "example_curl": f"curl '{base_url}/api/v1/android/downloads/{{jobId}}'",
            },
        },
    })
