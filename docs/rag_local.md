# Offline VF8 RAG on AGX Xavier

## Current implementation

The default profile is `sqlite_local`: SQLite FTS5 plus local E5 vectors.
Both `python run.py` and `scripts/run_rag_local.sh` use this profile. Existing
crawl/parser tools remain the corpus source. The original 878-chunk corpus is
preserved in tracked `data/handbooks/handbook-source.sqlite3` for rebuilds and
benchmark provenance.

The rebuilt, tracked runtime artifact is `data/handbooks/handbook.sqlite3`. It contains FTS5,
normalized float32 embedding BLOBs, source metadata and a model/corpus manifest.
There is no extra database server or SQLite extension. A scoped NumPy matrix
performs exact cosine search; independent FTS5 and vector candidates are merged
with reciprocal rank fusion. With the current 931 vectors × 384 dimensions the
vector matrix occupies about 1.36 MiB, excluding metadata/Python objects.

## Embedding choice

Initial model: [intfloat/multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small).
The [Xenova ONNX conversion](https://huggingface.co/Xenova/multilingual-e5-small)
provides an 8-bit artifact. Revision is pinned to
`761b726dd34fb83930e26aab4e9ac3899aa1fa78`, with SHA-256 recorded per file.

E5 uses 384-dimensional normalized vectors, masked mean pooling, and distinct
`query: ` / `passage: ` prefixes even for Vietnamese. Runtime uses only ONNX
Runtime CPU with two intra-op threads, one inter-op thread and thread spinning
disabled, following [ONNX Runtime thread controls](https://onnxruntime.ai/docs/performance/tune-performance/threading.html).
No torch, CUDA or cloud SDK is needed for embedding inference. This is a measured
starting candidate, not a claim that it is the globally best model for Xavier.

The model weight file is about 113 MiB. On the dev host an isolated adapter used
about 464 MiB resident RAM after the first query, with about 543 MiB peak RSS.
Tokenizer memory is significant; weight size alone is not the runtime budget.
These values must be remeasured on Xavier alongside the other models.

## Provision once, then run offline

Provision on PC or Xavier with network access, or copy the prepared files to the
device. This downloads public weights; it does not call a hosted embedding API.
Use a Python environment/container compatible with the installed JetPack version.
CPU-only inference avoids depending on JetPack CUDA wheels.

```bash
.venv/bin/python -m pip install -r requirements-rag.txt
.venv/bin/python -m src.vivi.cli.prepare_embeddings
# If Python HTTPS access is unavailable, the same pinned download can use curl:
.venv/bin/python -m src.vivi.cli.prepare_embeddings --download-client curl

# Embed the entire previously crawled handbook locally.
.venv/bin/python -m src.vivi.cli.build_local_index

# Start the API with local embedding/retrieval and rules/extractive fallback.
bash scripts/run_rag_local.sh

# Use the self-hosted SLM once configured; this is an example, not an installed model.
LOCAL_LLM_MODEL=YOUR_LOCAL_MODEL LLM_PROVIDER=local bash scripts/run_rag_local.sh
```

If the existing speech environment already has a working GPU ONNX Runtime, install
`requirements-rag-base.txt` instead and retain that runtime: the embedding adapter
still selects `CPUExecutionProvider`. Do not install CPU and GPU distributions over
each other in the same environment. The dev host already contains both package
distributions; reports therefore record the imported runtime version and actual
embedding execution provider as well as package metadata.

Copy `models/multilingual-e5-small-int8/` and `data/handbooks/handbook.sqlite3` together
to Xavier. Weights are ignored by Git; the runtime database is tracked. The standard
Dockerfile includes CPU embedding dependencies and copies prepared E5 artifacts
from the build context (provision them before building). A read-only model mount
is also supported:

```bash
docker build --build-arg REQUIREMENTS_FILE=requirements-rag.txt -t vivi-rag-local .
docker run --rm -p 127.0.0.1:8787:8787 \
  -v "$PWD/models:/app/models:ro" -v "$PWD/data:/app/data" \
  -e RAG_RETRIEVAL_MODE=sqlite_local -e VIVI_RAG_LOCAL_ONLY=true \
  -e LLM_PROVIDER=rules vivi-rag-local
```

Docker/ARM deployment has not been validated in this change; confirm the Xavier
JetPack, Python, ONNX Runtime wheel and memory budget before using it as acceptance
evidence. NVIDIA's [JetPack archive](https://developer.nvidia.com/embedded/jetpack-archive)
lists supported Xavier releases. These instructions address RAG; STT/TTS provider
selection remains separate.

## Corpus and artifact guarantees

The source currently contains 878 chunks. Long chunks are split on word boundaries
until the complete heading + passage prefix + content fits within 480 tokens of
the 512-token model budget, with 24-word overlap. The complete source is embedded;
long tails are not silently truncated. This produces 931 indexed chunks.

Build is performed in a temporary sibling database and atomically published only
after all vectors are validated and SQLite WAL is checkpointed. Failed builds
preserve the previous artifact. Runtime opens the published database read-only,
checks completeness and embedding identity, and refuses a model/index mismatch.
Restart after publishing a new artifact because readers cache an immutable matrix.

Changing model, pooling, prefixes, tokenizer or chunking requires rebuilding.
The CLI intentionally rebuilds all vectors; it builds from the source corpus. Legacy Gemini embedding/generation and Chroma
retrieval/indexing code have been removed.

## Local-only behavior and limits

`VIVI_RAG_LOCAL_ONLY=true` (default) permits only rules or the self-hosted local LLM in the
RAG services. Cloud provider graphs are unavailable in this profile. Missing local
embedding artifacts fail explicitly; no cloud embedding fallback is attempted.
Point the local LLM URL at your own loopback server. Benchmark additionally blocks
non-loopback TCP connections.

Cosine similarity alone does not establish answerability: development positives
and negatives overlap. `rag_local_min_similarity=0.0` is intentionally uncalibrated;
do not claim it is a hallucination rejection threshold. The existing grounding
validator checks source ID integrity, not full semantic support. The benchmark
exposes this limitation, including negative cases the extractive fallback answers
incorrectly. Use the actual SLM and human review to improve answer sufficiency.

See [evaluation instructions](../eval/README.md) for separate intent, retrieval and
oracle-evidence generation measurements.
