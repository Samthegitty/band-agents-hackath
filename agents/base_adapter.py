"""
agents/base_adapter.py — VyalaArchon Band Adapter (Revamped)
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from band import Emit
from band.core import SimpleAdapter
from openai import AsyncOpenAI
from langchain_core.tools import BaseTool

log = logging.getLogger("quantumshield.adapter")

AIML_API_KEY = os.environ["AIML_API_KEY"]
AIML_MODEL   = os.environ.get("AIML_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")

_client = AsyncOpenAI(
    api_key=AIML_API_KEY,
    base_url="https://api.aimlapi.com/v1",
)

def _strip_reasoning(text: str) -> str:
    if not text: return text
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    markers = [r"(?:^|\n)(?:final answer|final response|answer|response)\s*:?\s*\n"]
    for m in markers:
        parts = re.split(m, text, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2:
            text = parts[1]
            break
    return text.strip()

class AimlAdapter(SimpleAdapter):
    SUPPORTED_EMIT = frozenset({Emit.EXECUTION})

    def __init__(self, system_prompt: str, tools: list | None = None, own_handle: str | None = None):
        super().__init__()
        self.system_prompt = system_prompt
        self._custom_tools: list[BaseTool] = tools or []
        self._custom_tool_map = {t.name: t for t in self._custom_tools}
        self._history: dict[str, list[dict]] = {}
        self.own_handle = own_handle

    def _custom_tool_schemas(self) -> list[dict]:
        schemas = []
        for t in self._custom_tools:
            schema = t.args_schema.schema() if t.args_schema else {"type": "object", "properties": {}}
            schemas.append({"type": "function", "function": {"name": t.name, "description": t.description, "parameters": schema}})
        return schemas

    async def _run_custom_tool(self, name: str, args: dict) -> str:
        tool = self._custom_tool_map.get(name)
        if not tool: return f"Unknown tool: {name}"
        try: return str(tool.invoke(args))
        except Exception as e: return f"Tool error: {e}"

    async def _get_all_handles(self, tools) -> list[str]:
        try:
            participants = tools.participants
            if not participants:
                fresh = await tools.get_participants()
                participants = fresh if isinstance(fresh, list) else []
            def _handle_of(p):
                return p.get("handle") if isinstance(p, dict) else getattr(p, "handle", None)
            return [h for h in (_handle_of(p) for p in participants) if h]
        except Exception:
            return []

    async def _send_with_fallback(self, tools, content: str, mentions: list[str]) -> None:
        # Auto-fix broken namespace mentions just in case
        content = re.sub(r'@banjarapadam62/qs-curator', '@qs-curator', content)
        content = re.sub(r'@banjarapadam62/qs-studyplan', '@qs-studyplan', content)
        content = re.sub(r'@banjarapadam62/qs-assessment', '@qs-assessment', content)
        content = re.sub(r'@banjarapadam62/qs-orchestrator', '@qs-orchestrator', content)

        try:
            await tools.send_message(content, mentions=mentions)
        except Exception as e:
            if "cannot_mention_self" in str(e):
                all_handles = await self._get_all_handles(tools)
                for handle in all_handles:
                    if handle not in mentions:
                        try:
                            await tools.send_message(content, mentions=[handle])
                            return
                        except Exception:
                            continue
            else:
                raise

    async def _get_mentions(self, tools) -> list[str]:
        handles = await self._get_all_handles(tools)
        humans = [h for h in handles if "/" not in str(h)]
        return humans if humans else (handles[:1] if handles else [])

    async def _llm_call(self, messages: list[dict], all_tools: list[dict]) -> Any:
        for attempt in range(3):
            try:
                kwargs = {"model": AIML_MODEL, "messages": messages, "max_tokens": 1000}
                if all_tools:
                    kwargs["tools"] = all_tools
                    kwargs["tool_choice"] = "auto"
                resp = await _client.chat.completions.create(**kwargs)
                if resp and resp.choices: return resp.choices[0]
            except Exception as e:
                log.warning(f"LLM call failed (attempt {attempt+1}): {e}")
                if attempt == 2: raise
        return None

    async def on_message(self, msg, tools, history, participants_msg, contacts_msg, *, is_session_bootstrap: bool, room_id: str) -> None:
        try:
            await self._handle_message(msg, tools, history, participants_msg, contacts_msg, is_session_bootstrap=is_session_bootstrap, room_id=room_id)
        except Exception as e:
            log.error(f"[{room_id[:8]}] UNHANDLED ERROR: {e}", exc_info=True)

    async def _handle_message(self, msg, tools, history, participants_msg, contacts_msg, *, is_session_bootstrap: bool, room_id: str) -> None:
        # Hard gate: Only wake up if explicitly mentioned
        if self.own_handle and not is_session_bootstrap:
            content_lower = (msg.content or "").lower()
            handle_lower = self.own_handle.lower()
            mentioned = (f"@{handle_lower}" in content_lower or handle_lower in content_lower)
            if not mentioned:
                return

        room_history = self._history.setdefault(room_id, [])
        system = self.system_prompt + "\n\nIMPORTANT: Respond with ONLY your final message content. Do not narrate your reasoning."
        if participants_msg: system += f"\n\nRoom participants:\n{participants_msg}"
        room_history.append({"role": "user", "content": msg.content})

        platform_schemas = tools.get_openai_tool_schemas(include_contacts=True)
        all_tools = platform_schemas + self._custom_tool_schemas()
        messages = [{"role": "system", "content": system}, *room_history]
        mentions = await self._get_mentions(tools)

        for _ in range(8):
            choice = await self._llm_call(messages, all_tools)
            if not choice: 
                await self._send_with_fallback(tools, "I encountered an issue.", mentions)
                return

            if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
                asst = {"role": "assistant", "content": choice.message.content or "", "tool_calls": []}
                results = []
                for tc in choice.message.tool_calls:
                    fn_name = tc.function.name
                    try: fn_args = json.loads(tc.function.arguments)
                    except: fn_args = {}
                    
                    if fn_name in self._custom_tool_map:
                        result = await self._run_custom_tool(fn_name, fn_args)
                    else:
                        try:
                            result = await tools.execute_tool_call(fn_name, fn_args)
                            result = json.dumps(result) if not isinstance(result, str) else result
                        except Exception as e:
                            result = f"Platform tool error: {e}"
                    
                    asst["tool_calls"].append({"id": tc.id, "type": "function", "function": {"name": fn_name, "arguments": tc.function.arguments}})
                    results.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                messages.append(asst)
                messages.extend(results)
            else:
                reply = _strip_reasoning((choice.message.content or "").strip())

                # --- 🚨 THE AGENT KILL SWITCH 🚨 ---
                # If the Assessment agent just output the scan report, 
                # append the completion signal so the backend closes the UI.
                if self.own_handle == "qs-assessment" and "SCAN COMPLETE" in reply and "Top findings:" in reply:
                    if "processing complete" not in reply.lower():
                        reply += "\n\nProcessing complete."

                # --- STRICT BOILERPLATE FILTER ---
                ignore_phrases = [
                    "standing by", "waiting for", "awaiting", "i am the", "acknowledged",
                    "pipeline active", "pipeline is in motion", "noted", "understood",
                    "will notify you", "will ping you", "ready.", "ready to generate",
                    "still waiting", "in motion", "remain silent", "stay out of the way",
                    "study plan agent ready", "scan results received", "engineering context",
                    "target repo:", "learning path generation in progress", "the scan is already complete",
                    "results were posted", "pipeline is now waiting", "all three agents",
                    "all agents are already", "ready when you are", "just share a repo"
                ]
                
                is_done_signal = "processing complete" in reply.lower()
                is_boilerplate = any(phrase in reply.lower() for phrase in ignore_phrases)
                
                if (not reply or is_boilerplate) and not is_done_signal:
                    log.info(f"[{room_id[:8]}] Suppressed boilerplate.")
                    return

                if not reply: reply = "Task completed successfully."
                room_history.append({"role": "assistant", "content": reply})
                await self._send_with_fallback(tools, reply, mentions)
                return

        await self._send_with_fallback(tools, "Processing complete.", mentions)