"""
Hermes Agent Bridge — wires the NousResearch Hermes Agent framework into the
E-Labs Copilot execute-stream SSE protocol.

Hermes Agent (hermes-agent/) is a full autonomous agent runtime with its own
conversation loop, tool system, memory, and skills.  This bridge:

  1. Imports AIAgent directly from the hermes-agent source tree.
  2. Initialises it against the local Ollama OpenAI-compatible endpoint.
  3. Runs run_conversation() in a thread pool so the event loop stays free.
  4. Forwards agent callbacks → SSE events the Copilot frontend understands.

SSE events emitted (matching UpdateType in orchestrator/agents/executor.py):
  thinking     — agent is reasoning / planning
  step_start   — a new tool call / step is beginning
  tool_call    — tool name + input
  tool_result  — tool output
  step_done    — step completed
  done         — final response + token stats
  error        — something went wrong
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import AsyncGenerator

logger = logging.getLogger(__name__)

# ── Hermes Agent source path ──────────────────────────────────────────────────
_HERMES_SRC = str(Path(__file__).parent.parent.parent.parent / "hermes-agent")
if not Path(_HERMES_SRC).exists():
    # Fallback: search relative to workspace root
    _candidates = [
        Path(__file__).parents[5] / "hermes-agent",
        Path("C:/Users/AI-Machine/OneDrive/Documents/AI-Machine/hermes-agent"),
    ]
    for _c in _candidates:
        if _c.exists():
            _HERMES_SRC = str(_c)
            break

if _HERMES_SRC not in sys.path:
    sys.path.insert(0, _HERMES_SRC)

# ── Lazy import of AIAgent so import errors surface clearly ──────────────────
_AIAgent = None
_import_error: str | None = None

def _ensure_agent():
    global _AIAgent, _import_error
    if _AIAgent is not None:
        return True
    if _import_error is not None:
        return False
    try:
        from run_agent import AIAgent  # noqa: PLC0415
        _AIAgent = AIAgent
        return True
    except Exception as exc:
        _import_error = str(exc)
        logger.error("hermes_bridge: failed to import AIAgent: %s", exc)
        return False


# ── Sentinel for thread → async queue ────────────────────────────────────────
_DONE = object()


def _run_hermes_sync(
    query: str,
    messages: list[dict],
    model: str,
    session_id: str,
    evt_queue: "queue.Queue[dict | object]",
) -> None:
    """
    Blocking function executed in a thread.  Constructs an AIAgent, attaches
    callback shims that push SSE-style event dicts onto evt_queue, then runs
    run_conversation().  Puts _DONE on the queue when finished.
    """
    def _put(event_type: str, **kwargs):
        evt_queue.put({"type": event_type, **kwargs})

    # ── Callbacks forwarded to the frontend ──────────────────────────────────
    step_counter = [0]

    def on_tool_start(tool_name: str, tool_input: dict | str | None = None, **_):
        step_counter[0] += 1
        _put("step_start",
             step=step_counter[0],
             tool=tool_name,
             label=f"Using {tool_name}",
             input_preview=str(tool_input)[:200] if tool_input else "")

    def on_tool_complete(tool_name: str, result: str | None = None, duration_ms: int = 0, **_):
        _put("tool_result",
             step=step_counter[0],
             tool=tool_name,
             result_preview=str(result)[:400] if result else "",
             duration_ms=duration_ms)

    def on_step(step_info: dict | str | None = None, **_):
        if isinstance(step_info, dict):
            _put("step_done", **{k: v for k, v in step_info.items() if k != "type"})
        else:
            _put("step_done", summary=str(step_info) if step_info else "Step complete")

    thinking_buffer: list[str] = []
    thinking_timer = [time.time()]

    def on_thinking(content: str | None = None, **_):
        if not content:
            return
        thinking_buffer.append(content)
        # Flush every ~300 chars or every 1.5s to keep the UI updating
        if sum(len(c) for c in thinking_buffer) >= 300 or (time.time() - thinking_timer[0]) > 1.5:
            _put("thinking", content="".join(thinking_buffer))
            thinking_buffer.clear()
            thinking_timer[0] = time.time()

    def on_status(status: str | None = None, **_):
        if status:
            _put("step_start", step=step_counter[0], label=str(status), tool="")

    # ── Build AIAgent ─────────────────────────────────────────────────────────
    try:
        agent = _AIAgent(
            base_url="http://localhost:11434/v1",
            api_key="ollama",          # Ollama ignores the key value
            model=model,
            max_iterations=30,
            quiet_mode=True,           # suppress CLI output — we stream via callbacks
            skip_memory=True,          # keep first run simple; memory can be added later
            skip_context_files=True,
            session_id=session_id or None,
            persist_session=bool(session_id),
            tool_start_callback=on_tool_start,
            tool_complete_callback=on_tool_complete,
            step_callback=on_step,
            thinking_callback=on_thinking,
            status_callback=on_status,
            # Disable heavy optional toolsets that need extra API keys
            disabled_toolsets=["image_generation", "browser", "web_search", "firecrawl"],
        )
    except Exception as exc:
        _put("error", message=f"Hermes Agent init failed: {exc}")
        evt_queue.put(_DONE)
        return

    # ── Convert Copilot message history → OpenAI format ──────────────────────
    history: list[dict] = []
    for msg in (messages or []):
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role in ("user", "assistant") and content:
            history.append({"role": role, "content": content})

    # ── Run ───────────────────────────────────────────────────────────────────
    try:
        t0 = time.time()
        result = agent.run_conversation(
            user_message=query,
            conversation_history=history if history else None,
            task_id=session_id or "hermes-copilot",
        )
        elapsed_ms = int((time.time() - t0) * 1000)

        # Flush any remaining thinking
        if thinking_buffer:
            _put("thinking", content="".join(thinking_buffer))

        final_text = result.get("final_response") or result.get("content") or ""
        usage = result.get("usage") or {}
        _put("done",
             content=final_text,
             method="hermes",
             model=model,
             total_duration_ms=elapsed_ms,
             completed_steps=step_counter[0],
             total_steps=step_counter[0],
             tokens_used=usage)
    except Exception as exc:
        logger.exception("hermes_bridge: run_conversation failed")
        _put("error", message=f"Hermes Agent error: {exc}")

    evt_queue.put(_DONE)


# ── Public async generator ────────────────────────────────────────────────────

async def execute_hermes(
    query: str,
    messages: list[dict],
    model: str,
    session_id: str = "",
) -> AsyncGenerator[dict, None]:
    """
    Async generator that yields SSE event dicts from a Hermes Agent run.

    Usage in app.py:
        async for update in hermes_bridge.execute_hermes(query, messages, model, session_id):
            yield f"event: {update['type']}\\ndata: {json.dumps(update)}\\n\\n"
    """
    if not _ensure_agent():
        yield {"type": "error", "message": f"Hermes Agent not available: {_import_error}"}
        return

    evt_queue: "queue.Queue[dict | object]" = queue.Queue()

    # Run the blocking AIAgent in a thread pool
    loop = asyncio.get_event_loop()
    thread = threading.Thread(
        target=_run_hermes_sync,
        args=(query, messages, model, session_id, evt_queue),
        daemon=True,
    )
    thread.start()

    # Drain the queue asynchronously, yielding to the event loop between items
    POLL_INTERVAL = 0.05   # 50ms poll — responsive but not CPU-intensive
    HEARTBEAT_EVERY = 10.0  # seconds between heartbeats if agent is quiet

    last_event_at = time.time()

    while True:
        try:
            item = evt_queue.get_nowait()
        except queue.Empty:
            await asyncio.sleep(POLL_INTERVAL)
            now = time.time()
            if (now - last_event_at) >= HEARTBEAT_EVERY:
                yield {"type": "heartbeat", "status": "alive", "agent": "hermes"}
                last_event_at = now
            continue

        if item is _DONE:
            break

        yield item
        last_event_at = time.time()

    thread.join(timeout=5)


# ── Health check ──────────────────────────────────────────────────────────────

def get_manifest() -> dict:
    """Return Hermes Agent availability and version info."""
    available = _ensure_agent()
    version = "unknown"
    try:
        import importlib.metadata
        version = importlib.metadata.version("hermes-agent")
    except Exception:
        try:
            toml = Path(_HERMES_SRC) / "pyproject.toml"
            if toml.exists():
                text = toml.read_text(encoding="utf-8")
                import re
                m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
                if m:
                    version = m.group(1)
        except Exception:
            pass

    return {
        "available": available,
        "version": version,
        "src_path": _HERMES_SRC,
        "import_error": _import_error,
        "ollama_endpoint": "http://localhost:11434/v1",
    }
