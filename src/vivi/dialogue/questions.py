"""Question proposals and legacy adapters; IDs belong to checkpoint state."""

from datetime import UTC, datetime, timedelta

from src.vivi.dialogue.contracts import ActionDraft, PendingQuestion, QuestionOption, QuestionProposal
from src.vivi.navigation.contracts import DestinationSpec
from src.vivi.persistence.runtime_store import fingerprint


def propose_question(session):
    task = session.active
    index = task.current_index() if task else None
    step = task.steps[index] if index is not None else None
    if step is None and session.conversation_context.get("focus") == "information":
        return None
    prefix = step.draft.capability.split(".")[0] if step else session.navigation_focus
    if step is None and prefix == "navigation" and session.trip_plan and not (
        session.navigation_workspace.draft or session.navigation_workspace.journey
    ):
        prefix = "trip"
    if step and step.status == "offer":
        return QuestionProposal(purpose="confirm_proposal", owner=prefix, target_id=step.id,
                                revision=task.revision, kind="accept_offer", text=(step.result or {}).get("message", "Bạn đồng ý đề xuất này chứ?"),
                                yes_act="confirm", no_act="deny")
    if (step is None and task and task.steps
        and not any(s.draft.capability.startswith(("navigation.", "trip.")) for s in task.steps)):
        return None
    if step and step.status == "waiting_confirmation":
        return QuestionProposal(purpose="approve_execution", owner=prefix, target_id=step.id,
                                revision=task.revision, kind="confirm_action",
                                text=(step.confirmation or {}).get("question", "Bạn xác nhận thao tác này nhé."),
                                yes_act="confirm", no_act="deny")
    if prefix not in {"navigation", "trip"}:
        if step and step.status == "waiting_slot":
            return QuestionProposal(purpose="collect_slot", owner=prefix, target_id=step.id,
                                    revision=task.revision, kind="provide_slot",
                                    text=(step.result or {}).get("message", "Bạn bổ sung thông tin nhé."),
                                    slot=next(iter(step.missing_slots), None), slot_effect=step.draft)
        return None
    workspace = session.navigation_workspace
    owner = session.trip_plan if prefix == "trip" else workspace.draft or workspace.journey
    if owner is None:
        if step and step.status == "waiting_slot":
            destination = step.draft.arguments.destination
            missing_name = not step.draft.arguments.query and not (destination and destination.complete())
            return QuestionProposal(purpose="collect_slot", owner=prefix, target_id=step.id, revision=task.revision,
                                    kind="provide_destination", text=(step.result or {}).get("message", "Bạn muốn đến địa điểm nào?"),
                                    slot="name" if missing_name else "origin", slot_effect=step.draft)
        return None
    # Only these fields are needed. Never dump route/charging geometry here.
    raw = (owner.pending or {}).get("navigation") if getattr(owner, "pending", None) else None
    question = (raw.get("clarification") if raw else getattr(owner, "clarification", None)) or {}
    candidates = raw.get("candidates", []) if raw else getattr(owner, "candidates", [])
    status = raw.get("status") if raw else owner.status
    offer = raw.get("offer") if raw else getattr(owner, "offer", None)
    revision = owner.revision if prefix == "trip" else workspace.revision
    target = owner.id if prefix == "trip" else owner.request_id or owner.route_id or "navigation"
    identity = {"plan_id": owner.id, **({"route_revision": revision} if revision else {})} if prefix == "trip" else {}

    def action(name, **args):
        return ActionDraft(capability=f"{prefix}.{name}", arguments={**identity, **args})

    phase = question.get("phase")
    options = []
    if question:
        if question.get("revision", revision) != revision:
            return None
        for choice in question.get("choices", []):
            options.append(QuestionOption(id=choice["handle"], label=choice.get("label", ""),
                                          role=choice.get("kind", "choice"), effect=action("refine", choice_handle=choice["handle"])))
        keep = next((o.effect for o in options if o.role == "representative"), None)
        change = next((o.effect for o in options if o.role == "specific"), None)
        positive = phase == "confirm_destination"
        has_boolean = phase in {"confirm_destination", "change_destination"}
        stored_destination = (raw or {}).get("destination") or question.get("destination")
        destination = DestinationSpec.model_validate(stored_destination) if stored_destination else getattr(owner, "destination", None)
        if destination is None and question.get("region"):
            destination = DestinationSpec.from_arguments({"query": question["region"], "destination_scope": "region"})
        if phase == "place_area" and (destination is None or destination.query() != question.get("query")):
            destination = DestinationSpec.from_arguments({"query": question.get("query", "")})
        return QuestionProposal(purpose="confirm_proposal" if has_boolean else "collect_slot", owner=prefix,
                                target_id=target, revision=revision, kind=phase or "provide_destination",
                                text=question.get("question", "Bạn chọn địa điểm nhé."), options=options,
                                slot="area" if phase == "place_area" else "name" if not options else None,
                                slot_effect=action("refine") if phase == "specific" and not options else None,
                                destination=destination,
                                yes_effect=(keep if positive else change) if has_boolean else None,
                                no_effect=(change if positive else keep) if has_boolean else None)
    if status == "selecting" and candidates:
        options = [QuestionOption(id=p["handle"], label=p.get("spoken_label", p["name"]), role="place",
                                  effect=action("select", place_handle=p["handle"])) for p in candidates]
        return QuestionProposal(purpose="confirm_proposal" if len(options) == 1 else "select_entity",
                                owner=prefix, target_id=target, revision=revision,
                                kind="accept_place" if len(options) == 1 else "choose_place",
                                text="Đúng chỗ này chứ?" if len(options) == 1 else "Bạn chọn địa điểm nào?",
                                options=options, yes_effect=options[0].effect if len(options) == 1 else None,
                                no_effect=action("refine", reject_candidates=True))
    legacy_preview = status == "preview" and getattr(owner, "selected", None) and getattr(owner, "route", None)
    if offer and offer.get("status", "pending") == "pending" or legacy_preview:
        # Foreground questions owned by another step take precedence.
        if step and step.status not in {"completed", "cancelled"}:
            return None
        return QuestionProposal(purpose="approve_execution", owner=prefix, target_id=target, revision=revision,
                                kind="start_plan", text=(offer or {}).get("question", "Bạn xác nhận bắt đầu nhé."),
                                yes_effect=action("start", **({"route_revision": revision} if revision else {})),
                                no_effect=action("decline", **({"route_revision": revision} if revision else {})))
    if step and step.status == "waiting_slot":
        result = step.result or {}
        needs_origin = result.get("error_code") == "origin_required" or any(
            e.get("type") in {"navigation.locate", "trip.locate"} for e in result.get("ui_events", [])
        )
        return QuestionProposal(purpose="collect_slot", owner=prefix, target_id=target, revision=revision,
                                kind="provide_destination", text=(step.result or {}).get("message", "Bạn muốn đến địa điểm nào?"),
                                slot="origin" if needs_origin else "name", slot_effect=step.draft,
                                destination=getattr(owner, "destination", None))
    return None


