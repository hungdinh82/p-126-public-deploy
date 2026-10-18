import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from benchmarks.agent.html_report import catalog, esc, explain, page
from benchmarks.agent.oracles import decisions_match

parser = argparse.ArgumentParser(description="Build a compact, error-only benchmark comparison.")
parser.add_argument("--current", default="jetson-v13-complete")
parser.add_argument("--baseline", default="jetson-final")
parser.add_argument("--offline", default="offline-v13-complete")
parser.add_argument("--supplemental", default="jetson-v13-short-turns")
parser.add_argument("--supplemental-offline", default="offline-v13-short-turns")
parser.add_argument("--positions", default="jetson-v13-position-phrases")
parser.add_argument("--positions-offline", default="offline-v13-position-phrases")
parser.add_argument("--archive", default="jetson-v13-report.zip")
args = parser.parse_args()
root = Path("benchmarks/agent/results")
folder = root / args.current
new = json.loads((folder / "report.json").read_text())
old = json.loads((root / args.baseline / "report.json").read_text())
offline = json.loads((root / args.offline / "report.json").read_text())


def keys(report):
    return {(r["suite"], r["id"], r["repeat"]): r for r in report["results"]}


before, after = keys(old), keys(new)
assert before.keys() == after.keys() and len(after) == 834
assert old["dataset_sha256"] == new["dataset_sha256"] == offline["dataset_sha256"]
assert not new["inputs_changed_during_run"] and not offline["inputs_changed_during_run"]
assert new["source_sha256"] == offline["source_sha256"]
empty = hashlib.sha256(b"[]").hexdigest()
proposals = [
    p
    for row in new["results"]
    for t in row["turns"]
    for a in t.get("actual", {}).get("attempts", [])
    for p in a.get("proposal_attempts", [])
]
assert proposals and all(p.get("examples_sha256") == empty for p in proposals)
fixed = [key for key in after if not before[key]["passed"] and after[key]["passed"]]
regressed = [key for key in after if before[key]["passed"] and not after[key]["passed"]]
failed = [r for r in new["results"] if not r["passed"]]
labels = {
    "catalog": "Thiếu capability cần thiết trong tài liệu model nhận",
    "capability": "Sai/thiếu/thừa loại thao tác",
    "arguments": "Sai tham số, vị trí hoặc thứ tự",
    "order": "Sai thứ tự thao tác",
    "provider": "Fallback/provider không đạt",
    "service": "Lỗi dịch vụ model",
    "runtime": "Ngoại lệ runtime",
    "guard": "Xử lý phủ định/điều kiện/ngoài phạm vi không đạt",
    "state": "Trạng thái xe không đúng",
    "contract": "Kết quả hội thoại/lifecycle không đạt",
    "unknown": "Chưa đủ trace để phân loại",
}


def error_details(turn):
    """Describe the error in words without dumping proposals or expected JSON."""
    notes = []
    if any(
        attempt.get("outcome") == "fallback" and attempt.get("reason") == "ValueError"
        for attempt in turn.get("actual", {}).get("attempts", [])
    ):
        notes.append("Không diễn giải được proposal hợp lệ sau các lần thử; hệ thống chuyển fallback.")

    def operation(decision):
        args = decision.get("arguments", {})
        capability = decision.get("name")
        if capability == "door.set_open" and isinstance(args.get("open"), bool):
            return "mở cánh cửa" if args["open"] else "đóng cánh cửa"
        if capability == "door.set_lock" and isinstance(args.get("locked"), bool):
            return "khóa cửa" if args["locked"] else "mở khóa cửa"
        return None

    for path, check in turn.get("checks", {}).items():
        if check["passed"]:
            continue
        if ":decisions" in path or ":ordered_decisions" in path:
            wanted, actual = check["expected"], check["actual"]
            if not isinstance(actual, list):
                continue
            if len(actual) < len(wanted):
                notes.append("Bỏ sót thao tác được yêu cầu.")
            elif len(actual) > len(wanted):
                notes.append("Đề xuất thêm thao tác ngoài yêu cầu.")
            for expected, received in zip(wanted, actual):
                expected_verb, actual_verb = operation(expected), operation(received)
                if expected_verb and actual_verb and expected_verb != actual_verb:
                    notes.append(f"Chọn {actual_verb} thay vì {expected_verb}.")
                if expected.get("name") == received.get("name"):
                    args, desired = received.get("arguments", {}), expected.get("arguments", {})
                    position_keys = {"zone", "zones", "target_side", "target_row"}
                    desired_position = {k: v for k, v in desired.items() if k in position_keys}
                    actual_position = {k: v for k, v in args.items() if k in position_keys}
                    if desired_position and not decisions_match(
                        [{"name": "position", "arguments": actual_position}],
                        [{"name": "position", "arguments": desired_position}],
                    ):
                        notes.append("Vị trí/phạm vi thao tác bị thiếu hoặc sai.")
                    for field, message in {
                        "position_percent": "Sai mức mở cửa kính.",
                        "value_celsius": "Sai hoặc thiếu nhiệt độ đích.",
                        "temperature_delta_celsius": "Sai mức tăng/giảm nhiệt độ.",
                        "temperature_direction": "Nhầm hướng tăng/giảm nhiệt độ.",
                    }.items():
                        if field in desired and args.get(field) != desired[field]:
                            notes.append(message)
        elif path.endswith((".arguments.zone", ".arguments.zones", ".arguments.target_side", ".arguments.target_row")):
            notes.append("Chưa điền đúng thông tin vị trí vào yêu cầu.")
        elif path.startswith("vehicle."):
            notes.append("Trạng thái xe sau xử lý chưa đúng yêu cầu.")
        elif path == "output.status":
            notes.append("Luồng hội thoại chưa đi tới trạng thái cần thiết.")
    return list(dict.fromkeys(notes))


