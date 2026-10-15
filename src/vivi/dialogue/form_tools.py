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
    return {'location_choices': ZONE_LABELS if 'zone' in _fields(document)['properties'] else {},
            'form_id': f"{task.get('form_identity', 'active')}:step:{step['step_index']}", 'revision': task['revision'],
            'operation': document['name'], 'status': step['status'], 'values': values,
            'missing_fields': step.get('missing_slots', []), 'question': step.get('question'),
            'field_schema': _fields(document), 'field_guide': document['description']}


def lifecycle_tools(documents, tasks):
    form = current_form(tasks, documents)
    writable = [d for d in documents if d.get('_meta', {}).get('capability') in FORM_CAPABILITIES
                and (not form or d['name'] != form['operation'])]
    tools = []
    if writable:
        # Each branch is generated from the executable tool document, not a
        # free-form model-authored schema or arbitrary code.
        branches = [{'type': 'object', 'properties': {
            'operation': {'type': 'string', 'enum': [d['name']]}, 'values': _inline(d['inputSchema'])},
            'required': ['operation', 'values'], 'additionalProperties': False} for d in writable]
        tools.append({'type': 'function', 'function': {'name': 'form_create',
            'description': 'Tạo form yêu cầu mới. Chọn operation và điền values được người dùng nói. Thiếu vị trí hoặc giá trị thì bỏ trường đó; form sẽ hỏi thêm. Ví dụ mở cửa xe cho tôi: operation=door_set_open, values={open:true}, KHÔNG có zone. Không chọn driver làm mặc định. Chưa thực hiện thao tác.',
            'parameters': branches[0] if len(branches) == 1 else {'oneOf': branches}}})
    if not form:
        return tools
    identity = {'form_id': {'type': 'string', 'enum': [form['form_id']]},
                'revision': {'type': 'integer', 'enum': [form['revision']]}}
    patch_schema = copy.deepcopy(form['field_schema'])
    patch_schema['minProperties'] = 1
    tools.append({'type': 'function', 'function': {'name': 'form_update',
        'description': 'Điền hoặc sửa form hiện tại bằng câu trả lời người dùng, kể cả không dấu/lỗi gõ nhẹ. Chỉ gửi values thay đổi; giữ thao tác và thông tin khác. Vị trí: driver=bên tài/ghế lái; front_passenger=bên phụ/phụ/hành khách; rear_left=sau trái; rear_right=sau phải. Không reply thay cho điền form.',
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
        return [('create', payload['operation'], payload['values'])]
    form = current_form(tasks, documents)
    if not form or payload['form_id'] != form['form_id'] or payload['revision'] != form['revision']:
        raise ValueError('Stale form revision')
    values = payload['values']
    capability = next(d['_meta']['capability'] for d in documents if d['name'] == form['operation'])
    if name == 'form_submit':
        if not allow_submit or values != form['values']:
            raise ValueError('Submit requires current user consent and exactly the reviewed values')
        return SemanticTurn(act='confirm', task_ref='active')
    if all(form['values'].get(key) == value for key, value in values.items()):
        raise ValueError('No form values changed. Fill a missing field from the current answer, or ask a specific clarification; do not repeat existing values.')
    # Action values are validated independently; task/step handles are host-bound.
    from src.vivi.dialogue.contracts import StepUpdate

    index = int(form['form_id'].rsplit(':', 1)[-1])
    action = ActionDraft(capability=capability, arguments=values)
    return SemanticTurn(act='modify', task_ref='active', step_updates=[StepUpdate(step_index=index, action=action)])
