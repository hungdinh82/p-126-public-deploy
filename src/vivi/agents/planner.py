"""One tool-selection policy for deterministic and model-backed routing."""

from __future__ import annotations

import re

from src.vivi.agents.commands import CabinCommandParser
from src.vivi.agents.context import resolve_request
from src.vivi.agents.contracts import IntentDecision
from src.vivi.agents.dialogue import clarify, dialogue_decision
from src.vivi.rag.knowledge import analyze_question
from src.vivi.rag.scope import scope_rejection_reason
from src.vivi.text import normalize_text


def is_live_status_request(input_text: str) -> bool:
    """Read observations, while instructions/specifications still use the handbook."""
    text = normalize_text(input_text)
    if _polite_control_request(text):
        return False
    if re.search(
        r"\b(cach|lam sao|huong dan|tai sao|vi sao|nhu nao|the nao|"
        r"dung luong|khuyen cao|khuyen nghi|nen|tieu chuan|sdi|catl|toi da|"
        r"thi|xu ly|ra sao|can lam gi|gap vat can|khi)\b",
        text,
    ):
        return False
    if re.search(r"\b(dat|tang|giam|ha|chinh|mo|dong|bat|tat|khoa)\b", text) and not re.search(
        r"\b(dang|hien tai|bay gio|da.*chua|co.*khong)\b", text
    ):
        return False
    if "thong so" in text and not re.search(r"hien tai|bay gio|luc nay|dang", text):
        return False
    if re.search(r"(?:xe|ban|vivi).*(?:dang )?(?:gap van de|co loi|bao loi)|tinh trang loi|loi hien tai", text):
        return True
    if re.search(
        r"trang thai xe|tinh trang xe|quang duong con lai|con di duoc|"
        r"di (?:them |duoc ).*(?:bao xa|bao nhieu|km)",
        text,
    ):
        return True
    if re.search(r"\bpin\b", text) and re.search(
        r"\b(con|hien tai|bay gio|luc nay|"
        r"tinh trang|trang thai|phan tram|muc pin|kiem tra|doc)\b",
        text,
    ):
        return True
    if re.search(r"nhiet do|dieu hoa", text) and re.search(r"dang|hien tai|bay gio|bao nhieu|may do", text):
        return not re.search(r"\b(dat|tang|giam|ha|chinh)\b", text)
    if "ap suat lop" in text and re.search(r"hien tai|bay gio|dang|kiem tra|tinh trang|doc|cho biet|bao nhieu", text):
        return True
    return bool(
        re.search(r"cua|ghe|nhac|cop|capo|ca po|nap may", text) and re.search(r"dang|hien tai|da.*chua|co.*(?:dong|mo|khoa).*khong", text)
    )


def _polite_control_request(text: str) -> bool:
    if not re.search(r"\b(mo|dong|khoa|dat|tang|giam|ha|bat|tat|phat|dung)\b", text):
        return False
    if re.search(r"\b(cach|lam sao|huong dan|tai sao|vi sao)\b", text):
        return False
    return bool(
        re.search(
            r"(?:ban|vivi).{0,20}(?:co the|giup)|giup (?:minh|toi)|"
            r"^co the.{0,10}(?:mo|dong|khoa|dat|tang|giam|ha|bat|tat|phat)|"
            r"^(?:mo|dong|khoa|dat|tang|giam|ha|bat|tat|phat|dung).*duoc khong",
            text,
        )
    )


