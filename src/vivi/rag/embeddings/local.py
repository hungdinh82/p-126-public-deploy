from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from src.vivi.rag.embeddings.base import EmbeddingProvider


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class LocalE5EmbeddingProvider(EmbeddingProvider):
    """Offline E5 inference: ONNX INT8, masked mean pooling, unit vectors.

    Model download is deliberately a separate CLI operation. Runtime never
    imports a Hub client or downloads weights, including on a cache miss.
    """

    dimensions = 384

    def __init__(self, model_dir: Path | str, *, threads: int = 2, batch_size: int = 4):
        self.model_dir = Path(model_dir)
        self.threads = threads
        self.batch_size = batch_size
        self._lock = threading.Lock()
        self._session = None
        manifest_path = self.model_dir / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError("Local embedding model missing; run src.vivi.cli.prepare_embeddings")
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if self.manifest.get("dimensions") != self.dimensions:
            raise ValueError("Expected multilingual-e5-small with 384 dimensions")
        for name, expected in self.manifest["files"].items():
            path = self.model_dir / name
            if not path.is_file() or file_sha256(path) != expected:
                raise ValueError(f"Embedding artifact checksum mismatch: {name}")
        from tokenizers import Tokenizer

        self.tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
        identity = {
            "model": self.manifest["model"],
            "revision": self.manifest["revision"],
            "files": self.manifest["files"],
            "pooling": "masked_mean_l2",
            "prefixes": ["query: ", "passage: "],
            "dimensions": self.dimensions,
        }
        self.fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    def token_count(self, text: str) -> int:
        # Do not count through the truncated runtime tokenizer.
        with self._lock:
            self.tokenizer.no_truncation()
            self.tokenizer.no_padding()
            try:
                return len(self.tokenizer.encode(text).ids)
            finally:
                self.tokenizer.enable_truncation(max_length=512)
                self.tokenizer.enable_padding(pad_id=1, pad_token="<pad>")

    def _load(self):
        if self._session is None:
            import onnxruntime as ort

            options = ort.SessionOptions()
            options.intra_op_num_threads = self.threads
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.add_session_config_entry("session.intra_op.allow_spinning", "0")
            self._session = ort.InferenceSession(
                str(self.model_dir / "model_quantized.onnx"),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
        return self._session

    def _embed(self, texts: list[str], prefix: str) -> list[list[float]]:
        import numpy as np

        vectors = []
        with self._lock:
            session = self._load()
            names = {item.name for item in session.get_inputs()}
            for start in range(0, len(texts), self.batch_size):
                encoded = self.tokenizer.encode_batch([prefix + text for text in texts[start:start + self.batch_size]])
                arrays = {
                    "input_ids": np.asarray([item.ids for item in encoded], dtype=np.int64),
                    "attention_mask": np.asarray([item.attention_mask for item in encoded], dtype=np.int64),
                    "token_type_ids": np.asarray([item.type_ids for item in encoded], dtype=np.int64),
                }
                hidden = session.run(None, {name: arrays[name] for name in names})[0]
                mask = arrays["attention_mask"][..., None]
                pooled = (hidden * mask).sum(axis=1) / mask.sum(axis=1).clip(min=1)
                pooled /= np.linalg.norm(pooled, axis=1, keepdims=True).clip(min=1e-12)
                if pooled.shape[1] != self.dimensions or not np.isfinite(pooled).all():
                    raise ValueError("Invalid embedding output")
                vectors.extend(pooled.astype(np.float32).tolist())
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "passage: ") if texts else []

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "query: ")[0]
