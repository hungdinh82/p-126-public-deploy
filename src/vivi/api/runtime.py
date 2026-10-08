from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.vivi.config import Settings, settings
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

    def close(self) -> None:
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
    tts = create_tts(config)
    return Runtime(
        settings=config,
        store=store,
        vehicle=vehicle,
        orchestrator=create_langgraph_orchestrator(vehicle, store, config),
        stt=create_stt(config),
        tts=tts,
        tts_engines=create_tts_engines(config, tts),
        alerts=AlertEngine(),
        llm_models=llm_models(config),
    )


runtime = build_runtime()
