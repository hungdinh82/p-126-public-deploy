from __future__ import annotations

from pathlib import Path

import pytest

from eval.benchmark import local_network_only


def test_real_e5_is_offline_normalized_and_chunking_preserves_tail():
    directory = Path("models/multilingual-e5-small-int8")
    if not (directory / "manifest.json").exists():
        pytest.skip("Prepare the optional local embedding artifact first")
    np = pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    pytest.importorskip("tokenizers")
    from src.vivi.cli.build_local_index import embedding_text, split_chunk
    from src.vivi.rag.embeddings.local import LocalE5EmbeddingProvider
    from src.vivi.rag.schemas import HandbookChunk

    with local_network_only():
        provider = LocalE5EmbeddingProvider(directory)
        documents = provider.embed_documents([
            "Pin SDI của VF8 có dung lượng sử dụng 82 kWh.",
            "Công thức làm bánh mì với bột mì và men nở.",
        ])
        query = np.asarray(provider.embed_query("Dung lượng pin SDI VF8 bao nhiêu?"))
        matrix = np.asarray(documents)
        assert matrix.shape == (2, 384)
        assert np.allclose(np.linalg.norm(matrix, axis=1), 1, atol=1e-5)
        assert float(matrix[0] @ query) > float(matrix[1] @ query)
        content = "Nội dung cẩm nang xe. " * 350 + "Phần cuối không được mất."
        source = HandbookChunk(source_id="long", document_id="manual", source_url="https://example.test",
                               vehicle_model="VF8", model_year=2026, locale="vi_vn", chapter_id=1,
                               chapter="Pin", section_path=["Pin"], content_type="paragraph", content=content,
                               checksum="source", chunk_index=0)
        pieces = split_chunk(source, provider)
        assert len(pieces) > 1
        assert pieces[-1].content.endswith("Phần cuối không được mất.")
        assert all(provider.token_count("passage: " + embedding_text(piece)) <= 480 for piece in pieces)
