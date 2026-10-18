"""Offline HTML analysis and self-contained failure bundles from a completed run."""

import argparse
import hashlib
import html
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LABELS = {"doors": "Cửa xe", "windows": "Cửa kính", "music": "Bật/tắt nhạc", "climate": "Điều hòa",
          "body_panels": "Capo/cốp", "vehicle_status": "Trạng thái xe", "navigation": "Điều hướng",
          "information": "Hỏi thông tin", "memory": "Bộ nhớ", "dialogue": "Hội thoại",
          "hard_cases": "Câu khó", "multi_tool": "Kết hợp tool", "adversarial": "Đánh đố"}
STYLE = """
:root{font-family:system-ui,sans-serif;color:#e3e9f2;background:#101722;color-scheme:dark}
body{max-width:1200px;margin:auto;padding:28px}h1{font-size:2rem}h2{margin-top:32px}
a{color:#8cc9ff}p,li{line-height:1.65}.muted{color:#a9b6ca}.cards{display:flex;gap:14px;flex-wrap:wrap}
.card{background:#1a2739;border:1px solid #34455c;border-radius:12px;padding:16px;min-width:150px}
.card strong{display:block;font-size:1.8rem}table{width:100%;border-collapse:collapse;margin:16px 0}
td,th{text-align:left;padding:11px;border-bottom:1px solid #34455c;vertical-align:top}
pre{background:#09111d;border:1px solid #34455c;border-radius:8px;padding:16px;white-space:pre-wrap;overflow-wrap:anywhere}
code{overflow-wrap:anywhere}details{margin:12px 0}summary{cursor:pointer;padding:10px;background:#1a2739}
input,select{padding:10px;border-radius:6px;border:1px solid #50617a;margin:6px;background:#1a2739;max-width:90%}
.bad{color:#ffb1a6}.good{color:#8fe3b4}.scroll{overflow:auto}.tag{font-size:.8rem;color:#ffd49b}
.compare{display:grid;grid-template-columns:1fr 1fr;gap:14px}.compare>div{min-width:0}
.comparison-list{table-layout:fixed}.comparison-list th:first-child{width:30%}
.comparison-list pre{font-size:12px;max-height:280px;overflow:auto;margin:8px 0}
.input{border-left:3px solid #8cc9ff;padding:10px 14px;background:#1a2739}
@media(max-width:600px){body{padding:14px}td,th{padding:7px}.card{min-width:110px}}
@media(max-width:850px){.compare{grid-template-columns:1fr}.comparison-list{min-width:850px}}
"""


def esc(value):
    return html.escape(str(value), quote=True)


def block(value):
    return "<pre>" + esc(json.dumps(value, ensure_ascii=False, indent=2)) + "</pre>"


def page(title, body):
    return f'<!doctype html><html lang="vi"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(title)}</title><style>{STYLE}</style><body>{body}</body></html>'


def model_output(turn):
    """Expose recorded proposals, keeping repairs distinct from agent results."""
    actual = turn.get("actual", {})
    proposals = [proposal for attempt in actual.get("attempts", [])
                 for proposal in attempt.get("proposal_attempts", [])]
    if proposals:
        return [{"lần gọi": index, "tool_calls": proposal.get("calls", []),
                 "validation": proposal.get("validation")}
                for index, proposal in enumerate(proposals, 1)]
    if actual.get("tool_calls") or actual.get("first_calls"):
        return {"first_calls": actual.get("first_calls", []), "tool_calls": actual.get("tool_calls", [])}
    if not actual.get("attempts"):
        return "Không có output model được ghi nhận ở lượt này (có thể do host/rules xử lý)."
    semantic = actual.get("semantic") or actual
    if not actual.get("fallback") and any(attempt.get("outcome") == "served" for attempt in actual["attempts"]):
        return {key: semantic[key] for key in ("act", "text", "actions") if key in semantic}
    return {"ghi chú": "Không có tool call/model output hợp lệ trong log; xem lỗi attempt.",
            "attempts": [{key: attempt[key] for key in ("outcome", "reason", "http_status") if key in attempt}
                         for attempt in actual["attempts"]]}


