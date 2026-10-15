"""Documented native tools propose drafts; none execute vehicle operations."""
from __future__ import annotations

import copy
import json
import re

from src.vivi.dialogue.contracts import ActionDraft, SemanticTurn
from src.vivi.dialogue.form_tools import FORM_CAPABILITIES, decode_form_call, lifecycle_tools
from src.vivi.dialogue.local_semantics import local_semantic_tools
from src.vivi.dialogue.parser import Lexer
from src.vivi.dialogue.tool_discovery import DISCOVERY_TERMS

LOCAL_TOOL_INSTRUCTION = """Phân tích yêu cầu hiện tại của người dùng và gọi công cụ đúng. Với thao tác xe: form_create tạo form; khi có form, đọc values và field_schema rồi dùng form_update để điền hoặc sửa values. Không hỏi lặp lại khi người dùng đã bổ sung vị trí, hiểu không dấu và lỗi gõ nhẹ. Khi form đủ và người dùng đồng ý, gọi form_submit với toàn bộ values đã được hỏi xác nhận. Không tự chọn vị trí bị thiếu. Quy tắc cửa kính: LÊN/NÂNG/ĐÓNG gọi form_create_raise_window (0% mở), XUỐNG/HẠ/MỞ gọi form_create_lower_window (100% mở). "lên kính" => form_create_raise_window(zone=null); "xuống kính" hoặc "mở cửa kính" => form_create_lower_window(zone=null); "đóng cửa kính bên phụ" => form_create_raise_window(zone=front_passenger). Không nói vị trí thì zone=null; KHÔNG tự chọn driver/front_passenger/all/zones. Chỉ khi nói tất cả mới zone=all. Có con số phần trăm cụ thể mới dùng form_create_window_position. MỞ cửa/cốp/capo dùng open=true, ĐÓNG dùng open=false. Luôn giữ đúng chiều thao tác của câu hiện tại, kể cả khi câu trước là mở. Chỉ trả lời người dùng bằng tiếng Việt. Khi người dùng lạnh cần làm ấm bằng tăng nhiệt độ; khi nóng cần làm mát bằng giảm nhiệt độ. Đề xuất thao tác, không tự thực hiện. Tăng/giảm hoặc than nóng/lạnh dùng climate_adjust_temperature(direction, amount); không tự bịa nhiệt độ đích, amount mặc định 2 độ. Câu nghe không hiểu dùng dialogue_clarify để hỏi lại, không dùng reply. Với lệnh cấm/phủ định, điều kiện hoặc hẹn giờ: hỏi lại, không đề xuất thao tác.
"""


