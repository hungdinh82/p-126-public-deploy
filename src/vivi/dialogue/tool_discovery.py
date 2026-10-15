"""Find tool documentation, never interpret arguments or authorize an action."""
import re

from src.vivi.text import normalize_text

MAX_TOOL_DOCUMENTS = 8

# Positive discovery vocabulary. Unlike an intent parser these tags carry no
# arguments, defaults, task changes or execution decisions. A model may request
# more documentation if this initial shortlist is insufficient.
DISCOVERY_TERMS = {
    'conversation.respond': ('nói chuyện', 'tâm sự', 'kể chuyện', 'kể một', 'viết bài', 'sốt', 'bệnh', 'đau'),
    'web.search': ('trên mạng', 'internet', 'hiện nay', 'hiện tại', 'giá vàng', 'tin tức'),
    'weather.get_forecast': ('thời tiết', 'trời mưa', 'dự báo', 'nắng không'),
    'knowledge.lookup': ('là gì', 'giải thích', 'tại sao', 'lịch sử'),
    'memory.recall': ('nhớ gì', 'đã nhớ', 'tên tôi'),
    'memory.remember': ('ghi nhớ', 'hãy nhớ', 'nhớ rằng', 'tôi tên', 'gọi tôi'),
    'memory.forget': ('quên', 'xóa ghi nhớ'),
    'memory.reset': ('quên hết', 'xóa hết'),
    'manual.search': ('cẩm nang', 'hướng dẫn', 'thông số', 'sạc bao lâu', 'cách dùng'),
    'vehicle.get_status': ('trạng thái', 'kiểm tra', 'pin còn', 'bao nhiêu độ', 'đang mở', 'đang đóng'),
    'climate.set_temperature': ('nhiệt độ', 'điều hòa', 'điều hoà', 'lạnh', 'nóng'),
    'window.set_position': ('cửa sổ', 'cửa kính', 'kính'),
    'door.set_open': ('cửa', 'cánh cửa'),
    'door.set_lock': ('khóa', 'khoá'),
    'hood.set_open': ('capo', 'ca pô', 'ca-pô'),
    'trunk.set_open': ('cốp', 'cửa hậu'),
    'body.set_open': ('tất cả cửa', 'toàn bộ cửa'),
    'seat.set_heat_level': ('sưởi ghế',),
    'media.play': ('nhạc', 'bài hát', 'nghe bài', 'âm thanh', 'play music', 'phát lại nhạc', 'tiếp tục nhạc'),
    'media.pause': ('dừng nhạc', 'tắt nhạc', 'tạm dừng nhạc', 'ngừng nhạc', 'pause music'),
    'navigation.set_gear': ('số p', 'số d', 'chuyển số', 'vào số'),
    'navigation.search': ('tìm', 'gần đây', 'ở đâu'),
    'navigation.plan': ('chỉ đường', 'dẫn đường', 'đường đến', 'đi tới', 'đến'),
    'trip.plan': ('hành trình', 'chuyến đi', 'khứ hồi', 'lập kế hoạch'),
    'navigation.start': ('bắt đầu dẫn đường', 'bắt đầu đi'),
    'navigation.stop': ('dừng dẫn đường',),
    'navigation.end': ('kết thúc dẫn đường',),
    'trip.start': ('bắt đầu hành trình',),
    'trip.cancel': ('hủy hành trình',),
}


def discover_capabilities(text, capabilities):
    text = normalize_text(text)
    matches = []
    for item in capabilities:
        terms = (*DISCOVERY_TERMS.get(item['name'], ()), item['name'], item['name'].replace('.', '_'))
        for term in terms:
            for match in re.finditer(r'(?<!\w)' + re.escape(normalize_text(term)) + r'(?!\w)', text):
                matches.append((match.start(), match.end(), item['name']))
    names = {name for start, end, name in matches if not any(
        other_start <= start and end <= other_end and (other_start < start or end < other_end)
        for other_start, other_end, _ in matches)}
    # No match is not evidence of unsupported intent: retain the complete catalog.
    return [item for item in capabilities if item['name'] in names] if names else capabilities


def initial_tool_documents(text, capabilities):
    selected = discover_capabilities(text, capabilities)
    if len(selected) <= MAX_TOOL_DOCUMENTS:
        return selected, None
    # Progressive disclosure: expose a compact index rather than all navigation
    # schemas when lexical discovery is inconclusive. No capability disappears;
    # the model can explicitly fetch its documentation by name.
    core = {'conversation.respond', 'knowledge.lookup', 'web.search'}
    index = [{'name': item['name'], 'description': item.get('description', '')[:140]} for item in capabilities]
    return [item for item in capabilities if item['name'] in core], index
