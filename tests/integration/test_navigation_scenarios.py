"""Navigation scenarios end to end through the assistant graph.

Semantics are scripted per utterance; deterministic grammar (huỷ, gần hơn,
huỷ lộ trình) must answer without reaching the script.
"""

import json
import math

import pytest

from fixtures.core import Harness, ScriptedModel, action
from src.vivi.navigation.vietmap import Place, Route

ORIGIN = (21.0, 105.85)


def km(lat_km, lng_km=0.0):
    return ORIGIN[0] + lat_km / 111.2, ORIGIN[1] + lng_km / (111.2 * math.cos(math.radians(ORIGIN[0])))


POIS = [
    ("st-near", "Trạm Sạc Điện Ô Tô VinFast Times City", (0.4,), ["10013", "10013-1"]),
    ("st-mid", "Trạm Sạc Điện Ô Tô VinFast Kim Liên", (2.5,), ["10013", "10013-1"]),
    ("st-far", "Trạm Sạc Điện Ô Tô VinFast Ocean Park", (14,), ["10013", "10013-1"]),
    ("bike", "Trạm Sạc Xe Máy VinFast", (0.2,), ["10013"]),
    ("cafe1", "Highlands Coffee", (0.6,), ["1001", "1001-1"]),
    ("cafe2", "Cộng Cà Phê", (1.2,), ["1001", "1001-1"]),
    ("hosp", "Bệnh Viện Thanh Nhàn", (0.9,), ["7003"]),
    ("hosp2", "Bệnh Viện Bạch Mai", (3.0,), ["7003"]),
]


class Maps:
    def __init__(self):
        self.calls = []

    def poi(self, ref, name, offset, cats, address="Phường Vĩnh Tuy, Thành phố Hà Nội"):
        lat, lng = km(*offset)
        return Place(ref_id=ref, name=name, address=address, lat=lat, lng=lng, categories=cats)

    async def search(self, text, near=None, category=None, *, radius_m=None, layers=None):
        self.calls.append(("search", text, category, radius_m, layers))
        if layers == "WARD":
            if "xùa" in text.lower():
                return [Place(ref_id="ward", name="Xã Tà Xùa", address="Xã Tà Xùa, Tỉnh Sơn La", lat=21.3, lng=104.4)]
            if "đồ sơn" in text.lower():
                return [
                    Place(
                        ref_id="doson",
                        name="Phường Đồ Sơn",
                        address="Phường Đồ Sơn, Thành phố Hải Phòng",
                        lat=20.72,
                        lng=106.79,
                        boundaries=[{"type": 0, "name": "Hải Phòng", "full_name": "Thành phố Hải Phòng"}],
                    )
                ]
            return []
        if layers == "CITY":
            if "hải phòng" in text.lower():
                return [
                    Place(
                        ref_id="hp",
                        name="Thành phố Hải Phòng",
                        address="Thành phố Hải Phòng",
                        lat=20.86,
                        lng=106.68,
                        boundaries=[{"type": 0, "name": "Hải Phòng", "full_name": "Thành phố Hải Phòng"}],
                    )
                ]
            return []
        if radius_m:
            out = []
            for ref, name, offset, cats in POIS:
                p = self.poi(ref, name, offset, cats)
                dist = math.dist((p.lat, p.lng), ORIGIN) * 111200
                if dist <= radius_m and (not category or any(c == category or c.startswith(category) for c in cats)):
                    out.append(p)
            return out[::-1][:10]
        lower = text.lower()
        if "xùa" in lower:
            return [
                Place(
                    ref_id="taxua-cafe",
                    name="Tà Xùa Coffee Tea",
                    address="Phường Phú Diễn, Thành phố Hà Nội",
                    lat=21.05,
                    lng=105.76,
                )
            ]
        if "trạm sạc" in lower:  # text-only path ranks far ones first (old bug)
            return [self.poi(r, n, o, c) for r, n, o, c in POIS[:3][::-1]]
        lat, lng = km(30, 10)
        return [Place(ref_id="dest-" + text, name=text, address="Phường Phủ Lý, Tỉnh Ninh Bình", lat=lat, lng=lng)]

    async def place(self, ref_id):
        raise AssertionError("all fake places have coordinates")

    async def matrix(self, points):
        self.calls.append(("matrix", len(points) - 1))
        return [math.dist(points[0], p) * 111200 * 1.3 for p in points[1:]]

    async def route(self, origin, destination):
        self.calls.append(("route",))
        d = max(100, math.dist(origin, destination) * 111200 * 1.3)
        return Route(
            distance_m=d,
            duration_s=d / 10,
            coordinates=[[origin[1], origin[0]], [destination[1], destination[0]]],
            steps=[],
        )


