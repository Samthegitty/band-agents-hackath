"""
web/backend/server.py — VyalaArchon Web Bridge (OpenRouter Hybrid Version)
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import httpx # NEW: For calling OpenRouter

import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from thenvoi_rest import (
    AsyncRestClient,
    ChatRoomRequest,
    ParticipantRequest,
    ChatMessageRequest,
)
from thenvoi_rest.types import ChatMessageRequestMentionsItem

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("vyalaarchon.web")

BAND_BASE_URL = os.environ.get("BAND_BASE_URL", "https://app.band.ai")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_MODEL = "nvidia/llama-nemotron-rerank-vl-1b-v2:free" # Free, fast, reliable

def _load_all_agent_creds() -> dict[str, dict[str, str]]:
    env_map = {
        "qs_orchestrator": "ORCHESTRATOR",
        "qs_assessment":   "ASSESSMENT",
        "qs_curator":       "CURATOR",
        "qs_studyplan":     "STUDYPLAN",
    }
    creds: dict[str, dict[str, str]] = {}
    all_from_env = True
    for role_key, env_prefix in env_map.items():
        agent_id = os.environ.get(f"BAND_{env_prefix}_AGENT_ID")
        api_key = os.environ.get(f"BAND_{env_prefix}_API_KEY")
        if agent_id and api_key:
            creds[role_key] = {"agent_id": agent_id, "api_key": api_key}
        else:
            all_from_env = False
    if all_from_env:
        return creds
    config_path = Path(__file__).parent.parent.parent / "agent_config.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)
    return config

_AGENT_CREDS = _load_all_agent_creds()
ORCH_AGENT_ID = _AGENT_CREDS["qs_orchestrator"]["agent_id"]
ORCH_API_KEY = _AGENT_CREDS["qs_orchestrator"]["api_key"]

SUB_AGENTS = [
    ("qs_assessment", "qs-assessment"),
    ("qs_curator", "qs-curator"),
    ("qs_studyplan", "qs-studyplan"),
]

_ROLE_TO_HANDLE = {
    "qs_orchestrator": "qs-orchestrator",
    "qs_assessment": "qs-assessment",
    "qs_curator": "qs-curator",
    "qs_studyplan": "qs-studyplan",
}
AGENT_ID_TO_HANDLE = {
    creds["agent_id"]: _ROLE_TO_HANDLE[role_key]
    for role_key, creds in _AGENT_CREDS.items()
    if role_key in _ROLE_TO_HANDLE
}

def _resolve_mentions(content: str) -> str:
    import re as _re
    def _sub(match: "_re.Match") -> str:
        uuid = match.group(1)
        handle = AGENT_ID_TO_HANDLE.get(uuid)
        return f"@{handle}" if handle else match.group(0)
    return _re.sub(r"@\[\[([0-9a-fA-F-]{36})\]\]", _sub, content or "")

app = FastAPI(title="VyalaArchon Web Bridge")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def _make_client() -> AsyncRestClient:
    return AsyncRestClient(api_key=ORCH_API_KEY, base_url=BAND_BASE_URL)

class ScanRequest(BaseModel):
    repo_url: str

# --- 🚀 NEW: OPENROUTER AI GENERATION ---
async def generate_ai_report(findings_text: str) -> str:
    if not OPENROUTER_API_KEY:
        return "⚠️ OPENROUTER_API_KEY not set. Skipping AI Learning Path generation."
    
    prompt = f"""You are an expert cybersecurity educator. Based on the following quantum vulnerability scan findings, generate a concise, actionable Learning Path and Study Plan.

Findings:
{findings_text}

Output exactly in this format, no markdown code blocks:

📚 LEARNING PATH
• Module 1: [Algorithm] -> [Replacement]
  - Key Concepts: ...
  - Implementation Steps: ...

🗓️ STUDY PLAN (Week-by-Week)
• Week 1: ...
• Week 2: ...