GUIDANCE = {
    'trunk.set_open': 'Dùng cho cốp xe/cốp sau/cửa hậu. Không phải cửa ghế sau, cửa kính hay nắp capo. Chỉ open=true (mở) hoặc false (đóng).',
    'hood.set_open': 'Dùng cho nắp capo/khoang phía trước. Không phải cốp sau hoặc cửa cabin.',
    'window.set_position': 'Dùng cho cửa kính/cửa sổ. Bên tài=driver; bên phụ=front_passenger; sau trái=rear_left; sau phải=rear_right. Xuống/hạ/mở kính dùng window_open=100% mở; lên/nâng/đóng kính dùng window_close=0% mở. Không nói vị trí: zone=null, hỏi vị trí, không tự chọn bên tài. Giữ phần trăm được nói. Không mở cửa cabin.',
    'door.set_open': 'Mở cửa: open=true. Đóng cửa: open=false. Bên tài/cửa lái/tài xế là driver. Cửa còn lại phải căn cứ trạng thái cửa đang được cung cấp; không đoán vị trí. Chỉ mở/đóng cánh cửa cabin. Không dùng cho cửa sổ/cửa kính, cốp, capo hoặc mở khóa.',
    'door.set_lock': 'Chỉ khóa/mở khóa cửa cabin; không mở cánh cửa hoặc kính.',
    'climate.set_temperature': 'Đặt nhiệt độ dùng value_celsius. Tăng/giảm dùng temperature_delta_celsius, chỉ chọn một loại. Than nóng: delta=-2. Than lạnh: delta=2. Ví dụ "tôi lạnh quá" -> {"temperature_delta_celsius":2}.',
    'media.play': 'Phát/bật/mở nhạc hoặc tiếp tục nghe nhạc. Nhạc chung gọi với {}. Chỉ điền media_query khi có tên bài, ca sĩ, thể loại hoặc tâm trạng được nói rõ. Không tự đổi bài hoặc âm lượng. Demo UI phát nhạc mô phỏng, không khẳng định đã tìm được bản thu của ca sĩ.',
    'media.pause': 'Dừng/tắt/tạm dừng nhạc, arguments={}. Không tắt loa trả lời của trợ lý; bật/tắt loa là cài đặt UI.',
    'body.set_open': 'Chỉ dùng khi người dùng yêu cầu cùng lúc TẤT CẢ cửa cabin, capo VÀ cốp. Nếu chỉ nói tất cả cửa cabin: door_set_open(zone=all); tất cả cửa kính: window_set_position(zone=all).',
    'seat.set_heat_level': 'Sưởi ghế với level 0=tắt, 1–3=mức sưởi. Giữ vị trí ghế được nói, thiếu vị trí thì để backend hỏi. Không chỉnh nhiệt độ điều hòa, vị trí ghế hay bật làm mát ghế.',
    'manual.search': 'Tra hướng dẫn/thông số/cách dùng từ cẩm nang; không đọc số đo live hay điều khiển xe. Giữ query hiện tại. Chỉ điền model/năm/AC/DC khi có căn cứ.',
    'conversation.respond': 'Chỉ dùng khi hiểu được yêu cầu trò chuyện/kiến thức/truyện. Không dùng cho câu STT không hiểu hoặc câu hỏi trợ lý cũ; câu nghe không rõ dùng dialogue_clarify. Backend tự gắn query hiện tại, model chỉ chọn mode nếu cần.',
    'navigation.set_gear': 'Chỉ đề xuất số P hoặc D được yêu cầu. Chuyển D không chấp thuận tuyến mới; backend kiểm tra cửa/capo/cốp và tuyến trước khi đổi số.',
    'navigation.search': 'Tìm địa điểm và chờ người dùng chọn, không tự khởi hành. Không tạo place_handle hoặc tọa độ. pharmacy/charging là bộ lọc; tìm thông tin chung trên mạng dùng web_search.',
    'navigation.plan': 'Lập tuyến xem trước tới điểm đến và nơi xuất phát nếu được nói. Không tự bắt đầu dẫn đường hoặc đoán route_revision.',
    'navigation.select': 'Chỉ dùng place_handle có trong các kết quả của biểu mẫu hiện tại; không tự tạo ID hoặc chọn hộ khi người dùng chưa chọn.',
    'navigation.refine': 'Bổ sung/chỉnh điểm đến của biểu mẫu hiện tại: một trong query/destination, choice_handle được cấp hoặc reject_candidates=true. Không lấy điểm đến từ chủ đề đã đóng.',
    'navigation.start': 'Chỉ bắt đầu tuyến người dùng đang xác nhận với route_revision do backend cung cấp. Không suy quyền xác nhận từ một yêu cầu mới.',
    'navigation.cancel': 'Đóng bản nháp tìm/chọn tuyến đang chờ theo route_revision; giữ hành trình đã bắt đầu.',
    'navigation.decline': 'Từ chối bắt đầu tuyến đang được hỏi; dùng đúng route_revision, không tự huỷ hành trình khác.',
    'navigation.stop': 'Dừng hành trình đang chạy. Không phải dừng nhạc hoặc đóng một biểu mẫu xe.',
    'navigation.end': 'Kết thúc hành trình theo yêu cầu; khác tạm dừng để tiếp tục sau.',
    'trip.plan': 'Lập kế hoạch chuyến đi hoặc khứ hồi, độc lập tuyến đang chạy; query là đích, origin_query là xuất phát, trip_kind=round_trip khi quay về.',
    'trip.select': 'Chọn place_handle từ kết quả trip_plan hiện tại; giữ đúng plan_id và route_revision được cấp.',
    'trip.refine': 'Bổ sung kế hoạch hiện tại bằng query hoặc choice_handle; giữ đúng plan_id/route_revision, không dùng dữ liệu của kế hoạch đã đóng.',
    'trip.start': 'Bắt đầu kế hoạch đang được xác nhận với plan_id/route_revision hiện tại; không tự khởi hành ngay sau khi chỉ hỏi đường.',
    'trip.decline': 'Từ chối kế hoạch đang được hỏi, dùng plan_id/route_revision hiện tại.',
    'trip.cancel': 'Huỷ kế hoạch được chỉ rõ bằng plan_id/route_revision hiện tại, không tự suy ID từ lịch sử.',
    'vehicle.get_status': 'Chỉ dùng khi hỏi/kiểm tra trạng thái hoặc số đo. Không tự thêm summary khi yêu cầu mở cốp, cửa kính hoặc điều chỉnh nhiệt độ.',
}


