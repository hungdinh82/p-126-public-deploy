"""Export current registry-derived local proposals without connecting to a model."""

import argparse
import json
from pathlib import Path

from src.vivi.capabilities.registry import catalog
from src.vivi.dialogue.local_operations import CONTRACT_VERSION
from src.vivi.dialogue.local_tools import local_tool_documents, proposal_tools


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("docs/local_tool_catalog.json"))
    args = parser.parse_args()
    documents = local_tool_documents(catalog(navigation=True))
    tools = proposal_tools(documents, {}, "")
    result = {
        "contract": CONTRACT_VERSION,
        "proposal_only": True,
        "canonical_documents": documents,
        "fresh_session_tools": [{"type": t["type"], "function": t["function"]} for t in tools],
        "bindings": [
            {
                "model_tool": t["function"]["name"],
                "host_tool": t["_binding"]["canonical"]["name"],
                **{k: v for k, v in t["_binding"].items() if k != "canonical"},
            }
            for t in tools
            if t.get("_binding")
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
