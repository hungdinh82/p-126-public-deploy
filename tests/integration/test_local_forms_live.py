"""Opt-in Qwen regression adapted from 5d47f6c; isolated vehicle/checkpoints."""

import json
import os
import time

import pytest

from src.vivi.config import Settings
from src.vivi.dialogue.planner import ModelProvider, SemanticResolver
from src.vivi.graphs.assistant import build_assistant_graph
from src.vivi.persistence.checkpoints import ThreadedSqliteSaver
from src.vivi.persistence.runtime_store import RuntimeStore
from src.vivi.rag.generator import LocalHandbookGenerator
from tests.support.long_term_memory import services as services

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.live,
    pytest.mark.skipif(not os.getenv("VIVI_TEST_JETSON_URL"), reason="Set isolated Jetson inference endpoint"),
]


async def test_real_qwen_create_update_consent_and_new_topic(services, tmp_path):
    config = Settings(
        _env_file=None,
        local_llm_base_url=os.environ["VIVI_TEST_JETSON_URL"].rstrip("/") + "/v1",
        local_llm_model=os.getenv("VIVI_TEST_JETSON_MODEL", "vivi-qwen3-1.7b"),
        local_semantic_contract="operations_v1",
        local_semantic_transport=os.getenv("VIVI_TEST_LOCAL_TRANSPORT", "json_schema"),
    )
    client = LocalHandbookGenerator(config).client
    client.max_tokens = min(config.planner_max_tokens, config.local_llm_max_tokens)
    resolver = SemanticResolver(
        {"local": ModelProvider("local", client, config.local_budget_seconds)},
        budget_seconds=config.semantic_budget_seconds,
    )
    store = RuntimeStore(tmp_path / "runtime.sqlite3")
    graph = build_assistant_graph(
        services, ThreadedSqliteSaver(tmp_path / "checkpoint.sqlite3"), store, resolver=resolver
    )
    graph.default_provider = "local"
    report = {"scope": "Real Qwen and Core graph; memory vehicle, temporary checkpoints; STT/TTS excluded", "turns": []}

    async def speak(text):
        started = time.perf_counter()
        result = await graph.ainvoke(
            {"session_id": "synthetic-driver", "turn_id": str(len(report["turns"])), "input_text": text}
        )
        report["turns"].append(
            {
                "text": text,
                "wall_ms": round((time.perf_counter() - started) * 1000, 2),
                "output": result["output"],
                "steps": result["steps"],
            }
        )
        print(text, result["output"]["status"], report["turns"][-1]["wall_ms"], flush=True)
        return result

    try:
        result = await speak("mở cửa")
        assert result["steps"][0]["status"] == "waiting_slot"
        result = await speak("sau phải")
        assert result["output"]["confirmation"]
        assert result["steps"][0]["arguments"] == {"open": True, "zone": "rear_right"}
        await speak("ok")
        assert services.action_gateway.vehicle.state_for("synthetic-driver").door_states.rear_right.open
        result = await speak("đặt điều hòa 25 độ")
        assert result["output"]["diagnostics"]["semantic"]["actions"][0]["arguments"]["value_celsius"] == 25
        result = await speak("bật nhạc")
        assert services.action_gateway.vehicle.state_for("synthetic-driver").media_playing
        report["passed"] = True
    finally:
        from pathlib import Path

        output = Path(os.getenv("VIVI_TEST_FORM_REPORT", str(tmp_path / "local-forms-report.json")))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        await graph.aclose()
