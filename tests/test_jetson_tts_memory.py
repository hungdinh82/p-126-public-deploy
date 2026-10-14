import json

from jetson_ai import speech


def test_tts_load_is_deferred_when_reserve_would_be_breached(monkeypatch, tmp_path):
    (tmp_path / 'zerotts').mkdir()
    monkeypatch.setattr(speech, 'available_memory_mb', lambda: 7000)
    monkeypatch.setenv('JETSON_AI_TTS_MODEL', str(tmp_path / 'zerotts'))
    monkeypatch.setenv('JETSON_AI_TTS_PRECISION', 'fp32')
    monkeypatch.setenv('JETSON_AI_TTS_LOAD_ESTIMATE_MB', '6144')
    monkeypatch.setenv('JETSON_AI_MEMORY_RESERVE_MB', '2048')

    models = speech.SpeechModels()
    models.load('tts')

    assert models.info['tts']['status'] == 'unavailable'
    assert '7000 MB RAM available' in models.info['tts']['error']
    assert '8192 MB required' in models.info['tts']['error']


def test_cpu_int8_uses_smaller_memory_estimate(monkeypatch, tmp_path):
    model = tmp_path / 'zerotts-int8'
    model.mkdir()
    (model / 'quantization.json').write_text(json.dumps({
        'format': 'int8-dynamic-mixed', 'execution_provider': 'CPUExecutionProvider',
    }))
    monkeypatch.setattr(speech, 'available_memory_mb', lambda: 6000)
    monkeypatch.setenv('JETSON_AI_TTS_MODEL', str(tmp_path / 'zerotts'))
    monkeypatch.setenv('JETSON_AI_TTS_PRECISION', 'int8')
    monkeypatch.delenv('JETSON_AI_TTS_LOAD_ESTIMATE_MB', raising=False)
    monkeypatch.setenv('JETSON_AI_MEMORY_RESERVE_MB', '2048')

    models = speech.SpeechModels()
    models.load('tts')

    # The guarded loader proceeds beyond the memory check. Runtime dependencies
    # are intentionally absent from this unit test environment.
    assert 'TTS load deferred' not in models.info['tts']['error']