def priority_decision(input_text: str) -> IntentDecision | None:
    """Protect common tool boundaries even when an SLM routes them incorrectly."""
    from src.vivi.rag.scope import scope_rejection_reason

    text = normalize_text(input_text)
    if scope_rejection_reason(input_text) is None and re.search(
        r"khong (?:the )?(?:mo|dong|sac|khoa).*duoc|(?:mo|dong|sac|khoa).*khong duoc|"
        r"bi ket|khong hoat dong",
        text,
    ):
        return IntentDecision(route="handbook", intent="manual.search", arguments={"query": input_text})
    if not re.search(r"\b(cach|lam sao|vi sao|tai sao|sao|huong dan)\b", text) and re.search(
        r"\b(dung|khong)\s+(?:mo|dong|khoa|tang|giam|bat|tat|phat|ha)\b", text
    ):
        return IntentDecision(
            route="conversation",
            intent="conversation.respond",
            response_text="Được nhé, mình sẽ không thực hiện thao tác đó.",
        )
    if not re.search(r"\b(cach|huong dan|nhu nao|the nao|tai sao|vi sao)\b", text):
        controls = re.findall(r"\b(?:mo|dong|khoa|dat|chinh|phat|tat|suoi)\b.{0,25}?(?:cua|nhiet do|nhac|ghe)", text)
        if len(controls) > 1 and re.search(r"\bva\b|roi|dong thoi", text):
            return IntentDecision(
                route="clarify",
                intent="conversation.clarify",
                needs_clarification=True,
                clarification_question="Bạn muốn mình thực hiện thao tác nào trước?",
            )
        if controls and re.search(r"\b(neu|lat nua|ti nua|ngay mai)\b", text):
            return IntentDecision(
                route="clarify",
                intent="conversation.clarify",
                needs_clarification=True,
                clarification_question="Mình chưa hẹn giờ thao tác được. Bạn nói thao tác cần thực hiện ngay nhé.",
            )
    scope = scope_rejection_reason(input_text)
    if is_live_status_request(input_text) and (scope is None or scope.startswith("Bạn muốn tìm hiểu")):
        return IntentDecision(route="action", intent="vehicle.get_status", response_text="Mình kiểm tra nhé.")
    return None


class ToolPlanner:
    def __init__(self) -> None:
        self.commands = CabinCommandParser()

    def deterministic(self, input_text: str, history: list[dict], vehicle_state: dict | None) -> IntentDecision | None:
        request = resolve_request(input_text, history)
        query = request.text
        text = normalize_text(query)
        priority = priority_decision(input_text) or dialogue_decision(input_text)
        if priority is not None:
            return priority
        # A complaint can contain a renewed imperative. Require an explicit
        # action ending so a genuine "why didn't it open?" stays a question.
        repair = re.search(r"(?:sao|tai sao|vi sao).*khong (.+?)(?: luon)? (?:di|nhe)[.!?]*$", text)
        if repair:
            command = self.commands.parse(repair[1], vehicle_state)
            if command is not None:
                return command
        question = analyze_question(query)
        rejection = scope_rejection_reason(query)
        if rejection and not rejection.startswith("Bạn muốn tìm hiểu"):
            return IntentDecision(
                route="unsupported",
                intent="vehicle.prohibited"
                if "hệ thống an toàn hoặc điện cao áp" in rejection
                and not re.search(r"cach|lam sao|huong dan|tai sao|vi sao", text)
                else "unsupported.request",
                response_text=rejection,
            )
        knowledge = question.kind != "general" or bool(
            re.search(
                r"bao nhieu|o dau|khi nao|hoat dong|can luu y|can lam gi|can gi|ra sao|giup gi|loai nao|co nhung|khi|tai sao|vi sao|thi sao|co.*khong|thong tin|tinh nang|nhu nao|the nao",
                text,
            )
        )
        if knowledge and not _polite_control_request(text) and (question.topic is not None or rejection is None):
            return IntentDecision(route="handbook", intent="manual.search", arguments={"query": query})
        if re.search(r"\b(phanh|danh lai|vo lang|tang toc|truyen dong|tat tui khi)\b", text) and re.search(
            r"\b(dat|danh|tang|giam|tat|bat|mo|dong|phanh)\b", text
        ):
            return IntentDecision(
                route="unsupported",
                intent="vehicle.prohibited",
                response_text="Mình không thể điều khiển phanh, lái hoặc hệ thống an toàn của xe.",
            )
        command = self.commands.parse(query, vehicle_state)
        if command is not None:
            if command.route == "action" and re.search(r"\b(neu|lat nua|ti nua|ngay mai)\b", text):
                return clarify("Mình chưa tự động thực hiện theo điều kiện hay hẹn giờ được. Bạn nói thao tác cần thực hiện ngay nhé.")
            return command
        if re.search(r"\b(bat|tat|mo|dong|chinh|kich hoat|khoi dong)\b", text) and question.topic is not None:
            return IntentDecision(
                route="unsupported",
                intent="unsupported.request",
                response_text="Mình chưa điều khiển tính năng này bằng giọng nói được.",
            )
        if question.topic is not None or rejection is None:
            return IntentDecision(route="handbook", intent="manual.search", arguments={"query": query})
        return None

    def plan(self, input_text: str, history: list[dict], vehicle_state: dict | None) -> IntentDecision:
        return self.deterministic(input_text, history, vehicle_state) or clarify(
            "Bạn muốn mình làm gì hoặc giải thích tính năng nào trên xe?"
        )
