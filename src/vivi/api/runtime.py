from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from src.vivi.config import Settings, settings
from src.vivi.connectivity import ConnectivitySupervisor
from src.vivi.identity import SessionStore
from src.vivi.orchestration import LangGraphOrchestrator, create_langgraph_orchestrator
from src.vivi.persistence.event_store import EventStore
from src.vivi.providers import llm_models
from src.vivi.speech.stt import create_stt
from src.vivi.speech.tts import create_tts, create_tts_engines
from src.vivi.vehicle.alerts import AlertEngine
from src.vivi.vehicle.memory import VehicleSimulator
from src.vivi.vehicle.mqtt import MqttVehicleAdapter


@dataclass
class Runtime:
    settings: Settings
    store: EventStore
    vehicle: Any
    orchestrator: LangGraphOrchestrator
    stt: Any
    tts: Any
    tts_engines: dict[str, Any]
    alerts: AlertEngine
    llm_models: dict[str, str]
    connectivity: ConnectivitySupervisor
    sessions: SessionStore

    def close(self) -> None:
        self.connectivity.store.close()
        for graph in [self.orchestrator.graph]:
            if hasattr(graph, "close"):
                graph.close()
        if isinstance(self.vehicle, MqttVehicleAdapter):
            self.vehicle.close()

    async def aclose(self) -> None:
        await self.orchestrator.aclose()
        await self.connectivity.stop()
        self.connectivity.store.close()
        if isinstance(self.vehicle, MqttVehicleAdapter):
            self.vehicle.close()


def build_runtime(config: Settings = settings) -> Runtime:
    if config.vehicle_provider == "mqtt":
        vehicle = MqttVehicleAdapter(
            config.vehicle_id,
            host=config.mqtt_host,
            port=config.mqtt_port,
            username=config.mqtt_api_username or None,
            password=config.mqtt_api_password or None,
            timeout=config.mqtt_timeout_seconds,
        )
    else:
        vehicle = VehicleSimulator()
    store = EventStore(config)
    connectivity = ConnectivitySupervisor(config)
    tts = create_tts(config)
    tts_engines = create_tts_engines(config, tts, connectivity)
    sessions = SessionStore(config.auth_db_path, session_hours=config.auth_session_hours)
    sessions.provision_engineer(config.engineer_phone, config.engineer_pin)
    return Runtime(
        settings=config,
        store=store,
        vehicle=vehicle,
        orchestrator=create_langgraph_orchestrator(vehicle, store, config, connectivity=connectivity),
        stt=create_stt(config, connectivity),
        tts=tts,
        tts_engines=tts_engines,
        alerts=AlertEngine(),
        llm_models=llm_models(config),
        connectivity=connectivity,
        sessions=sessions,
    )


active_runtime: ContextVar[Runtime | None] = ContextVar("vivi_runtime", default=None)


class RuntimeReference:
    """Request-local dependency; importing a route never opens a database."""

    def __getattr__(self, name):
        current = active_runtime.get()
        if current is None:
            if name == "settings":
                return settings
            raise RuntimeError("Runtime is not bound. Use create_app(runtime=...) or application lifespan.")
        return getattr(current, name)


runtime = RuntimeReference()