case_catalog = catalog(new)
error_counts = Counter()
body = "<h1>Benchmark Jetson sau cải tiến tool/prompt</h1>"
body += f"<p>{esc(new['model'])} · cùng 834 ca và cùng expected với baseline.</p>"
body += f'<div class="cards"><div class="card">Trước<strong>{old["passed"]}/834</strong></div><div class="card">Sau<strong>{new["passed"]}/834</strong></div><div class="card">Ca được sửa<strong>{len(fixed)}</strong></div><div class="card">Ca giảm<strong>{len(regressed)}</strong></div></div>'
body += f"<p>Đã chạy đủ 834/834 ca live và {offline['cases']} ca offline ({offline['passed']} đạt). Dataset và expected không đổi. Mọi proposal ({len(proposals)} lần) đều có danh sách ví dụ rỗng theo hash log; không chèn cặp câu–đáp án vào prompt. Đây là corpus development; không phải holdout độc lập.</p>"
body += "<h2>Vấn đề và thay đổi</h2><ul><li>Model tự đoán ghế khi chưa có vị trí làm mất form: schema sinh chỉ cho phép vị trí có bằng chứng trong câu; chưa có vị trí giữ mảng rỗng.</li><li>Câu trả lời vị trí ngắn bị coi là yêu cầu mới: khi form đang chờ, giới hạn tool cập nhật và giữ nguyên thao tác/các trường đã biết. Câu hỏi, phủ định và lệnh mới đi theo luồng riêng.</li><li>Vị trí đủ cả hàng/phía điền cùng lượt; vị trí một phần được giữ để ghép lượt sau. Kiểm tra task đã lưu, payload xác nhận và trạng thái xe sau khi đồng ý.</li><li>Dùng chung từ vựng tên ghế; phân biệt dấu ở từ khác nghĩa. Discovery phân biệt khóa cửa với mở cánh cửa.</li><li>Kính có mức mở cụ thể dùng tool có trường phần trăm; câu ghép vẫn giữ các biến thể. Không hiểu hết kính ở vị trí cụ thể thành mọi kính.</li><li>Hướng dẫn điền form và chốt cửa chỉ gửi khi context cần, giảm nhiễu cho các miền khác.</li><li>Không chèn đáp án mẫu vào prompt; không đổi expected để khớp output.</li></ul>"
body += "<h2>Trước / sau theo tính năng</h2><table><thead><tr><th>Tính năng</th><th>Trước</th><th>Sau</th><th>Chênh lệch</th></tr></thead><tbody>"
for f, c in new["summary"]["feature"].items():
    b = old["summary"]["feature"][f]
    d = c["passed"] - b["passed"]
    body += f"<tr><td>{esc(f)}</td><td>{b['passed']}/{b['cases']}</td><td>{c['passed']}/{c['cases']}</td><td>{d:+}</td></tr>"
