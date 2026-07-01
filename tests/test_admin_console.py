from app.auth.service import AuthService
from app.auth.store import AuthStore

DATA_URL = "/api/v1/android/apps/org.fdroid.fdroid?provider=fake"


def _admin_session(settings) -> str:
    svc = AuthService(AuthStore(settings.auth_db_path), session_ttl_hours=settings.session_ttl_hours)
    svc.register_or_check_admin("ou_A", "A", "a@x")
    return svc.create_session("ou_A").id


def test_console_requires_login(auth_client):
    client, _ = auth_client()
    assert client.get("/admin", follow_redirects=False).status_code == 302


def test_create_key_requires_login(auth_client):
    client, _ = auth_client()
    r = client.post("/admin/api-keys", json={"name": "x"}, follow_redirects=False)
    assert r.status_code == 302  # require_admin_session（页面门禁）


def test_console_lists_and_creates_and_revokes(auth_client):
    client, settings = auth_client()
    hdr = {"Cookie": f"aps_session={_admin_session(settings)}"}

    assert client.get("/admin", headers=hdr).status_code == 200

    created = client.post("/admin/api-keys", json={"name": "ci"}, headers=hdr)
    assert created.status_code == 201
    raw = created.json()["key"]
    assert raw.startswith("aps_")

    # 库里只存哈希 + 前缀，无明文
    svc = AuthService(AuthStore(settings.auth_db_path))
    keys = svc.list_api_keys()
    assert len(keys) == 1
    assert keys[0].key_hash != raw
    assert keys[0].key_prefix.startswith("aps_")
    key_id = keys[0].id

    # 列表页展示前缀、不回显明文
    page = client.get("/admin", headers=hdr).text
    assert keys[0].key_prefix in page
    assert raw not in page

    # 新 Key 可调数据 API
    assert client.get(DATA_URL, headers={"Authorization": f"Bearer {raw}"}).status_code == 200

    # 吊销后立即失效
    assert client.post(f"/admin/api-keys/{key_id}/revoke", headers=hdr).status_code == 200
    assert client.get(DATA_URL, headers={"Authorization": f"Bearer {raw}"}).status_code == 401


def test_create_key_requires_name(auth_client):
    client, settings = auth_client()
    hdr = {"Cookie": f"aps_session={_admin_session(settings)}"}
    assert client.post("/admin/api-keys", json={}, headers=hdr).status_code == 400


def test_revoke_unknown_key_404(auth_client):
    client, settings = auth_client()
    hdr = {"Cookie": f"aps_session={_admin_session(settings)}"}
    assert client.post("/admin/api-keys/nope/revoke", headers=hdr).status_code == 404
