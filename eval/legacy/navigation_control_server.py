"""Temporary real backend for browser P/D checks; never opens user databases."""

import tempfile
from pathlib import Path

# Establish isolated environment before importing the application/runtime.
import tests.conftest  # noqa: F401
from src.vivi.api.app import app
from src.vivi.api.runtime import runtime as api_runtime
from src.vivi.orchestration import LangGraphOrchestrator
from tests.support.long_term_memory import services
from tests.support.navigation_lifecycle import runtime

fixture_directory = tempfile.TemporaryDirectory(prefix="vivi-navigation-browser-")
fixture_services = services.__wrapped__(Path(fixture_directory.name))
graph, ledger, gateway = runtime(fixture_services, Path(fixture_directory.name))
api_runtime.vehicle = gateway.vehicle
api_runtime.orchestrator = LangGraphOrchestrator({"rules": graph}, gateway, api_runtime.store, ledger=ledger)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8768, log_level="warning")
