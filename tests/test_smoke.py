import importlib

import pytest

import llmhub

MODULES = [
    "api_gateway", "task_queue", "scheduler", "model_router", "local_inference",
    "cloud_manager", "provider_adapters", "provider_adapters.base", "provider_adapters.mock",
    "budget_manager", "cost_tracker", "lifecycle_manager", "health_monitor",
    "persistence", "configuration", "weights", "reaper",
]  # fmt: skip


def test_version() -> None:
    assert llmhub.__version__ == "0.1.0"


@pytest.mark.parametrize("name", MODULES)
def test_import(name: str) -> None:
    importlib.import_module(f"llmhub.{name}")
