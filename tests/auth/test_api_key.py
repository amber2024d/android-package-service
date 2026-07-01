from app.auth.service import AuthService
from app.auth.store import AuthStore

DATA_URL = "/api/v1/android/apps/org.fdroid.fdroid?provider=fake"
SNAPSHOT_URL = "/api/v1/monitor/snapshot"


def _new_key(settings, name="ci") -> str:
    _, raw = AuthService(AuthStore(settings.auth_db_path)).create_api_key(name)
    return raw


def test_missing_key_401(auth_client):
    client, _ = auth_client()
    assert client.get(DATA_URL).status_code == 401


def test_invalid_key_401(auth_client):
    client, _ = auth_client()
    assert client.get(DATA_URL, headers={"Authorization": "Bearer aps_wrong"}).status_code == 401


def test_valid_bearer_key_200(auth_client):
    client, settings = auth_client()
    raw = _new_key(settings)
    assert client.get(DATA_URL, headers={"Authorization": f"Bearer {raw}"}).status_code == 200


def test_valid_x_api_key_header_200(auth_client):
    client, settings = auth_client()
    raw = _new_key(settings)
    assert client.get(DATA_URL, headers={"X-API-Key": raw}).status_code == 200


def test_revoked_key_401(auth_client):
    client, settings = auth_client()
    svc = AuthService(AuthStore(settings.auth_db_path))
    record, raw = svc.create_api_key("ci")
    svc.revoke_api_key(record.id)
    assert client.get(DATA_URL, headers={"Authorization": f"Bearer {raw}"}).status_code == 401


def test_api_key_disabled_bypasses(auth_client):
    client, settings = auth_client()
    settings.auth_api_key_enabled = False
    assert client.get(DATA_URL).status_code == 200


def test_auth_disabled_bypasses(auth_client):
    client, _ = auth_client(auth_enabled=False)
    assert client.get(DATA_URL).status_code == 200


def test_discover_public_and_reports_api_key(auth_client):
    client, _ = auth_client()
    r = client.get("/discover")  # 自描述保持公开（Agent 需先读契约）
    assert r.status_code == 200
    body = r.json()
    assert body["auth"]["type"] == "api_key"
    assert body["endpoints"]["apps"]["get_info"]["auth"] == "api_key"
    assert body["endpoints"]["system"]["monitor_snapshot"]["auth"] == "session_or_api_key"


def test_snapshot_accepts_session_or_api_key(auth_client):
    client, settings = auth_client()
    raw = _new_key(settings)
    # 无凭证 → 401
    assert client.get(SNAPSHOT_URL).status_code == 401
    # API Key → 放行（程序化监控，决策②）
    assert client.get(SNAPSHOT_URL, headers={"Authorization": f"Bearer {raw}"}).status_code == 200
    # 会话 → 放行
    svc = AuthService(AuthStore(settings.auth_db_path), session_ttl_hours=settings.session_ttl_hours)
    svc.register_or_check_admin("ou_A", "A", "a@x")
    sid = svc.create_session("ou_A").id
    assert client.get(SNAPSHOT_URL, headers={"Cookie": f"aps_session={sid}"}).status_code == 200
