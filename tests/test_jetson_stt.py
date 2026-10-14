import asyncio

import httpx
import pytest

from src.vivi.config import Settings
from src.vivi.speech.jetson import JetsonSTTAdapter
from src.vivi.speech.stt import create_stt


def config(**kwargs):
    return Settings(_env_file=None, stt_provider='jetson', stt_fallback_provider='off',
                    jetson_ai_url='http://jetson:8789', ai_server_auth_required=True,
                    jetson_ai_token='test-token', **kwargs)


def test_remote_factory_and_no_local_model_load():
    adapter = create_stt(config())
    assert isinstance(adapter, JetsonSTTAdapter)
    assert adapter.device == 'remote'
    assert adapter.streaming is False


def test_transcribe_forwards_wav_and_auth(monkeypatch, tmp_path):
    adapter = JetsonSTTAdapter(config())
    monkeypatch.setattr(adapter, '_wav', lambda path: b'RIFF-test-audio')

    def respond(request):
        assert request.url.path == '/v1/stt/transcriptions'
        assert request.headers['authorization'] == 'Bearer test-token'
        assert b'RIFF-test-audio' in request.content
        return httpx.Response(200, json={'text': ' Xin chào '})

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    assert asyncio.run(adapter.transcribe(tmp_path / 'audio.webm')) == 'Xin chào'


def test_missing_token_is_unavailable():
    adapter = JetsonSTTAdapter(config())
    adapter.config.jetson_ai_token = ''
    assert adapter.availability()[0] is False


def test_remote_error_is_not_hidden(monkeypatch, tmp_path):
    adapter = JetsonSTTAdapter(config())
    monkeypatch.setattr(adapter, '_wav', lambda path: b'audio')
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503)), **kwargs))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(adapter.transcribe(tmp_path / 'audio.wav'))


def test_warmup_uses_real_prompt_and_tools_without_executing():
    from src.vivi.agents.prompts import VOICE_PERSONA
    from src.vivi.dialogue.planner import SEMANTIC_INSTRUCTION, semantic_tools, warmup_local_client
    from src.vivi.structured_llm import StructuredChatClient

    class Client(StructuredChatClient):
        async def _acomplete(self, payload):
            self.payload = payload
            return httpx.Response(200, json={'choices': []})  # No parsing of one-token output.

    client = Client(base_url='http://jetson/v1', api_key='', model='qwen', timeout_seconds=60)
    asyncio.run(warmup_local_client(client))
    assert client.payload['messages'][0]['content'] == VOICE_PERSONA + SEMANTIC_INSTRUCTION
    assert client.payload['tools'] == semantic_tools()
    assert client.payload['max_tokens'] == 1
    assert client.payload['cache_prompt'] is True


@pytest.mark.parametrize('state,ready', [('ready', True), ('loading', False), ('unavailable', False)])
def test_availability_tracks_remote_model(monkeypatch, state, ready):
    adapter = JetsonSTTAdapter(config())
    monkeypatch.setattr('shutil.which', lambda binary: '/usr/bin/ffmpeg')
    client = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'capabilities': {'stt': {'status': state}}})), **kwargs))
    assert adapter.availability()[0] is ready


def test_convert_browser_audio_to_gateway_format(tmp_path):
    import shutil
    import wave

    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg unavailable')
    source = tmp_path / 'stereo.wav'
    with wave.open(str(source), 'wb') as wav:
        wav.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\x01\x00' * (2 * 4800))
    adapter = JetsonSTTAdapter(config())
    converted = tmp_path / 'converted.wav'
    converted.write_bytes(adapter._wav(source))
    with wave.open(str(converted), 'rb') as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        assert wav.getnframes() > 0