def _target_variants(schema):
    """Avoid duplicate location slots without removing support for missing targets."""
    props = schema['properties']
    variants = []
    for names, required in (({'zone'}, []), ({'zones'}, ['zones']),
                            ({'target_side', 'target_row'}, ['target_side']), ({'target_row'}, ['target_row'])):
        fields = {key: value for key, value in props.items()
                  if key not in {'zone', 'zones', 'target_side', 'target_row'} or key in names}
        variants.append({'type': 'object', 'properties': fields, 'required': required, 'additionalProperties': False})
    return {'oneOf': variants}


def local_tool_documents(capabilities):
    """MCP-shaped documentation from the same registry used by the executor.

    The schema permits missing slots because collecting them belongs to the reducer.
    Exporting these definitions does not implement an MCP transport or authorize calls.
    """
    definitions = local_semantic_tools(capabilities)[0]['function']['parameters']['$defs'] if capabilities else {}
    branches = definitions['ActionDraft']['oneOf'] if capabilities else []
    docs = []
    by_name = {item['name']: item for item in capabilities}
    for branch in branches:
        name = branch['properties']['capability']['const']
        schema = copy.deepcopy(branch['properties']['arguments'])
        if name in {'window.set_position', 'door.set_open', 'door.set_lock', 'seat.set_heat_level'}:
            schema = _target_variants(schema)
        elif name == 'climate.set_temperature':
            props = schema['properties']
            schema = {'type': 'object', 'properties': {'value_celsius': props['value_celsius']}, 'additionalProperties': False}
        needed = {}

        def references(value):
            if isinstance(value, dict):
                ref = value.get('$ref', '')
                if ref.startswith('#/$defs/'):
                    key = ref.rsplit('/', 1)[-1]
                    if key not in needed:
                        needed[key] = definitions[key]
                        references(definitions[key])
                for child in value.values():
                    references(child)
            elif isinstance(value, list):
                for child in value:
                    references(child)

        references(schema)
        if needed:
            schema['$defs'] = needed
        docs.append({'name': name.replace('.', '_'), 'description': branch.get('description', '') + ' ' + GUIDANCE.get(name, ''),
                     'inputSchema': schema, 'annotations': {'readOnlyHint': by_name[name].get('read_only', False)},
                     '_meta': {'capability': name, 'proposalOnly': True,
                               'discoveryTerms': list(DISCOVERY_TERMS.get(name, ()))}})
        if name == 'climate.set_temperature':
            docs[-1]['description'] = 'Chỉ ĐẶT nhiệt độ tuyệt đối khi người dùng nêu nhiệt độ đích: value_celsius là đúng số đó. Không dùng cho than nóng/lạnh hoặc tăng/giảm tương đối; không tự chọn nhiệt độ mặc định.'
            docs.append({'name': 'climate_adjust_temperature',
                         'description': 'Điều chỉnh tương đối nhiệt độ. Người dùng lạnh: tăng nhiệt độ (increase). Người dùng nóng: giảm nhiệt độ (decrease). amount là số độ dương; không nói số thì bỏ amount, mặc định 2 độ.',
                         'inputSchema': {'type': 'object', 'properties': {
                             'direction': {'type': 'string', 'enum': ['increase', 'decrease']},
                             'amount': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 30}},
                             'required': ['direction'], 'additionalProperties': False},
                         'annotations': {'readOnlyHint': False},
                         '_meta': {'capability': name, 'relativeTemperature': True, 'proposalOnly': True}})
    for act in ('clarify', 'reply', 'unsupported'):
        docs.append({'name': f'dialogue_{act}', 'description': {
            'clarify': 'CHỌN TOOL NÀY khi nghe không rõ hoặc chưa hiểu nghĩa câu nói. Hỏi lại bằng một câu hỏi ngắn, không nhại input. Không chọn unsupported cho câu nghe không rõ.',
            'reply': 'Trả lời ngắn cho lời chào/cảm ơn, không dùng để trả lời thay tool kiến thức hoặc thực hiện thao tác.',
            'unsupported': 'Chỉ dùng khi HIỂU RÕ thao tác nhưng hệ thống chưa có executor hỗ trợ. Câu nghe sai/không hiểu phải dùng dialogue_clarify. Không tự đề xuất thao tác thay thế.',
        }[act], 'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}},
                               'required': ['text'], 'additionalProperties': False},
                     'annotations': {'readOnlyHint': True}, '_meta': {'act': act, 'proposalOnly': True}})
    if by_name:
        docs.append({'name': 'dialogue_suggest_unrequested_action', 'description': 'UNSOLICITED suggestion only: người dùng CHƯA yêu cầu thao tác nào, AI tự gợi ý và hỏi có muốn không. KHÔNG dùng cho lệnh mở/đóng/đặt/tăng/giảm của người dùng. Không kết hợp với tool khác.',
                     'inputSchema': {'type': 'object', 'properties': {
                         'text': {'type': 'string'}, 'actions': {'type': 'array', 'minItems': 1, 'maxItems': 5,
                         'items': {'type': 'object', 'properties': {
                             'capability': {'type': 'string', 'enum': list(by_name)},
                             'arguments': {'type': 'object', 'additionalProperties': True}},
                             'required': ['capability'], 'additionalProperties': False}}},
                         'required': ['text', 'actions'], 'additionalProperties': False},
                     'annotations': {'readOnlyHint': True}, '_meta': {'act': 'offer', 'proposalOnly': True}})
    docs.append({'name': 'dialogue_find_tools', 'description': 'Lấy thêm tài liệu tool khi danh sách hiện tại thiếu công cụ phù hợp với yêu cầu. Không thực hiện thao tác.',
                 'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string'}},
                                 'required': ['query'], 'additionalProperties': False},
                 'annotations': {'readOnlyHint': True}, '_meta': {'discoveryOnly': True, 'proposalOnly': True}})
    return docs


