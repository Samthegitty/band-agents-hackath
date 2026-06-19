"""
web/backend/server.py
======================
VyalaArchon web backend.

Lets a person submit a GitHub repo URL from a browser instead of
typing directly into Band. Under the hood it:

  1. Creates a Band chat room (as the orchestrator agent)
  2. Adds qs-assessment, qs-curator, qs-studyplan to that room
  3. Posts "@qs-orchestrator scan <repo_url>" — same trigger as manual use
  4. Polls the Band room for new messages and forwards them to the
     browser over WebSocket as they arrive

Uses the official `thenvoi_rest` client (bundled with band-sdk) rather
than hand-rolled HTTP calls, so auth headers and request shapes match
exactly what Band expects.

The agents themselves are completely unchanged — this is a thin
REST/WebSocket bridge in front of the same Band room your agents
already listen to when you run `python main.py`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

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

# Production Band API base — the SDK's only built-in preset points to a
# dev environment, so we pass this explicitly.
BAND_BASE_URL = os.environ.get("BAND_BASE_URL", "https://app.band.ai")


def _load_all_agent_creds() -> dict[str, dict[str, str]]:
    """
    Loads agent_id + api_key for every agent, from env vars first
    (Railway/production), falling back to agent_config.yaml locally.
    Returns {role_key: {"agent_id": ..., "api_key": ...}}
    """
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

# Sub-agents to recruit into every new room: (role_key, handle)
SUB_AGENTS = [
    ("qs_assessment", "qs-assessment"),
    ("qs_curator", "qs-curator"),
    ("qs_studyplan", "qs-studyplan"),
]

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


@app.post("/api/scan")
async def start_scan(req: ScanRequest):
    """
    Creates a fresh Band room, recruits all agents, and kicks off
    a scan. Returns the room_id so the frontend can open a WebSocket
    to /ws/{room_id} and watch the pipeline run live.
    """
    client = _make_client()

    # 1. Create a new chat room (ChatRoomRequest only accepts task_id —
    #    Band rooms don't have a separate "title" field at creation time)
    room_resp = await client.agent_api_chats.create_agent_chat(
        chat=ChatRoomRequest(task_id=None)
    )
    room_id = room_resp.data.id
    log.info(f"Created room {room_id} for {req.repo_url}")

    # 2. Add the other three agents to the room, by their Band agent UUID
    for role_key, handle in SUB_AGENTS:
        agent_id = _AGENT_CREDS[role_key]["agent_id"]
        try:
            await client.agent_api_participants.add_agent_chat_participant(
                chat_id=room_id,
                participant=ParticipantRequest(participant_id=agent_id, role="member"),
            )
        except Exception as e:
            log.warning(f"Could not add {handle} ({agent_id}): {e}")

    # 3. Post the kickoff message. We're posting AS the orchestrator, so
    #    we can't mention it (Band rejects "cannot_mention_self"). Band
    #    also requires at least one mention per message, so we mention
    #    the assessment agent directly instead — same effect, since the
    #    orchestrator (sender) will route to assessment next either way.
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
    """
    Polls the Band room for new messages and forwards each one to
    the connected browser as JSON. Closes when the client disconnects.
    """
    await websocket.accept()
    seen_ids: set[str] = set()
    client = _make_client()

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
                        await websocket.send_json({
                            "id": msg_id,
                            "author": sender_name,
                            "content": getattr(msg, "content", ""),
                            "created_at": str(_inserted_at(msg)),
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