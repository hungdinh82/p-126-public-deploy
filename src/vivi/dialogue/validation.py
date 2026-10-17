"""Validate semantic references without interpreting the utterance again."""

from src.vivi.dialogue.contracts import SemanticTurn


def validate_semantic(value, session):
    from src.vivi.capabilities.registry import REGISTRY

    value.consent_question_id = None
    if any(a.capability == "navigation.plan" and a.arguments.purpose is None for a in value.actions[:-1]):
        return SemanticTurn(
            act="clarify",
            text="Bạn muốn tính kế hoạch rồi làm bước tiếp theo, hay chờ bắt đầu dẫn đường mới thực hiện?",
        )
    for action in value.drafts():
        if action.capability not in REGISTRY or action.capability.startswith("trip."):
            return SemanticTurn(act="unsupported", text="Mình chưa hỗ trợ thao tác đó.")
        args = action.arguments
        if action.capability.startswith("navigation."):
            owner = session.navigation_workspace.draft
            if args.route_revision is not None and (owner is None or args.route_revision != owner.revision):
                return SemanticTurn(act="clarify", text="Kế hoạch đã thay đổi. Bạn chọn lại nhé.")
            if args.plan_id and (owner is None or args.plan_id != getattr(owner, "id", None)):
                return SemanticTurn(act="clarify", text="Kế hoạch này không còn thuộc phiên hiện tại.")
        if action.capability.endswith(".start"):
            question = session.dialogue_question
            if not question or question.purpose != "approve_execution":
                return SemanticTurn(
                    act="clarify", text="Bạn hoàn tất địa điểm và xem tuyến trước khi xác nhận khởi hành nhé."
                )
    return value