def nav_event(r, kind, **extra):
    ws = r["navigation_workspace"]
    return {
        "id": f"{kind}-{ws['revision']}-{ws.get('event_sequence')}-{extra.get('gear', '')}",
        "kind": kind,
        "revision": ws["revision"],
        **extra,
    }


def start_action(r):
    d = r["navigation_workspace"]["draft"]
    return {"capability": "navigation.start", "arguments": {"plan_id": d["id"], "route_revision": d["revision"]}}


async def plan(h, text="chỉ đường tới Phủ Lý"):
    r = await h.turn(text)
    if (r.get("pending_question") or {}).get("kind") == "accept_place":
        r = await h.turn(question_reply={"question_id": r["pending_question"]["id"], "option_id": "yes"})
    return r


def yes(r):
    return {"question_id": r["pending_question"]["id"], "option_id": "yes"}


SCENARIOS = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


@scenario
async def s01_plan_start_button_end_then_new_search(h):
    r = await plan(h)
    r = await h.turn(action=start_action(r))
    assert r["navigation_workspace"]["journey"]["status"] == "running", "start button must start"
    r = await h.turn(navigation_event=nav_event(r, "end"))
    assert r["navigation_workspace"].get("journey") is None and r["navigation"]["status"] == "idle"
    r = await h.turn("tìm trạm sạc gần nhất")
    assert r["navigation"]["status"] == "selecting" and not r["navigation"].get("selected")
    assert r["navigation_workspace"]["draft"]["candidates"][0]["ref_id"] == "st-near"


@scenario
async def s02_preview_then_huy(h):
    r = await h.turn("chỉ đường tới Phủ Lý")
    r = await h.turn("huỷ")
    assert not r["navigation_workspace"].get("draft") and r["navigation"]["status"] == "idle"


@scenario
async def s03_preview_then_khong_xac_nhan_then_new_plan(h):
    r = await h.turn("chỉ đường tới Phủ Lý")
    r = await h.turn("không xác nhận")
    r = await h.turn("chỉ đường tới Nam Định")
    assert r["navigation_workspace"]["draft"]["destination"]["name"] == "Nam Định"


@scenario
async def s04_nearest_choose_and_start(h):
    r = await h.turn("tìm trạm sạc gần nhất")
    cands = r["navigation_workspace"]["draft"]["candidates"]
    assert [c["ref_id"] for c in cands][:2] == ["st-near", "st-mid"], cands
    assert "400 m" in r["response_text"] or "m;" in r["response_text"]
    choice = r["pending_question"]["choices"][0]["handle"] if r["pending_question"].get("choices") else None
    r = await h.turn(question_reply={"question_id": r["pending_question"]["id"], "option_id": choice})
    r = await h.turn(action=start_action(r))
    assert r["navigation_workspace"]["journey"]["status"] == "running"


@scenario
async def s05_nearer_followup_keeps_question(h):
    r = await h.turn("tìm trạm sạc gần nhất")
    qid = r["pending_question"]["id"]
    r = await h.turn("có chỗ nào gần hơn không?")
    assert r["response_text"].startswith("Đây đã là các trạm sạc gần xe nhất")
    assert (r.get("pending_question") or {}).get("id") == qid, "choice question must survive"


@scenario
async def s06_cafe_with_wrong_model_category(h):
    r = await h.turn("tìm quán cà phê gần nhất")
    assert r["navigation_workspace"]["draft"]["candidates"][0]["ref_id"] == "cafe1"


@scenario
async def s07_hospital_nearest_via_directions(h):
    r = await h.turn("chỉ đường tới bệnh viện gần nhất")
    d = r["navigation_workspace"]["draft"]
    assert d["destination"]["entity_type"] == "hospital" and d["destination"]["nearest"]
    names = [c["ref_id"] for c in d["candidates"]] or [(d.get("selected") or {}).get("ref_id")]
    assert names[0] == "hosp", names


@scenario
async def s08_new_request_replaces_open_preview(h):
    r = await h.turn("chỉ đường tới Phủ Lý")
    r = await h.turn("chỉ đường tới trạm sạc gần nhất")
    d = r["navigation_workspace"]["draft"]
    assert d["destination"]["entity_type"] == "charging", d["destination"]


