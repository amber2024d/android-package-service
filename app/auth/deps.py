"""鉴权依赖（阶段 19 会话门禁；阶段 20 追加 API Key）。

用 FastAPI `Depends` 精确挂在需要保护的端点上，不设全局中间件——豁免项（/health、/discover、
echarts、登录流）天然不挂（§3.7）。`auth_enabled=False` 时全部放行，保证现有测试与内网联调不受影响。

两个会话依赖按调用端语义分流（§3.2 矩阵）：
- `require_admin_session`：页面（/、/dashboard）——未登录 302 到 /auth/login（带 next）。
- `require_admin_api`：数据接口（snapshot）——未登录 401。阶段 20 会把 snapshot 升级为「会话或 API Key」。
"""

from urllib.parse import quote

from fastapi import Depends, HTTPException, Request

from app.auth.models import AdminUser
from app.auth.service import AuthService
from app.auth.store import AuthStore
from app.core.config import Settings, get_settings

SESSION_COOKIE = "aps_session"


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


async def require_admin_api(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> AdminUser | None:
    if not settings.auth_enabled:
        return None
    admin = _load_admin(request, get_auth_service(settings))
    if admin is None:
        raise HTTPException(status_code=401, detail="需要管理员登录。")
    return admin
