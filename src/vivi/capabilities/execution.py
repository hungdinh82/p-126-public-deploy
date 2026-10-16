"""External effects behind validated capability contracts."""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict

from src.vivi.agents.contracts import ActionProposal
from src.vivi.memory.sqlite import MemoryValue
from src.vivi.rag.generator import ExtractiveHandbookGenerator, normalize_answer, validate_grounding
from src.vivi.turn_control import current_control


class CapabilityRuntime:
    def __init__(self, services, ledger=None, generators=None, budgets=None, breaker_seconds=30,
                 provider_chains=None, chain_provider=None):
        self.services, self.ledger = services, ledger
        self.generators = generators or {"rules" if services.generation_provider == "unknown" else services.generation_provider: services.generator}
        self.provider_chains = provider_chains or {}
        self.budgets = budgets or {}
        self.generation_breakers = {}
        self.generation_failures = {}
        self.breaker_seconds = breaker_seconds
        self.chain_provider = chain_provider

    async def vehicle(self, event, step, arguments, command_id):
        gateway = self.services.action_gateway
        fields = arguments.pop("query", None) if step.draft.capability == "vehicle.get_status" else None
        proposal = ActionProposal(intent=step.draft.capability, arguments=arguments)
        # Reconcile a checkpoint written before an effect. The ledger result is
        # authoritative even when a consumed grant cannot be resolved again.
        previous = await asyncio.to_thread(self.ledger.command_result, event.session_id, command_id) if self.ledger else None
        if previous:
            if previous["fingerprint"] != self.ledger.action_key(proposal):
                return {"status": "unknown", "message": "Nội dung lệnh đã thay đổi sau gián đoạn."}
            result = previous["result"]
            verified = result["executed"] and result["verified"]
            return {"status": "completed" if verified else "unknown", "message": result["message"],
                    "vehicle_state": result["vehicle_state"], "action": proposal.model_dump(mode="json"),
                    "execution": {"allowed": True, **{k: v for k, v in result.items() if k != "vehicle_state"}}}
        if step.status in {"approved", "running"} and step.confirmation:
            bound, decision = await asyncio.to_thread(gateway.resolve_confirmation, event.session_id,
                command_id, step.confirmation["confirmation_id"], "approve")
            if bound is None or bound.model_dump() != proposal.model_dump():
                return {"status": "blocked", "message": decision.message or "Nội dung xác nhận không khớp thao tác."}
        else:
            decision = await asyncio.to_thread(gateway.check, event.session_id, proposal, turn_id=command_id)
        execution = {"allowed": decision.allowed, "executed": False, "verified": False,
                     "risk_class": decision.risk_class, "message": decision.message}
        if not decision.allowed:
            return {"status": "waiting_confirmation" if decision.confirmation else "blocked",
                    "message": decision.message, "confirmation": decision.confirmation,
                    "action": proposal.model_dump(mode="json"), "execution": execution}
        result = await asyncio.to_thread(gateway.execute, event.session_id, command_id, proposal)
        if proposal.intent == "vehicle.get_status" and result.verified:
            result.message = self.status_message(result.vehicle_state, fields)
        execution.update({k: v for k, v in asdict(result).items() if k != "vehicle_state"})
        return {"status": "completed" if result.executed and result.verified else "unknown",
                "message": result.message, "vehicle_state": result.vehicle_state,
                "action": proposal.model_dump(mode="json"), "execution": execution}

    async def memory(self, event, step, arguments, command_id):
        memory, profile = self.services.memory, self.services.memory_profile_id
        if memory is None:
            return {"status": "blocked", "message": "Bộ nhớ dài hạn đang tắt hoặc chưa sẵn sàng."}
        name = step.draft.capability
        grants = self.services.action_gateway.confirmations
        binding = {"command_id": command_id, "profile": profile, "capability": name, "arguments": arguments}
        if name == "memory.reset" and not (step.status in {"approved", "running"} and step.confirmation):
            grant = await asyncio.to_thread(grants.create_generic, event.session_id, binding, "Xoá toàn bộ thông tin cá nhân đã nhớ?")
            return {"status": "waiting_confirmation", "message": grant["preview"], "confirmation": grant}
        if name != "memory.recall":
            cached = await asyncio.to_thread(self.ledger.command_result, event.session_id, command_id) if self.ledger else None
            if cached:
                return cached["result"]
            if name == "memory.reset" and (not step.confirmation or not await asyncio.to_thread(grants.consume_generic, event.session_id, step.confirmation["confirmation_id"], binding)):
                return {"status": "blocked", "message": "Xác nhận xoá bộ nhớ không hợp lệ hoặc đã hết hạn."}
            control = current_control.get()
            if control:
                control.allow_dispatch()
            if self.ledger:
                await asyncio.to_thread(self.ledger.reserve_command, event.session_id, command_id,
                    {"intent": name, "arguments": arguments})
        if name == "memory.remember":
            key = arguments["memory_key"]
            kind = "command" if key.startswith("command.") else "note" if key.startswith("note.") else "fact"
            item = MemoryValue(key=key, kind=kind, value=arguments["memory_value"], label=key.split(".", 1)[-1].replace("-", " "))
            await asyncio.to_thread(memory.remember, profile, item, session_id=event.session_id, turn_id=command_id)
            message = f"Mình đã nhớ: {item.label}: {item.value}."
        elif name == "memory.forget":
            removed = await asyncio.to_thread(memory.forget, profile, arguments["memory_key"])
            message = "Mình đã quên thông tin đó." if removed else "Mình chưa lưu thông tin đó."
        elif name == "memory.reset":
            await asyncio.to_thread(memory.reset, profile)
            message = "Mình đã xoá thông tin đã ghi nhớ về bạn."
        else:
            items = await asyncio.to_thread(memory.list, profile)
            key = arguments.get("memory_key")
            items = [item for item in items if item["kind"] == "preference"] if key == "preferences" else [item for item in items if item["key"] == key] if key else items
            message = "Mình nhớ: " + "; ".join(f"{item['label']}: {item['value']}" for item in items) + "." if items else "Mình chưa ghi nhớ thông tin đó về bạn."
        result = {"status": "completed", "message": message, "verified": True}
        if self.ledger and name != "memory.recall":
            await asyncio.to_thread(self.ledger.finish_command, event.session_id, command_id, result)
        return result

    async def handbook(self, event, arguments, effective_provider, history):
        query = arguments["query"] + (" " + arguments["charging_mode"] if arguments.get("charging_mode") else "")
        model = arguments.get("requested_vehicle_model") or event.vehicle_model
        year = arguments.get("requested_model_year") or event.model_year
        chunks = await asyncio.to_thread(self.services.retriever.retrieve, query, model, year, event.locale)
        # Metadata is an explicit boundary. No answer strings or lexical scope
        # heuristics decide whether a model is allowed to answer.
        chunks = [chunk for chunk in chunks if chunk.vehicle_model == model
                  and chunk.model_year == year and chunk.locale == event.locale]
        if not chunks:
            return {"status": "blocked", "message": "Mình chưa có bằng chứng phù hợp phiên bản xe trong cẩm nang.",
                    "grounding_status": "insufficient_evidence", "citations": []}
        attempts = []
        chain = [effective_provider, "local", "rules"] if effective_provider not in {"local", "rules"} else [effective_provider, "rules"]
        if self.chain_provider is not None:
            chain = [*self.chain_provider(effective_provider), "rules"]
        elif effective_provider in self.provider_chains:
            chain = list(dict.fromkeys([*self.provider_chains[effective_provider], "rules"]))
        answer = None
        served = "rules"
        for provider in dict.fromkeys(chain):
            generator = self.generators.get(provider) or (ExtractiveHandbookGenerator() if provider == "rules" else None)
            if generator is None:
                continue
            client = getattr(generator, "client", None)
            model_trace = {
                "requested_model": getattr(client, "model", None),
                "served_model": None,
                "location": getattr(client, "execution_location", None)
                or ("cloud" if provider in {"google", "openai", "openrouter"} else "laptop"),
            }
            if time.monotonic() < self.generation_breakers.get(provider, 0):
                attempts.append({"provider": provider, "outcome": "circuit_open", **model_trace})
                continue
            try:
                async with asyncio.timeout(self.budgets.get(provider, 10 if provider != "local" else 12)):
                    answer = await generator.agenerate(query, chunks, history) if hasattr(generator, "agenerate") else await asyncio.to_thread(generator.generate, query, chunks, history)
                served = provider
                confirmed = getattr(client, "served_model", None)
                attempts.append({"provider": provider, "outcome": "served", **model_trace,
                                 "served_model": confirmed if isinstance(confirmed, str) and confirmed else None})
                self.generation_failures[provider] = 0
                self.generation_breakers[provider] = 0
                break
            except Exception as exc:
                failures = self.generation_failures.get(provider, 0) + 1
                self.generation_failures[provider] = failures
                cooldown = min(300, self.breaker_seconds * (2 ** (failures - 1)))
                self.generation_breakers[provider] = time.monotonic() + cooldown
                attempts.append({"provider": provider, "outcome": "fallback", "reason": type(exc).__name__,
                                 **model_trace})
        answer = normalize_answer(answer, chunks)
        grounded, reason = validate_grounding(answer, chunks)
        # Citations come from local metadata, never model-supplied URLs/titles.
        cited = {c.source_id for c in answer.citations}
        citations = [{"source_id": c.source_id, "section_path": c.section_path, "source_url": c.source_url} for c in chunks if c.source_id in cited] if grounded else []
        return {"status": "completed" if grounded else "blocked", "message": answer.answer if grounded else reason,
                "grounding_status": "supported" if grounded else "insufficient_evidence", "citations": citations,
                "generation_provider": served, "generation_attempts": attempts}

    @staticmethod
    def status_message(state, fields=None):
        selected = fields.split(",") if fields else ["summary"]
        parts = []
        if "summary" in selected:
            selected = ["battery", "temperature", "body"]
        if "temperature" in selected:
            parts.append(f"Điều hoà đang ở {state['temperature_celsius']:g} độ.")
        if "battery" in selected:
            parts.append(f"Pin còn {state['battery_percent']} phần trăm.")
        if "diagnostics" in selected:
            parts.append("Mình chưa đọc được mã lỗi của xe; bạn kiểm tra đèn cảnh báo và cẩm nang nhé.")
        if "range_time" in selected:
            parts.append("Mình chưa có số giờ di chuyển còn lại.")
        if "range" in selected or "range_time" in selected:
            parts.append(f"Quãng đường dự kiến còn {state['range_km']} km.")
        if "tires" in selected:
            parts.append("Áp suất lốp: " + "; ".join(f"{key}: {value:g} kPa" for key, value in state['tire_pressures_kpa'].items()) + ".")
        if "body" in selected:
            parts.append("Nắp capo đang mở." if state['hood_open'] else "Nắp capo đang đóng.")
            parts.append("Cốp sau đang mở." if state['trunk_open'] else "Cốp sau đang đóng.")
            parts.append("Các cửa: " + "; ".join(f"{key}: {'mở' if door['open'] else 'đóng'}, {'khóa' if door['locked'] else 'mở khóa'}" for key, door in state['door_states'].items()) + ".")
        return " ".join(parts)
