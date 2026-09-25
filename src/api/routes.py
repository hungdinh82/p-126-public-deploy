from functools import lru_cache

from fastapi import APIRouter, HTTPException

from src.agents.contracts import AssistantOutput
from src.agents.graph import agent, build_graph
from src.models.schemas import AssistRequest, ChatRequest, ChatResponse
from src.rag.runtime import create_services

router = APIRouter()


@lru_cache
def runtime_agent():
    # Lexical retrieval works immediately after crawl/parse and avoids making
    # the API depend on the remote embedding quota. Answer generation and
    # intent classification still use the configured Gemini adapter. The
    # cached graph also owns one in-memory simulator and confirmation store.
    return build_graph(create_services(retrieval_mode="lexical"))


@router.post("/chat", response_model=ChatResponse, deprecated=True)
async def chat(request: ChatRequest) -> ChatResponse:
    """Chat với AI agent."""
    try:
        result = await agent.ainvoke({"query": request.message})
        return ChatResponse(
            response=result.get("response", ""),
            analysis=result.get("analysis", ""),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status")
async def agent_status():
    """Kiểm tra trạng thái agent."""
    return {"status": "ready", "agent": "LangGraph Agent v1.0"}


@router.post("/assist", response_model=AssistantOutput)
async def assist(request: AssistRequest) -> AssistantOutput:
    """Run a final STT transcript through the complete assistant graph."""
    try:
        result = await runtime_agent().ainvoke(request.model_dump())
        return AssistantOutput.model_validate(result["output"])
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Assistant graph failed: {exc}") from exc
