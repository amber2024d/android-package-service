"""测试隔离：禁止 Settings 读取开发者本地 `.env`。

`app/core/config.py` 的 `Settings` 配了 `env_file=".env"`，会读仓库根的本地 `.env`（gitignored、开发者各异、
常把 provider/appmagic 全开）。这会让 `Settings(...)` / `get_settings()` 的未显式字段被本地 `.env` 污染，
用例变非确定（如工厂用例多出 apkmirror、catalog 用例真打 AppMagic）。这里在整轮测试期把 `env_file` 关掉，
让用例只受显式入参与代码默认值影响；真实环境变量仍生效（CI 也无 `.env`）。
"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import app


@pytest.fixture(autouse=True, scope="session")
def _isolate_settings_from_dotenv():
    original = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    yield
    Settings.model_config["env_file"] = original
    get_settings.cache_clear()


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
        settings.auth_api_key_enabled = True
        settings.feishu_app_id = "cli_test"
        settings.feishu_app_secret = "secret_test"
        settings.public_base_url = "https://svc.example"
        settings.download_async_enabled = False
        settings.ensure_directories()
        return TestClient(app), settings

    yield _make
    get_settings.cache_clear()
