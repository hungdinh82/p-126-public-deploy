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


class SpeechModels:
    def __init__(self):
        self.stt_engine = os.environ.get("JETSON_AI_STT_ENGINE", "transformers")
        self.stt_path = os.environ.get("JETSON_AI_STT_MODEL", "")
        self.stt_url = os.environ.get("JETSON_AI_STT_URL", "http://127.0.0.1:1236")
        self.tts_path = os.environ.get("JETSON_AI_TTS_MODEL", "")
        self.requested_device = os.environ.get("JETSON_AI_DEVICE", "cuda")
        self.models = {}
        self.info = {name: {
            "status": "unavailable", "model_id": "whisper-base" if name == "stt" else "ZeroTTS",
            "engine": self.stt_engine if name == "stt" else "onnxruntime",
            "model_path": self.stt_path if name == "stt" else self.tts_path,
            "device": "not-loaded", "using_gpu": False, "stream": False, "queue_depth": 0,
            "host": socket.gethostname(), "pid": os.getpid(), "load_ms": None,
            "last_latency_ms": None, "error": None,
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
            if self.requested_device == "cuda" and not gpu:
                raise RuntimeError("whisper-server has not confirmed a CUDA backend in its log")
            self.info["stt"].update(device="cuda" if gpu else "cpu", using_gpu=gpu,
                                    api_url=self.stt_url, pid=os.environ.get("JETSON_AI_STT_PID"))
            self.models["stt"] = True
            return
        import torch
        from transformers import WhisperForConditionalGeneration, WhisperProcessor
        if self.requested_device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("PyTorch CUDA is unavailable")
        device = "cuda" if self.requested_device == "cuda" else "cpu"
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
        import numpy as np
        import onnxruntime as ort
        from zerotts import ZeroTTS
        from zerotts.voices import Voice
        if not self.tts_path or not Path(self.tts_path).is_dir():
            raise FileNotFoundError("TTS model missing; run setup")
        if self.requested_device == "cuda":
            if hasattr(ort, "preload_dlls"):
                ort.preload_dlls(directory="")
            if "CUDAExecutionProvider" not in ort.get_available_providers():
                raise RuntimeError("ONNX Runtime lacks CUDAExecutionProvider")
            providers = [("CUDAExecutionProvider", {"arena_extend_strategy": "kSameAsRequested",
                                                   "cudnn_conv_algo_search": "HEURISTIC"}), "CPUExecutionProvider"]
        else:
            providers = ["CPUExecutionProvider"]
        model = ZeroTTS.from_pretrained(self.tts_path, providers=providers, warmup=False,
                                       intra_op_num_threads=2)
        sessions = [model.prefix_step_sess, model.local_frame_decode_sess, model.text_encoder_sess,
                    model.codec._decode_full_sess, model.codec._decode_step_sess]
        actual = [session.get_providers() for session in sessions]
        gpu = all(p and p[0] == "CUDAExecutionProvider" for p in actual)
        if self.requested_device == "cuda" and not gpu:
            raise RuntimeError("TTS sessions fell back to CPU; CUDA libraries are incompatible")
        pack = Path(os.environ.get("JETSON_AI_VOICE_PACK", str(Path(__file__).resolve().parents[1] / "voices/VIVI.zip")))
        with zipfile.ZipFile(pack) as archive:
            meta = json.loads(archive.read("VIVI/meta.json"))
            with np.load(io.BytesIO(archive.read("VIVI/voice.npz"))) as data:
                emb = data["voice_emb"].astype(np.float32)
        if emb.shape != (1, model.n_voice_queries, model.d_model):
            raise RuntimeError("VIVI voice does not match this TTS model")
        voice = Voice(name="VIVI", emb=emb, meta=meta)
        self.models["tts"] = model, voice
        self.info["tts"].update(device="cuda" if gpu else "cpu", using_gpu=gpu,
                                execution_providers=actual, voice="VIVI", sample_rate=int(model.sample_rate))

    def transcribe(self, audio: bytes) -> str:
        if self.stt_engine == "whisper_cpp":
            with httpx.Client(timeout=120, trust_env=False) as client:
                response = client.post(self.stt_url + "/inference", files={"file": ("input.wav", audio, "audio/wav")},
                                       data={"language": "vi", "response_format": "json"})
            response.raise_for_status()
            return str(response.json().get("text", "")).strip()
        import numpy as np
        import torch
        with wave.open(io.BytesIO(audio), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != 16000:
                raise ValueError("Upload mono 16-bit WAV at 16000 Hz")
            samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32) / 32768
        model, processor = self.models["stt"]
        features = processor(samples, sampling_rate=16000, return_tensors="pt").input_features
        with torch.inference_mode():
            tokens = model.generate(features.to(device=model.device, dtype=model.dtype),
                                    language="vi", task="transcribe", max_new_tokens=128)
        return processor.batch_decode(tokens, skip_special_tokens=True)[0].strip()

    def synthesize(self, text: str) -> bytes:
        import numpy as np
        model, voice = self.models["tts"]
        audio = np.asarray(model.synthesize(text, voice=voice), dtype=np.float32).reshape(-1)
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(int(model.sample_rate))
            wav.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        return output.getvalue()
