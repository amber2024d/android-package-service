import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app


@pytest.fixture
def auth_client(tmp_path):
    """构造带鉴权配置的 TestClient；隔离到 tmp_path，飞书凭证用占位（OAuth 调用在用例里 monkeypatch）。"""

    def _make(*, auth_enabled: bool = True):
        get_settings.cache_clear()
        settings = get_settings()
        settings.data_dir = tmp_path / "data"
        settings.temp_dir = tmp_path / "tmp"
        settings.nas_mount_path = tmp_path / "nas"
        settings.app_env = "development"  # secure cookie 关闭，便于 http 测试
        settings.auth_enabled = auth_enabled
        settings.feishu_app_id = "cli_test"
        settings.feishu_app_secret = "secret_test"
        settings.public_base_url = "https://svc.example"
        settings.download_async_enabled = False
        settings.ensure_directories()
        return TestClient(app), settings

    yield _make
    get_settings.cache_clear()
