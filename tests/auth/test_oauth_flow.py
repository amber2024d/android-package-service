from app.auth.feishu import FeishuUser
from app.auth.service import AuthService
from app.auth.store import AuthStore


def _svc(settings) -> AuthService:
    return AuthService(AuthStore(settings.auth_db_path), session_ttl_hours=settings.session_ttl_hours)


def _mock_feishu(monkeypatch, open_id: str) -> None:
    async def fake_exchange(settings, *, code, redirect_uri):
        return "u-token"

    async def fake_user(settings, *, access_token):
        return FeishuUser(open_id=open_id, name="N", email="e@x", avatar_url=None)

    monkeypatch.setattr("app.auth.feishu.exchange_code", fake_exchange)
    monkeypatch.setattr("app.auth.feishu.fetch_user_info", fake_user)


def test_login_redirects_to_feishu_authorize(auth_client):
    client, _ = auth_client()
    r = client.get("/auth/login?next=/dashboard", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("https://accounts.feishu.cn/open-apis/authen/v1/authorize")
    assert "client_id=cli_test" in loc
    assert "state=" in loc
    assert "redirect_uri=" in loc


def test_first_login_registers_admin(auth_client, monkeypatch):
    client, settings = auth_client()
    _mock_feishu(monkeypatch, "ou_A")
    state = _svc(settings).create_oauth_state("/dashboard")
    r = client.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard"
    assert "aps_session=" in r.headers.get("set-cookie", "")
    assert _svc(settings).get_admin().open_id == "ou_A"


def test_second_user_rejected_403(auth_client, monkeypatch):
    client, settings = auth_client()
    _svc(settings).register_or_check_admin("ou_A", "A", "a@x")
    _mock_feishu(monkeypatch, "ou_B")
    state = _svc(settings).create_oauth_state("/")
    r = client.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert r.status_code == 403
    assert "aps_session=" not in r.headers.get("set-cookie", "")
    assert _svc(settings).get_admin().open_id == "ou_A"


def test_invalid_state_rejected_400(auth_client, monkeypatch):
    client, settings = auth_client()
    _mock_feishu(monkeypatch, "ou_A")
    r = client.get("/auth/callback?code=abc&state=deadbeef", follow_redirects=False)
    assert r.status_code == 400
    assert _svc(settings).get_admin() is None  # 未换 token、未注册


def test_missing_params_rejected_400(auth_client):
    client, _ = auth_client()
    assert client.get("/auth/callback", follow_redirects=False).status_code == 400


def test_logout_clears_session(auth_client):
    client, settings = auth_client()
    svc = _svc(settings)
    svc.register_or_check_admin("ou_A", "A", "a@x")
    sid = svc.create_session("ou_A").id
    r = client.get("/auth/logout", headers={"Cookie": f"aps_session={sid}"}, follow_redirects=False)
    assert r.status_code == 302
    assert svc.get_session(sid) is None
