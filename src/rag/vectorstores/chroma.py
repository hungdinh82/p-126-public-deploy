from __future__ import annotations

from pathlib import Path

from src.rag.schemas import HandbookChunk, RetrievedChunk


class ChromaHandbookStore:
    def __init__(self, persist_dir: Path | str, collection_name: str) -> None:
        import chromadb

        Path(persist_dir).mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=str(persist_dir))
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def upsert(self, chunks: list[HandbookChunk], embeddings: list[list[float]]) -> None:
        if not chunks:
            return
        self.collection.upsert(
            ids=[chunk.source_id for chunk in chunks],
            documents=[chunk.content for chunk in chunks],
            metadatas=[chunk.chroma_metadata() for chunk in chunks],
            embeddings=embeddings,
        )

    def existing_records(self) -> dict[str, dict]:
        result = self.collection.get(include=["metadatas", "embeddings"])
        embeddings = result.get("embeddings")
        embedding_values = embeddings.tolist() if hasattr(embeddings, "tolist") else (embeddings or [])
        return {
            source_id: {**dict(metadata), "_embedding": list(embedding)}
            for source_id, metadata, embedding in zip(
                result["ids"], result.get("metadatas") or [], embedding_values, strict=False
            )
        }

    def delete(self, source_ids: list[str]) -> None:
        if source_ids:
            self.collection.delete(ids=source_ids)

    def query(
        self,
        query_embedding: list[float],
        *,
        vehicle_model: str,
        model_year: int,
        locale: str,
        limit: int,
    ) -> list[RetrievedChunk]:
        if self.collection.count() == 0:
            return []
        result = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=min(limit, self.collection.count()),
            where={
                "$and": [
                    {"vehicle_model": {"$eq": vehicle_model.upper()}},
                    {"model_year": {"$eq": model_year}},
                    {"locale": {"$eq": locale.lower()}},
                ]
            },
            include=["documents", "metadatas", "distances"],
        )
        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        chunks: list[RetrievedChunk] = []
        for source_id, content, metadata, distance in zip(
            ids, documents, metadatas, distances, strict=False
        ):
            chunks.append(
                RetrievedChunk(
                    source_id=source_id,
                    document_id=str(metadata["document_id"]),
                    source_url=str(metadata["source_url"]),
                    vehicle_model=str(metadata["vehicle_model"]),
                    model_year=int(metadata["model_year"]),
                    locale=str(metadata["locale"]),
                    chapter_id=int(metadata["chapter_id"]),
                    chapter=str(metadata["chapter"]),
                    section_path=str(metadata["section_path"]).split(" > "),
                    content_type=str(metadata["content_type"]),
                    content=content or "",
                    checksum=str(metadata["checksum"]),
                    chunk_index=int(metadata["chunk_index"]),
                    semantic_distance=float(distance),
                )
            )
        return chunks
