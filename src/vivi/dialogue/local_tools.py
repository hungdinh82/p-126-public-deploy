"""Documented native tools propose drafts; none execute vehicle operations."""
from __future__ import annotations

import copy
import json
import re

from src.vivi.dialogue.contracts import ActionDraft, SemanticTurn
from src.vivi.dialogue.local_semantics import local_semantic_tools
from src.vivi.dialogue.parser import Lexer
from src.vivi.dialogue.tool_discovery import DISCOVERY_TERMS

LOCAL_TOOL_INSTRUCTION = '''Bạn là bộ phân tích yêu cầu tiếng Việt trên xe. Gọi tool để biểu diễn chính xác điều NGƯỜI DÙNG YÊU CẦU; backend sẽ kiểm tra, xác nhận và thực thi sau. Không nói đã làm xong.
Chỉ phân tích utterance hiện tại. tasks và history là ngữ cảnh, không phải yêu cầu mới. Không lặp việc đã xong. Không thêm thao tác không được yêu cầu.
Tool được đưa vào theo tài liệu liên quan. Nếu thiếu tool phù hợp, gọi dialogue_find_tools để lấy thêm tài liệu; không dùng một tool gần giống để thay thế.
Cốp xe/cốp sau là trunk_set_open; nắp capo là hood_set_open; cửa kính/cửa sổ là window_set_position; cửa xe là door_set_open. Mở: open=true; đóng: open=false. Kính mở/hạ: position_percent=100; kính đóng/nâng: position_percent=0.
Bên tài/ghế lái là driver; bên phụ là front_passenger. Không nói vị trí thì bỏ zone. Chọn một trong zone, zones, hoặc target_side/target_row; không điền trùng.
Đặt nhiệt độ dùng value_celsius; tăng/giảm dùng temperature_delta_celsius. Nóng thì giảm 2 độ; lạnh thì tăng 2 độ.
Các đoạn nói nối tiếp là CHUNG một yêu cầu: giữ mọi thao tác độc lập, chỉ sửa giá trị được đính chính bằng giá trị cuối. Không lặp thao tác khi người dùng nhắc lại. Ví dụ đặt 20 độ, à không 22 độ, mở cốp: đề xuất đặt 22 độ VÀ mở cốp, không đặt 20 độ.
"à không", "ý tôi là", "sửa thành" là ĐÍNH CHÍNH giá trị trước đó, không phải lệnh cấm hay câu không hiểu. "đặt 20 độ, à không 22 độ" nghĩa là đặt 22 độ. Vẫn gọi các tool cho yêu cầu đã sửa và mọi thao tác bổ sung.
Chỉ hỏi trạng thái mới dùng vehicle_get_status, không tự thêm báo cáo trạng thái trước thao tác.
Yêu cầu rõ ràng như "mở...", "đặt..." phải gọi tool thao tác trực tiếp. Nếu muốn tự gợi ý một việc chưa được yêu cầu, lấy tài liệu về công cụ gợi ý qua dialogue_find_tools trước. Không gọi tool gợi ý cùng với tool thao tác.
Không hiểu thì dùng dialogue_clarify và hỏi lại ngắn gọn. Chào hỏi dùng dialogue_reply. Không đoán thao tác từ câu nghe không rõ. Lệnh cấm rõ ràng (đừng mở cửa), điều kiện hoặc hẹn giờ thì hỏi lại bằng dialogue_clarify. Đính chính "à không" vẫn được thực hiện với giá trị đã sửa.
Không điền null hoặc tham số không liên quan. Một lượt chỉ đề xuất tối đa năm thao tác, theo thứ tự người dùng nói. Các tool call là đề xuất có thứ tự, không phải lệnh chạy đồng thời.
'''

