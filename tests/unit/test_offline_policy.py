from src.vivi.config import Settings
from src.vivi.providers import configured_llm_providers
from src.vivi.speech.stt import create_stt
from src.vivi.speech.tts import create_tts, create_tts_engines


def test_offline_policy_excludes_all_cloud_ai_adapters():
    config = Settings(
        jetson_ai_policy="offline_only",
        rag_local_only=False,
        google_api_key="configured",
        openai_api_key="configured",
        stt_provider="soniox",
        stt_fallback_provider="off",
        soniox_api_key="configured",
        tts_provider="vieneu",
        vieneu_api_key="configured",
    )
    assert configured_llm_providers(config) == []
    assert create_stt(config).name == "off"
    default_tts = create_tts(config)
    assert default_tts.name == "off"
    assert set(create_tts_engines(config, default_tts)) == {"zerotts"}
