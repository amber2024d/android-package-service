from app.auth.service import AuthService
from app.auth.store import AuthStore


def _svc(settings) -> AuthService:
    return AuthService(AuthStore(settings.auth_db_path), session_ttl_hours=settings.session_ttl_hours)


def test_gate_blocks_unauthenticated(auth_client):
    client, _ = auth_client()
    assert client.get("/", follow_redirects=False).status_code == 302
    dash = client.get("/dashboard", follow_redirects=False)
    assert dash.status_code == 302
    assert dash.headers["location"].startswith("/auth/login")
    assert client.get("/api/v1/monitor/snapshot", follow_redirects=False).status_code == 401
    # 静态 echarts 放行
    assert client.get("/dashboard/echarts.min.js").status_code == 200


def test_gate_allows_valid_session(auth_client):
    client, settings = auth_client()
    svc = _svc(settings)
    svc.register_or_check_admin("ou_A", "A", "a@x")
    sid = svc.create_session("ou_A").id
    hdr = {"Cookie": f"aps_session={sid}"}
    assert client.get("/dashboard", headers=hdr).status_code == 200
    assert client.get("/api/v1/monitor/snapshot", headers=hdr).status_code == 200
    assert client.get("/", headers=hdr).status_code == 200


def test_expired_or_unknown_session_blocked(auth_client):
    client, settings = auth_client()
    # 未知会话 id → 视为未登录
    assert client.get("/dashboard", headers={"Cookie": "aps_session=nope"}, follow_redirects=False).status_code == 302


def test_auth_disabled_bypasses_gate(auth_client):
    client, _ = auth_client(auth_enabled=False)
    assert client.get("/dashboard").status_code == 200
    assert client.get("/api/v1/monitor/snapshot").status_code == 200
    assert client.get("/").status_code == 200
