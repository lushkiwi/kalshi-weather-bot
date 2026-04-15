from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.getenv("RUN_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="integration disabled; set RUN_INTEGRATION=1 to enable")
    for item in items:
        if "integration" in item.keywords or "integration" in str(item.fspath):
            item.add_marker(skip)
