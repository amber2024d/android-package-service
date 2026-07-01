"""飞书 OAuth 客户端（阶段 19，§3.3）。

用 httpx 手写飞书自建应用登录流（不引 authlib）。端点基址取 settings.feishu_auth_base / feishu_api_base，
默认飞书中国。逻辑参照 lark-auth-service 的 OAuth 部分，只保留「授权 URL 构造 / 换 token / 取用户信息」。
"""

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.auth.errors import AuthError
from app.core.config import Settings

# 沿用 lark-auth-service 的最小 scope：邮箱 + 员工号 + 基本信息（姓名/头像/open_id）。
AUTH_SCOPE = "contact:user.email:readonly contact:user.employee_id:readonly contact:user.base:readonly"


class FeishuOAuthError(AuthError):
    """飞书 OAuth 交互失败（换 token / 取用户信息返回非 0 或缺字段）。"""


@dataclass(frozen=True)
class FeishuUser:
    open_id: str
    name: str | None
    email: str | None
    avatar_url: str | None


def build_authorize_url(settings: Settings, *, state: str, redirect_uri: str) -> str:
    params = {
        "client_id": settings.feishu_app_id or "",
        "redirect_uri": redirect_uri,
        "scope": AUTH_SCOPE,
        "state": state,
        "response_type": "code",
    }
    return f"{settings.feishu_auth_base}/open-apis/authen/v1/authorize?{urlencode(params)}"


async def exchange_code(settings: Settings, *, code: str, redirect_uri: str) -> str:
    """authorization_code 换 user access_token（authen/v2/oauth/token，返回顶层 access_token）。"""
    url = f"{settings.feishu_api_base}/open-apis/authen/v2/oauth/token"
    payload = {
        "grant_type": "authorization_code",
        "client_id": settings.feishu_app_id,
        "client_secret": settings.feishu_app_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }
    async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
        resp = await client.post(url, json=payload)
    data = resp.json()
    if data.get("code") not in (0, None):
        raise FeishuOAuthError(data.get("error_description") or data.get("msg") or f"换 token 失败：code={data.get('code')}")
    token = data.get("access_token")
    if not token:
        raise FeishuOAuthError("飞书未返回 access_token")
    return token


async def fetch_user_info(settings: Settings, *, access_token: str) -> FeishuUser:
    """用 user access_token 取用户信息（authen/v1/user_info）。"""
    url = f"{settings.feishu_api_base}/open-apis/authen/v1/user_info"
    async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
    data = resp.json()
    if data.get("code") != 0:
        raise FeishuOAuthError(data.get("msg") or f"获取用户信息失败：code={data.get('code')}")
    d = data.get("data") or {}
    open_id = d.get("open_id")
    if not open_id:
        raise FeishuOAuthError("飞书用户信息缺少 open_id")
    return FeishuUser(open_id=open_id, name=d.get("name"), email=d.get("email"), avatar_url=d.get("avatar_url"))
