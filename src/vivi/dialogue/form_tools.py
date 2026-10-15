"""Model-facing form lifecycle projected from the existing task checkpoint.

No second store: revision, consent and effect ownership remain in the workflow.
These tools interpret model-supplied values, never natural-language text.
"""
from __future__ import annotations

import copy

from jsonschema import Draft202012Validator

from src.vivi.dialogue.contracts import ActionDraft, SemanticTurn
from src.vivi.vehicle.zones import ZONE_LABELS

FORM_CAPABILITIES = {'door.set_open', 'door.set_lock', 'window.set_position',
                     'seat.set_heat_level', 'hood.set_open', 'trunk.set_open',
                     'body.set_open', 'climate.set_temperature', 'media.play', 'media.pause'}


def _inline(schema):
    definitions = schema.get('$defs', {})
    def visit(value):
        if isinstance(value, dict):
            if '$ref' in value:
                return visit(definitions[value['$ref'].rsplit('/', 1)[-1]])
            return {k: visit(v) for k, v in value.items() if k != '$defs'}
        if isinstance(value, list):
            return [visit(v) for v in value]
        return value
    return visit(schema)


def _fields(document):
    schema = _inline(document['inputSchema'])
    properties = {}
    for branch in schema.get('oneOf', [schema]):
        properties.update(copy.deepcopy(branch.get('properties', {})))
    result = {'type': 'object', 'properties': properties, 'additionalProperties': False}
    if '$defs' in schema:
        result['$defs'] = copy.deepcopy(schema['$defs'])
    return result


def _operations(document):
    """Intent aliases remove the ambiguous set_open(bool) choice from models."""
    capability = document.get('_meta', {}).get('capability', '')
    if capability.endswith('.set_open'):
        for verb, value in [('open', True), ('close', False)]:
            schema = _inline(document['inputSchema'])
            for branch in schema.get('oneOf', [schema]):
                branch.get('properties', {}).pop('open', None)
                branch['required'] = [key for key in branch.get('required', []) if key != 'open']
                if 'zone' in branch.get('properties', {}):
                    # Explicit unknown is null; the model must decide rather
                    # than silently omit an actually supplied location.
                    branch['required'] = list(dict.fromkeys([*branch['required'], 'zone']))
                    branch['properties']['zone']['description'] = 'all=toàn bộ bốn cửa cabin; driver=bên tài/cửa lái; front_passenger=bên phụ; rear_left=sau trái; rear_right=sau phải. Không nói vị trí: null. Có số lượng/còn lại ít hơn bốn cửa: dùng zones, không dùng all.'
                if 'zones' in branch.get('properties', {}):
                    branch['properties']['zones']['description'] = 'Danh sách các cửa được yêu cầu. Với cửa còn lại, đọc vehicle_observation.door_open: true=đang mở, false=đang đóng. Đóng các cửa còn lại chỉ chọn cửa đang mở; mở các cửa còn lại chỉ chọn cửa đang đóng. Không thêm cửa đã ở trạng thái đích.'
            yield capability.split('.')[0] + '_' + verb, {'open': value}, schema
    else:
        yield document['name'], {}, _inline(document['inputSchema'])


def current_form(tasks, documents):
    task = (tasks or {}).get('active')
    if not task:
        return None
    step = next((s for s in task.get('steps', []) if s['step_index'] == task.get('current_step')), None)
    if not step or step['capability'] not in FORM_CAPABILITIES or step['status'] not in {'waiting_slot', 'waiting_confirmation'}:
        return None
    document = next((d for d in documents if d['name'] == step['capability'].replace('.', '_')), None)
    if not document:
        return None
    values = step.get('resolved_arguments') or step['arguments']
    operation = document['name']
    if step['capability'].endswith('.set_open') and isinstance(values.get('open'), bool):
        operation = step['capability'].split('.')[0] + ('_open' if values['open'] else '_close')
    return {'capability': step['capability'], 'location_choices': ZONE_LABELS if 'zone' in _fields(document)['properties'] else {},
            'form_id': f"{task.get('form_identity', 'active')}:step:{step['step_index']}", 'revision': task['revision'],
            'operation': operation, 'status': step['status'], 'values': values,
            'missing_fields': step.get('missing_slots', []), 'question': step.get('question'),
            'field_schema': _fields(document), 'field_guide': document['description']}