def current_question(session, proposal=None):
    source = propose_question(session)
    if source is None:
        return None
    task = session.active
    index = task.current_index() if task else None
    step = task.steps[index] if index is not None else None
    source_binding = fingerprint({"owner": source.owner, "target": source.target_id, "revision": source.revision,
                           "kind": source.kind, "options": [o.id for o in source.options], "slot": source.slot,
                           "destination": source.destination.model_dump(mode="json") if source.destination else None,
                           "task": task.id if step else None, "step": step.id if step else None,
                           "task_revision": task.revision if step else None,
                           "confirmation": step.confirmation if step else None})
    old = session.dialogue_question
    if old and old.source_binding == source_binding and proposal is None:
        return old
    proposal = proposal or source
    if (proposal.owner, proposal.target_id, proposal.revision) != (source.owner, source.target_id, source.revision):
        raise ValueError("QuestionProposal does not belong to the current target")
    meaning = proposal.model_dump(mode="json", exclude={"text"})
    binding = fingerprint({"source": source_binding, "meaning": meaning})
    expires = task.expires_at if step else datetime.now(UTC) + timedelta(minutes=3)
    if step and step.confirmation and step.confirmation.get("expires_at"):
        expires = min(expires, datetime.fromisoformat(step.confirmation["expires_at"]))
    return PendingQuestion(**proposal.model_dump(), id="question:" + binding[:32], binding=binding,
                           source_binding=source_binding,
                           task_id=task.id if step else None, step_id=step.id if step else None, expires_at=expires)


def legacy_view(question):
    return {"id": question.id, "kind": question.kind, "purpose": question.purpose, "owner": question.owner,
            "target_id": question.target_id, "revision": question.revision, "question": question.text,
            "expires_at": question.expires_at.isoformat(),
            "slot": question.slot,
            "destination": question.destination.model_dump(mode="json") if question.destination else None,
            "accepts_yes": bool(question.yes_effect or question.yes_act),
            "accepts_no": bool(question.no_effect or question.no_act),
            "choices": [{"handle": o.id, "label": o.label, "kind": o.role} for o in question.options]}
