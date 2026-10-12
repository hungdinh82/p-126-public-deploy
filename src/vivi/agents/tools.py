"""Executable tool contracts, shared by the planner, model prompt and validator.

The registry is the allow-list, not an execution bypass. Vehicle policy still
owns permission, confirmation, execution and verification.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    model_validator,
)

Zone = Literal["driver", "front_passenger", "rear_left", "rear_right", "all"]


class Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Temperature(Parameters):
    value_celsius: float = Field(ge=16, le=30, description="Nhiệt độ đích, không phải số độ tăng/giảm.")


class ZonedParameters(Parameters):
    zone: Zone | None = None
    zones: list[Literal["driver", "front_passenger", "rear_left", "rear_right"]] | None = Field(
        default=None, min_length=1, max_length=4
    )

    @model_validator(mode="after")
    def one_target(self):
        if (self.zone is None) == (self.zones is None):
            raise ValueError("Supply exactly one of zone or zones")
        if self.zones and len(set(self.zones)) != len(self.zones):
            raise ValueError("Duplicate cabin zones")
        return self


class Window(ZonedParameters):
    position_percent: StrictInt = Field(
        ge=0,
        le=100,
        description="Độ mở, không phải chiều cao kính: 0 = nâng kính lên/đóng; 100 = hạ kính xuống/mở. Hạ kính không nói mức nghĩa là 100, không phải 0.",
    )


class DoorOpen(ZonedParameters):
    open: StrictBool


class PanelOpen(Parameters):
    open: StrictBool


class DoorLock(ZonedParameters):
    locked: StrictBool


class SeatHeat(ZonedParameters):
    level: StrictInt = Field(ge=0, le=3, description="0 tắt sưởi, 1–3 mức sưởi.")


class MediaPlay(Parameters):
    media_query: str | None = Field(
        default=None,
        max_length=200,
        description="Requested song, artist or genre only. Omit for generic music; đi/nhé/nhạc are not search terms.",
    )


class SearchQuery(Parameters):
    query: str = Field(min_length=2, max_length=1000)


class ManualSearch(SearchQuery):
    requested_vehicle_model: str | None = Field(default=None, pattern=r"^VF[0-9]+$")
    requested_model_year: StrictInt | None = Field(default=None, ge=2000, le=2100)
    question_kind: Literal["charging_duration"] | None = None
    charging_mode: Literal["AC", "DC"] | None = None


class StatusRead(Parameters):
    query: str | None = Field(
        default=None,
        pattern=r"^(?:summary|temperature|battery|tires|range|range_time|body|diagnostics)(?:,(?:temperature|battery|tires|range|range_time|body|diagnostics))*$",
    )


class MemoryRecall(Parameters):
    memory_key: str | None = Field(
        default=None, description="Khoá đã lưu; preferences để chỉ đọc sở thích; bỏ trống để đọc tất cả."
    )


class MemoryForget(Parameters):
    memory_key: str = Field(min_length=1, max_length=100)


class MemoryRemember(MemoryForget):
    memory_value: str = Field(min_length=1, max_length=700)


class NavigationSearch(Parameters):
    query: str = Field(min_length=2, max_length=200)
    category: Literal["pharmacy", "charging"] | None = None
    nearest: bool = False
    destination_scope: Literal["place", "region"] = "place"


class NavigationSelect(Parameters):
    place_handle: str = Field(min_length=1, max_length=80)


class NavigationRefine(Parameters):
    reject_candidates: bool | None = None
    choice_handle: str | None = Field(default=None, min_length=1, max_length=80)
    query: str | None = Field(default=None, min_length=2, max_length=200)

    @model_validator(mode="after")
    def choice_or_query(self):
        if sum((bool(self.choice_handle), bool(self.query), self.reject_candidates is True)) != 1:
            raise ValueError("Provide exactly one choice, query, or reject_candidates=true")
        return self


class NavigationPlan(NavigationSearch):
    trip_kind: Literal["one_way", "round_trip"] = "one_way"
    origin_query: str | None = Field(default=None, min_length=2, max_length=200)
    query: str | None = Field(default=None, min_length=2, max_length=200)
    use_search_results: bool = False

    @model_validator(mode="after")
    def destination(self):
        if not self.query and not self.use_search_results:
            raise ValueError("A destination query or preceding search is required")
        return self


class NavigationStart(Parameters):
    route_revision: StrictInt = Field(ge=1)
    plan_id: str | None = None


class NavigationGear(Parameters):
    gear: Literal["P", "D"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: type[Parameters]
    missing_question: str
    read_only: bool = False

    def prompt_spec(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "read_only": self.read_only,
            "parameters": self.parameters.model_json_schema(),
        }


class ConversationQuery(SearchQuery):
    mode: Literal["chat", "general", "creative"] = "general"


class WeatherQuery(SearchQuery):
    location: str | None = Field(default=None, max_length=200)
    forecast_day: Literal["today", "tomorrow", "day_after_tomorrow"] = "today"


TOOLS = (
    ToolSpec("conversation.respond", "Trò chuyện, kiến thức phổ thông ổn định hoặc truyện ngắn giải trí. Không báo dữ liệu live hoặc thực thi xe.", ConversationQuery, "Bạn muốn trò chuyện về điều gì?", True),
    ToolSpec("web.search", "Tìm thông tin internet có nguồn: du lịch, địa điểm tham quan, tin mới, giá vàng. Không lập tuyến hay bắt đầu dẫn đường.", SearchQuery, "Bạn muốn tìm thông tin gì?", True),
    ToolSpec("weather.get_forecast", "Thời tiết hôm nay/ngày mai tại địa điểm hoặc vị trí hiện tại. Không lấy nhiệt độ điều hoà xe.", WeatherQuery, "Bạn muốn xem thời tiết ở đâu?", True),
    ToolSpec("knowledge.lookup", "Tra Wikipedia cho thông tin nền: địa danh, lịch sử, văn hóa. Không tra giá hoặc thời tiết mới nhất.", SearchQuery, "Bạn muốn tìm hiểu chủ đề nào?", True),
    ToolSpec(
        "memory.recall",
        "Đọc thông tin người dùng đã yêu cầu lưu. Không suy diễn từ lịch sử.",
        MemoryRecall,
        "Bạn muốn xem thông tin nào đã nhớ?",
        True,
    ),
    ToolSpec(
        "memory.remember",
        "Lưu khi người dùng yêu cầu ghi nhớ rõ ràng; key display_name, preferred_music, preferred_temperature, response_style hoặc note.*.",
        MemoryRemember,
        "Bạn muốn mình ghi nhớ điều gì?",
    ),
    ToolSpec(
        "memory.forget",
        "Xoá một mục đã lưu theo đúng memory_key có trong bộ nhớ.",
        MemoryForget,
        "Bạn muốn mình quên thông tin nào?",
    ),
    ToolSpec(
        "memory.reset",
        "Đề nghị xoá mọi thông tin cá nhân đã nhớ; hệ thống sẽ hỏi xác nhận trước khi xoá.",
        Parameters,
        "Bạn muốn xoá toàn bộ thông tin mình đã nhớ về bạn phải không?",
    ),
    ToolSpec(
        "manual.search",
        "Giải thích tính năng, cách dùng, thông số và xử lý sự cố VF8 từ cẩm nang. Không đọc trạng thái hiện tại, không thực hiện thao tác.",
        ManualSearch,
        "Bạn muốn tìm hiểu tính năng nào?",
        True,
    ),
    ToolSpec(
        "vehicle.get_status",
        "Đọc số đo và trạng thái xe hiện tại. Không dùng thông số cẩm nang làm số đo; chỉ trả những trường được adapter thực sự cung cấp.",
        StatusRead,
        "Bạn muốn kiểm tra thông tin nào của xe?",
        True,
    ),
    ToolSpec(
        "climate.set_temperature",
        "Đặt nhiệt độ điều hoà. Hỗ trợ nhiệt độ tuyệt đối hoặc mục tiêu đã tính từ số đo mới. Không bật/tắt điều hoà.",
        Temperature,
        "Bạn muốn đặt nhiệt độ bao nhiêu?",
    ),
    ToolSpec(
        "window.set_position",
        "Điều chỉnh độ mở cửa sổ tại vị trí được yêu cầu. Hạ kính xuống là mở (100%); nâng kính lên là đóng (0%). Khi có phần trăm dùng đúng mức đó. Không mở cửa xe.",
        Window,
        "Bạn muốn mở hoặc đóng cửa sổ bên nào?",
    ),
    ToolSpec(
        "door.set_open",
        "Mở hoặc đóng cánh cửa xe tại vị trí được yêu cầu. Không thay đổi khoá cửa.",
        DoorOpen,
        "Bạn muốn mở hay đóng cửa ở vị trí nào?",
    ),
    ToolSpec(
        "door.set_lock",
        "Khoá hoặc mở khoá cửa tại vị trí được yêu cầu. Mở khoá không có nghĩa mở cánh cửa.",
        DoorLock,
        "Bạn muốn điều chỉnh khoá cửa ở vị trí nào?",
    ),
    ToolSpec(
        "hood.set_open",
        "Mở hoặc đóng nắp capo (khoang phía trước). Chỉ khi xe đang đỗ.",
        PanelOpen,
        "Bạn muốn mở hay đóng nắp capo?",
    ),
    ToolSpec(
        "trunk.set_open",
        "Mở hoặc đóng cốp sau (cửa hậu). Chỉ khi xe đang đỗ.",
        PanelOpen,
        "Bạn muốn mở hay đóng cốp sau?",
    ),
    ToolSpec(
        "body.set_open",
        "Mở hoặc đóng cùng lúc cả bốn cửa, nắp capo và cốp sau, chỉ khi người dùng yêu cầu tất cả. Chỉ mở khi xe đang đỗ.",
        PanelOpen,
        "Bạn muốn mở hay đóng tất cả cửa, capo và cốp?",
    ),
    ToolSpec(
        "seat.set_heat_level",
        "Chỉnh mức sưởi ghế tại vị trí được yêu cầu. Không điều chỉnh vị trí ghế.",
        SeatHeat,
        "Bạn muốn điều chỉnh sưởi ghế ở vị trí nào?",
    ),
    ToolSpec("media.play", "Phát nhạc hoặc nội dung âm thanh được yêu cầu.", MediaPlay, "Bạn muốn nghe gì?"),
    ToolSpec("media.pause", "Dừng phát âm thanh.", Parameters, "Bạn muốn dừng nhạc phải không?"),
)
TOOL_REGISTRY = {tool.name: tool for tool in TOOLS}
NAVIGATION_TOOLS = (
    ToolSpec(
        "navigation.search",
        "Tìm địa điểm. pharmacy/charging là loại địa điểm, nearest cần vị trí và khoảng cách lái xe. Luôn chờ chọn kết quả.",
        NavigationSearch,
        "Bạn muốn tìm địa điểm nào?",
        True,
    ),
    ToolSpec(
        "navigation.plan",
        "Chờ chọn điểm đến trước khi lập tuyến xem trước. use_search_results dùng danh sách đã tìm ở bước trước. Không tự bắt đầu.",
        NavigationPlan,
        "Bạn muốn đi đến đâu?",
        True,
    ),
    ToolSpec(
        "navigation.select",
        "Chọn đúng handle từ các địa điểm đã tìm để lập tuyến xem trước.",
        NavigationSelect,
        "Bạn muốn chọn địa điểm nào?",
        True,
    ),
    ToolSpec(
        "navigation.refine",
        "Làm rõ vùng đang chờ: choice_handle từ navigation.clarification.choices hoặc query là địa điểm cụ thể trong vùng. Không tự bắt đầu tuyến.",
        NavigationRefine,
        "Bạn muốn đến trung tâm hay địa điểm cụ thể nào?",
        True,
    ),
    ToolSpec(
        "navigation.start",
        "Bắt đầu tuyến xem trước sau xác nhận, bắt buộc route_revision hiện tại; không tự suy quyền xác nhận.",
        NavigationStart,
        "Bạn chọn tuyến xem trước nhé.",
    ),
    ToolSpec(
        "navigation.cancel",
        "Huỷ tìm/chọn điểm đang chờ theo revision; giữ hành trình đang chạy.",
        NavigationStart,
        "Bạn muốn huỷ yêu cầu nào?",
    ),
    ToolSpec(
        "navigation.end",
        "Kết thúc hành trình, về P; không tiếp tục tuyến này khi chuyển D.",
        Parameters,
        "Bạn muốn kết thúc hành trình?",
    ),
    ToolSpec(
        "navigation.decline", "Từ chối bắt đầu, giữ tuyến để xem lại.", NavigationStart, "Bạn muốn giữ tuyến nào?"
    ),
    ToolSpec("navigation.stop", "Dừng hành trình hiện tại.", Parameters, "Bạn muốn dừng dẫn đường?"),
    ToolSpec(
        "navigation.set_gear",
        "Chuyển P để đỗ/tạm dừng hoặc D để tiếp tục tuyến đã chấp nhận; không nhận bản nháp.",
        NavigationGear,
        "Bạn muốn chuyển P hay D?",
    ),
)
NAVIGATION_TOOL = NAVIGATION_TOOLS[0]
TOOL_REGISTRY.update({tool.name: tool for tool in NAVIGATION_TOOLS})


class TripSelect(NavigationSelect):
    plan_id: str
    route_revision: StrictInt = Field(ge=1)


class TripRefine(NavigationRefine):
    plan_id: str
    route_revision: StrictInt = Field(ge=1)


class TripIdentity(Parameters):
    plan_id: str
    route_revision: StrictInt = Field(ge=1)


TRIP_TOOLS = (
    ToolSpec(
        "trip.plan",
        "Lập kế hoạch chuyến đi/sạc, độc lập dẫn đường đang chạy. Tách điểm đến query, origin_query và trip_kind (round_trip nếu quay lại). Không tự chạy.",
        NavigationPlan,
        "Bạn muốn lập kế hoạch tới đâu?",
        True,
    ),
    ToolSpec(
        "trip.select",
        "Chọn handle địa điểm của trip_plan hiện tại; giữ đúng ID/revision.",
        TripSelect,
        "Bạn chọn địa điểm nào?",
        True,
    ),
    ToolSpec(
        "trip.refine",
        "Đổi/giữ điểm mặc định theo câu hỏi trip_plan.clarification, dùng handle hoặc query và ID/revision.",
        TripRefine,
        "Bạn muốn đổi điểm đến nào?",
        True,
    ),
    ToolSpec(
        "trip.start",
        "Chấp nhận đề nghị bắt đầu kế hoạch đã xem, với đúng ID/revision. Server kiểm tra lại pin/vị trí/policy trước khi chạy.",
        TripIdentity,
        "Bạn muốn bắt đầu kế hoạch nào?",
    ),
    ToolSpec(
        "trip.decline",
        "Từ chối bắt đầu, vẫn giữ kế hoạch để xem lại.",
        TripIdentity,
        "Bạn muốn giữ kế hoạch nào?",
        True,
    ),
    ToolSpec(
        "trip.cancel",
        "Huỷ kế hoạch đang chờ; không dừng chuyến đang chạy.",
        TripIdentity,
        "Bạn muốn huỷ kế hoạch nào?",
        True,
    ),
)
TOOL_REGISTRY.update({tool.name: tool for tool in TRIP_TOOLS})


def validate_tool_arguments(name: str, arguments: dict) -> tuple[dict | None, str | None]:
    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        return None, "Mình chưa điều khiển tính năng này bằng giọng nói được."
    try:
        return spec.parameters.model_validate(arguments).model_dump(exclude_none=True), None
    except ValidationError as exc:
        if name == "climate.set_temperature" and any(
            error["type"] in {"greater_than_equal", "less_than_equal"} for error in exc.errors()
        ):
            return None, "Nhiệt độ hỗ trợ nằm trong khoảng 16 đến 30 độ C."
        if any(error["type"] == "extra_forbidden" for error in exc.errors()):
            return None, "Mình chưa hiểu rõ tham số của thao tác. Bạn nói lại yêu cầu nhé."
        missing = {error["loc"][0] for error in exc.errors() if error["type"] == "missing"}
        questions = {
            "value_celsius": "Bạn muốn đặt nhiệt độ bao nhiêu?",
            "position_percent": "Bạn muốn mở kính bao nhiêu phần trăm?",
            "open": "Bạn muốn mở hay đóng?",
            "locked": "Bạn muốn khoá hay mở khoá?",
            "level": "Bạn muốn sưởi ghế mức mấy?",
            "memory_value": "Bạn muốn mình nhớ tên nào?"
            if arguments.get("memory_key") == "display_name"
            else "Bạn muốn mình ghi nhớ nội dung gì?",
        }
        if (
            name in {"door.set_open", "window.set_position", "door.set_lock", "seat.set_heat_level"}
            and not arguments.get("zone")
            and not arguments.get("zones")
        ):
            questions["target"] = {
                "door.set_open": "Bạn muốn mở cửa ở vị trí nào?"
                if arguments.get("open") is True
                else "Bạn muốn đóng cửa ở vị trí nào?"
                if arguments.get("open") is False
                else "Bạn muốn thao tác với cửa ở vị trí nào?",
                "window.set_position": "Bạn muốn điều chỉnh kính ở vị trí nào?",
                "door.set_lock": "Bạn muốn khoá hay mở khoá cửa ở vị trí nào?",
                "seat.set_heat_level": "Bạn muốn điều chỉnh sưởi ghế ở vị trí nào?",
            }[name]
            missing.add("target")
        return None, next((questions[key] for key in questions if key in missing), spec.missing_question)
