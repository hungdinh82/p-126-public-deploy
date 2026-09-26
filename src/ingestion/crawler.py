from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

API_URL = "https://omapi.vinfastauto.com/fe/v1/menu"
DETAIL_URL = "https://om.vinfastauto.com/{locale}/detail"


@dataclass(frozen=True)
class CrawlTarget:
    vehicle_model: str = "VF8"
    model_year: int = 2026
    locale: str = "vi_vn"

    @property
    def language_country(self) -> tuple[str, str]:
        language, country = self.locale.lower().split("_", maxsplit=1)
        return language, country

    @property
    def slug(self) -> Path:
        return Path(self.vehicle_model.lower()) / str(self.model_year) / self.locale.lower()


class VinFastManualCrawler:
    """Download every text chapter exposed by VinFast's public manual API."""

    def __init__(
        self,
        data_dir: Path,
        *,
        timeout_seconds: float = 30,
        request_delay_seconds: float = 0.15,
        retries: int = 3,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.timeout_seconds = timeout_seconds
        self.request_delay_seconds = request_delay_seconds
        self.retries = retries

    async def crawl(self, target: CrawlTarget, *, force: bool = False) -> dict[str, Any]:
        raw_dir = self.data_dir / "raw" / target.slug
        chapter_dir = raw_dir / "chapters"
        chapter_dir.mkdir(parents=True, exist_ok=True)
        language, country = target.language_country
        params = {
            "carModel": target.vehicle_model.upper(),
            "version": str(target.model_year),
            "lang": language,
            "country": country,
            "content": "chapter",
        }

        headers = {
            "Accept": "application/json",
            "User-Agent": "ViVi-Handbook-RAG/0.1 (+local evaluation)",
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds, headers=headers) as client:
            menu_payload = await self._get_json(client, params)
            menu_nodes = self._validated_data(menu_payload)
            self._write_json(raw_dir / "menu.json", menu_payload)

            chapters = list(self._chapter_nodes(menu_nodes))
            chapter_records: list[dict[str, Any]] = []
            for index, (parent_name, chapter) in enumerate(chapters, start=1):
                chapter_id = int(chapter["id"])
                destination = chapter_dir / f"{chapter_id}.json"
                status = "cached"
                error: str | None = None
                if force or not destination.exists():
                    try:
                        payload = await self._get_json(client, {**params, "chapter": chapter_id})
                        full_nodes = self._validated_data(payload)
                        full_chapter = self._find_node(full_nodes, chapter_id)
                        if full_chapter is None or not full_chapter.get("html"):
                            raise ValueError(f"chapter {chapter_id} returned no HTML content")
                        snapshot = {
                            "vehicle_model": target.vehicle_model.upper(),
                            "model_year": target.model_year,
                            "locale": target.locale.lower(),
                            "parent": parent_name,
                            "chapter": full_chapter,
                            "fetched_at": datetime.now(UTC).isoformat(),
                        }
                        self._write_json(destination, snapshot)
                        status = "crawled"
                    except Exception as exc:  # keep a complete manifest for resumable runs
                        status = "failed"
                        error = str(exc)
                chapter_records.append(
                    {
                        "id": chapter_id,
                        "parent": parent_name,
                        "name": chapter.get("name", ""),
                        "status": status,
                        "error": error,
                        "path": str(destination.relative_to(raw_dir)),
                    }
                )
                if index < len(chapters):
                    await asyncio.sleep(self.request_delay_seconds)

        manifest = {
            "schema_version": 1,
            "source": DETAIL_URL.format(locale=target.locale.lower()),
            "api": API_URL,
            "vehicle_model": target.vehicle_model.upper(),
            "model_year": target.model_year,
            "locale": target.locale.lower(),
            "crawled_at": datetime.now(UTC).isoformat(),
            "chapter_count": len(chapter_records),
            "successful_count": sum(row["status"] != "failed" for row in chapter_records),
            "failed_count": sum(row["status"] == "failed" for row in chapter_records),
            "chapters": chapter_records,
        }
        manifest["checksum"] = self._checksum(manifest["chapters"])
        self._write_json(raw_dir / "manifest.json", manifest)
        return manifest

    async def _get_json(self, client: httpx.AsyncClient, params: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                response = await client.get(API_URL, params=params)
                response.raise_for_status()
                payload = response.json()
                if not payload.get("success"):
                    raise ValueError(payload.get("message") or "manual API returned success=false")
                return payload
            except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt + 1 < self.retries:
                    await asyncio.sleep(0.5 * (2**attempt))
        raise RuntimeError(f"manual API failed after {self.retries} attempts: {last_error}")

    @staticmethod
    def _validated_data(payload: dict[str, Any]) -> list[dict[str, Any]]:
        data = payload.get("data")
        if not isinstance(data, list):
            raise ValueError("manual API response does not contain a data list")
        return data

    @staticmethod
    def _chapter_nodes(nodes: list[dict[str, Any]]):
        for parent in nodes:
            for child in parent.get("childs") or []:
                if child.get("id") is not None:
                    yield str(parent.get("name") or ""), child

    @classmethod
    def _find_node(cls, nodes: list[dict[str, Any]], node_id: int) -> dict[str, Any] | None:
        for node in nodes:
            if int(node.get("id", -1)) == node_id:
                return node
            found = cls._find_node(node.get("childs") or [], node_id)
            if found is not None:
                return found
        return None

    @staticmethod
    def _checksum(value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