def lifecycle_tools(documents, tasks):
    form = current_form(tasks, documents)
    choices = [(d, operation, constants, schema) for d in documents
               if d.get('_meta', {}).get('capability') in FORM_CAPABILITIES
               for operation, constants, schema in _operations(d)
               if not form or operation != form['operation']]
    tools = []
    if choices:
        branches = [{'type': 'object', 'description': d['description'], 'properties': {
            'operation': {'type': 'string', 'enum': [operation]}, 'values': schema},
            'required': ['operation', 'values'], 'additionalProperties': False}
                    for d, operation, _, schema in choices]
        tools.append({'type': 'function', 'function': {'name': 'form_create',
            'description': 'Tạo form yêu cầu mới. operation door_open=mở cửa, door_close=đóng cửa; trunk_open/mở cốp, trunk_close/đóng cốp; hood_open/mở capo, hood_close/đóng capo. Chọn đúng operation theo câu hiện tại. Có tất cả/toàn bộ: zone=all; không bỏ vị trí đã nói rõ. values chỉ chứa vị trí hoặc số được nói; thiếu thì bỏ trường để hỏi thêm. Không tự chọn driver hoặc mọi cửa từ trạng thái xe. Ví dụ mở cửa xe cho tôi => door_open với values={zone:null}; đóng cửa xe cho tôi => door_close với values={zone:null}; thiếu vị trí thì phải để form hỏi. Chỉ suy vị trí từ trạng thái xe khi người dùng nói rõ còn lại/đang mở/đang đóng. Chưa thực hiện thao tác.',
            'parameters': branches[0] if len(branches) == 1 else {'oneOf': branches}}})
    if not form:
        return tools
    identity = {'form_id': {'type': 'string', 'enum': [form['form_id']]},
                'revision': {'type': 'integer', 'enum': [form['revision']]}}
    patch_schema = copy.deepcopy(form['field_schema'])
    if form['status'] == 'waiting_slot' and isinstance(form['values'].get('open'), bool):
        patch_schema['properties'].pop('open', None)
    patch_schema['minProperties'] = 1
    tools.append({'type': 'function', 'function': {'name': 'form_update',
        'description': 'Điền hoặc sửa form hiện tại bằng câu trả lời người dùng, kể cả không dấu/lỗi gõ nhẹ. Chỉ gửi values thay đổi; giữ thao tác và thông tin khác. Vị trí: driver=bên tài/ghế lái/cửa lái/tài xế; front_passenger=bên phụ/phụ/hành khách; rear_left=sau trái; rear_right=sau phải. Không reply thay cho điền form.',
        'parameters': {'type': 'object', 'properties': {**identity, 'values': patch_schema},
                       'required': ['form_id', 'revision', 'values'], 'additionalProperties': False}}})
    if form['status'] == 'waiting_confirmation':
        full_schema = copy.deepcopy(form['field_schema'])
        full_schema['required'] = list(form['values'])
        tools.append({'type': 'function', 'function': {'name': 'form_submit',
            'description': 'Chỉ khi người dùng xác nhận OK cho form này: gửi TOÀN BỘ values của form hiện tại. Không sửa giá trị trong lúc xác nhận. Backend kiểm tra quyền xác nhận rồi mới thực hiện.',
            'parameters': {'type': 'object', 'properties': {**identity, 'values': full_schema},
                           'required': ['form_id', 'revision', 'values'], 'additionalProperties': False}}})
    return tools


def decode_form_call(calls, documents, tasks, *, allow_submit=False):
    """Validate a model form call and translate it into the existing reducer DTO."""
    if not any(name in {'form_create', 'form_update', 'form_submit'} for name, _ in calls):
        return None
    if len(calls) > 1 and all(name == 'form_create' for name, _ in calls):
        if len(calls) > 5:
            raise ValueError('At most five form requests per turn')
        return [item for call in calls for item in decode_form_call([call], documents, tasks)]
    if len(calls) != 1:
        raise ValueError('A form update or submission must be atomic')
    name, payload = calls[0]
    offered = {t['function']['name']: t['function']['parameters'] for t in lifecycle_tools(documents, tasks)}
    if name not in offered:
        raise ValueError('Form is no longer available')
    errors = list(Draft202012Validator(offered[name]).iter_errors(payload))
    if errors:
        raise ValueError('Invalid form arguments: ' + errors[0].message)
    if name == 'form_create':
        for document in documents:
            for operation, constants, _ in _operations(document):
                if operation == payload['operation']:
                    return [('create', document['name'], {**payload['values'], **constants})]
        raise ValueError('Unknown form operation')
    form = current_form(tasks, documents)
    if not form or payload['form_id'] != form['form_id'] or payload['revision'] != form['revision']:
        raise ValueError('Stale form revision')
    values = payload['values']
    capability = form['capability']
    if name == 'form_submit':
        if not allow_submit or values != form['values']:
            raise ValueError('Submit requires current user consent and exactly the reviewed values')
        return SemanticTurn(act='confirm', task_ref='active')
    if (values.get('zone') is not None or values.get('zones') is not None) and any(
            values.get(key) is not None for key in ('target_side', 'target_row')):
        raise ValueError('Use a concrete zone/zones OR partial target_side/target_row, never both location representations')
    if values.get('zone') is not None and values.get('zones') is not None:
        raise ValueError('Use zone OR zones, not both')
    if all(form['values'].get(key) == value for key, value in values.items()):
        raise ValueError('No form values changed. Fill a missing field from the current answer, or ask a specific clarification; do not repeat existing values.')
    # Action values are validated independently; task/step handles are host-bound.
    from src.vivi.dialogue.contracts import StepUpdate

    index = int(form['form_id'].rsplit(':', 1)[-1])
    action = ActionDraft(capability=capability, arguments=values)
    return SemanticTurn(act='modify', task_ref='active', step_updates=[StepUpdate(step_index=index, action=action)])
