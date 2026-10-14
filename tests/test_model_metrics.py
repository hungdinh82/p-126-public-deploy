from jetson_ai.model_metrics import GIB, memory_snapshot


def test_shared_process_and_missing_process(tmp_path):
    (tmp_path / '12').mkdir()
    (tmp_path / '12/status').write_text('VmRSS: 1048576 kB\n')
    (tmp_path / 'meminfo').write_text('MemTotal: 2097152 kB\nMemAvailable: 524288 kB\nSwapTotal: 100 kB\nSwapFree: 40 kB\n')
    models = {'stt': {'pid': 12}, 'tts': {'pid': '12'}, 'llm': {'pid': 99}}
    system = memory_snapshot(models, tmp_path)
    assert models['tts']['memory']['rss_bytes'] == GIB
    assert models['tts']['memory']['shared_with'] == ['stt']
    assert models['llm']['memory']['rss_bytes'] is None
    assert system['available_bytes'] == GIB / 2
    assert system['swap_used_bytes'] == 60 * 1024


def test_unknown_pid_and_unavailable_proc(tmp_path):
    models = {'tts': {'pid': None}, 'llm': {'pid': '../1'}, 'stt': {'pid': -1}}
    system = memory_snapshot(models, tmp_path)
    assert all(m['pid'] is None and m['memory']['rss_gib'] is None for m in models.values())
    assert system['total_bytes'] is None


def test_system_proc_falls_back_to_ps_for_process_rss(monkeypatch):
    import subprocess

    def fake_run(command, **kwargs):
        assert command == ['ps', '-o', 'rss=', '-p', '999999']
        return subprocess.CompletedProcess(command, 0, stdout='2048\n', stderr='')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    models = {'llm': {'pid': 999999}}

    memory_snapshot(models)

    assert models['llm']['memory']['rss_bytes'] == 2048 * 1024


def test_capabilities_reports_native_pid_and_architecture(monkeypatch, tmp_path):
    import asyncio
    import os

    import httpx

    from jetson_ai.app import ServiceConfig, create_app

    monkeypatch.setenv('JETSON_AI_STT_ENGINE', 'whisper_cpp')
    monkeypatch.delenv('JETSON_AI_STT_PID', raising=False)
    monkeypatch.setenv('JETSON_AI_LLM_PID', str(os.getpid()))
    log = tmp_path / 'llama.log'
    log.write_text('general.architecture str = qwen3\n')
    app = create_app(ServiceConfig(token='', auth_required=False, llm_log_path=str(log),
                                  llm_model_path='Qwen3-1.7B-Q4_K_M.gguf'),
                     transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    endpoint = next(route.endpoint for route in app.routes if route.path == '/v1/capabilities')
    result = asyncio.run(endpoint())
    models = result['capabilities']
    assert models['llm']['architecture'] == 'qwen3'
    assert models['llm']['precision'] == 'Q4_K_M (GGUF weights)'
    assert models['llm']['memory']['rss_bytes'] > 0
    assert models['stt']['pid'] is None  # External STT must not inherit the gateway PID.
    assert models['stt']['memory']['rss_bytes'] is None
    assert app.state.speech.info['stt']['pid'] == os.getpid()  # Polling does not mutate state.
    assert 'llm' in models['tts']['memory']['shared_with']
