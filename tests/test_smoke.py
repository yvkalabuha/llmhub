import importlib

import pytest

import llmhub

MODULES = [
    "api_gateway", "task_queue", "scheduler", "model_router", "local_inference",
    "cloud_manager", "provider_adapters", "provider_adapters.base", "provider_adapters.mock",
    "budget_manager", "cost_tracker", "lifecycle_manager", "health_monitor",
    "persistence", "configuration", "weights", "reaper",
    # stream F (T-368): integration modules
    "clock", "controller", "killswitch", "reaper.core", "reaper.__main__",
    "cloud_manager.provisioner", "cloud_manager.offers", "cloud_manager.pools",
    "provider_adapters.guarded", "provider_adapters.contract", "lifecycle_manager.timeouts",
    "lifecycle_manager.safety", "budget_manager.limits", "cost_tracker.reconcile",
]  # fmt: skip

EXPORTING = [
    "configuration", "persistence", "provider_adapters", "lifecycle_manager", "task_queue",
    "scheduler", "budget_manager", "cost_tracker", "cloud_manager", "reaper",
]  # fmt: skip


def test_version() -> None:
    assert llmhub.__version__ == "0.3.3"


@pytest.mark.parametrize("name", MODULES)
def test_import(name: str) -> None:
    importlib.import_module(f"llmhub.{name}")


@pytest.mark.parametrize("name", EXPORTING)
def test_package_exports_resolve(name: str) -> None:
    pkg = importlib.import_module(f"llmhub.{name}")
    assert pkg.__all__
    for attr in pkg.__all__:
        assert getattr(pkg, attr) is not None
    with pytest.raises(AttributeError):
        _ = pkg.no_such_name


def test_one_safety_api() -> None:
    from llmhub.budget_manager import accounts
    from llmhub.lifecycle_manager import safety

    assert accounts.block_provider is safety.block_provider
    assert accounts.is_blocked is safety.is_blocked is safety.provider_blocked
    assert accounts.resume_provider is safety.resume_provider


def test_no_projectvoicebot_imports() -> None:
    import pathlib

    root = pathlib.Path(llmhub.__file__).parent
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "projectVoiceBot" not in text and "import voicebot" not in text, path
