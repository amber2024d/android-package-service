"""测试隔离：禁止 Settings 读取开发者本地 `.env`。

`app/core/config.py` 的 `Settings` 配了 `env_file=".env"`，会读仓库根的本地 `.env`（gitignored、开发者各异、
常把 provider/appmagic 全开）。这会让 `Settings(...)` / `get_settings()` 的未显式字段被本地 `.env` 污染，
用例变非确定（如工厂用例多出 apkmirror、catalog 用例真打 AppMagic）。这里在整轮测试期把 `env_file` 关掉，
让用例只受显式入参与代码默认值影响；真实环境变量仍生效（CI 也无 `.env`）。
"""

import pytest

from app.core.config import Settings, get_settings


@pytest.fixture(autouse=True, scope="session")
def _isolate_settings_from_dotenv():
    original = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    yield
    Settings.model_config["env_file"] = original
    get_settings.cache_clear()
