from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_public_image_provisions_pinned_local_embeddings():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "RAG_RETRIEVAL_MODE=sqlite_local" in dockerfile
    assert "python -m src.vivi.cli.prepare_embeddings" in dockerfile
    assert "761b726dd34fb83930e26aab4e9ac3899aa1fa78" in dockerfile
    assert "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39" in dockerfile
    assert "f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193" in dockerfile
    assert (ROOT / "data/handbooks/handbook.sqlite3").is_file()


def test_hardware_default_keeps_local_vector_retrieval():
    config = (ROOT / "config.toml").read_text(encoding="utf-8")

    assert 'rag_retrieval_mode = "sqlite_local"' in config


def test_public_image_publishes_to_current_repository_namespace():
    workflow = (ROOT / ".github/workflows/deploy-render.yml").read_text(encoding="utf-8")

    assert "IMAGE_NAME: ghcr.io/${{ github.repository }}" in workflow
    assert "ghcr.io/ai20k-build-phase-cohort-4/p-126" not in workflow
    assert "packages: write" in workflow


def test_public_deploy_fetches_merge_history_before_validation():
    workflow = (ROOT / ".github/workflows/deploy-render.yml").read_text(encoding="utf-8")

    checkout_step = workflow.split("- name: Check out source", 1)[1].split(
        "- name: Set up Python", 1
    )[0]
    assert "fetch-depth: 0" in checkout_step
