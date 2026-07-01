"""鉴权子系统（阶段 18–20）：数据层（auth.sqlite）、纯逻辑、飞书 OAuth、依赖与路由。"""

from app.auth.errors import AdminSeatTakenError, AuthError
from app.auth.models import AdminUser, ApiKey, Session
from app.auth.service import AuthService
from app.auth.store import AuthStore

__all__ = [
    "AuthError",
    "AdminSeatTakenError",
    "AdminUser",
    "ApiKey",
    "Session",
    "AuthService",
    "AuthStore",
]