GUIDANCE = {
    'trunk.set_open': 'Dùng cho cốp xe/cốp sau/cửa hậu. Không phải cửa ghế sau, cửa kính hay nắp capo. Chỉ open=true (mở) hoặc false (đóng).',
    'hood.set_open': 'Dùng cho nắp capo/khoang phía trước. Không phải cốp sau hoặc cửa cabin.',
    'window.set_position': 'Dùng cho cửa kính/cửa sổ. Bên tài=driver; bên phụ=front_passenger; sau trái=rear_left; sau phải=rear_right. Mở/hạ=100, đóng/nâng=0. Giữ phần trăm được nói. Không mở cửa cabin.',
    'door.set_open': 'Chỉ mở/đóng cánh cửa cabin. Không dùng cho cửa sổ/cửa kính, cốp, capo hoặc mở khóa.',
    'door.set_lock': 'Chỉ khóa/mở khóa cửa cabin; không mở cánh cửa hoặc kính.',
    'climate.set_temperature': 'Đặt nhiệt độ dùng value_celsius. Tăng/giảm dùng temperature_delta_celsius, chỉ chọn một loại. Than nóng: delta=-2. Than lạnh: delta=2. Ví dụ "tôi lạnh quá" -> {"temperature_delta_celsius":2}.',
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
            docs[-1]['description'] = 'ĐẶT nhiệt độ tuyệt đối khi nêu nhiệt độ đích, ví dụ đặt 23 độ: value_celsius=23. Không dùng cho than nóng/lạnh hoặc tăng/giảm tương đối.'
            for tool_name, description, sign in (
                ('climate_warm_cabin', 'Làm ẤM cabin bằng TĂNG nhiệt độ. Dùng khi người dùng than LẠNH hoặc yêu cầu tăng nhiệt độ. amount là số độ tăng; không nói số thì bỏ amount, backend mặc định 2 độ.', 1),
                ('climate_cool_cabin', 'Làm MÁT cabin bằng GIẢM nhiệt độ. Dùng khi người dùng than NÓNG hoặc yêu cầu giảm nhiệt độ. amount là số độ giảm; không nói số thì bỏ amount, backend mặc định 2 độ.', -1),
            ):
                docs.append({'name': tool_name, 'description': description,
                             'inputSchema': {'type': 'object', 'properties': {'amount': {
                                 'type': 'number', 'exclusiveMinimum': 0, 'maximum': 30}}, 'additionalProperties': False},
                             'annotations': {'readOnlyHint': False},
                             '_meta': {'capability': name, 'temperatureSign': sign, 'proposalOnly': True}})
    for act in ('clarify', 'reply', 'unsupported'):
        docs.append({'name': f'dialogue_{act}', 'description': {
            'clarify': 'Hỏi lại khi yêu cầu nghe không rõ, không hiểu, thiếu nghĩa, hoặc chưa thể đề xuất an toàn. Không tự đoán.',
            'reply': 'Trả lời ngắn cho lời chào/cảm ơn, không dùng để trả lời thay tool kiến thức hoặc thực hiện thao tác.',
            'unsupported': 'Giải thích thao tác không được hỗ trợ; không đề xuất thao tác thay thế không được yêu cầu.',
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


def native_proposal_tools(capabilities, *, include_suggestions=False, utterance=''):
    tools = [{'type': 'function', 'function': {'name': item['name'], 'description': item['description'],
                                            'parameters': item['inputSchema']}}
            for item in local_tool_documents(capabilities)
            if include_suggestions or item['_meta'].get('act') != 'offer']
    # Bind an explicitly named, unambiguous target into the generation schema.
    # No target is guessed. Multiple targets/tools, negation and exclusions stay
    # unconstrained so they can be resolved by the existing dialogue machinery.
    zoned = {'window.set_position', 'door.set_open', 'door.set_lock', 'seat.set_heat_level'}
    candidates = [item['name'] for item in capabilities if item['name'] in zoned]
    normalized = Lexer().normalize(utterance)
    locations = set(re.findall(r'\b(?:driver|front_passenger|rear_left|rear_right|all)\b', normalized))
    if len(candidates) == len(locations) == 1 and not re.search(r'\b(?:khong|dung|tru|ngoai)\b', normalized):
        name, location = candidates[0].replace('.', '_'), next(iter(locations))
        for tool in tools:
            if tool['function']['name'] == name:
                properties = tool['function']['parameters']['oneOf'][0]['properties']
                properties['zone'] = {'type': 'string', 'const': location}
                tool['function']['parameters'] = {'type': 'object', 'properties': properties,
                                                   'required': ['zone'], 'additionalProperties': False}
    return tools


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
    navigation = context.get('tasks', {}).get('navigation') or {}
    if navigation.get('status') == 'idle' and not any(navigation.get(key) for key in (
            'request_id', 'destination', 'route_id', 'candidates', 'selected', 'origin')):
        context.get('tasks', {}).pop('navigation', None)
    context = prune(context)
    # Preserve real speech boundaries instead of asking a small SLM to decode
    # escaped newlines inside a JSON string. Context remains before current input.
    prefix = ('NGỮ CẢNH (chỉ tham khảo, không thực hiện lại):\n'
              + json.dumps(context, ensure_ascii=False, separators=(',', ':'))
              + '\nLỜI NGƯỜI DÙNG HIỆN TẠI (đọc tất cả các dòng theo thứ tự):\n') if context else ''
    return prefix + value.get('utterance', '')


def proposals_to_turn(calls, capabilities, *, include_suggestions=False):
    """Translate wire proposals atomically, retaining order; never execute them."""
    docs = {item['name']: item for item in local_tool_documents(capabilities)
            if include_suggestions or item['_meta'].get('act') != 'offer'}
    actions = []
    read_only = True
    if not 1 <= len(calls) <= 5:
        raise ValueError('Expected one to five proposals')
    for name, args in calls:
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
            if meta['act'] == 'clarify' and '?' not in text:
                text = 'Bạn nói rõ lại yêu cầu nhé.'
            return SemanticTurn(act=meta['act'], text=text)
        # Normalize equivalent location representations, never invent a target.
        args = dict(args)
        if 'temperatureSign' in meta:
            if set(args) - {'amount'}:
                raise ValueError('Relative temperature proposal only accepts amount')
            amount = args.get('amount', 2)
            if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not 0 < amount <= 30:
                raise ValueError('Temperature amount must be positive and at most 30')
            args = {'temperature_delta_celsius': meta['temperatureSign'] * amount}
        if isinstance(args.get('zones'), list) and len(args['zones']) == 1 and not args.get('zone'):
            args['zone'] = args.pop('zones')[0]
        if args.get('zone'):
            args.pop('target_side', None)
            args.pop('target_row', None)
        action = ActionDraft(capability=meta['capability'], arguments=args)
        if action.arguments.value_celsius is not None and action.arguments.temperature_delta_celsius is not None:
            raise ValueError('Temperature target and delta are mutually exclusive')
        actions.append(action)
        read_only &= docs[name]['annotations']['readOnlyHint']
    return SemanticTurn(act='question' if read_only else 'request', actions=actions)