def native_proposal_tools(capabilities, *, include_suggestions=False, utterance='', tasks=None):
    tools = [{'type': 'function', 'function': {'name': item['name'], 'description': item['description'],
                                            'parameters': item['inputSchema']}}
            for item in local_tool_documents(capabilities)
            if include_suggestions or item['_meta'].get('act') != 'offer']
    # The host owns the current question; do not let the model substitute a
    # previous assistant question as the query sent to the answer generator.
    for tool in tools:
        if tool['function']['name'] == 'conversation_respond':
            schema = tool['function']['parameters']
            schema['properties'].pop('query', None)
            schema['required'] = [key for key in schema.get('required', []) if key != 'query']
    # Slot values come from the model. Schemas restrict types and allowed zones,
    # never force a location inferred by a lexical match of the utterance.
    # Each editable step has its own small, typed patch tools. No arbitrary task
    # IDs, completed steps or capabilities can be invented in a patch proposal.
    by_name = {item['name']: item for item in local_tool_documents(capabilities)}
    updates = []
    bound_names = set()
    for handle, task in _editable_tasks(tasks):
        for index in task.get('editable_steps', []):
            step = next((step for step in task.get('steps', []) if step['step_index'] == index), None)
            if not step:
                continue
            for name, document in by_name.items():
                if document['_meta'].get('capability') != step['capability']:
                    continue
                parameters = document['inputSchema']
                description = f"Chỉ sửa/bổ sung bước {index} của {handle}: {document['description']}. Không dùng cho yêu cầu mới không liên quan."
                if (step.get('status') == 'waiting_slot'
                    and not set(step.get('missing_slots', [])) - {'zone', 'target_side', 'target_row'}
                    and step['capability'] in {
                        'door.set_open', 'door.set_lock', 'window.set_position', 'seat.set_heat_level'}):
                    description = ('Điền vị trí vào biểu mẫu đang hỏi. Giữ nguyên thao tác trước đó. '
                        'zone: driver=bên tài/ghế lái; front_passenger=bên phụ/phụ/hành khách; '
                        'rear_left=sau trái; rear_right=sau phải; all=tất cả. '
                        'Thiếu hàng ghế: target_side=left/right. Thiếu bên: target_row=front/rear. '
                        'Tài là người lái xe, thuộc driver; phụ là người ngồi cạnh tài xế, thuộc front_passenger. '
                        'Hiểu không dấu và lỗi gõ nhẹ, ký tự cuối bị lặp vẫn giữ nghĩa từ gốc. '
                        'Chỉ dùng khi lời hiện tại trả lời vị trí, không cho yêu cầu mới.')
                    parameters = {'type': 'object', 'properties': {
                        'zone': {'type': 'string', 'enum': ['driver', 'front_passenger', 'rear_left', 'rear_right', 'all'],
                                 'description': 'driver: ghế lái/bên tài/trước trái. front_passenger: ghế phụ/hành khách phía trước. rear_left: sau trái. rear_right: sau phải. all: tất cả.'},
                        'target_side': {'type': 'string', 'enum': ['left', 'right']},
                        'target_row': {'type': 'string', 'enum': ['front', 'rear']}},
                        'additionalProperties': False, 'minProperties': 1}
                if step.get('status') == 'waiting_slot':
                    bound_names.add(name)
                updates.append({'type': 'function', 'function': {
                    'name': _update_name(handle, index, name),
                    'description': description, 'parameters': parameters}})
    # Location is a form slot; the requested operation itself is mandatory.
    for tool in tools:
        if tool['function']['name'] == 'door_set_open':
            schema = tool['function']['parameters']
            for branch in schema.get('oneOf', [schema]):
                branch['required'] = list(dict.fromkeys([*branch.get('required', []), 'open']))
    legacy = [*[tool for tool in tools if tool['function']['name'] not in bound_names], *updates]
    if tasks is None:
        return legacy
    documents = list(by_name.values())
    forms = lifecycle_tools(documents, tasks, utterance=utterance)
    if tasks.get('confirmation_allowed') and any(t['function']['name'] == 'form_submit' for t in forms):
        return [t for t in forms if t['function']['name'] == 'form_submit']
    managed = {item['name'] for item in documents
               if item.get('_meta', {}).get('capability') in FORM_CAPABILITIES}
    return [*forms, *[tool for tool in legacy if tool['function']['name'] not in managed
                     and not tool['function']['name'].startswith('update_active_')]]


