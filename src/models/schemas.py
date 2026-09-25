from uuid import uuid4

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=5000, description="Tin nhắn từ user")


class ChatResponse(BaseModel):
    response: str = Field(..., description="Phản hồi từ agent")
    analysis: str = Field(default="", description="Phân tích nội bộ")


class VehicleContext(BaseModel):
    temperature_celsius: float = 23
    window_driver_percent: int = Field(default=0, ge=0, le=100)
    driver_door_open: bool = False
    media_playing: bool = False
    driving: bool = False
    battery_percent: int = Field(default=82, ge=0, le=100)
    range_km: int = Field(default=328, ge=0)


class AssistRequest(BaseModel):
    input_text: str = Field(min_length=1, max_length=1000)
    session_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=100)
    turn_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=100)
    vehicle_model: str = "VF8"
    model_year: int = Field(default=2026, ge=2000, le=2100)
    locale: str = "vi_vn"
    vehicle_state: VehicleContext | None = None
    confirmation_id: str | None = Field(default=None, min_length=1, max_length=100)
    confirmation_decision: str | None = Field(default=None, pattern="^(approve|deny)$")
