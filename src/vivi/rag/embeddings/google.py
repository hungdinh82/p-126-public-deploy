from __future__ import annotations

import re
import time

from src.vivi.rag.embeddings.base import EmbeddingProvider


class GoogleEmbeddingProvider(EmbeddingProvider):
    def __init__(self, api_key: str, model: str, dimensions: int = 768) -> None:
        if not api_key:
            raise RuntimeError("GOOGLE_API_KEY is required for handbook embeddings")
        from google import genai

        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.dimensions = dimensions

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "RETRIEVAL_DOCUMENT")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "RETRIEVAL_QUERY")[0]

    def _embed(self, texts: list[str], task_type: str) -> list[list[float]]:
        from google.genai import types
        from google.genai.errors import ClientError

        response = None
        for attempt in range(5):
            try:
                response = self.client.models.embed_content(
                    model=self.model,
                    contents=texts,
                    config=types.EmbedContentConfig(
                        task_type=task_type,
                        output_dimensionality=self.dimensions,
                    ),
                )
                break
            except ClientError as exc:
                if exc.code != 429 or attempt == 4:
                    raise
                if "PerDay" in str(exc):
                    raise RuntimeError(
                        "Google embedding daily quota is exhausted; use lexical retrieval or retry after quota reset"
                    ) from exc
                match = re.search(r"retry in\s+([0-9.]+)s", str(exc), flags=re.IGNORECASE)
                delay = float(match.group(1)) if match else 45.0
                # Keep a small margin for rolling quota windows and never block
                # longer than one minute in a single sleep.
                time.sleep(min(delay + 2.0, 55.0))
        if response is None:
            raise RuntimeError("embedding API did not return a response")
        embeddings = response.embeddings or []
        if len(embeddings) != len(texts):
            raise RuntimeError(f"embedding API returned {len(embeddings)} vectors for {len(texts)} texts")
        return [list(item.values or []) for item in embeddings]
