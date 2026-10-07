from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from src.vivi.agents.graph import build_graph
from src.vivi.rag.runtime import create_services


def _print_result(result: dict) -> None:
    print(f"\nViVi: {result.get('answer') or result.get('response') or 'Không có phản hồi.'}")
    output = result.get("output") or {}
    print(f"Route: {output.get('route', result.get('route', 'unknown'))}")
    if output.get("action_proposal"):
        print("Action proposal: " + json.dumps(output["action_proposal"], ensure_ascii=False))
    execution = output.get("execution")
    if execution:
        if execution.get("verified"):
            execution_label = "verified"
        elif execution.get("executed"):
            execution_label = "unverified"
        elif execution.get("allowed"):
            execution_label = "allowed"
        else:
            execution_label = "not executed"
        print(f"Execution: {execution_label}")
    elif output.get("requires_execution"):
        print("Execution: pending safety gateway")
    confirmation = output.get("confirmation")
    if confirmation:
        print(f"Confirmation: {confirmation['confirmation_id']} — {confirmation['preview']}")
    citations = result.get("citations") or []
    if citations:
        print("\nNguồn:")
        for index, citation in enumerate(citations, start=1):
            path = " > ".join(citation["section_path"])
            print(f"[{index}] {path}")
            print(f"    {citation['source_url']}")
    print(f"\nGrounding: {result.get('grounding_status', 'unknown')}")
    timings = result.get("timings") or {}
    if timings:
        print("Latency: " + ", ".join(f"{key}={value:.2f}ms" for key, value in timings.items()))
        print(f"Total: {sum(timings.values()):.2f}ms")
    if result.get("errors"):
        print("Errors: " + json.dumps(result["errors"], ensure_ascii=False))


def _ask(
    graph,
    session_id: str,
    *,
    input_text: str | None = None,
    payload: dict | None = None,
    confirmation_id: str | None = None,
    confirmation_decision: str | None = None,
) -> dict:
    state = {
        "session_id": session_id,
        "turn_id": str(uuid4()),
    }
    if input_text is not None:
        state["input_text"] = input_text
    if payload is not None:
        state["model_input"] = payload
    if confirmation_id is not None:
        state["confirmation_id"] = confirmation_id
        state["confirmation_decision"] = confirmation_decision
    return graph.invoke(state)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask grounded questions about the VF8 2026 handbook")
    parser.add_argument("--session", default="terminal-preview")
    parser.add_argument("--text", help="Raw transcript text, equivalent to STT final output")
    parser.add_argument("--input", type=Path, help="Legacy/preclassified JSON fixture")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument(
        "--retrieval",
        choices=("sqlite", "sqlite_local", "lexical"),
        default=None,
        help="Use the configured local SQLite vector pipeline; sqlite selects the FTS5-only baseline",
    )
    args = parser.parse_args()
    if not args.text and not args.input and not args.interactive:
        parser.error("choose --text, --input FILE, or --interactive")

    try:
        graph = build_graph(create_services(retrieval_mode=args.retrieval))
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        parser.exit(2, f"Không thể khởi tạo RAG: {exc}\n")
    if args.input:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        _print_result(_ask(graph, args.session, payload=payload))
    if args.text:
        _print_result(_ask(graph, args.session, input_text=args.text))
    if args.interactive:
        print("ViVi handbook VF8 2026. Gõ 'exit' để thoát.")
        while True:
            try:
                query = input("\nBạn: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if query.lower() in {"exit", "quit", "thoát", "thoat"}:
                break
            if query:
                result = _ask(graph, args.session, input_text=query)
                _print_result(result)
                confirmation = (result.get("output") or {}).get("confirmation")
                if confirmation:
                    choice = input("Xác nhận thao tác? [y/N]: ").strip().lower()
                    decision = "approve" if choice in {"y", "yes", "có", "co"} else "deny"
                    resolved = _ask(
                        graph,
                        args.session,
                        input_text="Xác nhận" if decision == "approve" else "Hủy",
                        confirmation_id=confirmation["confirmation_id"],
                        confirmation_decision=decision,
                    )
                    _print_result(resolved)


if __name__ == "__main__":
    main()
