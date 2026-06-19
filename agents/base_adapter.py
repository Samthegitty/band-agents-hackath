"""
agents/base_adapter.py — VyalaArchon Band Adapter
Routes Band messages through OpenRouter (OpenAI-compatible).
Strips reasoning-model "thinking" leakage before posting to Band.
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
    """
    Some reasoning models (DeepSeek R1 etc.) leak chain-of-thought into
    message.content instead of a separate reasoning field. Strip it.
    """
    if not text:
        return text

    # 1. Strip explicit <think>...</think> blocks
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)

    # 2. If the text contains a clear "final answer" marker, take everything after it
    markers = [
        r"(?:^|\n)(?:final answer|final response|answer|response)\s*:?\s*\n",
    ]
    for m in markers:
        parts = re.split(m, text, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2:
            text = parts[1]
            break

    # 3. Heuristic: reasoning paragraphs often start with "We need to",
    # "Let's", "I should", "Thus we need". If the text STARTS this way
    # and there's a clear emoji/checkmark/structured section later,
    # cut everything before that section.
    reasoning_starts = re.match(
        r"^(we need to|let'?s|i should|thus we|note:|the user)",
        text.strip(), re.IGNORECASE,
    )
    if reasoning_starts:
        # Look for the first "real" content marker
        content_marker = re.search(
            r"(✅|📚|🗓️|^\*\*|\n#{1,3}\s)", text, re.MULTILINE
        )
        if content_marker:
            text = text[content_marker.start():]

    return text.strip()


class AimlAdapter(SimpleAdapter):
    SUPPORTED_EMIT = frozenset({Emit.EXECUTION})

    def __init__(self, system_prompt: str, tools: list | None = None):
        super().__init__()
        self.system_prompt = system_prompt
        self._custom_tools: list[BaseTool] = tools or []
        self._custom_tool_map = {t.name: t for t in self._custom_tools}
        self._history: dict[str, list[dict]] = {}

    def _custom_tool_schemas(self) -> list[dict]:
        schemas = []
        for t in self._custom_tools:
            schema = (
                t.args_schema.schema()
                if t.args_schema
                else {"type": "object", "properties": {}}
            )
            schemas.append({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": schema,
                },
            })
        return schemas

    async def _run_custom_tool(self, name: str, args: dict) -> str:
        tool = self._custom_tool_map.get(name)
        if not tool:
            return f"Unknown tool: {name}"
        try:
            result = tool.invoke(args)
            return str(result)
        except Exception as e:
            log.error(f"Tool {name} failed: {e}")
            return f"Tool error: {e}"

    async def _get_all_handles(self, tools) -> list[str]:
        """Helper to fetch all available participant handles."""
        try:
            participants = tools.participants
            if not participants:
                fresh = await tools.get_participants()
                participants = fresh if isinstance(fresh, list) else []

            def _handle_of(p) -> str | None:
                if isinstance(p, dict):
                    return p.get("handle")
                return getattr(p, "handle", None)

            return [h for h in (_handle_of(p) for p in participants) if h]
        except Exception as e:
            log.warning(f"Could not resolve participants: {e}")
            return []

    async def _send_with_fallback(self, tools, content: str, mentions: list[str]) -> None:
        """
        Sends a message with the given mentions. If Band rejects it for
        cannot_mention_self (the mention turned out to be the sender itself),
        retry by mentioning a different available participant. Band strictly
        requires at least one mention, so we cannot retry with an empty list.
        """
        try:
            await tools.send_message(content, mentions=mentions)
        except Exception as e:
            err_str = str(e)
            if "cannot_mention_self" in err_str:
                log.warning("Hit cannot_mention_self — trying other participants")
                
                all_handles = await self._get_all_handles(tools)
                
                # Try each handle that wasn't in the original failing mentions
                for handle in all_handles:
                    if handle not in mentions:
                        try:
                            log.info(f"Retrying send with mention: {handle}")
                            await tools.send_message(content, mentions=[handle])
                            return  # Success!
                        except Exception as e2:
                            if "cannot_mention_self" in str(e2):
                                log.warning(f"Mention {handle} is also self, trying next...")
                                continue
                            else:
                                log.error(f"Retry with {handle} failed with different error: {e2}")
                                raise
                
                log.error("All available participants resulted in cannot_mention_self. Cannot send message.")
                raise
            else:
                raise

    async def _get_mentions(self, tools) -> list[str]:
        """
        Band requires at least one mention per message. Prefer mentioning
        a human participant (handle with no "/"). If no human is in the
        room, fall back to mentioning any agent actually present.
        """
        handles = await self._get_all_handles(tools)
        humans = [h for h in handles if "/" not in str(h)]
        if humans:
            return humans
        return handles[:1] if handles else []

    async def _llm_call(self, messages: list[dict], all_tools: list[dict]) -> Any:
        """Call LLM with retry on None/empty response."""
        for attempt in range(3):
            try:
                kwargs: dict[str, Any] = {
                    "model": AIML_MODEL,
                    "messages": messages,
                    "max_tokens": 1000,
                }
                if all_tools:
                    kwargs["tools"] = all_tools
                    kwargs["tool_choice"] = "auto"

                resp = await _client.chat.completions.create(**kwargs)

                if resp is None or not resp.choices:
                    log.warning(f"LLM returned empty response (attempt {attempt+1})")
                    if attempt == 2:
                        kwargs.pop("tools", None)
                        kwargs.pop("tool_choice", None)
                        resp = await _client.chat.completions.create(**kwargs)
                        if resp and resp.choices:
                            return resp.choices[0]
                    continue

                return resp.choices[0]

            except Exception as e:
                log.warning(f"LLM call failed (attempt {attempt+1}): {e}")
                if attempt == 2:
                    raise

        return None

    async def on_message(
        self,
        msg,
        tools,
        history,
        participants_msg,
        contacts_msg,
        *,
        is_session_bootstrap: bool,
        room_id: str,
    ) -> None:
        try:
            await self._handle_message(
                msg, tools, history, participants_msg, contacts_msg,
                is_session_bootstrap=is_session_bootstrap, room_id=room_id,
            )
        except Exception as e:
            # Never die silently — log loudly and try to notify the room
            log.error(f"[{room_id[:8]}] UNHANDLED ERROR in on_message: {e}", exc_info=True)
            try:
                mentions = await self._get_mentions(tools)
                await self._send_with_fallback(
                    tools,
                    f"⚠️ I hit an internal error processing this request: {type(e).__name__}: {e}",
                    mentions,
                )
            except Exception as e2:
                log.error(f"[{room_id[:8]}] Could not even send error message: {e2}")

    async def _handle_message(
        self,
        msg,
        tools,
        history,
        participants_msg,
        contacts_msg,
        *,
        is_session_bootstrap: bool,
        room_id: str,
    ) -> None:
        room_history = self._history.setdefault(room_id, [])

        system = self.system_prompt
        system += (
            "\n\nIMPORTANT: Respond with ONLY your final message content. "
            "Do not narrate your reasoning, do not explain your plan, "
            "do not write phrases like 'we need to' or 'let's' — "
            "just output the final message directly."
        )
        if participants_msg:
            system += f"\n\nRoom participants:\n{participants_msg}"

        room_history.append({"role": "user", "content": msg.content})

        platform_schemas = tools.get_openai_tool_schemas(include_contacts=True)
        custom_schemas   = self._custom_tool_schemas()
        all_tools = platform_schemas + custom_schemas

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            *room_history,
        ]

        mentions = await self._get_mentions(tools)

        max_iterations = 8
        iteration = 0
        while iteration < max_iterations:
            iteration += 1

            choice = await self._llm_call(messages, all_tools)

            if choice is None:
                fallback = "I encountered an issue processing your request. Please try again."
                # Use the fallback wrapper here too to avoid crashes
                await self._send_with_fallback(tools, fallback, mentions)
                return

            if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
                asst: dict[str, Any] = {
                    "role": "assistant",
                    "content": choice.message.content or "",
                    "tool_calls": [],
                }
                results = []

                for tc in choice.message.tool_calls:
                    fn_name = tc.function.name
                    try:
                        fn_args = json.loads(tc.function.arguments)
                    except Exception:
                        fn_args = {}
                    log.info(f"[{room_id[:8]}] Tool: {fn_name}({list(fn_args.keys())})")

                    if fn_name in self._custom_tool_map:
                        result = await self._run_custom_tool(fn_name, fn_args)
                    else:
                        try:
                            result = await tools.execute_tool_call(fn_name, fn_args)
                            result = json.dumps(result) if not isinstance(result, str) else result
                        except Exception as e:
                            result = f"Platform tool error: {e}"

                    asst["tool_calls"].append({
                        "id": tc.id, "type": "function",
                        "function": {"name": fn_name, "arguments": tc.function.arguments},
                    })
                    results.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })

                messages.append(asst)
                messages.extend(results)

            else:
                reply = (choice.message.content or "").strip()
                reply = _strip_reasoning(reply)

                # --- AGGRESSIVE BOILERPLATE FILTER ---
                # Suppress status updates that cause infinite ping-pong loops
                ignore_phrases = [
                    "standing by",
                    "waiting for",
                    "awaiting",
                    "i am the",
                    "acknowledged",
                    "pipeline active",
                    "pipeline is in motion",
                    "noted",
                    "understood",
                    "will notify you",
                    "will ping you",
                    "ready.",
                    "ready to generate",
                    "still waiting",
                    "in motion",
                    # --- NEW PHRASES TO STOP THE CURRENT LOOP ---
                    "remain silent",
                    "stay out of the way",
                    "study plan agent ready",
                    "scan results received",
                    "engineering context",
                    "target repo:",
                ]
                
                # Special case: We MUST allow "Processing complete." to pass through 
                # so the UI knows the pipeline is done.
                is_done_signal = "processing complete" in reply.lower()
                
                is_boilerplate = any(phrase in reply.lower() for phrase in ignore_phrases)
                
                if (not reply or is_boilerplate) and not is_done_signal:
                    log.info(f"[{room_id[:8]}] Suppressing boilerplate reply: {reply[:50]}...")
                    return

                if not reply:
                    reply = "Task completed successfully."

                room_history.append({"role": "assistant", "content": reply})
                await self._send_with_fallback(tools, reply, mentions)
                return

        await self._send_with_fallback(tools, "Processing complete.", mentions)