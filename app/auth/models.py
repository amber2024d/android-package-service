"""鉴权数据模型（阶段 18）。字段与 auth.sqlite 四表列一一对齐；时间统一 ISO8601 UTC 字符串。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AdminUser:
    open_id: str
    name: str | None
    email: str | None
    created_at: str


@dataclass(frozen=True)
class ApiKey:
    id: str
    name: str
    key_prefix: str
    key_hash: str
    created_at: str
    last_used_at: str | None = None
    revoked_at: str | None = None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


@dataclass(frozen=True)
class Session:
    id: str
    open_id: str
    created_at: str
    expires_at: str
