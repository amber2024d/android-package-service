"""鉴权纯逻辑（阶段 18，§3.4/§3.5）。

无任何 HTTP 依赖：API Key 生成/哈希/校验、服务端会话建查删、OAuth state 建校验（TTL + 单次）、
单管理员注册判定。SQL 经 `AuthStore.connect()`（参照 `DownloadJobStore` 直接在此层写 SQL）。
"""

import hashlib
import secrets
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.auth.errors import AdminSeatTakenError
from app.auth.models import AdminUser, ApiKey, Session
from app.auth.store import AuthStore

API_KEY_PREFIX = "aps_"
_KEY_PREFIX_LEN = 12  # "aps_" + 前 8 位随机，供控制台列表识别（§3.4）
_DEFAULT_OAUTH_STATE_TTL_SECONDS = 600


class AuthService:
    def __init__(
        self,
        store: AuthStore,
        *,
        session_ttl_hours: float = 24,
        oauth_state_ttl_seconds: int = _DEFAULT_OAUTH_STATE_TTL_SECONDS,
    ):
        self.store = store
        self.session_ttl = timedelta(hours=session_ttl_hours)
        self.oauth_state_ttl = timedelta(seconds=oauth_state_ttl_seconds)

    # ---- 单管理员 ----

    def get_admin(self) -> AdminUser | None:
        with closing(self.store.connect()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM admin_user LIMIT 1").fetchone()
        return self._row_to_admin(row) if row else None

    def register_or_check_admin(self, open_id: str, name: str | None, email: str | None) -> AdminUser:
        """空表→注册为管理员；同 open_id→放行；不同 open_id→抛 AdminSeatTakenError（§3.3）。"""
        now = self._now_iso()
        with closing(self.store.connect()) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM admin_user LIMIT 1").fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO admin_user (open_id, name, email, created_at) VALUES (?, ?, ?, ?)",
                        (open_id, name, email, now),
                    )
                    conn.execute("COMMIT")
                    return AdminUser(open_id=open_id, name=name, email=email, created_at=now)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        # 事务外判定，避免 raise 触发对已提交事务的 ROLLBACK。
        if row["open_id"] != open_id:
            raise AdminSeatTakenError(row["open_id"])
        return self._row_to_admin(row)

    # ---- API Key ----

    def create_api_key(self, name: str) -> tuple[ApiKey, str]:
        """返回 (记录, 明文)；明文只在此返回一次，库里只存哈希（§3.4）。"""
        raw = API_KEY_PREFIX + secrets.token_urlsafe(32)
        record = ApiKey(
            id=uuid4().hex,
            name=name,
            key_prefix=raw[:_KEY_PREFIX_LEN],
            key_hash=self.hash_key(raw),
            created_at=self._now_iso(),
        )
        with closing(self.store.connect()) as conn:
            conn.execute(
                "INSERT INTO api_keys (id, name, key_prefix, key_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (record.id, record.name, record.key_prefix, record.key_hash, record.created_at),
            )
        return record, raw

    def verify_api_key(self, raw: str | None) -> ApiKey | None:
        """命中未吊销的 Key 返回记录并更新 last_used_at，否则 None。"""
        if not raw:
            return None
        key_hash = self.hash_key(raw)
        now = self._now_iso()
        with closing(self.store.connect()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL",
                (key_hash,),
            ).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now, row["id"]))
        return self._row_to_key(row, last_used_at=now)

    def list_api_keys(self) -> list[ApiKey]:
        with closing(self.store.connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
        return [self._row_to_key(row) for row in rows]

    def revoke_api_key(self, key_id: str) -> bool:
        now = self._now_iso()
        with closing(self.store.connect()) as conn:
            result = conn.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (now, key_id),
            )
        return result.rowcount > 0

    # ---- 会话 ----

    def create_session(self, open_id: str) -> Session:
        now = self._now()
        session = Session(
            id=secrets.token_urlsafe(32),
            open_id=open_id,
            created_at=now.isoformat(),
            expires_at=(now + self.session_ttl).isoformat(),
        )
        with closing(self.store.connect()) as conn:
            self._purge_expired(conn, now)
            conn.execute(
                "INSERT INTO sessions (id, open_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (session.id, session.open_id, session.created_at, session.expires_at),
            )
        return session

    def get_session(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        now_iso = self._now_iso()
        with closing(self.store.connect()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ? AND expires_at > ?",
                (session_id, now_iso),
            ).fetchone()
        return Session(row["id"], row["open_id"], row["created_at"], row["expires_at"]) if row else None

    def delete_session(self, session_id: str) -> None:
        with closing(self.store.connect()) as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    # ---- OAuth state ----

    def create_oauth_state(self, next_url: str | None) -> str:
        now = self._now()
        state = secrets.token_hex(16)  # 16 字节 hex 防 CSRF（§3.3）
        with closing(self.store.connect()) as conn:
            self._purge_expired(conn, now)
            conn.execute(
                "INSERT INTO oauth_states (state, created_at, expires_at, next) VALUES (?, ?, ?, ?)",
                (state, now.isoformat(), (now + self.oauth_state_ttl).isoformat(), next_url),
            )
        return state

    def consume_oauth_state(self, state: str | None) -> tuple[bool, str | None]:
        """返回 (是否有效, 回跳路径)；无论有效与否都删除该 state（单次、防重放）。"""
        if not state:
            return False, None
        now_iso = self._now_iso()
        with closing(self.store.connect()) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM oauth_states WHERE state = ?", (state,)).fetchone()
                conn.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        if row is None or row["expires_at"] <= now_iso:
            return False, None
        return True, row["next"]

    # ---- helpers ----

    @staticmethod
    def hash_key(raw: str) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @classmethod
    def _now_iso(cls) -> str:
        return cls._now().isoformat()

    @staticmethod
    def _purge_expired(conn: sqlite3.Connection, now: datetime) -> None:
        now_iso = now.isoformat()
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_iso,))
        conn.execute("DELETE FROM oauth_states WHERE expires_at <= ?", (now_iso,))

    @staticmethod
    def _row_to_admin(row: sqlite3.Row) -> AdminUser:
        return AdminUser(open_id=row["open_id"], name=row["name"], email=row["email"], created_at=row["created_at"])

    @staticmethod
    def _row_to_key(row: sqlite3.Row, *, last_used_at: str | None = None) -> ApiKey:
        return ApiKey(
            id=row["id"],
            name=row["name"],
            key_prefix=row["key_prefix"],
            key_hash=row["key_hash"],
            created_at=row["created_at"],
            last_used_at=last_used_at if last_used_at is not None else row["last_used_at"],
            revoked_at=row["revoked_at"],
        )
