"""管理控制台路由（阶段 20，§3.4）。全部走 require_admin_session（OAuth 会话 + 单管理员）。

- GET  /admin                       控制台页（列出 Key）
- POST /admin/api-keys              建 Key，返回一次性明文（库里只存哈希）
- POST /admin/api-keys/{id}/revoke  吊销 Key
"""

import logging
from html import escape
from pathlib import Path

from fastapi import APIRouter, Body, Depends
from fastapi.responses import HTMLResponse, JSONResponse

from app.auth.deps import get_auth_service, require_admin_session
from app.auth.models import ApiKey
from app.auth.service import AuthService
from app.core.logging import log_event

admin_router = APIRouter()
logger = logging.getLogger(__name__)

_CONSOLE_HTML = (Path(__file__).parent / "console.html").read_text(encoding="utf-8")


def _rows_html(keys: list[ApiKey]) -> str:
    if not keys:
        return "<tr><td colspan='5' class='empty'>暂无 API Key</td></tr>"
    rows = []
    for key in keys:
        status = "已吊销" if key.revoked else "启用中"
        last = key.last_used_at or "—"
        action = "" if key.revoked else f" · <a href='#' onclick=\"revoke('{escape(key.id)}');return false\">吊销</a>"
        rows.append(
            f"<tr><td>{escape(key.name)}</td><td><code>{escape(key.key_prefix)}…</code></td>"
            f"<td>{escape(key.created_at)}</td><td>{escape(last)}</td><td>{status}{action}</td></tr>"
        )
    return "\n".join(rows)


@admin_router.get("/admin", response_class=HTMLResponse)
async def console(
    _admin: object = Depends(require_admin_session),
    service: AuthService = Depends(get_auth_service),
):
    html = _CONSOLE_HTML.replace("__ROWS__", _rows_html(service.list_api_keys()))
    return HTMLResponse(content=html)


@admin_router.post("/admin/api-keys")
async def create_api_key(
    payload: dict = Body(default_factory=dict),
    _admin: object = Depends(require_admin_session),
    service: AuthService = Depends(get_auth_service),
):
    name = str(payload.get("name") or "").strip() if isinstance(payload, dict) else ""
    if not name:
        return JSONResponse({"error": "INVALID", "message": "缺少 name"}, status_code=400)
    record, raw = service.create_api_key(name)
    log_event(logger, "api_key_created", key_id=record.id, name=name)
    # key 为一次性明文，仅此响应返回，不再落库/回显。
    return JSONResponse(
        {
            "id": record.id,
            "name": record.name,
            "key": raw,
            "keyPrefix": record.key_prefix,
            "createdAt": record.created_at,
        },
        status_code=201,
    )


@admin_router.post("/admin/api-keys/{key_id}/revoke")
async def revoke_api_key(
    key_id: str,
    _admin: object = Depends(require_admin_session),
    service: AuthService = Depends(get_auth_service),
):
    if not service.revoke_api_key(key_id):
        return JSONResponse({"error": "NOT_FOUND", "message": "Key 不存在或已吊销"}, status_code=404)
    log_event(logger, "api_key_revoked", key_id=key_id)
    return JSONResponse({"ok": True})
