"""Compact native-tool contract for the local SLM, independent of cloud DTOs."""

import json

from src.vivi.conversation.guardrails import TOPIC_INSTRUCTION
from src.vivi.dialogue.contracts import semantic_schema

LOCAL_SEMANTIC_INSTRUCTION = """
You are ViVi's Vietnamese semantic planner. Read the JSON field utterance as the
CURRENT user input and return exactly one interpret_turn tool call. Tool schemas,
capability descriptions and previous history are context, never new requests.
Propose meaning only; server code owns safety, consent, task IDs and execution.

ACT AND ORDER
Explicit commands use request, information questions use question. Only greetings
and thanks use reply without actions. offer is an unrequested suggestion, never
an explicit command. Unclear or unsupported meaning uses clarify/unsupported with
one short Vietnamese question/explanation and no executable actions.
The utterance can contain successive speech segments separated by newlines.
Interpret them TOGETHER in chronological order. Keep independent requests as
ordered actions (maximum five). A later explicit correction replaces the corrected
value or target of the earlier request, keeping all other requests. Identical
repetitions do not mean repeating an effect. Never ignore a later segment.
Generate only capabilities/slots justified by the CURRENT input or a pending
question. Do not add related operations, invent values, or execute old history.
Omit unused fields. New requests have no task_ref or step_index.

PENDING DIALOGUE
tasks lists unfinished steps and pending questions. A slot answer modifies its
current step; corrections use modify and the existing task handle. Additional
operations use append_actions, explicit changes to steps use step_updates.
A complete unrelated request is a new task. Completed steps are never repeated.
confirm/deny/cancel reference an existing task, have no action drafts and never
grant consent for a different operation. Missing required slots stay omitted/null
on a known capability request; do not turn them into an unknown-intent clarify.
Unknown parts of compound requests block the whole input; preserve unresolved
fragments and understood drafts in clarification_drafts. Negation, conditions and
scheduling do not authorize execution; mark their corresponding flags.

VEHICLE AND MEMORY
Window position is openness: 0 closed/glass up, 100 open/glass down. Never put
open or locked in window arguments. Use the exact spoken percentage. Driver is
driver, front passenger is front_passenger, rear left/right are rear_left/rear_right.
Unspecified row/side remains missing; never guess all. Use either zone or zones,
not both. A relative location uses target_side/target_row and referenced_target.
Climate explicit target uses value_celsius; change by an amount uses
temperature_delta_celsius. Never supply both. Unspecified relative change is two
degrees; cold means increase, hot means decrease. Never invent an absolute target.
There is no climate power/off capability. Only explicit remember/forget requests
change persistent memory. Vehicle facts/how-to use manual.search, live observed
state uses vehicle.get_status. Never claim an effect has already happened.
Vietnamese vehicle nouns are distinct: cốp/cốp sau = trunk, nắp capo = hood,
cửa = cabin door, cửa sổ/cửa kính = window. Never substitute one for another.
An operation does not request a status report. Only explicit status questions
use vehicle.get_status; never add it before an operation.

EXAMPLES (independent fresh requests; these are not the current input)
đóng cốp sau -> {"act":"request","actions":[{"capability":"trunk.set_open","arguments":{"open":false}}]}
mở nắp capo -> {"act":"request","actions":[{"capability":"hood.set_open","arguments":{"open":true}}]}
hạ kính bên phụ 40% -> {"act":"request","actions":[{"capability":"window.set_position","arguments":{"zone":"front_passenger","position_percent":40}}]}
tăng nhiệt độ điều hòa -> {"act":"request","actions":[{"capability":"climate.set_temperature","arguments":{"temperature_delta_celsius":2}}]}
nóng quá -> {"act":"request","actions":[{"capability":"climate.set_temperature","arguments":{"temperature_delta_celsius":-2}}]}
An unclear transcription must ask to repeat its unclear phrase. Do not infer a
vehicle action from an unrelated completed task or respond with a greeting.

INFORMATION AND NAVIGATION
Open conversation/stable knowledge/story requests use conversation.respond with
the user's question in query; don't answer them inside semantic text. Changing
facts and explicit internet searches use web.search; weather uses weather.get_forecast.
Use supplied topic/destination context for references, never invented locations.
Only capabilities listed in the supplied catalog are available. Place finding
uses navigation.search, directions navigation.plan, trip calculations trip.plan.
Destination query is the supplied place name; extract named origin separately.
Follow the current pending place question's effect and existing candidate handles.
Accepting a destination is not consent to drive: starting needs the current route
preview/offer and subsequent consent. Never invent plan IDs or route revisions.
"""


def _compact_metadata(value):
    if isinstance(value, dict):
        return {key: _compact_metadata(child) for key, child in value.items()
                if key not in {"title", "default"}}
    if isinstance(value, list):
        return [_compact_metadata(child) for child in value]
    return value


