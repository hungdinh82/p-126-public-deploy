"""Export the local planner's MCP-shaped tool documentation (no MCP transport)."""
import argparse
import json
from pathlib import Path

from src.vivi.capabilities.registry import catalog
from src.vivi.dialogue.local_tools import local_tool_documents

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--navigation', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = {'tools': local_tool_documents(catalog(args.navigation)),
              '_meta': {'contract': 'local-proposals-v1', 'proposalOnly': True, 'mcpTransport': False}}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(f'{len(result["tools"])} tools written to {args.output}')
