from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from src.vivi.config import get_settings
from src.vivi.orchestration import create_langgraph_orchestrator
from src.vivi.persistence.event_store import EventStore
from src.vivi.vehicle.memory import VehicleSimulator


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
    confirmation = result.get("pending_question")
    if confirmation:
        print(f"Question: {confirmation['id']} — {confirmation['question']}")
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
    question_id: str | None = None,
    option_id: str | None = None,
) -> dict:
    state = {
        "session_id": session_id,
        "turn_id": str(uuid4()),
    }
    if input_text is not None:
        state["input_text"] = input_text
    if payload is not None:
        state["action"] = (
            payload
            if "capability" in payload
            else {
                "capability": payload["intent"],
                "arguments": {
                    k: v
                    for k, v in payload.get("arguments", {}).items()
                    if k not in {"vehicle_model", "model_year", "locale"}
                },
            }
        )
        state["input_text"] = state["action"].get("arguments", {}).get("query", "Thao tác trực tiếp")
    if question_id is not None:
        state["question_reply"] = {"question_id": question_id, "option_id": option_id}
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
        config = get_settings()
        if args.retrieval:
            config = config.model_copy(update={"rag_retrieval_mode": args.retrieval})
        runtime = create_langgraph_orchestrator(VehicleSimulator(), EventStore(config), config)
        graph = runtime.graph
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        parser.exit(2, f"Không thể khởi tạo RAG: {exc}\n")
    try:
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
                    confirmation = result.get("pending_question")
                    if confirmation and confirmation.get("purpose") == "approve_execution":
                        choice = input("Xác nhận thao tác? [y/N]: ").strip().lower()
                        decision = "approve" if choice in {"y", "yes", "có", "co"} else "deny"
                        resolved = _ask(
                            graph,
                            args.session,
                            input_text="Xác nhận" if decision == "approve" else "Hủy",
                            question_id=confirmation["id"],
                            option_id="yes" if decision == "approve" else "no",
                        )
                        _print_result(resolved)
    finally:
        graph.close()


if __name__ == "__main__":
    main()