def comparison(turn):
    failed = {key: value for key, value in turn.get("checks", {}).items() if not value["passed"]}
    expected = {key: value["expected"] for key, value in failed.items()}
    observed = {key: value["actual"] for key, value in failed.items()}
    actual = turn.get("actual", {})
    body = '<div class="compare"><div><h3 class="good">Expected — kỳ vọng bài test</h3>' + block(expected)
    body += '</div><div><h3 class="bad">Output model — các lần gọi đã ghi nhận</h3>' + block(model_output(turn)) + '</div></div>'
    body += '<h3>Kết quả agent dùng để chấm</h3>' + block(observed)
    output = actual.get("output", {})
    if output.get("response_text"):
        body += '<p><strong>Phản hồi cuối cho người dùng:</strong></p><div class="input">' + esc(output["response_text"]) + '</div>'
    if turn.get("error"):
        body += '<p class="bad">' + esc(turn["error"]) + '</p>'
    return body


def catalog(report):
    """Capture exact authored JSONL, including initial state, history and oracles."""
    cases = {}
    hashes = {}
    for path in sorted((ROOT / "cases").rglob("*.jsonl")):
        relative = str(path.relative_to(ROOT / "cases"))
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        for line, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            data = json.loads(raw)
            cases[(path.stem, data["id"])] = {
                "source": f"benchmarks/agent/cases/{relative}:{line}",
                "sha256": hashes[relative], "test": data,
            }
    if report.get("dataset_sha256") != hashes:
        raise ValueError("Dataset differs from inference snapshot; refusing to attach mismatched tests")
    return cases


def explain(turn):
    """Evidence-based failure explanation; never infer an unobserved model cause."""
    actual = turn.get("actual", {})
    attempts = actual.get("attempts", [])
    failures = {k: v for k, v in turn.get("checks", {}).items() if not v["passed"]}
    reasons = []
    proposals = [p for attempt in attempts for p in attempt.get("proposal_attempts", [])]
    offered = {cap for proposal in proposals for cap in proposal.get("catalog", [])}
    wanted = {decision["name"] for key, check in failures.items() if "decisions" in key
              for decision in (check["expected"] if isinstance(check["expected"], list) else [])}
    if offered and wanted - offered:
        reasons.append(("catalog", f"Không lần proposal nào cung cấp capability kỳ vọng {sorted(wanted - offered)}. Catalog được cung cấp là {sorted(offered)}. Có tool discovery, nhưng trace không cho thấy capability thiếu được đưa vào lần gọi tiếp theo; cần rà bước chọn/tìm tool trước khi quy hoàn toàn cho model."))
    if turn.get("error"):
        reasons.append(("runtime", f"Lượt chạy phát sinh ngoại lệ: {turn['error']}. Chưa đủ dữ liệu để kết luận model hiểu sai."))
    http_errors = sorted({a["http_status"] for a in attempts if isinstance(a.get("http_status"), int) and a["http_status"] >= 400})
    if http_errors:
        reasons.append(("service", f"Dịch vụ model trả HTTP {http_errors}; đây là lỗi dịch vụ trong lượt chạy."))
    for key, check in failures.items():
        if key == "provider_served" or key.endswith("effective_provider") or key == "fallback":
            reasons.append(("provider", "Provider được phục vụ/fallback không đúng kỳ vọng; không tính phản hồi rules thành model hiểu đúng."))
        elif "decisions" in key:
            expected = check["expected"] if isinstance(check["expected"], list) else []
            observed = check["actual"] if isinstance(check["actual"], list) else []
            def names(values):
                return Counter(v.get("name") for v in values)
            if names(expected) != names(observed):
                reasons.append(("capability", f"Bài test yêu cầu {dict(names(expected))}; agent trả {dict(names(observed))}. Có capability bị thiếu, thừa hoặc chọn sai."))
            elif "ordered_decisions" in key and expected != observed and Counter(json.dumps(v, sort_keys=True) for v in expected) == Counter(json.dumps(v, sort_keys=True) for v in observed):
                reasons.append(("order", "Đủ thao tác nhưng thứ tự không khớp câu yêu cầu tuần tự."))
            else:
                reasons.append(("arguments", f"Đúng nhóm capability nhưng tham số/đích hoặc thứ tự không khớp. Kỳ vọng: {json.dumps(expected, ensure_ascii=False)}. Thực tế: {json.dumps(observed, ensure_ascii=False)}."))
        elif key == "blocked" or key.startswith(("negated", "conditional", "scheduled", "conflicting", "unresolved")):
            reasons.append(("guard", "Cách xử lý câu phủ định, thiếu thông tin, điều kiện hoặc yêu cầu ngoài phạm vi không khớp chính sách của bài test."))
        elif key.startswith(("vehicle", "changed_paths")):
            reasons.append(("state", "Trạng thái xe/thay đổi sau lượt chạy không khớp kỳ vọng. Đây là kết quả cả luồng agent, chưa xác định riêng lỗi model hay host."))
        else:
            reasons.append(("contract", f"Không đạt điều kiện `{key}` của bài test; cần đối chiếu trạng thái hội thoại, output và trace để xác định tầng gây lỗi."))
    return list(dict.fromkeys(reasons)) or [("unknown", "Ca được đánh dấu fail nhưng không có assertion/ngoại lệ chi tiết trong log.")]


