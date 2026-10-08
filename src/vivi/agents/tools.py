"""Executable tool contracts, shared by the planner, model prompt and validator.

The registry is the allow-list, not an execution bypass. Vehicle policy still
owns permission, confirmation, execution and verification.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, field_validator

from src.vivi.text import normalize_text

Zone = Literal["driver", "front_passenger", "rear_left", "rear_right", "all"]


class Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Temperature(Parameters):
    value_celsius: float = Field(ge=16, le=30, description="Nhiệt độ đích, không phải số độ tăng/giảm.")


class Window(Parameters):
    position_percent: StrictInt = Field(ge=0, le=100, description="0 đóng hoàn toàn, 100 mở hoàn toàn.")
    zone: Zone


class DoorOpen(Parameters):
    open: StrictBool
    zone: Zone


class DoorLock(Parameters):
    locked: StrictBool
    zone: Zone


class SeatHeat(Parameters):
    level: StrictInt = Field(ge=0, le=3, description="0 tắt sưởi, 1–3 mức sưởi.")
    zone: Zone


class MediaPlay(Parameters):
    media_query: str | None = Field(default=None, max_length=200, description="Requested song, artist or genre only. Omit for generic music; đi/nhé/nhạc are not search terms.")

    @field_validator("media_query")
    @classmethod
    def generic_music_is_not_a_title(cls, value):
        if value is not None and normalize_text(value).strip(" .!?") in {"nhac", "music", "di", "nhe", "luon di"}:
            return None
        return value


class ManualSearch(Parameters):
    query: str = Field(min_length=2, max_length=1000)


class MemoryRecall(Parameters):
    memory_key: str | None = Field(default=None, description="Khoá đã lưu; preferences để chỉ đọc sở thích; bỏ trống để đọc tất cả.")


class MemoryForget(Parameters):
    memory_key: str = Field(min_length=1, max_length=100)


class MemoryRemember(MemoryForget):
    memory_value: str = Field(min_length=1, max_length=700)


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


TOOLS = (
    ToolSpec("memory.recall", "Đọc thông tin người dùng đã yêu cầu lưu. Không suy diễn từ lịch sử.",
             MemoryRecall, "Bạn muốn xem thông tin nào đã nhớ?", True),
    ToolSpec("memory.remember", "Lưu khi người dùng yêu cầu ghi nhớ rõ ràng; key display_name, preferred_music, preferred_temperature, response_style hoặc note.*.",
             MemoryRemember, "Bạn muốn mình ghi nhớ điều gì?"),
    ToolSpec("memory.forget", "Xoá một mục đã lưu theo đúng memory_key có trong bộ nhớ.",
             MemoryForget, "Bạn muốn mình quên thông tin nào?"),
    ToolSpec("memory.reset", "Đề nghị xoá mọi thông tin cá nhân đã nhớ; hệ thống sẽ hỏi xác nhận trước khi xoá.",
             Parameters, "Bạn muốn xoá toàn bộ thông tin mình đã nhớ về bạn phải không?"),
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
        Parameters,
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
        "Điều chỉnh độ mở cửa sổ tại vị trí được yêu cầu. Không mở cửa xe.",
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
        "seat.set_heat_level",
        "Chỉnh mức sưởi ghế tại vị trí được yêu cầu. Không điều chỉnh vị trí ghế.",
        SeatHeat,
        "Bạn muốn điều chỉnh sưởi ghế ở vị trí nào?",
    ),
    ToolSpec("media.play", "Phát nhạc hoặc nội dung âm thanh được yêu cầu.", MediaPlay, "Bạn muốn nghe gì?"),
    ToolSpec("media.pause", "Dừng phát âm thanh.", Parameters, "Bạn muốn dừng nhạc phải không?"),
)
TOOL_REGISTRY = {tool.name: tool for tool in TOOLS}


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
        return None, spec.missing_question