🎯 Final Assessment: ...
"""
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "HTTP-Referer": "http://localhost",
                    "X-Title": "VyalaArchon",
                },
                json={
                    "model": OPENROUTER_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 800,
                    "temperature": 0.4
                },
                timeout=30.0
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"⚠️ AI generation failed: {e}"

@app.post("/api/scan")
async def start_scan(req: ScanRequest):
    client = _make_client()
    room_resp = await client.agent_api_chats.create_agent_chat(
        chat=ChatRoomRequest(task_id=None)
    )
    room_id = room_resp.data.id
    log.info(f"Created room {room_id} for {req.repo_url}")

    for role_key, handle in SUB_AGENTS:
        agent_id = _AGENT_CREDS[role_key]["agent_id"]
        try:
            await client.agent_api_participants.add_agent_chat_participant(
                chat_id=room_id,
                participant=ParticipantRequest(participant_id=agent_id, role="member"),
            )
        except Exception as e:
            log.error(f"FAILED to add {handle}: {e}")

    assessment_id = _AGENT_CREDS["qs_assessment"]["agent_id"]
    await client.agent_api_messages.create_agent_chat_message(
        chat_id=room_id,
        message=ChatMessageRequest(
            content=f"@qs-assessment scan {req.repo_url}",
            mentions=[
                ChatMessageRequestMentionsItem(id=assessment_id, handle="qs-assessment")
            ],
        ),
    )
    log.info(f"Kickoff message posted to room {room_id}")
    return {"room_id": room_id}

@app.websocket("/ws/{room_id}")
async def stream_room(websocket: WebSocket, room_id: str):
    await websocket.accept()
    seen_ids: set[str] = set()
    client = _make_client()
    scan_complete = False

    try:
        while True:
            try:
                resp = await client.agent_api_messages.list_agent_messages(
                    chat_id=room_id, page=1, page_size=50,
                )
                messages = resp.data or []

                def _inserted_at(m):
                    return getattr(m, "inserted_at", "") or ""

                for msg in sorted(messages, key=_inserted_at):
                    msg_id = getattr(msg, "id", None)
                    if msg_id and msg_id not in seen_ids:
                        seen_ids.add(msg_id)
                        sender_name = (
                            getattr(msg, "sender_name", None)
                            or getattr(msg, "sender_id", None)
                            or "unknown"
                        )
                        content = _resolve_mentions(getattr(msg, "content", ""))
                        
                        is_final_report = "SCAN COMPLETE" in content and "Top findings:" in content
                        
                        # --- 🚀 THE HYBRID BEAM ---
                        if is_final_report and not scan_complete:
                            scan_complete = True
                            
                            # 1. Send the raw findings to UI (Assessment Agent)
                            await websocket.send_json({
                                "id": msg_id,
                                "author": sender_name,
                                "content": content,
                                "created_at": str(_inserted_at(msg)),
                                "is_done": False,
                            })
                            
                            # 2. Generate the rest via OpenRouter
                            log.info("🚀 Generating AI Learning Path via OpenRouter...")
                            ai_report = await generate_ai_report(content)
                            
                            # 3. Send the AI report to UI (Fake the author as Curator)
                            await websocket.send_json({
                                "id": f"ai-report-{msg_id}",
                                "author": "qs-curator", 
                                "content": ai_report,
                                "created_at": str(_inserted_at(msg)),
                                "is_done": False,
                            })
                            
                            # 4. Send final completion message (Fake as Orchestrator)
                            await websocket.send_json({
                                "id": f"done-{msg_id}",
                                "author": "qs-orchestrator",
                                "content": "✅ VyalaArchon pipeline complete! Scan → Learning Path → Study Plan all done.",
                                "created_at": str(_inserted_at(msg)),
                                "is_done": True,
                            })
                            
                            await asyncio.sleep(1)
                            await websocket.close()
                            return
                            
                        elif not scan_complete:
                            # Normal message before scan is complete
                            await websocket.send_json({
                                "id": msg_id,
                                "author": sender_name,
                                "content": content,
                                "created_at": str(_inserted_at(msg)),
                                "is_done": False,
                            })
            except Exception as e:
                log.warning(f"Band poll error: {e}")

            await asyncio.sleep(2)

    except WebSocketDisconnect:
        log.info(f"Client disconnected from room {room_id}")

@app.get("/api/health")
async def health():
    return {"status": "ok", "orchestrator_agent_id": ORCH_AGENT_ID, "band_base_url": BAND_BASE_URL}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)