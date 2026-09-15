"""Shared fixtures for the strategy tests.

The pure helper module is loaded straight from the strategy directory via importlib,
so these tests run without Freqtrade. Freqtrade-dependent tests live in
test_strategy_classes.py and skip themselves when Freqtrade is missing.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

STRATEGY_DIR = Path(__file__).resolve().parents[2] / "user_data" / "strategies"


def load_module_from_path(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def lib() -> ModuleType:
    return load_module_from_path("btctrend_lib", STRATEGY_DIR / "btctrend_lib.py")


@pytest.fixture(scope="session")
def strategy_dir() -> Path:
    return STRATEGY_DIR