body += '</tbody></table><h2 id="failures">Ca còn lỗi</h2><p>Chỉ ghi lỗi; không hiển thị output model hoặc expected chi tiết. Ca nhiều lượt được phân biệt lỗi đầu tiên và lỗi dây chuyền.</p><input id="q" placeholder="Lọc theo ID, tính năng hoặc câu hỏi"><select id="kind"><option value="">Tất cả</option><option value="regressed">Mới giảm</option><option value="persistent">Vẫn lỗi từ trước</option></select><p id="count"></p><table><thead><tr><th>Ca / input</th><th>Lỗi gì</th><th>Trước → sau</th></tr></thead><tbody id="rows">'
failroot = folder / "failures"
failroot.mkdir(exist_ok=True)
for row in failed:
    k = (row["suite"], row["id"], row["repeat"])
    first = next(t for t in row["turns"] if not t["passed"])
    cats = set()
    descriptions = []
    for t in row["turns"]:
        if t["passed"]:
            continue
        reasons = explain(t)
        current = {c for c, _ in reasons}
        cats.update(current)
        notes = [labels[c] for c in sorted(current)]
        notes.extend(error_details(t))
        paths = [key for key, v in t.get("checks", {}).items() if not v["passed"]]
        descriptions.append(
            f"Lượt {t.get('turn', '?')}: " + "; ".join(notes) + ". Điều kiện không đạt: " + ", ".join(paths) + "."
        )
    if len(row["turns"]) > 1 and first is not row["turns"][-1]:
        descriptions.append(
            "Lỗi phát sinh từ lượt "
            + str(first.get("turn", "?"))
            + "; các lượt sau có thể là hệ quả của task/câu hỏi trước chưa đúng."
        )
    error_counts.update(cats)
    text = " / ".join(t.get("text", "") for t in row["turns"])
    status = "regressed" if before[k]["passed"] else "persistent"
    safe = hashlib.sha256(":".join(map(str, k)).encode()).hexdigest()[:16]
    body += (
        f'<tr data-kind="{status}"><td><strong>{esc(row["id"])}</strong><p>{esc(text)}</p><small>{esc(row["feature"])} · {esc(row["suite"])} · <a href="failures/{safe}.json" download>JSON ca lỗi</a></small></td><td>'
        + "".join("<p>" + esc(d) + "</p>" for d in descriptions)
        + f"</td><td>{'Đạt → lỗi' if status == 'regressed' else 'Lỗi → lỗi'}</td></tr>"
    )
    (failroot / (safe + ".json")).write_text(
        json.dumps(
            {"test": case_catalog[(row["suite"], row["id"])], "result": row, "errors": descriptions},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
body += (
    "</tbody></table><h2>Tóm tắt nhóm lỗi</h2><ul>"
    + "".join(f"<li>{esc(labels[c])}: {n} ca</li>" for c, n in error_counts.most_common())
    + "</ul><p>Một ca có thể có nhiều loại lỗi. Phân loại dựa trên trace, chưa khẳng định nguyên nhân nội tại của model.</p>"
)
body += f'<p>Xe/DB/map mô phỏng; không đo STT/TTS hoặc phần cứng. Không test sưởi ghế; nhạc chỉ bật/tắt clip cố định. Các ca offline không cộng vào độ đúng của model.</p><p><a href="../{esc(args.archive)}">Tải báo cáo và dữ liệu JSON</a></p>'
supplemental_path = root / args.supplemental / "report.json"
if supplemental_path.exists():
    supplemental = json.loads(supplemental_path.read_text())
    assert supplemental["source_sha256"] == new["source_sha256"]
    assert not supplemental["inputs_changed_during_run"]
    supplemental_offline = json.loads((root / args.supplemental_offline / "report.json").read_text())
    assert supplemental_offline["source_sha256"] == new["source_sha256"]
    assert not supplemental_offline["inputs_changed_during_run"]
    assert supplemental["supplemental_dataset_sha256"] == supplemental_offline["supplemental_dataset_sha256"]
    assert not supplemental["supplemental_inputs_changed"]
    previous_supplemental = json.loads((root / "jetson-short-turns/report.json").read_text())
    assert supplemental["supplemental_dataset_sha256"] == previous_supplemental["supplemental_dataset_sha256"]
    body += (
        f'<h2>Bộ bổ sung: câu ngắn và form nhiều lượt</h2><p>Jetson đạt '
        f'{supplemental["passed"]}/{supplemental["cases"]} ca, kiểm tra riêng alias ghế, '
        f'điền hàng/phía ở nhiều lượt và sửa thông tin còn thiếu (trước: {previous_supplemental["passed"]}/{previous_supplemental["cases"]}). Backend offline đạt {supplemental_offline["passed"]}/{supplemental_offline["cases"]}. '
        'Không gộp các ca bổ sung vào điểm baseline.</p>'
        f'<p><a href="../{esc(args.supplemental)}/analysis.html">Xem lỗi của bộ nhiều lượt</a></p>'
    )
    body += '<table><tr><th>Nhóm câu ngắn/form</th><th>Trước</th><th>Sau</th></tr>'
    for group, counts in supplemental["summary"]["group"].items():
        previous = previous_supplemental["summary"]["group"][group]
        body += f'<tr><td>{esc(group)}</td><td>{previous["passed"]}/{previous["cases"]}</td><td>{counts["passed"]}/{counts["cases"]}</td></tr>'
    body += '</table>'
    for row in supplemental["results"]:
        for turn in row["turns"]:
            for attempt in turn.get("actual", {}).get("attempts", []):
                assert all(p.get("examples_sha256") == empty for p in attempt.get("proposal_attempts", []))
positions_path = root / args.positions / "report.json"
if positions_path.exists():
    positions = json.loads(positions_path.read_text())
    positions_offline = json.loads((root / args.positions_offline / "report.json").read_text())
    for report in (positions, positions_offline):
        assert report["source_sha256"] == new["source_sha256"]
        assert not report["inputs_changed_during_run"] and not report["supplemental_inputs_changed"]
    assert positions["supplemental_dataset_sha256"] == positions_offline["supplemental_dataset_sha256"]
    for row in positions["results"]:
        for turn in row["turns"]:
            for attempt in turn.get("actual", {}).get("attempts", []):
                assert all(p.get("examples_sha256") == empty for p in attempt.get("proposal_attempts", []))
    body += f'<h2>Cách nói vị trí đủ hàng/phía</h2><p>Jetson: {positions["passed"]}/{positions["cases"]}; backend: {positions_offline["passed"]}/{positions_offline["cases"]}. Kiểm tra giá trị lưu trong task và payload xác nhận; sau đồng ý chỉ cửa được chỉ định thay đổi. Bộ bổ sung được viết trước khi chạy, không gộp vào điểm baseline.</p><p><a href="../{esc(args.positions)}/analysis.html">Xem lỗi</a></p>'
body += """<script>const q=document.querySelector('#q'),k=document.querySelector('#kind');function filter(){let n=0;for(const r of document.querySelectorAll('#rows tr')){r.hidden=!(r.textContent.toLocaleLowerCase('vi').includes(q.value.toLocaleLowerCase('vi'))&&(!k.value||k.value===r.dataset.kind));if(!r.hidden)n++;}document.querySelector('#count').textContent=n+' ca lỗi đang hiển thị';}q.addEventListener('input',filter);k.addEventListener('change',filter);filter();</script>"""
(folder / "analysis.html").write_text(page("Jetson: phân tích lỗi sau cải tiến", body))
comparison = {
    "old_passed": old["passed"],
    "new_passed": new["passed"],
    "cases": 834,
    "fixed": fixed,
    "regressed": regressed,
    "unchanged_failure": len(failed) - len(regressed),
    "categories": dict(error_counts),
    "model_proposals_without_examples": len(proposals),
    "source_sha256": new["source_sha256"],
    "dataset_sha256": new["dataset_sha256"],
}
(folder / "comparison.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n")
archive = root / args.archive
with ZipFile(archive, "w", ZIP_DEFLATED) as z:
    for dirname in (args.current, args.offline, args.supplemental, args.supplemental_offline, args.positions, args.positions_offline):
        for p in sorted((root / dirname).rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(root))
    for dataset_dir in (root.parent / "cases", root.parent / "supplemental"):
        for path in sorted(dataset_dir.rglob("*.jsonl")):
            z.write(path, Path("datasets") / path.relative_to(root.parent))
with ZipFile(archive) as z:
    assert z.testzip() is None
print(
    json.dumps(
        {
            key: value
            for key, value in comparison.items()
            if key in ("old_passed", "new_passed", "cases", "unchanged_failure", "model_proposals_without_examples")
        },
        ensure_ascii=False,
    )
)
print("fixed", len(fixed), "regressed", len(regressed), "archive", archive)