@scenario
async def s09_gear_d_on_unconfirmed_preview(h):
    r = await plan(h)
    r = await h.turn(navigation_event=nav_event(r, "gear", gear="D"))
    assert not h.vehicle.state_for("driver").driving
    if (r.get("pending_question") or {}).get("purpose") == "approve_execution":
        r = await h.turn(question_reply=yes(r))
        assert r["navigation_workspace"]["journey"]["status"] == "running"


@scenario
async def s10_running_trip_new_draft_cancel_keeps_trip_then_end_by_voice(h):
    r = await plan(h)
    r = await h.turn(action=start_action(r))
    r = await h.turn("tìm trạm sạc gần nhất")
    assert r["navigation"]["status"] == "running" and r["navigation"]["pending"]
    r = await h.turn("huỷ")
    assert r["navigation_workspace"]["journey"]["status"] == "running" and not r["navigation_workspace"].get("draft")
    r = await h.turn("huỷ lộ trình hiện tại")
    if (r.get("pending_question") or {}).get("purpose") == "approve_execution":
        r = await h.turn(question_reply=yes(r))
    assert not r["navigation_workspace"].get("journey"), "trip should end"


@scenario
async def s11_arrival_then_new_search(h):
    r = await plan(h)
    r = await h.turn(action=start_action(r))
    dist = r["navigation_workspace"]["journey"]["route"]["distance_m"]
    r = await h.turn(navigation_event=nav_event(r, "progress", distance_m=dist))
    assert r["navigation"]["status"] == "completed"
    r = await h.turn("tìm quán cà phê gần nhất")
    assert r["navigation"]["status"] == "selecting" and not r["navigation_workspace"].get("journey")
    assert r["navigation_workspace"]["history"][-1]["status"] == "completed"


@scenario
async def s12_position_jitter_keeps_choice(h):
    r = await h.turn("tìm trạm sạc gần nhất")
    q = r["pending_question"]
    rev = r["navigation_workspace"]["draft"]["revision"]
    pos = {"lat": ORIGIN[0] + 1e-6, "lng": ORIGIN[1], "label": "Vị trí của bạn"}
    r = await h.turn(navigation_event={**nav_event(r, "position"), "position": pos})
    assert r["navigation_workspace"]["draft"]["revision"] == rev
    r = await h.turn(question_reply={"question_id": q["id"], "option_id": q["choices"][0]["handle"]})
    assert r["navigation"]["status"] == "preview"


@scenario
async def s13_named_commune(h):
    r = await h.turn("đường đi tới Tà Xùa")
    d = r["navigation_workspace"]["draft"]
    first = (d["candidates"] or [d.get("selected")])[0]
    assert first["ref_id"] == "ward", first


@scenario
async def s14_cancel_with_nothing_open(h):
    r = await h.turn("huỷ")
    assert r["response_text"] == "Mình đã bỏ qua yêu cầu đó." and r["navigation"]["status"] == "idle"


@scenario
async def s15_region_change_then_spoken_place_with_stale_handle(h):
    r = await h.turn("chỉ đường tới Hải Phòng")
    q = r["pending_question"]
    change = next(c for c in q["choices"] if "Đổi" in c["label"])
    r = await h.turn(question_reply={"question_id": q["id"], "option_id": change["handle"]})
    SEMANTICS["Đồ Sơn"] = action(
        "navigation.refine", {"query": "Đồ Sơn", "choice_handle": change["handle"], "reject_candidates": True}
    )
    r = await h.turn("Đồ Sơn")
    assert "Lựa chọn đã thay đổi" not in r["response_text"], r["response_text"]


@scenario
async def s16_replace_running_route_with_start_button(h):
    r = await plan(h)
    r = await h.turn(action=start_action(r))
    old = r["navigation_workspace"]["journey"]["route_id"]
    r = await h.turn("tìm trạm sạc gần nhất")
    q = r["pending_question"]
    r = await h.turn(question_reply={"question_id": q["id"], "option_id": q["choices"][0]["handle"]})
    assert r["navigation"]["status"] == "running" and r["navigation"]["pending"], "running trip stays primary"
    r = await h.turn(action=start_action(r))
    j = r["navigation_workspace"]["journey"]
    assert j["status"] == "running" and j["route_id"] != old and j["selected"]["ref_id"] == "st-near"


@scenario
async def s17_ui_cancel_button_on_choices(h):
    r = await h.turn("tìm trạm sạc gần nhất")
    d = r["navigation_workspace"]["draft"]
    r = await h.turn(
        action={"capability": "navigation.cancel", "arguments": {"plan_id": d["id"], "route_revision": d["revision"]}}
    )
    assert r["navigation"]["status"] == "idle" and not r["navigation_workspace"].get("draft")


