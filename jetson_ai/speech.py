"""Resident STT/TTS engines; independent of Laptop workflow modules."""
from __future__ import annotations

import io
import json
import os
import socket
import time
import wave
import zipfile
from pathlib import Path

import httpx

from jetson_ai.tts_profile import resolve_tts_model


def available_memory_mb() -> int:
    """Read system RAM headroom; Xavier shares memory between CPU and GPU."""
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) // 1024
    raise RuntimeError('MemAvailable is missing; refusing to load another model')


class SpeechModels:
    def __init__(self):
        self.stt_engine = os.environ.get("JETSON_AI_STT_ENGINE", "transformers")
        self.stt_path = os.environ.get("JETSON_AI_STT_MODEL", "")
        self.stt_url = os.environ.get("JETSON_AI_STT_URL", "http://127.0.0.1:1236")
        self.tts_path = os.environ.get("JETSON_AI_TTS_MODEL", "")
        self.requested_device = os.environ.get("JETSON_AI_DEVICE", "cuda")
        self.requested_stt_device = os.environ.get("JETSON_AI_STT_DEVICE", self.requested_device)
        if self.requested_stt_device not in {"cuda", "cpu"}:
            raise ValueError("JETSON_AI_STT_DEVICE must be cuda or cpu")
        self.models = {}
        self.info = {name: {
            "status": "unavailable", "model_id": "whisper-base" if name == "stt" else "ZeroTTS",
            "engine": self.stt_engine if name == "stt" else "onnxruntime",
            "model_path": self.stt_path if name == "stt" else self.tts_path,
            "device": "not-loaded", "using_gpu": False, "stream": True, "queue_depth": 0,
            "stream_protocol": "websocket_pcm16" if name == "stt" else "http_pcm16",
            "stream_mode": "rolling_utterance" if name == "stt" else "decoder_chunks",
            "host": socket.gethostname(), "pid": os.getpid(), "load_ms": None,
            "last_latency_ms": None, "first_chunk_ms": None, "error": None,
        } for name in ("stt", "tts")}

    def load(self, name: str) -> None:
        state = self.info[name]
        state.update(status="loading", error=None)
        started = time.monotonic()
        try:
            if name == "stt":
                self._load_stt()
            else:
                self._load_tts()
            load_ms = round((time.monotonic() - started) * 1000)
            if name == "stt" and self.stt_engine == "whisper_cpp":
                load_ms = int(os.environ.get("JETSON_AI_STT_LOAD_MS", load_ms))
            state.update(status="ready", load_ms=load_ms)
        except Exception as exc:
            state.update(status="unavailable", device="not-loaded", using_gpu=False,
                         error=f"{type(exc).__name__}: {exc}")

    def _load_stt(self):
        if not self.stt_path or not Path(self.stt_path).exists():
            raise FileNotFoundError("STT model missing; run setup")
        if self.stt_engine == "whisper_cpp":
            with httpx.Client(timeout=3, trust_env=False) as client:
                client.get(self.stt_url + "/").raise_for_status()
            # Actual whisper-server log, not the requested device flag.
            log = Path(os.environ.get("JETSON_AI_STT_LOG", ""))
            data = log.read_text(errors="replace") if log.is_file() else ""
            import re
            gpu = bool(re.search(r"using (?:CUDA|GPU)|CUDA\d*.*(?:buffer|compute)", data, re.I))
            if self.requested_stt_device == "cuda" and not gpu:
                raise RuntimeError("whisper-server has not confirmed a CUDA backend in its log")
            if self.requested_stt_device == "cpu" and gpu:
                raise RuntimeError("CPU STT requested but whisper-server loaded a CUDA backend")
            self.info["stt"].update(device="cuda" if gpu else "cpu", using_gpu=gpu,
                                    api_url=self.stt_url, pid=os.environ.get("JETSON_AI_STT_PID"))
            self.models["stt"] = True
            return
        import torch
        from transformers import WhisperForConditionalGeneration, WhisperProcessor
        if self.requested_stt_device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("PyTorch CUDA is unavailable")
        device = self.requested_stt_device
        dtype = torch.float16 if device == "cuda" else torch.float32
        processor = WhisperProcessor.from_pretrained(self.stt_path, local_files_only=True)
        model = WhisperForConditionalGeneration.from_pretrained(
            self.stt_path, torch_dtype=dtype, local_files_only=True,
        ).to(device).eval()
        self.models["stt"] = model, processor
        actual = str(next(model.parameters()).device)
        self.info["stt"].update(device=actual, using_gpu=actual.startswith("cuda"),
                                gpu_name=torch.cuda.get_device_name(0) if actual.startswith("cuda") else None)

    def _load_tts(self):
        if not self.tts_path or not Path(self.tts_path).is_dir():
            raise FileNotFoundError("TTS model missing; run setup")
        path, precision = resolve_tts_model(Path(self.tts_path), os.environ.get("JETSON_AI_TTS_PRECISION"))
        # The CPU dynamic INT8 artifact is much smaller than the fp32 and
        # TensorRT variants. Use a conservative profile-specific estimate,
        # while allowing an operator override for measured deployments.
        default_estimate = 3072 if precision == "int8-dynamic-mixed" else 6144
        estimate = int(os.environ.get('JETSON_AI_TTS_LOAD_ESTIMATE_MB', str(default_estimate)))
        reserve = int(os.environ.get('JETSON_AI_MEMORY_RESERVE_MB', '2048'))
        if estimate <= 0 or reserve <= 0:
            raise ValueError('TTS load estimate and RAM reserve must be positive')
        available = available_memory_mb()
        if available < estimate + reserve:
            raise MemoryError(
                f'TTS load deferred: {available} MB RAM available, '
                f'{estimate + reserve} MB required to preserve the {reserve} MB system reserve'
            )
        import numpy as np
        import onnxruntime as ort
        from zerotts import ZeroTTS
        from zerotts.voices import Voice
        # Dynamic MatMulInteger quantization is a CPU experiment, not CUDA or
        # TensorRT INT8. Keep STT/SLM on their independently requested device.
        device = "cpu" if precision == "int8-dynamic-mixed" else self.requested_device
        trt = precision == "int8-qdq-mixed"
        if (trt or precision == "float16-mixed") and device != "cuda":
            raise ValueError(f"{precision} requires --device cuda; no CPU replacement is permitted")
        self.info["tts"].update(model_path=str(path), precision=precision)
        if device == "cuda":
            from jetson_ai.tensorrt_tts import CUDA_OPTIONS
            if hasattr(ort, "preload_dlls"):
                ort.preload_dlls(directory="")
            if "CUDAExecutionProvider" not in ort.get_available_providers():
                raise RuntimeError("ONNX Runtime lacks CUDAExecutionProvider")
            providers = [("CUDAExecutionProvider", dict(CUDA_OPTIONS)), "CPUExecutionProvider"]
        else:
            providers = ["CPUExecutionProvider"]
        # Avoid allocating three CUDA bootstrap graphs before replacing them
        # with TensorRT. Keep codec on CPU during the engine verification.
        bootstrap_providers = ["CPUExecutionProvider"] if trt else providers
        model = ZeroTTS.from_pretrained(str(path), providers=bootstrap_providers, warmup=False,
                                       intra_op_num_threads=2)
        trt_sessions = None
        if trt:
            from jetson_ai.tensorrt_tts import configure_sessions
            trt_sessions, identity, cache = configure_sessions(model, path)
        pack = Path(os.environ.get("JETSON_AI_VOICE_PACK", str(Path(__file__).resolve().parents[1] / "voices/VIVI.zip")))
        with zipfile.ZipFile(pack) as archive:
            meta = json.loads(archive.read("VIVI/meta.json"))
            with np.load(io.BytesIO(archive.read("VIVI/voice.npz"))) as data:
                emb = data["voice_emb"].astype(np.float32)
        if emb.shape != (1, model.n_voice_queries, model.d_model):
            raise RuntimeError("VIVI voice does not match this TTS model")
        voice = Voice(name="VIVI", emb=emb, meta=meta)
        if trt_sessions:
            from jetson_ai.tensorrt_tts import verify_execution
            verification = verify_execution(model, voice, trt_sessions, cache)
            for session in (model.codec._decode_full_sess, model.codec._decode_step_sess):
                session.set_providers(providers)
                session.disable_fallback()
            self.info["tts"].update(engine="tensorrt+onnxruntime", gpu_name=identity["gpu"],
                                    provider_profile=verification["providers"], engine_cache=str(cache),
                                    int8_compute_layers=verification["int8_compute_layers"],
                                    internal_layer_precision_verified=True)
        sessions = [model.prefix_step_sess, model.local_frame_decode_sess, model.text_encoder_sess,
                    model.codec._decode_full_sess, model.codec._decode_step_sess]
        actual = [session.get_providers() for session in sessions]
        gpu = all(p and p[0] in ("CUDAExecutionProvider", "TensorrtExecutionProvider") for p in actual)
        if device == "cuda" and not gpu:
            raise RuntimeError("TTS sessions fell back to CPU; CUDA libraries are incompatible")
        self.models["tts"] = model, voice
        self.info["tts"].update(device="cuda" if gpu else "cpu", using_gpu=gpu,
                                execution_providers=actual, voice="VIVI", sample_rate=int(model.sample_rate))

    def transcribe(self, audio: bytes) -> str:
        if self.stt_engine == "whisper_cpp":
            with httpx.Client(timeout=120, trust_env=False) as client:
                response = client.post(self.stt_url + "/inference", files={"file": ("input.wav", audio, "audio/wav")},
                                       # Native whisper-server reuses its decoder state.
                                       # Each uploaded utterance (including rolling audio
                                       # snapshots) must be decoded from its own audio,
                                       # without tokens from another request or session.
                                       data={"language": "vi", "response_format": "json",
                                             "no_context": "true"})
            response.raise_for_status()
            return str(response.json().get("text", "")).strip()
        import numpy as np
        import torch
        with wave.open(io.BytesIO(audio), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != 16000:
                raise ValueError("Upload mono 16-bit WAV at 16000 Hz")
            samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32) / 32768
        model, processor = self.models["stt"]
        texts = []
        # Whisper's feature extractor only covers a 30-second window. Decode
        # every window so the 60-second API limit does not silently drop audio.
        for offset in range(0, len(samples), 30 * 16000):
            features = processor(samples[offset:offset + 30 * 16000], sampling_rate=16000,
                                 return_tensors="pt").input_features
            with torch.inference_mode():
                tokens = model.generate(features.to(device=model.device, dtype=model.dtype),
                                        language="vi", task="transcribe", max_new_tokens=256)
            texts.append(processor.batch_decode(tokens, skip_special_tokens=True)[0].strip())
        return " ".join(text for text in texts if text)

    def synthesize(self, text: str) -> bytes:
        # A WAV response can still use the bounded streaming codec internally.
        # Full-utterance decoding retains large intermediate convolution buffers.
        model, _ = self.models["tts"]
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(int(model.sample_rate))
            for pcm in self.synthesize_stream(text):
                wav.writeframes(pcm)
        return output.getvalue()

    def synthesize_stream(self, text: str):
        """Yield real ZeroTTS decoder chunks as mono PCM16, without a WAV header."""
        import numpy as np

        from jetson_ai.tensorrt_tts import CODEC_CHUNK_FRAMES

        model, voice = self.models["tts"]
        chunks = model.synthesize_stream(text, voice=voice, max_chunk_frames=CODEC_CHUNK_FRAMES)
        try:
            for chunk in chunks:
                samples = np.asarray(chunk, dtype=np.float32).reshape(-1)
                if not np.isfinite(samples).all():
                    raise RuntimeError("TTS produced non-finite audio")
                pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
                if pcm:
                    yield pcm
        finally:
            chunks.close()
