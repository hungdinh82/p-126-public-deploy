"""Semantic wire names for Vietnamese operations; public tool contracts stay stable."""

from copy import deepcopy

NAMES = {
    "open_door": "mở cánh cửa", "close_door": "đóng cánh cửa",
    "lock_door": "khóa chốt cửa", "unlock_door": "mở khóa chốt cửa",
    "raise_window": "nâng kính đóng kín", "lower_window": "hạ kính mở hết",
    "set_window": "đặt phần trăm mở kính",
    "open_hood": "mở nắp capo", "close_hood": "đóng nắp capo",
    "open_trunk": "mở cốp sau", "close_trunk": "đóng cốp sau",
    "open_body": "mở tất cả cửa capo cốp", "close_body": "đóng tất cả cửa capo cốp",
    "play_music": "bật nhạc", "pause_music": "tắt nhạc",
    "set_temperature": "đặt nhiệt độ đích", "increase_temperature": "tăng nhiệt độ ấm hơn",
    "decrease_temperature": "giảm nhiệt độ mát hơn",
    "get_cabin_temperature": "đọc nhiệt độ cabin", "get_battery_level": "đọc phần trăm pin",
    "get_tire_pressure": "đọc áp suất lốp", "get_remaining_range": "đọc quãng đường còn lại",
    "get_remaining_drive_time": "đọc số giờ còn đi được", "get_vehicle_diagnostics": "đọc mã lỗi xe",
    "get_door_status": "đọc trạng thái cánh cửa", "get_window_positions": "đọc độ mở kính",
    "get_hood_status": "đọc trạng thái capo", "get_trunk_status": "đọc trạng thái cốp",
    "get_body_status": "đọc tất cả cửa capo cốp", "get_vehicle_overview": "đọc tổng quan xe",
    "memory_remember": "lưu ghi nhớ", "memory_recall": "đọc ghi nhớ",
    "memory_forget": "xóa một ghi nhớ", "memory_reset": "xóa toàn bộ ghi nhớ",
    "conversation_respond": "trò chuyện", "create_content": "sáng tác nội dung",
    "lookup_information": "tra cứu thông tin", "manual_search": "tra cẩm nang xe",
    "weather_get_forecast": "dự báo thời tiết",
    "navigation_search": "tìm địa điểm", "navigation_select": "chọn điểm trong danh sách",
    "navigation_refine": "sửa điểm đến đang tìm", "navigation_cancel": "hủy bản nháp tuyến",
    "navigation_decline": "giữ tuyến chỉ xem chưa đi", "navigation_start": "bắt đầu dẫn đường",
    "navigation_stop": "tạm dừng hành trình", "navigation_end": "kết thúc hành trình",
    "navigation_set_gear": "chuyển số xe",
    "update_current_request": "điền sửa form hiện tại",
    "dialogue_find_tools": "tìm tài liệu tool", "dialogue_clarify": "hỏi rõ phần chưa hiểu",
    "dialogue_unsupported": "báo yêu cầu chưa hỗ trợ",
}


def localized_tools(tools):
    result, reverse = deepcopy(tools), {}
    for tool in result:
        name = tool["function"]["name"]
        if name.startswith(("get_directions", "calculate_trip")):
            base = "chỉ đường" if name.startswith("get_directions") else "tính hành trình"
            wire = base + (" khứ hồi" if "round_trip" in name else " một chiều")
            wire += " có nơi xuất phát" if name.endswith("_from") else ""
        else:
            root = name.removesuffix("_except")
            wire = NAMES.get(root, root) + (" trừ vị trí" if name.endswith("_except") else "")
        if wire in reverse:
            raise ValueError("Duplicate localized operation")
        reverse[wire] = name
        tool["function"]["name"] = wire
    return result, reverse
