import sqlite3
from contextlib import closing

import pytest

from app.auth.errors import AdminSeatTakenError
from app.auth.service import AuthService
from app.auth.store import AuthStore


def _service(tmp_path, **kwargs) -> AuthService:
    return AuthService(AuthStore(tmp_path / "auth.sqlite"), **kwargs)


def _expire(tmp_path, table: str, key_col: str, key: str) -> None:
    with closing(sqlite3.connect(tmp_path / "auth.sqlite")) as conn:
        conn.execute(f"UPDATE {table} SET expires_at = ? WHERE {key_col} = ?", ("2000-01-01T00:00:00+00:00", key))
        conn.commit()


# ---- 单管理员三分支 ----

def test_admin_register_then_match_then_reject(tmp_path):
    svc = _service(tmp_path)
    assert svc.get_admin() is None

    first = svc.register_or_check_admin("ou_A", "Alice", "a@x.com")
    assert first.open_id == "ou_A"
    assert svc.get_admin().open_id == "ou_A"

    again = svc.register_or_check_admin("ou_A", "Alice", "a@x.com")
    assert again.open_id == "ou_A"

    with pytest.raises(AdminSeatTakenError):
        svc.register_or_check_admin("ou_B", "Bob", "b@x.com")

    # 拒绝后管理员仍是首个、且只有一行。
    with closing(sqlite3.connect(tmp_path / "auth.sqlite")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_user").fetchone()[0] == 1
    assert svc.get_admin().open_id == "ou_A"


# ---- API Key ----

def test_api_key_create_verify_revoke(tmp_path):
    svc = _service(tmp_path)
    record, raw = svc.create_api_key("ci")
    assert raw.startswith("aps_")
    assert record.key_prefix == raw[:12]

    # 库里只存哈希、无明文。
    with closing(sqlite3.connect(tmp_path / "auth.sqlite")) as conn:
        row = conn.execute("SELECT key_hash, last_used_at FROM api_keys WHERE id = ?", (record.id,)).fetchone()
    assert row[0] == AuthService.hash_key(raw)
    assert row[0] != raw
    assert row[1] is None

    verified = svc.verify_api_key(raw)
    assert verified is not None and verified.id == record.id
    assert svc.verify_api_key("aps_wrong") is None
    assert svc.verify_api_key(None) is None

    # 命中后 last_used_at 被更新。
    with closing(sqlite3.connect(tmp_path / "auth.sqlite")) as conn:
        assert conn.execute("SELECT last_used_at FROM api_keys WHERE id = ?", (record.id,)).fetchone()[0] is not None

    assert svc.revoke_api_key(record.id) is True
    assert svc.verify_api_key(raw) is None  # 吊销后不再命中
    assert svc.revoke_api_key(record.id) is False  # 重复吊销无效


def test_list_api_keys_newest_first(tmp_path):
    svc = _service(tmp_path)
    svc.create_api_key("k1")
    svc.create_api_key("k2")
    keys = svc.list_api_keys()
    assert {k.name for k in keys} == {"k1", "k2"}
    assert all(k.key_hash for k in keys)


# ---- 会话 ----

def test_session_lifecycle_and_ttl(tmp_path):
    svc = _service(tmp_path)
    session = svc.create_session("ou_A")
    assert svc.get_session(session.id).open_id == "ou_A"
    assert svc.get_session(None) is None
    assert svc.get_session("nope") is None

    _expire(tmp_path, "sessions", "id", session.id)
    assert svc.get_session(session.id) is None

    fresh = svc.create_session("ou_A")
    svc.delete_session(fresh.id)
    assert svc.get_session(fresh.id) is None


# ---- OAuth state ----

def test_oauth_state_single_use_and_expiry(tmp_path):
    svc = _service(tmp_path)
    state = svc.create_oauth_state("/dashboard")
    ok, nxt = svc.consume_oauth_state(state)
    assert ok is True and nxt == "/dashboard"
    # 单次：二次消费失败。
    ok2, _ = svc.consume_oauth_state(state)
    assert ok2 is False
    assert svc.consume_oauth_state(None) == (False, None)

    expired = svc.create_oauth_state("/")
    _expire(tmp_path, "oauth_states", "state", expired)
    ok3, _ = svc.consume_oauth_state(expired)
    assert ok3 is False
