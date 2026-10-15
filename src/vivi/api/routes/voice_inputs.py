"""Acknowledge voice input after durable storage, never after inference."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from src.vivi.api.runtime import runtime
from src.vivi.domain.models import TurnRequest
from src.vivi.speech.inbox import InboxFullError

router = APIRouter(prefix='/api/v1/voice',tags=['voice'])


def service():
    if runtime.voice is None:
        raise HTTPException(503,'Dịch vụ tiếp nhận giọng nói chưa khởi động')
    return runtime.voice


def public(job):
    return {name:job[name] for name in ('id','sequence','session_id','turn_id','status','transcript','response',
                                      'revision','response_revision','error','attempts','created_at','updated_at')}


async def accept(session_id,turn_id,context,**content):
    try:
        # Validate routing/context before acknowledging durable input. The real
        # transcript is validated again at execution; long text remains in history.
        TurnRequest(session_id=session_id,turn_id=turn_id,transcript='voice input',**context)
        job = await asyncio.to_thread(service().inbox.accept,session_id,turn_id,context,**content)
    except InboxFullError as exc:
        raise HTTPException(503,str(exc),headers={'Retry-After':'5'}) from exc
    except (ValueError,TypeError) as exc:
        raise HTTPException(409,str(exc)) from exc
    return public(job)


@router.post('/inputs',status_code=202)
async def receive_audio(audio: UploadFile=File(...),session_id: str=Form(...,min_length=1,max_length=100),
                        turn_id: str=Form(...,min_length=1,max_length=100),llm_provider: Literal['local']=Form('local'),
                        position: str=Form('null')):
    content = await audio.read(25*1024*1024+1)
    await audio.close()
    if not content or len(content)>25*1024*1024:
        raise HTTPException(400,'Audio rỗng hoặc vượt quá 25 MB')
    try:
        location=json.loads(position)
    except ValueError as exc:
        raise HTTPException(400,'Vị trí không hợp lệ') from exc
    return await accept(session_id,turn_id,{'llm_provider':llm_provider,'position':location},
                        audio=content,suffix=Path(audio.filename or 'audio.webm').suffix.lower())


class TranscriptInput(BaseModel):
    session_id: str=Field(min_length=1,max_length=100)
    turn_id: str=Field(min_length=1,max_length=100)
    transcript: str=Field(min_length=1,max_length=10000)
    llm_provider: Literal['local']='local'
    position: dict|None=None


@router.post('/transcripts',status_code=202)
async def receive_transcript(value: TranscriptInput):
    result=await accept(value.session_id,value.turn_id,{'llm_provider':value.llm_provider,'position':value.position},
                        transcript=value.transcript)
    service().transcript_received(value.session_id)
    return result


@router.get('/inputs/{identity}')
async def input_status(identity: str,session_id: str):
    job=await asyncio.to_thread(service().inbox.get,identity,session_id)
    if job is None:
        raise HTTPException(404,'Không tìm thấy lời nói đã tiếp nhận')
    return public(job)


@router.get('/inputs')
async def input_history(session_id: str):
    return {'inputs':[public(job) for job in await asyncio.to_thread(service().inbox.list,session_id)]}
