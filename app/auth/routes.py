"""飞书 OAuth 登录路由（阶段 19，§3.3）。/auth/* 全部公开（回调自带 state 校验）。"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import feishu
from app.auth.deps import SESSION_COOKIE, get_auth_service
from app.auth.errors import AdminSeatTakenError
from app.auth.feishu import FeishuOAuthError
from app.auth.service import AuthService
from app.core.config import Settings, get_settings
from app.core.logging import log_event

auth_router = APIRouter(prefix="/auth")
logger = logging.getLogger(__name__)


def _redirect_uri(settings: Settings) -> str:
    return f"{settings.public_base_url.rstrip('/')}/auth/callback"


def _page(message: str, status_code: int = 200) -> HTMLResponse:
    body = (
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Android Package Service · 登录</title></head>"
        "<body style='font-family:system-ui,PingFang SC,sans-serif;background:#0a0a0b;color:#fafafa;"
        "display:flex;min-height:100vh;align-items:center;justify-content:center'>"
        f"<div style='max-width:420px;text-align:center;padding:2rem'>{message}</div></body></html>"
    )
    return HTMLResponse(content=body, status_code=status_code)


@auth_router.get("/login")
async def login(
    request: Request,
    next: str = "/",
    settings: Settings = Depends(get_settings),
    service: AuthService = Depends(get_auth_service),
):
    state = service.create_oauth_state(next)
    url = feishu.build_authorize_url(settings, state=state, redirect_uri=_redirect_uri(settings))
    return RedirectResponse(url, status_code=302)


@auth_router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    settings: Settings = Depends(get_settings),
    service: AuthService = Depends(get_auth_service),
):
    if error:
        return _page("授权被拒绝。", status_code=400)
    if not code or not state:
        return _page("缺少 code 或 state 参数。", status_code=400)
    ok, next_url = service.consume_oauth_state(state)
    if not ok:
        return _page("无效或已过期的 state，请重新登录。", status_code=400)
    try:
        token = await feishu.exchange_code(settings, code=code, redirect_uri=_redirect_uri(settings))
        user = await feishu.fetch_user_info(settings, access_token=token)
    except FeishuOAuthError as exc:
        log_event(logger, "oauth_failed", reason=str(exc))
        return _page(f"飞书授权失败：{exc}", status_code=502)
    try:
        service.register_or_check_admin(user.open_id, user.name, user.email)
    except AdminSeatTakenError:
        log_event(logger, "oauth_seat_taken", open_id=user.open_id)
        return _page("本服务仅允许一个管理员，注册名额已被占用。", status_code=403)
    session = service.create_session(user.open_id)
    response = RedirectResponse(next_url or "/", status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        session.id,
        httponly=True,
        samesite="lax",
        secure=(settings.app_env == "production"),
        max_age=int(settings.session_ttl_hours * 3600),
    )
    log_event(logger, "oauth_login_ok", open_id=user.open_id)
    return response


@auth_router.get("/logout")
async def logout(
    request: Request,
    service: AuthService = Depends(get_auth_service),
):
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        service.delete_session(session_id)
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response