def _editable_tasks(tasks):
    tasks = tasks or {}
    for handle, task in [('active', tasks.get('active')),
                         *[(f'suspended:{i}', task) for i, task in enumerate(tasks.get('suspended', [])[:3])]]:
        if task:
            yield task.get('handle', handle), task


def _update_name(handle, index, name):
    return f"update_{handle.replace(':', '_')}_step_{index}__{name}"


def native_proposal_examples(tools, form=None):
    """Gold tool-call demonstrations, never runtime answers or slot overrides.

    End with missing-location examples so a small model does not carry the last
    example's passenger/driver into an unspecified request.
    """
    names = {tool['function']['name'] for tool in tools}
    if form:
        if form['capability'] != 'window.set_position' or 'form_update' not in names:
            return []
        examples = []
        for index, (text, zone) in enumerate([('sau phải', 'rear_right'), ('bên phụ', 'front_passenger'), ('bên tài', 'driver')]):
            call_id = f'slot_example_{index}'
            arguments = {'form_id': form['form_id'], 'revision': form['revision'], 'values': {'zone': zone}}
            examples.extend([
                {'role': 'user', 'content': native_proposal_prompt({'utterance': text, 'form': form})},
                {'role': 'assistant', 'content': None, 'tool_calls': [{'id': call_id, 'type': 'function',
                    'function': {'name': 'form_update', 'arguments': json.dumps(arguments, ensure_ascii=False)}}]},
                {'role': 'tool', 'tool_call_id': call_id, 'content': 'Updated location only; original operation retained. No effect executed.'},
            ])
        return examples
    cases = [
        ('hạ kính sau phải 30 phần trăm', 'form_create_window_position', 'sau phải', 'rear_right'),
        ('lên kính bên tài', 'form_create_raise_window', 'bên tài', 'driver'),
        ('mở cửa kính bên phụ', 'form_create_lower_window', 'bên phụ', 'front_passenger'),
        ('lên kính', 'form_create_raise_window', None, None),
        ('xuống kính', 'form_create_lower_window', None, None),
        ('mở cửa kính', 'form_create_lower_window', None, None),
        ('đóng cửa kính', 'form_create_raise_window', None, None),
    ]
    examples = []
    for index, (text, name, evidence, zone) in enumerate(cases):
        if name not in names:
            continue
        call_id = f'example_{index}'
        arguments = {'location_text': evidence, 'zone': zone}
        if name == 'form_create_window_position':
            arguments['position_percent'] = 30
        examples.extend([
            {'role': 'user', 'content': 'YÊU CẦU HIỆN TẠI:\n' + text},
            {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': call_id, 'type': 'function', 'function': {
                    'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}]},
            {'role': 'tool', 'tool_call_id': call_id, 'content': 'Form draft only. No effect executed.'},
        ])
    return examples


def native_proposal_prompt(value):
    def prune(item):
        if isinstance(item, dict):
            return {key: cleaned for key, child in item.items()
                    if (cleaned := prune(child)) not in (None, {}, [])}
        if isinstance(item, list):
            return [prune(child) for child in item]
        return item

    context = {key: item for key, item in value.items() if key not in {'capabilities', 'utterance'}}
    context = copy.deepcopy(context)
    clarification = context.get('tasks', {}).get('clarification')
    if clarification:
        # A free-text clarification is not a slot form. It contains no typed
        # pending operation and must not become the next user's command.
        if not clarification.get('drafts'):
            context['tasks'].pop('clarification', None)
        else:
            clarification.pop('question', None)
    navigation = context.get('tasks', {}).get('navigation') or {}
    if navigation.get('status') == 'idle' and not any(navigation.get(key) for key in (
            'request_id', 'destination', 'route_id', 'candidates', 'selected', 'origin')):
        context.get('tasks', {}).pop('navigation', None)
    if context.get('form'):
        for key in ('tasks', 'recent_turns', 'conversation_context'):
            context.pop(key, None)
    context = prune(context)
    # Preserve real speech boundaries instead of asking a small SLM to decode
    # escaped newlines inside a JSON string. Context remains before current input.
    prefix = ('NGỮ CẢNH (chỉ tham khảo, không thực hiện lại):\n'
              + json.dumps(context, ensure_ascii=False, separators=(',', ':'))
              + '\nLỜI NGƯỜI DÙNG HIỆN TẠI (đọc tất cả các dòng theo thứ tự):\n') if context else ''
    return (prefix or 'YÊU CẦU HIỆN TẠI:\n') + value.get('utterance', '')


def proposals_to_turn(calls, capabilities, *, include_suggestions=False, utterance=None, tasks=None):
    """Translate wire proposals atomically, retaining order; never execute them."""
    from jsonschema import Draft202012Validator

    docs = {item['name']: item for item in local_tool_documents(capabilities)
            if include_suggestions or item['_meta'].get('act') != 'offer'}
    decoded = decode_form_call(calls, list(docs.values()), tasks, utterance=utterance,
                               allow_submit=bool((tasks or {}).get('confirmation_allowed')))
    if isinstance(decoded, SemanticTurn):
        return decoded
    if decoded is None and (tasks or {}).get('confirmation_allowed'):
        raise ValueError('The current form was approved: use form_submit with all reviewed values')
    if decoded is not None:
        calls = [(name, values) for _, name, values in decoded]
    updates = {}
    for handle, task in _editable_tasks(tasks):
        for step in task.get('steps', []):
            index = step['step_index']
            if index not in task.get('editable_steps', []):
                continue
            for name, document in list(docs.items()):
                if document['_meta'].get('capability') == step['capability']:
                    updates[_update_name(handle, index, name)] = (handle, index, name)
    offered = {tool['function']['name']: tool['function']['parameters'] for tool in native_proposal_tools(
        capabilities, include_suggestions=include_suggestions, utterance=utterance or '', tasks=tasks)}
    # Existing native callers remain wire-compatible, but the model is shown
    # the form lifecycle tools when a task context is supplied.
    for name, document in docs.items():
        offered.setdefault(name, document['inputSchema'])
    for name, (_, _, original) in updates.items():
        offered.setdefault(name, docs[original]['inputSchema'])
    actions = []
    patches = []
    patch_handle = None
    read_only = True
    if not 1 <= len(calls) <= 5:
        raise ValueError('Expected one to five proposals')
    for wire_name, args in calls:
        if wire_name not in offered:
            raise ValueError('Tool not offered for this context')
        if utterance is not None or wire_name in updates:
            errors = list(Draft202012Validator(offered[wire_name]).iter_errors(args))
            if errors:
                raise ValueError(f'Invalid tool arguments: {errors[0].message}')
        patch = updates.get(wire_name)
        name = patch[2] if patch else wire_name
        if patch:
            if patch_handle and patch_handle != patch[0]:
                raise ValueError('A turn may only modify one task')
            patch_handle = patch[0]
        if name not in docs:
            raise ValueError('Tool not offered for this context')
        meta = docs[name]['_meta']
        if meta.get('discoveryOnly'):
            raise ValueError('Tool discovery is not an action proposal')
        if 'act' in meta:
            if len(calls) != 1:
                raise ValueError('Dialogue reply cannot be combined with action proposals')
            if meta['act'] == 'offer':
                if set(args) != {'text', 'actions'}:
                    raise ValueError('Offer requires only text and actions')
                frame = SemanticTurn.model_validate({**args, 'act': 'offer'})
                available = {item['name'] for item in capabilities}
                if any(action.capability not in available for action in frame.actions):
                    raise ValueError('Offer contains a tool not available in this context')
                return frame
            if set(args) != {'text'} or not isinstance(args['text'], str) or not args['text'].strip():
                raise ValueError('Expected a nonempty dialogue text')
            text = args['text']
            if meta['act'] == 'clarify' and ('?' not in text or (utterance is not None and
                    Lexer().normalize(text).strip(' ?.!') == Lexer().normalize(utterance).strip(' ?.!'))):
                text = 'Bạn nói rõ lại yêu cầu nhé.'
            return SemanticTurn(act=meta['act'], text=text)
        # Normalize equivalent location representations, never invent a target.
        args = dict(args)
        if meta.get('relativeTemperature'):
            if set(args) - {'amount', 'direction'} or args.get('direction') not in {'increase', 'decrease'}:
                raise ValueError('Relative temperature requires direction and optional amount')
            amount = args.get('amount', 2)
            if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not 0 < amount <= 30:
                raise ValueError('Temperature amount must be positive and at most 30')
            args = {'temperature_delta_celsius': (1 if args['direction'] == 'increase' else -1) * amount}
        if isinstance(args.get('zones'), list) and len(args['zones']) == 1 and not args.get('zone'):
            args['zone'] = args.pop('zones')[0]
        if args.get('zone'):
            args.pop('target_side', None)
            args.pop('target_row', None)
        if (meta['capability'] == 'media.play' and utterance is not None
            and Lexer().normalize(utterance) in {'bat nhac', 'mo nhac', 'phat nhac', 'nghe nhac'}
            and Lexer().normalize(args.get('media_query') or '') in {'nhac', 'am nhac', 'music'}):
            args.pop('media_query', None)  # Generic music noun is not a song title.
        if meta['capability'] == 'conversation.respond' and utterance is not None:
            args['query'] = utterance
        action = ActionDraft(capability=meta['capability'], arguments=args)
        if (utterance is not None and action.capability == 'climate.set_temperature'
            and action.arguments.value_celsius is not None and not _has_temperature_target(utterance)):
            raise ValueError('No absolute temperature was supplied in the CURRENT input. Do not invent value_celsius. '
                             'For increase/decrease or feeling hot/cold, use climate_adjust_temperature with direction and optional amount.')
        if action.arguments.value_celsius is not None and action.arguments.temperature_delta_celsius is not None:
            raise ValueError('Temperature target and delta are mutually exclusive')
        if patch:
            patches.append({'step_index': patch[1], 'action': action})
        else:
            actions.append(action)
        read_only &= docs[name]['annotations']['readOnlyHint']
    if patches:
        return SemanticTurn(act='modify', task_ref=patch_handle, step_updates=patches, append_actions=actions)
    return SemanticTurn(act='question' if read_only else 'request', actions=actions)


def _has_temperature_target(utterance):
    text = Lexer().normalize(utterance)
    return bool(re.search(r"\d|\b(?:muoi|hai muoi|ba muoi)\b", text)
                or re.search(r"\b(?:nhu cu|nhu da luu|yeu thich|thuong dung)\b", text))