def failure_bundle(row, authored, report):
    first = next(index for index, turn in enumerate(row["turns"]) if not turn["passed"])
    return {"id": row["id"], "suite": row["suite"], "feature": row["feature"],
            "generation": {k: report.get(k) for k in ("mode", "provider", "model", "source_sha256", "scoring_source_sha256", "dataset_sha256")},
            **authored, "result": row, "first_failed_turn": row["turns"][first].get("turn"),
            "multi_turn_note": ("Ca đã fail trước lượt cuối. Lỗi ở lượt sau có thể là hệ quả của task/câu hỏi chưa được tạo đúng; không kết luận riêng từ xác nhận sai khi lượt trước đã fail." if first < len(row["turns"]) - 1 else None),
            "explanations": [{"turn": turn.get("turn"), "reasons": explain(turn)}
                             for turn in row["turns"] if not turn["passed"]]}


def write_html(report, output):
    cases = catalog(report)
    output.mkdir(parents=True, exist_ok=True)
    rows = report["results"]
    failed = [row for row in rows if not row["passed"]]
    groups = Counter()
    failure_links = []
    for row in failed:
        authored = cases[(row["suite"], row["id"])]
        bundle = failure_bundle(row, authored, report)
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", row["id"])
        suffix = hashlib.sha256(f"{row['suite']}:{row['id']}:{row['repeat']}".encode()).hexdigest()[:8]
        relative = Path("failures") / row["feature"] / f"{row['suite']}--{safe_id}--r{row['repeat']}-{suffix}"
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.with_suffix(".json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        categories = set()
        body = f'<a href="../../analysis.html">← Phân tích tổng hợp</a><h1>{esc(row["id"])}</h1><p>{esc(row["suite"])} · {esc(row["feature"])} · lần {row["repeat"]}</p>'
        body += f'<p>Nguồn test: <code>{esc(authored["source"])}</code></p><p><a href="{esc(target.with_suffix(".json").name)}" download>JSON đầy đủ: bài test + output + giải thích</a></p>'
        if bundle["multi_turn_note"]:
            body += f'<p class="bad">Lượt fail đầu tiên: {esc(bundle["first_failed_turn"])}. {esc(bundle["multi_turn_note"])}</p>'
        for turn in row["turns"]:
            body += f'<h2>Lượt {esc(turn.get("turn", "?"))}: {"ĐẠT" if turn["passed"] else "FAIL"}</h2><div class="input">{esc(turn.get("text", ""))}</div>'
            if not turn["passed"]:
                reasons = explain(turn)
                categories.update(category for category, _ in reasons)
                body += "<ul>" + "".join(f"<li>{esc(reason)}</li>" for _, reason in reasons) + "</ul>"
                body += comparison(turn)
            body += "<details><summary>Output/trace đầy đủ của lượt</summary>" + block(turn) + "</details>"
        body += "<h2>Nội dung bài test gốc</h2>" + block(authored["test"])
        body += "<details><summary>Metadata phiên chạy</summary>" + block(bundle["generation"]) + "</details>"
        target.with_suffix(".html").write_text(page(row["id"], body), encoding="utf-8")
        groups.update(categories)
        texts = [turn.get("text", "") for turn in row["turns"]]
        failure_links.append((row, relative.with_suffix(".html").as_posix(), categories, " / ".join(filter(None, texts))))

    total = len(rows)
    passed = sum(row["passed"] for row in rows)
    body = f'<h1>Phân tích benchmark agent</h1><p class="muted">{esc(report.get("model"))} · {esc(report.get("provider"))} · {esc(report.get("mode"))}</p>'
    body += '<div class="cards">' + "".join(f'<div class="card">{name}<strong>{value}</strong></div>' for name, value in [
        ("Đã chạy", total), ("Đạt", passed), ("Không đạt", len(failed)), ("Tỉ lệ đạt", f"{passed / total * 100:.1f}%" if total else "—")]) + "</div>"
    body += '<p>Điểm được tính theo oracle cố định. Phân tích dưới đây dựa trên assertion và trace đã ghi; không khẳng định nguyên nhân nội tại của model.</p>'
    coverage_path = output / "coverage.json"
    if coverage_path.exists():
        coverage = json.loads(coverage_path.read_text())
        offline_link = esc(coverage.get("offline_report", "../offline-complete/analysis.html"))
        archive_link = esc(coverage.get("archive", "../jetson-benchmark-report.zip"))
        body += f'<section class="card"><h2>Độ phủ toàn bộ corpus</h2><p>{coverage["live_cases"]} ca live và {coverage["offline_cases"]} ca offline, phủ {coverage["unique_cases"]} ca riêng biệt; có {coverage["overlap"]} ca trùng.</p><p><a href="coverage.json">Đối chiếu độ phủ</a> · <a href="{offline_link}">Baseline offline</a> · <a href="{archive_link}" download>Tải toàn bộ HTML/JSON (ZIP)</a></p></section>'
    if report.get("inputs_changed_during_run"):
        body += '<p class="bad">Source/dataset đổi trong khi chạy: kết quả này không phải baseline cố định.</p>'
    body += '<h2>Kết quả theo tính năng</h2><div class="scroll"><table><thead><tr><th>Tính năng</th><th>Đạt / Tổng</th><th>Fail</th><th>Tỉ lệ</th></tr></thead><tbody>'
    for feature, label in LABELS.items():
        selected = [row for row in rows if row["feature"] == feature]
        if selected:
            successes = sum(row["passed"] for row in selected)
            body += f'<tr><td>{esc(label)}</td><td>{successes}/{len(selected)}</td><td>{len(selected)-successes}</td><td>{successes/len(selected)*100:.1f}%</td></tr>'
    body += '</tbody></table></div><h2>Phân tích lỗi</h2><p>Một ca có thể thuộc nhiều nhóm lỗi; tổng dưới đây không phải số ca độc lập cộng dồn.</p><ul>'
    meanings = {"catalog": "Thiếu capability kỳ vọng trong catalog được đưa cho model", "capability": "Chọn sai/thiếu/thừa capability", "arguments": "Sai tham số/đích/thứ tự", "order": "Sai thứ tự", "provider": "Provider/fallback không đạt", "service": "Lỗi HTTP dịch vụ", "runtime": "Ngoại lệ runtime", "guard": "Xử lý ràng buộc/ngoài phạm vi", "state": "Trạng thái xe không đạt", "contract": "Output/lifecycle không đạt", "unknown": "Chưa đủ trace"}
    body += "".join(f'<li>{esc(meanings[key])}: <strong>{count}</strong> ca</li>' for key, count in groups.most_common()) + "</ul>"
    weakest = sorted(((feature, counts) for feature, counts in report.get("summary", {}).get("feature", {}).items()),
                     key=lambda item: item[1]["passed"] / item[1]["cases"])
    body += '<h3>Nhận xét và hướng kiểm tra</h3><ul>'
    if groups["catalog"]:
        body += f'<li>{groups["catalog"]} ca fail có capability kỳ vọng vắng mặt trong toàn bộ catalog của các proposal đã ghi. Ưu tiên rà tool discovery/catalog; không xem toàn bộ các ca này là model tự chọn sai khi đã được cung cấp đủ tool.</li>'
    for feature, counts in weakest[:3]:
        if counts["passed"] < counts["cases"]:
            body += f'<li>{esc(LABELS[feature])}: {counts["cases"]-counts["passed"]}/{counts["cases"]} ca fail; là một trong các nhóm có tỉ lệ đạt thấp nhất của lượt chạy này.</li>'
    if groups["capability"] or groups["arguments"]:
        body += '<li>Với ca sai capability/tham số: so sánh tool call đầu, kết quả sau repair và semantic cuối trong failure file. Các vị trí cần rà gồm <code>src/vivi/dialogue/small_tools.py</code>, <code>tool_discovery.py</code> và <code>local_operations.py</code>; đây là hướng điều tra, chưa phải kết luận lỗi ở file nào.</li>'
    if groups["provider"] or groups["service"] or groups["runtime"]:
        body += '<li>Tách các ca lỗi provider, HTTP và timeout khỏi lỗi hiểu ý định khi đánh giá model; kiểm tra <code>attempts</code>, budget và ngoại lệ trong từng file.</li>'
    body += '<li>Không sửa expected theo kết quả model. Ca có thể mơ hồ hoặc oracle quá chặt cần được review bằng input và ngữ cảnh gốc trước khi kết luận lỗi agent.</li></ul>'
    body += '<h2 id="failure-list">Danh sách failure</h2><label>Tìm input/ID <input id="search" type="search" placeholder="Nhập câu hoặc ID"></label><label>Tính năng <select id="feature"><option value="">Tất cả</option>'
    body += "".join(f'<option value="{feature}">{esc(label)}</option>' for feature, label in LABELS.items()) + '</select></label><p id="count" aria-live="polite"></p>'
    body += '<p>Đọc ba cột từ trái sang phải: input → expected → output model. Output model giữ các lần gọi/repair theo thứ tự; kết quả agent là dữ liệu sau xử lý dùng để chấm.</p>'
    body += '<div class="scroll"><table class="comparison-list"><thead><tr><th>Ca / input</th><th>Expected</th><th>Output model / kết quả agent</th></tr></thead><tbody id="failures">'
    for row, link, categories, text in failure_links:
        expected_parts, actual_parts = [], []
        for turn in row["turns"]:
            if turn["passed"]:
                continue
            failed_checks = {key: value for key, value in turn.get("checks", {}).items() if not value["passed"]}
            label = f'<strong>Lượt {esc(turn.get("turn", "?"))}</strong>'
            expected_parts.append(label + block({key: value["expected"] for key, value in failed_checks.items()}))
            actual_parts.append(label + '<p>Tool call/output model:</p>' + block(model_output(turn))
                                + '<details><summary>Kết quả agent dùng để chấm</summary>'
                                + block({key: value["actual"] for key, value in failed_checks.items()}) + '</details>')
        inputs = ''.join(f'<p><strong>Lượt {esc(turn.get("turn", "?"))}:</strong> {esc(turn.get("text", ""))}</p>' for turn in row['turns'])
        body += f'<tr data-feature="{esc(row["feature"])}"><td><a href="{esc(link)}">{esc(row["id"])}</a>{inputs}<span class="tag">{esc(LABELS[row["feature"]])} · {esc(row["suite"])} · {esc(row["input_style"])}</span><p class="muted">{esc(", ".join(meanings[key] for key in sorted(categories)))}</p><a href="{esc(link)}">Mở giải thích và ngữ cảnh đầy đủ →</a></td><td>{"".join(expected_parts)}</td><td>{"".join(actual_parts)}</td></tr>'
    body += '</tbody></table></div><h2>Phạm vi và giới hạn</h2>'
    body += '<p>Xe, bản đồ và dữ liệu phiên được mô phỏng. Không đo STT/TTS hay xe thật. Sưởi ghế ngoài phạm vi; nhạc chỉ bật/tắt đoạn cố định. Ca fixture/offline không phải độ chính xác ngôn ngữ của model.</p>'
    body += "<p>Bộ không chạy trong lượt này và các ca loại trừ:</p>" + block({key: report.get(key) for key in ("not_run_suites", "excluded")})
    body += '<details><summary>Latency, repair, fallback và metadata</summary>' + block({key: value for key, value in report.items() if key != "results"}) + '</details><p><a href="report.json">Report JSON gốc</a></p>'
    body += """<script>
const search=document.querySelector('#search'),feature=document.querySelector('#feature');
function filter(){let count=0;for(const row of document.querySelectorAll('#failures tr')){
const show=(!feature.value||row.dataset.feature===feature.value)&&row.textContent.toLocaleLowerCase('vi').includes(search.value.toLocaleLowerCase('vi'));
row.hidden=!show;if(show)count++;}document.querySelector('#count').textContent=count+' failure đang hiển thị';}
search.addEventListener('input',filter);feature.addEventListener('change',filter);filter();
</script>"""
    (output / "analysis.html").write_text(page("Phân tích benchmark agent", body), encoding="utf-8")
    (output / "failures").mkdir(exist_ok=True)
    (output / "failures" / "index.html").write_text(page("Failure index", '<h1>Failure files</h1><p><a href="../analysis.html">Mở phân tích và bộ lọc failure</a></p>'), encoding="utf-8")
    (output / "analysis.json").write_text(json.dumps({"cases": total, "passed": passed, "failed": len(failed), "failure_categories": dict(groups), "analysis_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "failure_files": [link for _, link, _, _ in failure_links]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    write_html(json.loads(args.report.read_text(encoding="utf-8")), args.report.parent)


if __name__ == "__main__":
    main()
