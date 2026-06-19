"""
band_client.py
==============
Thin wrapper around the Band SDK.
ALL agent-to-agent communication flows through this module —
this is what makes QuantumShield a real Band multi-agent system,
not just a Python function chain.

Every agent uses:
  - post_message()   → publish structured context to the Band room
  - get_messages()   → read what other agents have posted
  - post_task()      → orchestrator assigns work to a specific agent
  - claim_task()     → agent picks up its assigned task
"""

from __future__ import annotations

import os
import json
import logging
import time
from typing import Optional
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("quantumshield.band")

BAND_API_KEY = os.environ["BAND_API_KEY"]
BAND_ROOM_ID = os.environ["BAND_ROOM_ID"]

# Band SDK import — falls back to REST if SDK not installed
try:
    from band import BandClient as _BandSDK
    _USE_SDK = True
    log.info("Band SDK loaded")
except ImportError:
    _USE_SDK = False
    log.info("Band SDK not found — using REST fallback")

BAND_BASE_URL = "https://api.band.ai/v1"


class BandClient:
    """
    Wraps the Band SDK (or REST API) so every agent
    communicates through a shared Band room.
    """

    def __init__(self):
        self.api_key = BAND_API_KEY
        self.room_id = BAND_ROOM_ID
        self._headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if _USE_SDK:
            self._sdk = _BandSDK(api_key=self.api_key)
        else:
            import requests
            self._session = requests.Session()
            self._session.headers.update(self._headers)

    # ── Core primitives ────────────────────────────────────────────────────

    def post_message(self, agent_name: str, message_type: str, payload: dict) -> dict:
        """
        Post a structured message to the Band room.
        Every piece of inter-agent context flows through here.

        message_type conventions:
          TASK_ASSIGNED   — orchestrator → specific agent
          SCAN_COMPLETE   — assessment agent result
          LEARNING_PATH   — curator result
          STUDY_PLAN      — study plan result
          ERROR           — any agent error
        """
        body = {
            "room_id": self.room_id,
            "author": agent_name,
            "type": message_type,
            "payload": payload,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        log.info(f"[Band] {agent_name} → {message_type}")

        if _USE_SDK:
            return self._sdk.rooms.post_message(
                room_id=self.room_id,
                content=json.dumps(body),
                metadata={"type": message_type, "agent": agent_name},
            )
        else:
            import requests
            resp = requests.post(
                f"{BAND_BASE_URL}/rooms/{self.room_id}/messages",
                headers=self._headers,
                json=body,
                timeout=15,
            )
            if resp.status_code not in (200, 201):
                log.warning(f"Band post failed: {resp.status_code} {resp.text[:200]}")
                # Return the body anyway so local dev continues
                return body
            return resp.json()

    def get_messages(
        self,
        message_type: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        """
        Fetch recent messages from the Band room.
        Agents call this to read results from upstream agents.
        """
        if _USE_SDK:
            msgs = self._sdk.rooms.list_messages(
                room_id=self.room_id, limit=limit
            )
            raw = [m.content for m in msgs]
        else:
            import requests
            resp = requests.get(
                f"{BAND_BASE_URL}/rooms/{self.room_id}/messages",
                headers=self._headers,
                params={"limit": limit},
                timeout=15,
            )
            if resp.status_code != 200:
                log.warning(f"Band fetch failed: {resp.status_code}")
                return []
            raw = resp.json().get("messages", [])

        # Parse JSON payloads
        parsed = []
        for m in raw:
            if isinstance(m, str):
                try:
                    m = json.loads(m)
                except Exception:
                    continue
            parsed.append(m)

        # Filter by type if requested
        if message_type:
            parsed = [m for m in parsed if m.get("type") == message_type]

        return parsed

    def post_task(self, target_agent: str, task_type: str, data: dict) -> dict:
        """
        Orchestrator uses this to assign a task to a specific agent.
        Posts a TASK_ASSIGNED message that only the target agent should act on.
        """
        return self.post_message(
            agent_name="orchestrator",
            message_type="TASK_ASSIGNED",
            payload={
                "target_agent": target_agent,
                "task_type": task_type,
                "data": data,
            },
        )

    def claim_task(self, agent_name: str, task_type: str) -> Optional[dict]:
        """
        Agent calls this to pick up its assigned task from the Band room.
        Returns the task data if found, None otherwise.
        """
        messages = self.get_messages(message_type="TASK_ASSIGNED", limit=50)
        for msg in messages:
            p = msg.get("payload", {})
            if (
                p.get("target_agent") == agent_name
                and p.get("task_type") == task_type
            ):
                return p.get("data", {})
        return None

    def wait_for_message(
        self,
        message_type: str,
        timeout: int = 120,
        poll_interval: int = 3,
    ) -> Optional[dict]:
        """
        Poll Band room until a message of the given type appears.
        Used by agents that need to wait for upstream results.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            msgs = self.get_messages(message_type=message_type, limit=10)
            if msgs:
                return msgs[0]
            log.info(f"[Band] Waiting for {message_type}...")
            time.sleep(poll_interval)
        log.warning(f"[Band] Timeout waiting for {message_type}")
        return None


# Module-level singleton — all agents share one client
_client: Optional[BandClient] = None


def get_band_client() -> BandClient:
    global _client
    if _client is None:
        _client = BandClient()
    return _client