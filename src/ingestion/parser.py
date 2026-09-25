from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode

from bs4 import BeautifulSoup, Tag

from src.ingestion.crawler import CrawlTarget
from src.rag.schemas import HandbookChunk


_SPACE_RE = re.compile(r"\s+")
_HEADING_CLASSES = {
    "detail-heading",
    "sub-section",
    "section",
    "subsection",
}


@dataclass(frozen=True)
class TextBlock:
    section: str
    content_type: str
    text: str


class HandbookParser:
    def __init__(self, data_dir: Path, *, target_chars: int = 2200, overlap_chars: int = 280) -> None:
        self.data_dir = Path(data_dir)
        self.target_chars = target_chars
        self.overlap_chars = overlap_chars

    def parse(self, target: CrawlTarget) -> list[HandbookChunk]:
        raw_dir = self.data_dir / "raw" / target.slug
        manifest_path = raw_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"crawl manifest not found: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        failures = [row for row in manifest["chapters"] if row["status"] == "failed"]
        if failures:
            failed_ids = ", ".join(str(row["id"]) for row in failures)
            raise RuntimeError(f"crawl is incomplete; retry failed chapters: {failed_ids}")

        chunks: list[HandbookChunk] = []
        for row in manifest["chapters"]:
            snapshot_path = raw_dir / row["path"]
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            chunks.extend(self._parse_snapshot(snapshot, target))

        parsed_dir = self.data_dir / "parsed" / target.slug
        parsed_dir.mkdir(parents=True, exist_ok=True)
        output = parsed_dir / "chunks.jsonl"
        temporary = output.with_suffix(".jsonl.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            for chunk in chunks:
                stream.write(chunk.model_dump_json() + "\n")
        temporary.replace(output)
        summary = {
            "schema_version": 1,
            "vehicle_model": target.vehicle_model.upper(),
            "model_year": target.model_year,
            "locale": target.locale.lower(),
            "chunk_count": len(chunks),
            "source_manifest_checksum": manifest["checksum"],
            "chunks_checksum": self._sha256(output.read_bytes()),
        }
        (parsed_dir / "manifest.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return chunks

    def _parse_snapshot(self, snapshot: dict, target: CrawlTarget) -> list[HandbookChunk]:
        chapter = snapshot["chapter"]
        chapter_id = int(chapter["id"])
        chapter_name = self._clean(chapter.get("name") or f"Chapter {chapter_id}")
        parent = self._clean(snapshot.get("parent") or "")
        blocks = list(self._extract_blocks(chapter.get("html") or "", chapter_name))
        source_url = self._source_url(target, chapter_id)
        document_id = f"{target.vehicle_model.upper()}-{target.model_year}-{target.locale.lower()}-{chapter_id}"

        chunks: list[HandbookChunk] = []
        for index, (section, content_type, content) in enumerate(self._group_blocks(blocks)):
            section_path = [value for value in (parent, chapter_name, section) if value]
            checksum = self._sha256(content.encode("utf-8"))
            source_id = f"vf-{hashlib.sha256(f'{document_id}:{index}:{checksum}'.encode()).hexdigest()[:20]}"
            chunks.append(
                HandbookChunk(
                    source_id=source_id,
                    document_id=document_id,
                    source_url=source_url,
                    vehicle_model=target.vehicle_model.upper(),
                    model_year=target.model_year,
                    locale=target.locale.lower(),
                    chapter_id=chapter_id,
                    chapter=chapter_name,
                    section_path=section_path,
                    content_type=content_type,
                    content=content,
                    checksum=checksum,
                    chunk_index=index,
                )
            )
        return chunks

    def _extract_blocks(self, html: str, default_section: str) -> Iterable[TextBlock]:
        soup = BeautifulSoup(html, "html.parser")
        current_section = default_section
        for element in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "tr"]):
            if not isinstance(element, Tag):
                continue
            if element.name in {"li", "tr"} and element.find_parent(["li", "tr"]) is not None:
                continue
            if element.name == "p" and element.find_parent(["li", "tr"]) is not None:
                continue
            text = self._clean(element.get_text(" ", strip=True))
            if not text or text.upper() in {"CẢNH BÁO", "THẬN TRỌNG", "LƯU Ý"}:
                continue
            classes = {str(value).lower() for value in element.get("class", [])}
            is_heading = element.name in {"h1", "h2", "h3", "h4"} or bool(classes & _HEADING_CLASSES)
            if is_heading:
                current_section = text
                continue
            content_type = "paragraph"
            class_text = " ".join(classes)
            if "warning" in class_text:
                content_type = "warning"
            elif "caution" in class_text:
                content_type = "caution"
            elif "note" in class_text:
                content_type = "note"
            elif element.name == "li":
                content_type = "list"
            elif element.name == "tr":
                content_type = "table"
            yield TextBlock(section=current_section, content_type=content_type, text=text)

    def _group_blocks(self, blocks: list[TextBlock]) -> Iterable[tuple[str, str, str]]:
        current_section = ""
        current_type = "paragraph"
        buffer: list[str] = []

        def flush() -> Iterable[tuple[str, str, str]]:
            nonlocal buffer
            if not buffer:
                return []
            combined = "\n".join(buffer)
            buffer = []
            return [(current_section, current_type, part) for part in self._split_text(combined)]

        for block in blocks:
            would_overflow = buffer and sum(len(value) for value in buffer) + len(block.text) > self.target_chars
            boundary = block.section != current_section or block.content_type in {"warning", "caution"}
            if boundary or would_overflow:
                yield from flush()
                current_section = block.section
                current_type = block.content_type
            elif not current_section:
                current_section = block.section
                current_type = block.content_type
            buffer.append(block.text)
        yield from flush()

    def _split_text(self, text: str) -> list[str]:
        if len(text) <= self.target_chars:
            return [text]
        parts: list[str] = []
        start = 0
        while start < len(text):
            end = min(len(text), start + self.target_chars)
            if end < len(text):
                boundary = text.rfind("\n", start, end)
                if boundary <= start:
                    boundary = text.rfind(". ", start, end)
                if boundary > start + self.target_chars // 2:
                    end = boundary + 1
            part = text[start:end].strip()
            if part:
                parts.append(part)
            if end >= len(text):
                break
            start = max(end - self.overlap_chars, start + 1)
        return parts

    @staticmethod
    def _source_url(target: CrawlTarget, chapter_id: int) -> str:
        query = urlencode(
            {"car": target.vehicle_model.upper(), "year": target.model_year, "lv2": chapter_id}
        )
        return f"https://om.vinfastauto.com/{target.locale.lower()}/detail?{query}"

    @staticmethod
    def _clean(value: str) -> str:
        return _SPACE_RE.sub(" ", value.replace("\ufeff", " ")).strip()

    @staticmethod
    def _sha256(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()