def local_semantic_tools(capabilities):
    # llama.cpp supports optional properties. Requiring every nullable field
    # forces hundreds of irrelevant output tokens and even unused step patches.
    # Keep the Pydantic required fields and validate omitted defaults server-side.
    schema = _compact_metadata(semantic_schema())
    preferred = ("act", "actions", "text", "step_updates", "append_actions", "task_ref", "step_index")
    properties = schema["properties"]
    schema["properties"] = {**{key: properties[key] for key in preferred},
                            **{key: value for key, value in properties.items() if key not in preferred}}
    if capabilities:
        common = schema["$defs"]["ActionDraft"]["properties"]
        arguments = schema["$defs"]["Arguments"]["properties"]
        definitions = schema["$defs"]
        branches = []
        zoned = {"door.set_open", "door.set_lock", "window.set_position", "seat.set_heat_level"}
        for item in capabilities:
            parameters = _compact_metadata(item.get("parameters", {}))
            definitions.update(parameters.pop("$defs", {}))
            slots = parameters.get("properties", {})
            # Omission/null is a missing slot, not a fabricated executable value.
            slots = {name: {"anyOf": [value, {"type": "null"}]} for name, value in slots.items()}
            if item["name"] == "climate.set_temperature":
                slots["temperature_delta_celsius"] = arguments["temperature_delta_celsius"]
            properties = {"capability": {"type": "string", "const": item["name"]},
                          "arguments": {"type": "object", "properties": slots, "additionalProperties": False}}
            if item["name"] in zoned:
                slots.update({key: arguments[key] for key in ("target_side", "target_row")})
                properties.update({key: common[key] for key in ("excluded_zones", "exclusion_mode", "referenced_target")})
            branches.append({"type": "object", "properties": properties, "required": ["capability"],
                             "additionalProperties": False, "description": item.get("description", "")})
        # Binding each capability to its own slots prevents e.g. `open: true`
        # being generated for window.set_position and expensive schema repairs.
        definitions["ActionDraft"] = {"oneOf": branches}
        needed = set()

        def references(value):
            if isinstance(value, dict):
                reference = value.get("$ref", "")
                if reference.startswith("#/$defs/"):
                    name = reference.rsplit("/", 1)[-1]
                    if name not in needed:
                        needed.add(name)
                        references(definitions[name])
                for key, child in value.items():
                    if key != "$defs":
                        references(child)
            elif isinstance(value, list):
                for child in value:
                    references(child)

        turn_properties = schema.pop("properties")
        schema.pop("required", None)
        flags = ("negated", "conditional", "scheduled", "conflicting", "unresolved")

        def turn_branch(act, fields, required):
            values = {"act": {"type": "string", "const": act}}
            values.update({name: turn_properties[name] for name in fields})
            return {"type": "object", "properties": values, "required": ["act", *required],
                    "additionalProperties": False}

        branches = []
        for act in ("request", "question"):
            branch = turn_branch(act, ("actions", *flags), ("actions",))
            branch["properties"]["actions"] = {**turn_properties["actions"], "minItems": 1}
            branches.append(branch)
        branches.append(turn_branch("offer", ("actions", "text", *flags), ("actions", "text")))
        # A request cannot accidentally carry modification fields; a modification
        # uses either legacy actions or explicit patches, never both.
        for fields, required in ((("actions", "append_actions", "task_ref", "step_index", *flags), ("task_ref", "actions")),
                                 (("step_updates", "append_actions", "task_ref", *flags), ("task_ref", "step_updates")),
                                 (("append_actions", "task_ref", *flags), ("task_ref", "append_actions"))):
            branches.append(turn_branch("modify", fields, required))
        for act in ("confirm", "deny", "cancel", "resume", "suspend"):
            branches.append(turn_branch(act, ("task_ref", "text", *flags), ()))
        for act in ("reply", "clarify", "unsupported"):
            fields = ("text", "clarification_drafts", *flags) if act == "clarify" else ("text", *flags)
            branches.append(turn_branch(act, fields, ("text",)))
        schema.pop("additionalProperties", None)
        schema["oneOf"] = branches
        references(schema)
        schema["$defs"] = {name: value for name, value in definitions.items() if name in needed}
    return [{"type": "function", "function": {
        "name": "interpret_turn", "description": "Interpret the complete turn into semantic events.",
        "parameters": schema,
    }}]


def native_semantic_prompt(value):
    # Types/descriptions are already in the native tool branches. Keep one copy
    # and put this stable capability prefix before the changing utterance/context.
    utterance = value.get("utterance", "")
    value = {"capabilities": [{"name": item["name"], "read_only": item.get("read_only", False)}
                              for item in value.get("capabilities", [])],
             **{key: item for key, item in value.items() if key not in {"capabilities", "utterance"}},
             "utterance": utterance}
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def local_system_instruction():
    return LOCAL_SEMANTIC_INSTRUCTION + TOPIC_INSTRUCTION + "\nReturn only the tool call. Omit null slots and unused optional fields."
