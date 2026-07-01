"""鉴权依赖（阶段 19 会话门禁；阶段 20 追加 API Key）。

用 FastAPI `Depends` 精确挂在需要保护的端点上，不设全局中间件——豁免项（/health、/discover、
echarts、登录流）天然不挂（§3.7）。`auth_enabled=False` 时全部放行，保证现有测试与内网联调不受影响。

依赖清单（§3.2 矩阵）：
- `require_admin_session`：页面（/、/dashboard、/admin）——未登录 302 到 /auth/login（带 next）。
- `require_api_key`：数据 API（/api/v1/android/*）——缺/错 Key 401；auth_api_key_enabled 关时放行。
- `require_session_or_api_key`：snapshot——会话（面板）或 API Key（程序化监控）任一即可，否则 401。
"""

from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.models import AdminUser, ApiKey
from app.auth.service import AuthService
from app.auth.store import AuthStore
from app.core.config import Settings, get_settings

SESSION_COOKIE = "aps_session"
API_KEY_HEADER = "X-API-Key"

# auto_error=False：缺头/非 Bearer 时返回 None 而非直接 401，交由依赖自行分流（兼容 X-API-Key）。
_bearer = HTTPBearer(auto_error=False)


def _extract_api_key(request: Request, credentials: HTTPAuthorizationCredentials | None) -> str | None:
    if credentials is not None and credentials.scheme.lower() == "bearer":
        return credentials.credentials
    return request.headers.get(API_KEY_HEADER)


def get_auth_service(settings: Settings = Depends(get_settings)) -> AuthService:
    return AuthService(AuthStore(settings.auth_db_path), session_ttl_hours=settings.session_ttl_hours)


def _load_admin(request: Request, service: AuthService) -> AdminUser | None:
    session = service.get_session(request.cookies.get(SESSION_COOKIE))
    if session is None:
        return None
    admin = service.get_admin()
    if admin is None or admin.open_id != session.open_id:
        return None
    return admin


async def require_admin_session(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> AdminUser | None:
    if not settings.auth_enabled:
        return None
    admin = _load_admin(request, get_auth_service(settings))
    if admin is None:
        nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise HTTPException(status_code=302, headers={"Location": f"/auth/login?next={quote(nxt, safe='')}"})
    return admin


async def require_api_key(
    request: Request,
    settings: Settings = Depends(get_settings),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> ApiKey | None:
    """数据 API 门禁（§3.4）。auth_enabled 或 auth_api_key_enabled 关时放行（内网/测试兼容）。"""
    if not (settings.auth_enabled and settings.auth_api_key_enabled):
        return None
    service = AuthService(AuthStore(settings.auth_db_path))
    key = service.verify_api_key(_extract_api_key(request, credentials))
    if key is None:
        raise HTTPException(status_code=401, detail="需要有效的 API Key（Authorization: Bearer <key>，或 X-API-Key）。")
    return key


async def require_session_or_api_key(
    request: Request,
    settings: Settings = Depends(get_settings),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AdminUser | ApiKey | None:
    """snapshot 组合门禁（§3.2 决策②）：先试会话（面板浏览器），再试 API Key（程序化监控）。"""
    if not settings.auth_enabled:
        return None
    service = AuthService(AuthStore(settings.auth_db_path), session_ttl_hours=settings.session_ttl_hours)
    admin = _load_admin(request, service)
    if admin is not None:
        return admin
    key = service.verify_api_key(_extract_api_key(request, credentials))
    if key is not None:
        return key
    raise HTTPException(status_code=401, detail="需要管理员登录或有效的 API Key。")
