import pytest

from app.core.config import Settings


def _settings(tmp_path, **overrides):
    return Settings(
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "tmp",
        nas_mount_path=tmp_path / "nas",
        **overrides,
    )


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_upstream_proxy_normalizes_to_none(tmp_path, blank):
    # compose 的 `${UPSTREAM_PROXY:-}` 未配代理时给空串；必须归一为 None（直连），否则 httpx(proxy="") 抛 ValueError。
    settings = _settings(tmp_path, upstream_proxy=blank)
    assert settings.upstream_proxy is None


def test_real_upstream_proxy_is_preserved(tmp_path):
    settings = _settings(tmp_path, upstream_proxy="http://user:pass@host:3128")
    assert settings.upstream_proxy == "http://user:pass@host:3128"