@scenario
async def s18_nearest_nothing_within_radius(h):
    backup = list(POIS)
    POIS[:] = [p for p in POIS if p[0] != "hosp" and p[0] != "hosp2"] + [("hosp-far", "Bệnh Viện Xa", (80,), ["7003"])]
    try:
        r = await h.turn("chỉ đường tới bệnh viện gần nhất")
        assert "30 km" in r["response_text"], r["response_text"]
    finally:
        POIS[:] = backup


@scenario
async def s19_pause_new_draft_then_end_keeps_new_draft(h):
    r = await plan(h)
    r = await h.turn(action=start_action(r))
    r = await h.turn(navigation_event=nav_event(r, "gear", gear="P"))
    r = await h.turn("tìm quán cà phê gần nhất")
    r = await h.turn(navigation_event=nav_event(r, "end"))
    ws = r["navigation_workspace"]
    assert ws.get("journey") is None and ws["draft"]["destination"]["entity_type"] == "cafe"
    assert r["navigation"]["status"] == "selecting"


@scenario
async def s20_end_by_voice_right_after_start(h):
    r = await plan(h)
    r = await h.turn(action=start_action(r))
    r = await h.turn("huỷ lộ trình hiện tại")
    if (r.get("pending_question") or {}).get("purpose") == "approve_execution":
        r = await h.turn(question_reply=yes(r))
    assert not r["navigation_workspace"].get("journey"), "trip should end"
    assert not h.vehicle.state_for("driver").driving


@scenario
async def s21_first_request_without_location_resumes_after_locate(h):
    r = await h.turn("chỉ đường tới Phủ Lý", position=None)
    assert r["pending_question"]["slot"] == "origin"
    assert {"type": "navigation.locate"} in r["ui_events"]
    pos = {"lat": ORIGIN[0], "lng": ORIGIN[1], "label": "Vị trí của bạn", "simulated": False}
    r = await h.turn(navigation_event={**nav_event(r, "position"), "position": pos}, position=None)
    assert r["pending_question"]["kind"] == "accept_place", r["response_text"]
    assert r["navigation_workspace"]["draft"]["origin"]["lat"] == ORIGIN[0]


SEMANTICS = {
    "chỉ đường tới Phủ Lý": action("navigation.plan", {"query": "Phủ Lý", "purpose": "navigate"}),
    "chỉ đường tới Nam Định": action("navigation.plan", {"query": "Nam Định", "purpose": "navigate"}),
    "tìm trạm sạc gần nhất": action(
        "navigation.search", {"query": "trạm sạc", "category": "charging", "nearest": True}
    ),
    "tìm quán cà phê gần nhất": action(
        "navigation.search", {"query": "quán cà phê", "category": "charging", "nearest": True}
    ),
    "chỉ đường tới bệnh viện gần nhất": action(
        "navigation.plan", {"query": "bệnh viện gần nhất", "purpose": "navigate"}
    ),
    "chỉ đường tới trạm sạc gần nhất": action("navigation.plan", {"query": "trạm sạc gần nhất", "purpose": "navigate"}),
    "đường đi tới Tà Xùa": action("navigation.plan", {"query": "Tà Xùa", "purpose": "navigate"}),
    "không xác nhận": {"act": "deny", "task_ref": "active"},
    "chỉ đường tới Hải Phòng": action("navigation.plan", {"query": "Hải Phòng", "purpose": "navigate"}),
    "huỷ": {"act": "cancel", "task_ref": "active"},
    "huỷ lộ trình hiện tại": {"act": "clarify", "text": "MODEL-SHOULD-NOT-BE-ASKED"},
    "có chỗ nào gần hơn không?": {"act": "clarify", "text": "MODEL-SHOULD-NOT-BE-ASKED"},
}


class ByText(ScriptedModel):
    def __init__(self):
        super().__init__([], destination={"mode": "clarify", "handle": None, "reason": "ambiguous"})

    async def agenerate_json(self, **kwargs):
        if kwargs["schema_name"] != "destination_choice":
            utterance = json.loads(kwargs["user"])["utterance"]
            self.replies = iter([SEMANTICS[utterance]])
        return await super().agenerate_json(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_navigation_scenario(tmp_path, name):
    h = Harness(tmp_path, model=ByText(), maps=Maps())
    try:
        await SCENARIOS[name](h)
    finally:
        await h.close()
