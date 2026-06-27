"""
测试全局配置。
工具层测试依赖真实克隆的仓库，环境不满足时跳过整组测试。
"""
import os
import pytest
from pathlib import Path

REPO_BASE = Path("/tmp/repomind_repos")
TEST_OWNER = "requests"
TEST_REPO = "requests"


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "needs_repo: skip if test repo is not cloned"
    )
    config.addinivalue_line(
        "markers", "regression: full pipeline regression test (slow, requires API key)"
    )
    config.addinivalue_line(
        "markers", "quality: regression with score threshold (answer must be clear)"
    )
    config.addinivalue_line(
        "markers", "smoke: regression structural check only, no score threshold"
    )


@pytest.fixture(scope="session", autouse=True)
def inject_env_from_settings():
    """把 pydantic-settings 从 .env 加载的 token 同步进 os.environ，
    供 dataset._headers() 等直接用 os.getenv 的地方使用。"""
    try:
        from config import settings
        token = settings.github_token.get_secret_value()
        if token:
            os.environ.setdefault("GITHUB_TOKEN", token)
    except Exception:
        pass


@pytest.fixture(scope="session")
def requests_repo():
    path = REPO_BASE / f"{TEST_OWNER}__{TEST_REPO}"
    if not path.exists():
        pytest.skip(f"Test repo not cloned: {path}")
    return path
