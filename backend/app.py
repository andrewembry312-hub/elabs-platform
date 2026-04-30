import sys
import os
import re
import time
import json
import sqlite3

import asyncio
import logging
import webbrowser
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Body, Request, Depends, Security
from fastapi.security.api_key import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# Add Conjoined (shared engine) to path so orchestrator is importable
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "Conjoined"))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from orchestrator import ceo, muscles, config
from orchestrator import tool_executor, vram_manager, comfyui_bridge
from orchestrator import agent_registry, personality_engine
from orchestrator import model_registry, hardware_selector
from orchestrator import api_key_manager, cloud_models
from orchestrator import agentic_loop
from orchestrator import file_manager
from orchestrator import model_benchmark_db
from orchestrator import multimodal_benchmark_db
from orchestrator.backends import registry as backend_registry
from orchestrator import reasoning_models
from orchestrator import industry_agents as _industry_agents
from orchestrator.agents import suggestions as suggestion_engine
from orchestrator import conversation_manager
from orchestrator import artifact_builder
from orchestrator import model_wrapper
from orchestrator import stress_test
from orchestrator import machine_engine
from orchestrator.capabilities_context import build_tool_instructions, build_smart_guidance
import openclaw_bridge
import hermes_bridge
import platform as _platform
import re as _re


# Agent-mode MACHINE handoff state (per chat session id).
# Used to auto-open THE MACHINE once on the first agent task, then reuse link-only behavior.
_agent_machine_projects: dict[str, str] = {}
_agent_machine_auto_opened: set[str] = set()


def _try_windows_bash_correction(command: str, result: dict) -> str | None:
    """If a bash command failed due to OS mismatch on Windows, return a corrected command.

    Returns None if no correction is possible (let the error stand).
    Only applies on Windows hosts.
    """
    if _platform.system() != "Windows":
        return None

    stderr = result.get("stderr", "") + result.get("stdout", "")
    cmd_lower = command.lower().strip()

    # notify-send → non-blocking PowerShell MessageBox
    if "notify-send" in cmd_lower or ("not recognized" in stderr and "notify" in stderr):
        # Extract the message text from notify-send 'text' or notify-send "text"
        match = _re.search(r"notify-send\s+['\"](.+?)['\"]", command)
        msg = match.group(1) if match else "Notification"
        return (
            'start "" powershell -WindowStyle Hidden -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; '
            f"[System.Windows.Forms.MessageBox]::Show('{msg}', 'Message')\""
        )

    # MessageBox without Add-Type → add it
    if "TypeNotFound" in stderr and "MessageBox" in stderr:
        if "Add-Type" not in command:
            fixed = command.replace(
                "[System.Windows.Forms.MessageBox]",
                "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]",
            )
            # Make non-blocking with hidden window
            fixed = _make_nonblocking_powershell(fixed)
            return fixed

    # xdg-open → Start-Process
    if "xdg-open" in cmd_lower or ("not recognized" in stderr and "xdg" in stderr):
        return command.replace("xdg-open", "Start-Process")

    # grep → Select-String
    if ("not recognized" in stderr and "grep" in stderr):
        return command.replace("grep", "Select-String")

    # ls → Get-ChildItem
    if cmd_lower.startswith("ls") and "not recognized" in stderr:
        return command.replace("ls", "Get-ChildItem", 1)

    # cat → Get-Content
    if cmd_lower.startswith("cat ") and "not recognized" in stderr:
        return command.replace("cat ", "Get-Content ", 1)

    return None


def _make_nonblocking_powershell(command: str) -> str:
    """Wrap a PowerShell command so it runs non-blocking with a hidden console window.

    Prevents: (1) blocking the bash executor waiting for user interaction,
    (2) a visible PowerShell console flashing on screen.
    """
    cmd_lower = command.lower().strip()
    # Already wrapped
    if cmd_lower.startswith("start "):
        return command
    # Add start with hidden window
    return f'start "" /B powershell -WindowStyle Hidden -NoProfile -Command "{command}"' if "powershell" not in cmd_lower else \
           f'start "" /B {command}'.replace("powershell", "powershell -WindowStyle Hidden", 1)


_BLOCKING_GUI_PATTERNS = _re.compile(
    r"MessageBox|\.Show\(|WinForms|System\.Windows\.Forms|"
    r"notification|toast|popup|alert\(",
    _re.IGNORECASE,
)


def _pre_correct_bash_command(command: str) -> str:
    """Pre-correct a bash command BEFORE execution to avoid known issues.

    This prevents: double execution (command runs, times out, auto-correction
    runs it again), and visible PowerShell console windows.

    Only applies on Windows.
    """
    if _platform.system() != "Windows":
        return command

    cmd_lower = command.lower().strip()

    # Already wrapped with start — leave it alone
    if cmd_lower.startswith("start "):
        return command

    # Blocking GUI commands → wrap with start /B + hidden window
    if _BLOCKING_GUI_PATTERNS.search(command):
        # It's a PowerShell command that will show a GUI element
        if "powershell" in cmd_lower:
            # Insert -WindowStyle Hidden if not present
            if "-windowstyle" not in cmd_lower:
                command = command.replace("powershell", "powershell -WindowStyle Hidden", 1)
            return f'start "" /B {command}'
        else:
            # Wrap the whole thing in a hidden PowerShell
            return f'start "" /B powershell -WindowStyle Hidden -NoProfile -Command "{command}"'

    # notify-send → non-blocking MessageBox (pre-correct, don't wait for failure)
    if "notify-send" in cmd_lower:
        match = _re.search(r"notify-send\s+['\"](.+?)['\"]", command)
        msg = match.group(1) if match else "Notification"
        return (
            'start "" /B powershell -WindowStyle Hidden -NoProfile -Command "'
            "Add-Type -AssemblyName System.Windows.Forms; "
            f"[System.Windows.Forms.MessageBox]::Show('{msg}', 'Message')\""
        )

    # xdg-open → Start-Process (pre-correct)
    if "xdg-open" in cmd_lower:
        return command.replace("xdg-open", "Start-Process")

    return command


def _process_tool_calls_in_response(response_text: str) -> tuple[str, list[dict]]:
    """Detect and execute <tool_call> blocks in a muscle's response.

    Returns (processed_text, tool_results) where processed_text has tool
    results appended and tool_results is a list for frontend display.

    Follows claw-code pattern: extract → execute → inject result.
    Includes Windows auto-correction: if a bash command fails with a
    recognizable OS mismatch, fix and retry once.
    """
    tool_calls = tool_executor.extract_tool_calls(response_text)
    if not tool_calls:
        return response_text, []

    tool_results = []
    processed = response_text

    for tc in tool_calls:
        tool_name = tc.get("tool", "unknown")
        params = tc.get("params", {})

        # Pre-correct bash commands BEFORE execution to prevent blocking/double-run
        if tool_name == "bash" and params.get("command"):
            original_cmd = params["command"]
            corrected_cmd = _pre_correct_bash_command(original_cmd)
            if corrected_cmd != original_cmd:
                params = {**params, "command": corrected_cmd}
                tc = {**tc, "params": params}

        # Execute the tool
        result = tool_executor.execute_tool(tc)
        inner = result.get("result", {})

        # Post-execution auto-correction for errors we couldn't predict
        if tool_name == "bash" and (inner.get("exit_code", 0) != 0 or inner.get("timed_out", False)):
            corrected = _try_windows_bash_correction(params.get("command", ""), inner)
            if corrected:
                corrected_tc = {"tool": "bash", "params": {"command": corrected, "timeout": params.get("timeout", 30)}}
                result = tool_executor.execute_tool(corrected_tc)
                inner = result.get("result", {})
                params = corrected_tc["params"]  # update for display

        # Build a human-readable result summary
        if tool_name == "bash":
            stdout = inner.get("stdout", "").strip()
            stderr = inner.get("stderr", "").strip()
            exit_code = inner.get("exit_code", -1)
            blocked = inner.get("blocked", False)
            if blocked:
                summary = f"⛔ Blocked: {stderr[:200]}"
            elif exit_code == 0:
                summary = stdout[:2000] if stdout else "(no output)"
            else:
                summary = f"Exit code {exit_code}: {stderr[:500] or stdout[:500]}"
        elif tool_name == "read_file":
            content = inner.get("content", inner.get("text", ""))
            summary = content[:2000] if content else str(inner)[:500]
        elif tool_name in ("file_write", "file_edit"):
            summary = inner.get("status", inner.get("message", str(inner)[:200]))
        else:
            summary = str(inner)[:500]

        tool_results.append({
            "tool": tool_name,
            "params": {k: (v[:100] + "..." if isinstance(v, str) and len(v) > 100 else v)
                       for k, v in params.items()},
            "result_summary": summary,
            "success": inner.get("exit_code", 0) == 0 if tool_name == "bash"
                       else "error" not in inner and not inner.get("blocked"),
        })

        # Append result to the response text so the user sees it
        processed += f"\n\n---\n**[Tool Result: {tool_name}]**\n```\n{summary}\n```"

    return processed, tool_results

app = FastAPI(title="Local AI Orchestrator WebUI")
log = logging.getLogger("app")
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

# ── CORS ─────────────────────────────────────────────────────────────────────
from fastapi.middleware.cors import CORSMiddleware as _CORSMiddleware

_CORS_BASE_ORIGINS = [
    "http://127.0.0.1:8001",
    "http://127.0.0.1:8080",
    "http://www.elabs.com:8888",
    "https://andrewembry312-hub.github.io",
    "https://copilot.elabsai.com",
    "https://www.elabsai.com",
]
_extra = [o.strip() for o in os.environ.get("ELABS_EXTRA_CORS", "").split(",") if o.strip()]
_CORS_ORIGINS = _CORS_BASE_ORIGINS + _extra

app.add_middleware(
    _CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Disable browser caching for JS/CSS during development
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse

class NoCacheDevMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Strip If-None-Match and If-Modified-Since headers for .js/.css files
        # This prevents Starlette StaticFiles from returning 304
        path = request.url.path
        if path.endswith('.js') or path.endswith('.css') or path.endswith('.html'):
            scope = request.scope
            headers = dict(scope.get('headers', []))
            # Remove conditional headers to force 200
            new_headers = [(k, v) for k, v in scope.get('headers', []) if k not in (b'if-none-match', b'if-modified-since')]
            scope['headers'] = new_headers

        response = await call_next(request)
        if path.endswith('.js') or path.endswith('.css') or path.endswith('.html'):
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
        return response

app.add_middleware(NoCacheDevMiddleware)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Inject minimal security headers on every response (OWASP baseline)."""
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        # Allow same-origin embedding for THE MACHINE lab iframes.
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; img-src 'self' data: blob:; font-src 'self' https://fonts.gstatic.com; connect-src 'self' ws: wss: http://127.0.0.1:18789 https://copilot.elabsai.com https://gateway.elabsai.com; frame-src 'self'; frame-ancestors 'self'; object-src 'none';"
        )
        return response

app.add_middleware(SecurityHeadersMiddleware)

# Enable OpenClaw agentic execution by default for local E-Labs runtime
import os as _os
_os.environ.setdefault("OPENCLAW_ENABLED", "1")

# ── Auth router ───────────────────────────────────────────────────────────────
try:
    from auth_router import router as _auth_router, auth_guard as _auth_guard
    app.include_router(_auth_router, prefix="/api/auth")
    log_init = logging.getLogger("app")
    log_init.info("Auth router mounted at /api/auth")
except ImportError as _auth_err:
    logging.getLogger("app").warning("auth_router not loaded: %s", _auth_err)
    def _auth_guard(*_a, **_kw):  # no-op passthrough if auth_router unavailable
        return None

# ── Billing router ─────────────────────────────────────────────────────────────
try:
    from billing_router import router as _billing_router, check_and_increment_usage as _check_usage
    app.include_router(_billing_router, prefix="/api/billing")
    logging.getLogger("app").info("Billing router mounted at /api/billing")
except ImportError as _billing_err:
    logging.getLogger("app").warning("billing_router not loaded: %s", _billing_err)
    def _check_usage(*_a, **_kw): return True  # allow all if billing unavailable

# ── Org router ─────────────────────────────────────────────────────────────────
try:
    from org_router import router as _org_router
    app.include_router(_org_router, prefix="/api/auth")
    logging.getLogger("app").info("Org router mounted at /api/auth/orgs")
except ImportError as _org_err:
    logging.getLogger("app").warning("org_router not loaded: %s", _org_err)

# ── Store the uvicorn event loop for workspace_watcher SSE broadcasts ─────────
# broadcast_task_event is called from background swarm threads; without a stored
# loop reference, asyncio.get_event_loop() from a thread returns the WRONG loop
# and events are silently dropped, making activity bubbles fall back to fake text.
@app.on_event("startup")
async def _capture_event_loop():
    import asyncio as _asyncio
    from orchestrator.workspace_watcher import set_main_loop
    set_main_loop(_asyncio.get_event_loop())

# ── Background project scheduler ─────────────────────────────────────────────
# Polls every 30 seconds; fires any project whose scheduled_at has passed.
import threading as _threading
import time as _time

def _schedule_runner():
    """Background daemon: fires scheduled projects when their run_at time passes."""
    _time.sleep(10)  # Wait for app to fully start before first check
    while True:
        try:
            fired = machine_engine.fire_scheduled_projects()
            if fired:
                log.info("Scheduler: fired projects %s", fired)
        except Exception as _e:
            log.warning("Scheduler error: %s", _e)
        _time.sleep(30)

_sched_thread = _threading.Thread(target=_schedule_runner, daemon=True, name="project-scheduler")
_sched_thread.start()


# ---------- Optional API key auth (set ELABS_API_KEY env var to enforce) ----------
_ELABS_API_KEY = os.environ.get("ELABS_API_KEY", "")
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def require_api_key(api_key: str = Security(_api_key_header)):
    """Dependency: enforces X-API-Key header if ELABS_API_KEY is set in env.
    In local dev (no env var set) this is a no-op but logs a warning."""
    if _ELABS_API_KEY:
        if api_key != _ELABS_API_KEY:
            raise HTTPException(status_code=401, detail="Unauthorized — X-API-Key required")
    else:
        log_warning = logging.getLogger("app.auth")
        log_warning.debug("ELABS_API_KEY not set — auth check skipped (local dev mode)")


# ---------- Request/Response models ----------

class PromptReq(BaseModel):
    prompt: str
    mode: str = "auto"  # canonical: "chat", "agent", "plan" | legacy: "auto", "max", "gwen", "nemotron", "multi", "toolcaller"
    model: str | None = None  # Ollama-compat: copilot-nemo / copilot-standard / copilot-deep
    system: str | None = None  # Optional system prompt (used by fleet agents)
    agent_id: str | None = None  # Optional agent context — overrides mode with agent's routing_mode
    cloud_model_id: str | None = None  # Optional cloud model — routes to cloud API instead of local
    session_id: str | None = None  # Optional session for conversation memory
    project_id: str | None = None  # Optional project ID — injects project-scoped memory
    messages: list[dict] | None = None  # Previous messages for context: [{role, content}, ...]
    include_history: bool = False  # Include previous chat sessions in model context
    thinking: bool = False  # Show model reasoning traces (applies to all modes)


class TaskResult(BaseModel):
    muscle: str
    task: str
    reasoning: str
    response: str
    route_time: float
    exec_time: float
    tokens: int


class MultiResult(BaseModel):
    subtasks: list[dict]
    total_time: float


# ---------- Tool interception ----------

def handle_tools_in_prompt(prompt: str) -> str | None:
    m = re.search(r"TOOL:calc\(([^)]+)\)", prompt)
    if m:
        expr = m.group(1)
        if re.match(r"^[0-9+\-*/(). %]+$", expr):
            try:
                result = eval(expr)
                return f"[calc: {expr} = {result}]"
            except Exception as e:
                return f"[calc error: {e}]"
        return "[calc error: unsupported characters]"
    return None


def _machine_blocked_payload(reason: str, session_id: str | None = None) -> dict:
    """Return a non-launching MACHINE response for routes that must stay conversational."""
    response = (
        "THE MACHINE is restricted to Agent mode with The Machine/OpenCLAW runtime selected. "
        "Use Agent mode and choose The Machine when you want a multi-agent workflow project."
    )
    if reason:
        response += f"\n\nBlocked reason: {reason}"
    if session_id:
        conversation_manager.add_message(session_id, "assistant", response)
    return {
        "muscle": "MACHINE_BLOCKED",
        "task": "machine_launch_blocked",
        "reasoning": reason,
        "response": response,
        "machine": False,
        "machine_blocked": True,
        "route_time": 0,
        "exec_time": 0,
        "tokens": 0,
        "session_id": session_id,
    }


def _openclaw_runtime_ready(timeout: float = 2.0) -> tuple[bool, dict]:
    """Return whether the configured OpenCLAW gateway is reachable and enabled."""
    import urllib.request as _urlreq

    settings = globals().get("_oc_settings", {}) or {}
    if settings.get("enabled", True) is False:
        return False, {"enabled": False, "reason": "OpenCLAW integration disabled"}
    gateway = os.environ.get("OPENCLAW_URL", settings.get("gateway", "http://127.0.0.1:18789")).rstrip("/")
    try:
        req = _urlreq.Request(f"{gateway}/")
        with _urlreq.urlopen(req, timeout=timeout) as resp:
            return True, {"gateway": gateway, "status_code": getattr(resp, "status", 200), "enabled": True}
    except Exception as exc:
        return False, {"gateway": gateway, "enabled": True, "error": str(exc)}


def _can_launch_machine_from_agent(mode: str, agent_id: str, active_backend: str = "") -> tuple[bool, dict]:
    """Central policy: MACHINE launches only from Agent mode with OpenCLAW runtime ready."""
    if mode != "agent":
        return False, {"reason": "MACHINE handoff requires Agent mode", "mode": mode, "agent_id": agent_id}
    # Allow if the active backend is openclaw OR the agent_id is openclaw
    is_openclaw = (agent_id == "openclaw") or (active_backend == "openclaw")
    if not is_openclaw:
        return False, {"reason": "MACHINE handoff requires The Machine/OpenCLAW runtime", "mode": mode, "agent_id": agent_id, "active_backend": active_backend}
    ready, details = _openclaw_runtime_ready()
    if not ready:
        return False, {"reason": "OpenCLAW runtime is not reachable", "mode": mode, "agent_id": agent_id, "active_backend": active_backend, **details}
    return True, {"reason": "OpenCLAW runtime ready", "mode": mode, "agent_id": agent_id, "active_backend": active_backend, **details}


# ---------- Endpoints ----------

@app.get("/health")
def health():
    return {
        "status": "ok",
        "ceo_mode": config.CEO_MODE,
        "ceo_model": config.CEO_LOCAL_MODEL,
        "muscles": {k: v["model"] for k, v in config.MUSCLES.items()},
    }


@app.get("/dev/help-revisions")
def dev_help_revisions():
    """Return revision metadata for all help pages. Used by validation runner."""
    import glob as _glob, re as _re
    help_dir = Path(__file__).parent.parent / "frontend" / "help"
    pages = []
    for hp in sorted(help_dir.glob("*.html")):
        text = hp.read_text(encoding="utf-8", errors="replace")
        m = _re.search(r'<meta\s+name="help-revision"\s+content="(\d+)"\s+data-updated="([^"]*)"', text)
        pages.append({
            "path": hp.name,
            "revision": int(m.group(1)) if m else 0,
            "updated": m.group(2) if m else "",
            "status": "stamped" if m else "no-stamp",
        })
    return {"server": "the-machine", "pages": pages}


# ---------- Conversation/Session Management ----------

@app.post("/api/sessions")
def create_session(req: dict = Body(...)):
    """Create a new conversation session."""
    name = req.get("name", "")
    session_id = conversation_manager.create_session(name)
    return {"session_id": session_id, "name": conversation_manager.get_session(session_id)["name"]}


@app.get("/api/sessions")
def list_sessions():
    """List all conversation sessions."""
    return {"sessions": conversation_manager.list_sessions()}


@app.get("/api/sessions/search")
def search_conversations(q: str = "", session_id: str = ""):
    """Full-text search across conversation messages."""
    if not q or len(q.strip()) < 2:
        raise HTTPException(400, "Query must be at least 2 characters")
    results = conversation_manager.search_messages(q.strip(), session_id=session_id or None)
    return {"query": q, "results": results, "count": len(results)}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    """Get a specific session with full message history."""
    session = conversation_manager.get_session(session_id)
    if not session:
        raise HTTPException(404, f"Session not found: {session_id}")
    return session


@app.get("/api/sessions/{session_id}/stats")
def get_session_stats(session_id: str):
    """Get session statistics."""
    stats = conversation_manager.get_stats(session_id)
    if not stats:
        raise HTTPException(404, f"Session not found: {session_id}")
    return stats


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str):
    """Delete a session."""
    if conversation_manager.delete_session(session_id):
        return {"status": "deleted", "session_id": session_id}
    raise HTTPException(404, f"Session not found: {session_id}")


@app.put("/api/sessions/{session_id}/messages/{msg_index}")
def edit_message(session_id: str, msg_index: int, body: dict = Body(...)):
    """Edit a message in a session."""
    content = body.get("content", "")
    if not content:
        raise HTTPException(400, "content is required")
    if not conversation_manager.edit_message(session_id, msg_index, content):
        raise HTTPException(404, "Session or message not found")
    return {"status": "edited", "session_id": session_id, "msg_index": msg_index}


@app.delete("/api/sessions/{session_id}/messages/{msg_index}")
def delete_message(session_id: str, msg_index: int):
    """Soft-delete a message in a session."""
    if not conversation_manager.delete_message(session_id, msg_index):
        raise HTTPException(404, "Session or message not found")
    return {"status": "deleted", "session_id": session_id, "msg_index": msg_index}


@app.post("/api/sessions/{session_id}/messages/{msg_index}/rate")
def rate_message(session_id: str, msg_index: int, body: dict = Body(...)):
    """Rate a message (1-5) with optional tags."""
    rating = body.get("rating", 0)
    tags = body.get("tags", [])
    if not isinstance(rating, int) or rating < 1 or rating > 5:
        raise HTTPException(400, "rating must be 1-5")
    if not conversation_manager.rate_message(session_id, msg_index, rating, tags):
        raise HTTPException(404, "Session or message not found")
    return {"status": "rated", "session_id": session_id, "msg_index": msg_index, "rating": rating}


# Keywords that indicate a media/generation request — skip CEO for speed
_MEDIA_KEYWORDS = re.compile(
    r"\b(generate|create|make|draw|paint|render|design)\b.*\b(image|picture|photo|illustration|artwork|video|animation|clip)\b"
    r"|\b(image|picture|photo|video)\b.*\b(of|showing|depicting|with)\b"
    r"|\b(edit|modify|change|transform|inpaint|animate)\b.*\b(image|picture|photo|video)\b",
    re.IGNORECASE,
)


def build_history_context(include_history: bool = False) -> str:
    """Build a summary of previous chat sessions for model context."""
    if not include_history:
        return ""
    
    all_sessions = conversation_manager.list_sessions()
    if not all_sessions:
        return ""
    
    # Build summary of recent sessions (exclude current one)
    history_items = []
    for s in all_sessions[-10:]:  # Last 10 sessions
        if not s.get("messages"):
            continue
        # Create brief summary
        msg_count = len(s["messages"])
        first_msg = s["messages"][0].get("content", "")[:100] if s["messages"] else ""
        created = s.get("created_at", "unknown")
        history_items.append(f"- Session ({created}): {msg_count} messages, started with '{first_msg}...'")
    
    if not history_items:
        return ""
    
    return f"\n\n[PREVIOUS CHAT HISTORY]\nYou have access to your conversation history. Here are recent sessions:\n" + "\n".join(history_items)



@app.post("/api/chat")
def chat_alias(body: dict = Body(...)):
    """Alias for /api/generate. Accepts {message, session} or {prompt, session_id} formats."""
    prompt = body.get("message") or body.get("prompt", "")
    if not prompt:
        raise HTTPException(400, "message or prompt is required")
    req = PromptReq(
        prompt=prompt,
        mode=body.get("mode", "auto"),
        session_id=body.get("session") or body.get("session_id"),
        model=body.get("model"),
    )
    return generate(req)


@app.post("/api/generate")
def generate(req: PromptReq, _user=Depends(_auth_guard)):
    """Main endpoint. Routes through CEO or directly to a muscle."""
    # Create or ensure session exists
    session_id = req.session_id
    if session_id and not conversation_manager.session_exists(session_id):
        conversation_manager.create_session()  # Create if doesn't exist
    
    # Add user message to conversation history
    if session_id:
        conversation_manager.add_message(session_id, "user", req.prompt)
    
    # Get conversation context if available (for models that support it)
    messages_context = []
    if session_id:
        messages_context = conversation_manager.get_context(session_id, max_messages=10)
    
    # Check tool usage first
    tool_res = handle_tools_in_prompt(req.prompt)
    if tool_res is not None:
        if session_id:
            conversation_manager.add_message(session_id, "assistant", tool_res)
        return {"muscle": "TOOL", "response": tool_res, "route_time": 0, "exec_time": 0, "tokens": 0, "session_id": session_id}

    # If agent_id provided, use agent's routing mode instead of raw mode
    mode = req.mode.strip().lower()
    # Ollama-compat: model field (e.g. "copilot-nemo") overrides mode when mode is default
    if req.model and mode == "auto":
        mode = req.model.strip().lower()
    agent = None
    if req.agent_id:
        agent = agent_registry.get_agent(req.agent_id)
        if agent:
            mode = agent.get("routing_mode", mode)

    # Cloud model override — route entire request to cloud API
    if req.cloud_model_id:
        entry = model_registry.get_model(req.cloud_model_id)
        if not entry:
            raise HTTPException(404, f"Cloud model not found: {req.cloud_model_id}")
        if entry.get("location") != "cloud":
            raise HTTPException(400, f"Model '{req.cloud_model_id}' is local, not cloud")
        t0 = time.time()
        result = cloud_models.call_cloud_model(
            entry, prompt=req.prompt,
            messages=[{"role": "user", "content": req.prompt}],
        )
        exec_time = time.time() - t0
        if "error" in result:
            error_msg = f"Error: {result['error']}"
            if session_id:
                conversation_manager.add_message(session_id, "assistant", error_msg)
            return {"muscle": "CLOUD", "task": req.prompt, "reasoning": f"Cloud: {entry['name']}",
                    "response": error_msg, "route_time": 0,
                    "exec_time": round(exec_time, 1), "tokens": 0, "session_id": session_id}
        
        response_text = result.get("text", "")
        if session_id:
            conversation_manager.add_message(session_id, "assistant", response_text)
        return {
            "muscle": f"CLOUD:{entry['provider'].upper()}",
            "task": req.prompt,
            "reasoning": f"Cloud model: {entry['name']} ({entry['provider']})",
            "response": response_text,
            "route_time": 0,
            "exec_time": round(exec_time, 1),
            "tokens": result.get("usage", {}).get("output_tokens",
                      result.get("usage", {}).get("completion_tokens", 0)),
            "cost_usd": result.get("cost_usd", 0),
            "model_used": entry["name"],
            "provider": entry["provider"],
            "files": result.get("files", []),
            "session_id": session_id,
        }

    # Toolcaller mode — redirect to the tool-calling endpoint
    if mode == "toolcaller":
        return toolcall(req)

    # Build conversation history from request
    history = []
    if req.messages:
        for m in req.messages[-20:]:
            if m.get("role") in ("user", "assistant", "system") and m.get("content"):
                history.append({"role": m["role"], "content": m["content"]})
    # Fall back to session conversation history when the client doesn't
    # send its own messages list (e.g. API callers, voice input, tests)
    elif messages_context:
        history = [m for m in messages_context[-20:]
                   if m.get("role") in ("user", "assistant", "system") and m.get("content")]

    # Auto-compact history if approaching context window limit
    from orchestrator.features import feature
    if feature("CONTEXT_COMPACTION") and history:
        from orchestrator.context_compactor import auto_compact, get_model_context_size
        ctx_size = get_model_context_size(mode) if mode else 8192
        history = auto_compact(history, context_size=ctx_size)

    # ── MEMORY TIERS (aligned with 6-tier architecture) ──
    # Tier 1: Working Memory = current chat messages (already in history from frontend)
    # Tier 2: Conversation History = cross-convo messages (already scoped by frontend to same project)
    # Tier 3: Saved Memory = persistent user-level facts (global, always available)
    # Tier 4: Project Instructions = per-project custom rules (sent as system msg from frontend)
    # Tier 5: Model Knowledge = baked into the model
    # Tier 6: Not present = no external tracking
    
    # Load Tier 3: Global saved memory (user-level facts that persist everywhere)
    mem = _load_memory()
    all_facts = mem.get("facts", [])
    # Retrieve only relevant facts based on user's prompt (not full dump)
    relevant_facts = _retrieve_relevant_facts(all_facts, req.prompt)
    mem_system = ""
    if relevant_facts:
        fact_lines = []
        for f in relevant_facts:
            cat = f.get("category", "")
            cat_label = f" [{cat}]" if cat and cat != "unknown" else ""
            fact_lines.append(f"- {f.get('text', '')}{cat_label}")
        mem_system = (
            "[Saved Memory — persistent facts about the user]\n"
            "IMPORTANT: These are confirmed facts the user previously told you. "
            "Use them naturally when relevant. If the user asks about something covered here, "
            "answer from these facts confidently.\n"
            + "\n".join(fact_lines) + "\n[End saved memory]\n"
        )
    
    # Load project-scoped memory if in a project context
    proj_mem_system = ""
    relevant_proj = []
    if req.project_id:
        proj_mem = _load_project_memory(req.project_id)
        proj_facts = proj_mem.get("facts", [])
        relevant_proj = _retrieve_relevant_facts(proj_facts, req.prompt)
        if relevant_proj:
            proj_lines = [f"- {f.get('text', '')}" for f in relevant_proj]
            proj_mem_system = "[Project Memory — facts specific to this project only]\n" + "\n".join(proj_lines) + "\n[End project memory]\n"
        # Touch last_used_at on retrieved project facts
        for rf in relevant_proj:
            for pf in proj_facts:
                if pf.get("id") == rf.get("id"):
                    pf["last_used_at"] = time.time()
                    pf["access_count"] = pf.get("access_count", 0) + 1
        if relevant_proj:
            _save_project_memory(req.project_id, proj_mem)
    
    # Touch last_used_at on retrieved global facts
    for rf in relevant_facts:
        for af in all_facts:
            if af.get("id") == rf.get("id"):
                af["last_used_at"] = time.time()
                af["access_count"] = af.get("access_count", 0) + 1
    if relevant_facts:
        _save_memory(mem)
    
    # Combine all memory tiers into system prompt
    combined_mem_system = mem_system + proj_mem_system if (mem_system or proj_mem_system) else ""
    if combined_mem_system:
        n_proj = len(relevant_proj) if req.project_id else 0
        print(f"[Memory] Injecting {len(relevant_facts)} global + {n_proj} project facts for query: {req.prompt[:80]!r}")

    # Direct Ollama model (mounted from Settings, not a named muscle)
    if mode.startswith("direct:"):
        raw_model = mode[7:]  # strip 'direct:' prefix
        t0 = time.time()
        # Separate system messages (frontend context) from conversation history
        system_msgs = [m for m in history if m.get("role") == "system"]
        chat_msgs = [m for m in history if m.get("role") != "system"]
        # Build combined system content: persistent memory + project memory + frontend context + tool instructions
        combined_system = combined_mem_system
        for sm in system_msgs:
            combined_system += sm["content"] + "\n"
        combined_system += "\n" + build_tool_instructions()
        direct_msgs = []
        if combined_system.strip():
            direct_msgs.append({"role": "system", "content": combined_system.strip()})
        direct_msgs += chat_msgs + [{"role": "user", "content": req.prompt}]

        # Check if model uses llamacpp backend (e.g. PrismML server)
        # Respect the LLAMACPP_KV_ENABLED toggle so the user can disable it from Settings.
        from orchestrator.muscles import _get_llamacpp_meta, _llamacpp_chat
        llamacpp_meta = _get_llamacpp_meta(raw_model) if config.LLAMACPP_KV_ENABLED else None
        llamacpp_ep = llamacpp_meta["endpoint"] if llamacpp_meta else None
        if llamacpp_ep:
            try:
                result = _llamacpp_chat(llamacpp_ep, direct_msgs)
            except Exception as e:
                exec_time = time.time() - t0
                error_msg = f"Error from llamacpp ({raw_model}): {e}"
                if session_id:
                    conversation_manager.add_message(session_id, "assistant", error_msg)
                return {
                    "muscle": raw_model.replace(':latest', '').upper(),
                    "task": req.prompt,
                    "reasoning": f"Direct call to {raw_model} (llamacpp) failed",
                    "response": error_msg,
                    "route_time": 0,
                    "exec_time": round(exec_time, 1),
                    "tokens": 0,
                    "session_id": session_id,
                }
        else:
            payload = json.dumps({
                "model": raw_model,
                "messages": direct_msgs,
                "stream": False, "keep_alive": "30m",
                "options": config.get_ollama_options(),
            }).encode()
            import urllib.request as _urlreq
            _req = _urlreq.Request(f"{config.OLLAMA_URL}/api/chat", data=payload,
                                   headers={"Content-Type": "application/json"})
            try:
                with _urlreq.urlopen(_req, timeout=300) as _resp:
                    result = json.loads(_resp.read())
            except Exception as e:
                exec_time = time.time() - t0
                error_detail = str(e)
                # Try to read Ollama error body
                if hasattr(e, 'read'):
                    try:
                        error_detail = e.read().decode()
                    except Exception:
                        pass
                elif hasattr(e, 'fp') and e.fp:
                    try:
                        error_detail = e.fp.read().decode()
                    except Exception:
                        pass
                error_msg = f"Error from Ollama ({raw_model}): {error_detail}"
                if session_id:
                    conversation_manager.add_message(session_id, "assistant", error_msg)
                return {
                    "muscle": raw_model.replace(':latest', '').upper(),
                    "task": req.prompt,
                    "reasoning": f"Direct call to {raw_model} failed",
                    "response": error_msg,
                    "route_time": 0,
                    "exec_time": round(exec_time, 1),
                    "tokens": 0,
                    "session_id": session_id,
                }
        exec_time = time.time() - t0
        resp = (result.get("message") or {}).get("content", "")
        if "<think>" in resp:
            resp = resp.split("</think>")[-1].strip()
        # Detect and execute any <tool_call> blocks in the response
        resp, tool_results = _process_tool_calls_in_response(resp)
        if session_id:
            conversation_manager.add_message(session_id, "assistant", resp)
        _direct_ret = {
            "muscle": raw_model.replace(':latest', '').upper(),
            "task": req.prompt,
            "reasoning": f"Direct call to mounted model: {raw_model}",
            "response": resp,
            "route_time": 0,
            "exec_time": round(exec_time, 1),
            "tokens": result.get("eval_count", 0),
            "session_id": session_id,
            "tool_results": tool_results,
        }
        # Annotate with framework metadata when llama.cpp handled this request
        if llamacpp_meta:
            _direct_ret["framework"]      = "llamacpp"
            _direct_ret["kv_tier"]        = str(llamacpp_meta.get("tier", 1))
            _direct_ret["kv_scheme"]      = llamacpp_meta.get("scheme", "q8_0")
            _direct_ret["context_window"] = llamacpp_meta.get("context_window", 40000)
        return _direct_ret

    # Direct MACHINE mode (bypass CEO)
    if mode == "machine":
        return _machine_blocked_payload("Direct /api/generate machine mode is disabled; use Agent mode with The Machine/OpenCLAW runtime.", session_id)

    # GitHub Copilot SDK path (copilot-nemo / copilot-standard / copilot-deep)
    if mode.startswith("copilot-"):
        tier = mode.replace("copilot-", "") or "nemo"
        t0 = time.time()
        try:
            from orchestrator.copilot_integration import get_bridge
            bridge = get_bridge()
            system = req.system or ""
            if combined_mem_system:
                system = combined_mem_system + "\n" + system
            result = bridge.send_blocking(req.prompt, system=system.strip(), tier=tier,
                                           caller_context=req.agent_id or f"copilot-{tier}")
            exec_time = time.time() - t0
            resp = result.text if result.success else f"[Copilot error: {result.error}]"
            if session_id:
                conversation_manager.add_message(session_id, "assistant", resp)
            return {
                "muscle": f"COPILOT_{tier.upper()}",
                "task": req.prompt,
                "reasoning": f"GitHub Copilot SDK ({result.model})",
                "response": resp,
                "route_time": 0,
                "exec_time": round(exec_time, 1),
                "tokens": len(resp.split()),
                "session_id": session_id,
            }
        except Exception as e:
            exec_time = time.time() - t0
            err = f"[Copilot bridge error: {e}]"
            if session_id:
                conversation_manager.add_message(session_id, "assistant", err)
            return {"muscle": "COPILOT_ERROR", "task": req.prompt, "reasoning": str(e),
                    "response": err, "route_time": 0, "exec_time": round(exec_time, 1),
                    "tokens": 0, "session_id": session_id}

    # Direct muscle call (bypass CEO)
    if mode in ("max", "gwen", "nemotron"):
        t0 = time.time()
        system_prompt = config.MUSCLES[mode.upper()].get("system_prompt", "")
        # Inject persistent + project memory at the TOP of system prompt
        if combined_mem_system:
            system_prompt = combined_mem_system + "\n" + system_prompt
        system_prompt += build_history_context(req.include_history)
        # Inject tool instructions so model knows how to use <tool_call>
        system_prompt += "\n\n" + build_tool_instructions()
        # Inject smart guidance so model can suggest better modes/models
        system_prompt += build_smart_guidance(mode, "chat")
        result = muscles.call_muscle(mode, req.prompt, system=system_prompt, history=history)
        exec_time = time.time() - t0
        resp = result.get("response", "")
        if "<think>" in resp:
            resp = resp.split("</think>")[-1].strip()
        # Detect and execute any <tool_call> blocks in the response
        resp, tool_results = _process_tool_calls_in_response(resp)
        if session_id:
            conversation_manager.add_message(session_id, "assistant", resp)
        return {
            "muscle": mode.upper(),
            "task": req.prompt,
            "reasoning": f"Direct call to {mode.upper()} (CEO bypassed)",
            "response": resp,
            "route_time": 0,
            "exec_time": round(exec_time, 1),
            "tokens": result.get("eval_count", 0),
            "session_id": session_id,
            "tool_results": tool_results,
        }

    # Auto mode — fast-path: detect obvious media requests without CEO
    if _MEDIA_KEYWORDS.search(req.prompt):
        return toolcall(req)

    # CEO-routed (auto mode) — with smart model selection
    try:
        t0 = time.time()
        routing = ceo.smart_route(req.prompt)
        route_time = time.time() - t0
    except Exception as e:
        error_msg = f"Routing error — is Ollama running? ({e})"
        if session_id:
            conversation_manager.add_message(session_id, "assistant", error_msg)
        return {
            "muscle": "ERROR",
            "task": req.prompt,
            "reasoning": f"CEO routing failed: {e}",
            "response": error_msg,
            "route_time": 0, "exec_time": 0, "tokens": 0,
            "session_id": session_id,
        }

    muscle_name = routing.get("muscle", "NEMOTRON")
    task = routing.get("task", req.prompt)
    reasoning = routing.get("reasoning", "")

    # CEO routed to toolcaller — redirect
    if muscle_name == "TOOLCALLER":
        return toolcall(req)

    # CEO routed to artifact builder — redirect
    if muscle_name == "ARTIFACT":
        t1 = time.time()
        result = artifact_builder.build_artifact(req.prompt)
        exec_time = time.time() - t1
        if result["success"]:
            resp_payload = {
                "muscle": "ARTIFACT",
                "task": routing.get("task", req.prompt),
                "reasoning": reasoning,
                "response": result.get("bundled_html", ""),
                "artifact": True,
                "manifest": result.get("manifest"),
                "build_time": result.get("build_time", 0),
                "route_time": round(route_time, 1),
                "exec_time": round(exec_time, 1),
                "tokens": 0,
                "session_id": session_id,
            }
            if session_id:
                conversation_manager.add_message(session_id, "assistant", f"[Artifact built: {result.get('manifest', {}).get('title', 'Untitled')}]")
            return resp_payload
        else:
            error_msg = f"Artifact build failed: {result.get('error', 'unknown error')}"
            if session_id:
                conversation_manager.add_message(session_id, "assistant", error_msg)
            return {
                "muscle": "ARTIFACT",
                "task": req.prompt,
                "reasoning": reasoning,
                "response": error_msg,
                "route_time": round(route_time, 1),
                "exec_time": round(exec_time, 1),
                "tokens": 0,
                "session_id": session_id,
            }

    # CEO routed to THE MACHINE — create project and return launch info
    if muscle_name == "MACHINE":
        return _machine_blocked_payload("CEO auto-routing to MACHINE is disabled outside Agent mode with The Machine/OpenCLAW runtime.", session_id)

    # Use smart-selected model if available, otherwise default muscle model
    model_override = routing.get("smart_ollama_tag", "")
    try:
        t1 = time.time()
        system_prompt = config.MUSCLES[muscle_name.upper()].get("system_prompt", "")
        # Inject persistent + project memory at the TOP of system prompt (models attend to beginning most)
        if combined_mem_system:
            system_prompt = combined_mem_system + "\n" + system_prompt
        # Extract frontend system messages (project instructions, etc.) and merge
        memory_system_msgs = [m["content"] for m in history if m.get("role") == "system"]
        chat_history = [m for m in history if m.get("role") != "system"]
        if memory_system_msgs:
            system_prompt = system_prompt.rstrip() + "\n\n" + "\n".join(memory_system_msgs)
        # Inject tool instructions so model knows how to use <tool_call>
        system_prompt += "\n\n" + build_tool_instructions()
        # Inject smart guidance so model can suggest better modes/models
        system_prompt += build_smart_guidance(muscle_name, "auto")
        # Auto-compact long conversation history (claw-code pattern)
        from orchestrator.features import feature
        if feature("CONTEXT_COMPACTION") and chat_history:
            from orchestrator.context_compactor import auto_compact
            model_tag = model_override or muscle_name.lower()
            chat_history = auto_compact(
                [{"role": "system", "content": system_prompt}] + chat_history,
                context_size=8192,
            )
            # Extract system back out (auto_compact preserves it)
            chat_history = [m for m in chat_history if m.get("role") != "system"]
        result = muscles.call_muscle(muscle_name, req.prompt, system=system_prompt, model_override=model_override, history=chat_history)
        exec_time = time.time() - t1
    except Exception as e:
        return {
            "muscle": muscle_name,
            "task": task,
            "reasoning": reasoning,
            "response": f"Muscle call failed — is Ollama running? ({e})",
            "route_time": round(route_time, 1), "exec_time": 0, "tokens": 0,
        }

    resp = result.get("response", "")
    if "<think>" in resp:
        resp = resp.split("</think>")[-1].strip()
    # Detect and execute any <tool_call> blocks in the response
    resp, tool_results = _process_tool_calls_in_response(resp)

    if session_id:
        conversation_manager.add_message(session_id, "assistant", resp)

    response = {
        "muscle": muscle_name,
        "task": task,
        "reasoning": reasoning,
        "response": resp,
        "route_time": round(route_time, 1),
        "exec_time": round(exec_time, 1),
        "tokens": result.get("eval_count", 0),
        "session_id": session_id,
        "tool_results": tool_results,
    }
    # Include smart routing metadata when available
    if routing.get("smart_model_id"):
        response["smart_model"] = {
            "id": routing["smart_model_id"],
            "name": routing.get("smart_model_name", ""),
            "score": routing.get("smart_score", 0),
            "task_type": routing.get("smart_task_type", ""),
            "priority": routing.get("smart_priority", ""),
        }
    # Session provenance — which industry agent runtime (if any) is powering this response
    response["session_brand"] = _industry_agents.resolve_session_brand(
        agent_id=req.agent_id or None,
        muscle=muscle_name,
        model_tag=model_override or "",
    ).get("session_indicator", "Standard Local Backend")
    return response


@app.post("/api/multi")
def multi_generate(req: PromptReq):
    """Decompose a complex request into subtasks and execute each."""
    t_total = time.time()

    t0 = time.time()
    plan = ceo.decompose(req.prompt)
    decompose_time = time.time() - t0

    # Ambiguity gate: return clarification request if prompt is too vague
    if plan.get("clarification_needed"):
        return {
            "clarification_needed": True,
            "question": plan.get("question", "Could you be more specific?"),
            "ambiguity": plan.get("ambiguity", {}),
            "decompose_time": round(decompose_time, 1),
        }

    subtasks = plan.get("subtasks", [])
    results = []

    for st in sorted(subtasks, key=lambda x: x.get("order", 0)):
        muscle_name = st["muscle"]
        task = st["task"]
        t1 = time.time()
        system_prompt = config.MUSCLES[muscle_name.upper()].get("system_prompt", "")
        system_prompt += build_history_context(req.include_history)
        # Inject tool instructions for subtasks too
        system_prompt += "\n\n" + build_tool_instructions()
        result = muscles.call_muscle(muscle_name, task, system=system_prompt)
        exec_time = time.time() - t1
        resp = result.get("response", "")
        if "<think>" in resp:
            resp = resp.split("</think>")[-1].strip()
        # Detect and execute any <tool_call> blocks
        resp, tool_results = _process_tool_calls_in_response(resp)
        results.append({
            "muscle": muscle_name,
            "task": task,
            "response": resp,
            "exec_time": round(exec_time, 1),
            "tokens": result.get("eval_count", 0),
            "tool_results": tool_results,
        })

    return {
        "subtasks": results,
        "decompose_time": round(decompose_time, 1),
        "total_time": round(time.time() - t_total, 1),
    }


@app.post("/api/tool/calc")
def tool_calc(body: dict):
    expr = body.get("expr", "")
    if re.match(r"^[0-9+\-*/(). %]+$", expr):
        try:
            return {"result": eval(expr)}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
    raise HTTPException(status_code=400, detail="invalid characters")


@app.get("/api/models")
def list_models():
    """Return loaded models from Ollama."""
    return muscles.list_loaded()


@app.get("/api/chat/model-options")
def get_chat_model_options():
    """Return model/routing choices for the prompt-area Chat model selector."""
    options = [
        {
            "value": "auto",
            "label": "Auto",
            "description": "Selects the best installed or preferred model for the task.",
            "installed": True,
            "kind": "router",
        },
    ]
    try:
        direct_models = list_ollama_models()
    except Exception:
        direct_models = []

    seen_values = {item["value"] for item in options}
    for model in direct_models:
        name = model.get("name") if isinstance(model, dict) else str(model)
        if not name:
            continue
        value = f"direct:{name}"
        if value in seen_values:
            continue
        seen_values.add(value)
        clean = name.replace(":latest", "")
        options.append({
            "value": value,
            "label": model.get("display_name") or clean,
            "description": model.get("notes") or model.get("task") or "Direct local model route",
            "installed": bool(model.get("pulled", True)),
            "kind": "direct",
            "source": model.get("source", "ollama"),
        })

    try:
        cloud_models_available = reasoning_models.list_all()
    except Exception:
        cloud_models_available = []
    for model in cloud_models_available:
        if model.get("location") != "cloud":
            continue
        model_id = model.get("id") or model.get("api_model_id")
        if not model_id:
            continue
        value = f"cloud:{model_id}"
        if value in seen_values:
            continue
        seen_values.add(value)
        options.append({
            "value": value,
            "label": model.get("name") or model_id,
            "description": model.get("cost") or "Cloud model",
            "installed": bool(model.get("installed", False)),
            "kind": "cloud",
            "provider": model.get("provider", "cloud"),
        })

    return {"default": "auto", "options": options}


@app.get("/api/capabilities")
def get_capabilities(mode: str = "auto"):
    """Return available capabilities, active model info, and guidance for the current chat mode."""

    # All available capabilities
    all_capabilities = {
        "voice_input": {"icon": "🎤", "label": "Voice Input", "desc": "Speak naturally using the microphone button"},
        "bash_execution": {"icon": "🔨", "label": "Bash Execution", "desc": "Run shell commands, test code, verify solutions — all models can do this"},
        "file_operations": {"icon": "📄", "label": "File Operations", "desc": "Read, write, and edit files in the project"},
        "project_context": {"icon": "📁", "label": "Project Context", "desc": "Access workspace structure and conversation history"},
        "artifact_generation": {"icon": "🎨", "label": "Artifact Builder", "desc": "Create standalone HTML/React/Python apps"},
        "memory_persistence": {"icon": "💾", "label": "Memory", "desc": "Recall previous facts about user and project"},
    }

    # Mode-specific info
    mode_info = {
        "auto": {
            "label": "Auto (CEO Routed)",
            "desc": "CEO analyzes your request and picks the best model automatically.",
            "tools": ["bash", "file_write", "file_edit", "read_file"],
            "tips": ["Just describe what you need — the CEO picks MAX, GWEN, or NEMOTRON", "Ask to 'run' or 'test' something and the model will use bash", "For complex multi-step tasks, try Agent or Auto-Solve mode"],
        },
        "max": {
            "label": "MAX (Writing Specialist)",
            "desc": "Creative writing, documentation, prose, editing, translation, summarization.",
            "tools": ["bash", "file_write", "file_edit", "read_file"],
            "tips": ["MAX excels at documentation, README files, and prose", "Can run commands and write files — ask naturally", "For code-heavy tasks, switch to GWEN"],
        },
        "gwen": {
            "label": "GWEN (Code Specialist)",
            "desc": "Code generation, debugging, refactoring, architecture, testing.",
            "tools": ["bash", "file_write", "file_edit", "read_file"],
            "tips": ["GWEN can write code AND test it with bash", "Ask her to 'run the tests' or 'build the project'", "She'll read your existing files to understand context"],
        },
        "nemotron": {
            "label": "NEMOTRON (Research Specialist)",
            "desc": "Research, analysis, fact-checking, data processing, strategy.",
            "tools": ["bash", "file_write", "file_edit", "read_file"],
            "tips": ["NEMOTRON can analyze data files and run scripts", "Great for breaking down complex problems", "Can read files and summarize findings"],
        },
        "agent": {
            "label": "Agent",
            "desc": "Autonomous multi-step solve with tools. Plans, executes, verifies iteratively.",
            "tools": ["bash", "file_write", "file_edit", "read_file", "generate_image", "web_search", "search_memory", "save_memory"],
            "tips": ["Best for tasks that need planning + execution in one go", "The agent iterates — check, fix, re-check automatically", "Enable Thinking toggle (🧠) to see the model's reasoning traces"],
        },
        "plan": {
            "label": "Plan",
            "desc": "Generates a plan for your approval before executing. You control when execution starts.",
            "tools": ["bash", "file_write", "file_edit", "read_file", "generate_image", "web_search", "search_memory", "save_memory"],
            "tips": ["Review the plan before approving", "Good for high-stakes tasks where you want oversight", "Combines planning rigor with full execution power"],
        },
        "think": {
            "label": "Think (legacy → Agent + Thinking toggle)",
            "desc": "Deprecated: use Agent mode with the Thinking toggle (🧠) enabled.",
            "tools": ["bash", "file_write", "file_edit", "read_file"],
            "tips": ["Switch to Agent mode and enable the Thinking toggle (🧠)"],
        },
        "autosolve": {
            "label": "Auto-Solve (legacy → Agent)",
            "desc": "Deprecated: use Agent mode.",
            "tools": ["bash", "file_write", "file_edit", "read_file", "generate_image", "generate_video", "web_search", "search_memory", "save_memory"],
            "tips": ["Switch to Agent mode for the same autonomous multi-step solving"],
        },
        "machine": {
            "label": "THE MACHINE",
            "desc": "Multi-agent workflow — decomposes your request into a pipeline of specialized agents.",
            "tools": ["bash", "file_write", "file_edit", "read_file", "generate_image", "generate_video", "web_search"],
            "tips": ["Best for large projects: 'build a dashboard with API + frontend'", "Each agent in the pipeline specializes in one part", "Opens a workflow visualization you can monitor"],
        },
        "multi": {
            "label": "Multi-Task",
            "desc": "Decomposes your request into subtasks and routes each to the best model.",
            "tools": ["bash", "file_write", "file_edit", "read_file"],
            "tips": ["Good for requests that span writing + coding + research", "Each subtask runs on the best-fit model", "Results are combined into a single response"],
        },
        "toolcaller": {
            "label": "Toolcaller",
            "desc": "Uses the tool execution route for prompts that need external actions.",
            "tools": ["bash", "file_write", "file_edit", "read_file", "generate_image", "generate_video", "web_search"],
            "tips": ["Good for explicit tool/action requests", "Auto can also choose this route when it detects the need", "Use Agent for longer tool-heavy workflows"],
        },
    }

    requested_mode = mode.lower() if mode else "auto"
    # Resolve legacy mode aliases to canonical 3-mode model
    _mode_aliases = {"native": "agent", "think": "agent", "autosolve": "agent"}
    canonical_mode = _mode_aliases.get(requested_mode, requested_mode)
    info = mode_info.get(canonical_mode, mode_info.get(requested_mode, mode_info["auto"]))
    capabilities = list(all_capabilities.keys())
    if canonical_mode == "machine":
        capabilities.append("multi_agent_workflow")

    return {
        "mode": canonical_mode,
        "requested_mode": requested_mode,
        "mode_label": info["label"],
        "mode_description": info["desc"],
        "thinking_toggle_supported": True,
        "capabilities": capabilities,
        "descriptions": {cap: all_capabilities[cap]["desc"] for cap in capabilities if cap in all_capabilities},
        "icons": {cap: all_capabilities[cap]["icon"] for cap in capabilities if cap in all_capabilities},
        "available_tools": info["tools"],
        "tips": info["tips"],
        "help_text": f"{info['label']}: {info['desc']}",
        "all_models": {
            "MAX": {"domain": "writing", "tools": True, "best_for": "Documentation, prose, translation, editing"},
            "GWEN": {"domain": "coding", "tools": True, "best_for": "Code generation, debugging, testing, architecture"},
            "NEMOTRON": {"domain": "research", "tools": True, "best_for": "Analysis, fact-checking, data processing, strategy"},
        },
    }


@app.post("/api/toolcall")
def toolcall(req: PromptReq):
    """Send prompt to the toolcaller model (Gemma3 4B with wrapped tools).
    
    VRAM pipeline:
      ONLOAD toolcaller → get tool_calls → OFFLOAD toolcaller
      → tool_executor handles prompt enhance + diffusion with its own onload/offload
    """
    import json as _json

    t0 = time.time()

    # ── ONLOAD toolcaller (auto-loaded by Ollama on /api/chat) ────────────
    payload = {
        "model": "toolcaller",
        "messages": [{"role": "user", "content": req.prompt}],
        "stream": False,
        "think": False,
        "keep_alive": "30m",
        "options": config.get_ollama_options(),
    }
    data = _json.dumps(payload).encode()
    import urllib.request as _urlreq
    url_req = _urlreq.Request(
        f"{config.OLLAMA_URL}/api/chat",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with _urlreq.urlopen(url_req, timeout=config.MUSCLE_TIMEOUT) as resp:
        result = _json.loads(resp.read())
    raw_output = result.get("message", {}).get("content", "")
    model_time = time.time() - t0

    # ── OFFLOAD toolcaller before tool execution ──────────────────────────
    # Free 3.3GB VRAM so prompt engineer + diffusion have full GPU
    vram_manager.offload("toolcaller")

    # ── Process tool calls (prompt enhance + diffusion handle own VRAM) ───
    t1 = time.time()
    processed = tool_executor.process_model_output(raw_output)
    tool_time = time.time() - t1

    # Build media URLs for frontend
    media_urls = []
    for fp in processed.get("media_files", []):
        fname = os.path.basename(fp)
        media_urls.append(f"/api/media/{fname}")

    return {
        "muscle": "TOOLCALLER",
        "text": processed["text"],
        "tool_calls": processed["tool_calls"],
        "tool_results": processed["tool_results"],
        "has_media": processed["has_media"],
        "media_urls": media_urls,
        "model_time": round(model_time, 1),
        "tool_time": round(tool_time, 1),
        "tokens": result.get("eval_count", 0),
    }


@app.get("/api/media/{filename}")
def serve_media(filename: str):
    """Serve generated images/videos from the outputs directory."""
    # Sanitize filename to prevent path traversal
    safe_name = os.path.basename(filename)
    filepath = os.path.join(comfyui_bridge.OUTPUT_DIR, safe_name)
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(filepath)


_gpu_vram_cache: tuple | None = None

def _detect_gpu_vram_mb() -> tuple:
    """Return (gpu_name, vram_total_mb, vram_free_mb) using best available method. Cached after first call.

    Detection priority:
    1. Manual override via GPU_VRAM_OVERRIDE_MB env var (e.g. GPU_VRAM_OVERRIDE_MB=98304)
    2. nvidia-smi (discrete NVIDIA GPU)
    3. AMD UMA: total physical RAM (Win32_PhysicalMemory sum) minus OS-visible RAM
       This correctly detects BIOS-allocated unified memory (e.g. 128GB total, 98GB to GPU)
    4. WMI Win32_VideoController.AdapterRAM (fallback, usually reports only 4GB on AMD APU)
    5. orchestrator detect_gpu
    """
    global _gpu_vram_cache
    if _gpu_vram_cache is not None:
        return _gpu_vram_cache
    import subprocess as _sp

    # 0. Manual override — set GPU_VRAM_OVERRIDE_MB=98304 in environment or settings
    _override = os.environ.get("GPU_VRAM_OVERRIDE_MB", "").strip()
    if _override:
        try:
            _override_mb = int(_override)
            if _override_mb > 0:
                # Still get GPU name from WMI for display
                try:
                    import json as _j0
                    _out0 = _sp.check_output(
                        ["powershell", "-NoProfile", "-Command",
                         "Get-CimInstance Win32_VideoController | Select-Object -First 1 -ExpandProperty Name"],
                        timeout=5, text=True
                    ).strip()
                    _name0 = _out0 if _out0 else "GPU"
                except Exception:
                    _name0 = "GPU"
                _gpu_vram_cache = (_name0, _override_mb, _override_mb)
                return _gpu_vram_cache
        except ValueError:
            pass

    # 1. nvidia-smi (discrete NVIDIA GPU)
    try:
        out = _sp.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
            timeout=5, text=True
        ).strip().splitlines()[0]
        parts = [p.strip() for p in out.split(",")]
        _gpu_vram_cache = (parts[0], int(parts[1]), int(parts[2]))
        return _gpu_vram_cache
    except Exception:
        pass

    # 2. AMD UMA / unified memory detection
    # Total physical RAM (all DIMMs) minus OS-visible RAM = GPU carved-out allocation
    # e.g. 128 GB installed, 31.6 GB visible to OS → 96.4 GB allocated to GPU
    try:
        import json as _j, subprocess as _sp2
        # Get GPU name
        _name_out = _sp2.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_VideoController | Select-Object -First 1 -ExpandProperty Name"],
            timeout=5, text=True
        ).strip()
        gpu_name = _name_out if _name_out else "Unknown GPU"

        # Total physical RAM installed (BIOS level, sum of all DIMMs)
        _phys_out = _sp2.check_output(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_PhysicalMemory | Measure-Object -Property Capacity -Sum).Sum"],
            timeout=8, text=True
        ).strip()
        phys_total_mb = int(_phys_out) // (1024 * 1024) if _phys_out.isdigit() else 0

        # OS-visible RAM (after GPU UMA carve-out)
        import psutil as _psutil
        os_visible_mb = int(_psutil.virtual_memory().total // (1024 * 1024))

        if phys_total_mb > 0 and phys_total_mb > os_visible_mb:
            gpu_allocated_mb = phys_total_mb - os_visible_mb
            # Sanity check: GPU allocation should be at least 1 GB
            if gpu_allocated_mb >= 1024:
                _gpu_vram_cache = (gpu_name, gpu_allocated_mb, gpu_allocated_mb)
                return _gpu_vram_cache
            # If gap is small, this isn't a UMA setup — fall through to WMI method
    except Exception:
        pass

    # 3. WMI Win32_VideoController.AdapterRAM (fallback — often reports only 4GB on AMD APU)
    try:
        import json as _j3, subprocess as _sp3
        out = _sp3.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM | ConvertTo-Json"],
            timeout=8, text=True
        )
        items = _j3.loads(out)
        if isinstance(items, dict):
            items = [items]
        best_name, best_mb = "Unknown GPU", 0
        for item in items:
            ram = item.get("AdapterRAM") or 0
            mb = int(ram) // (1024 * 1024)
            if mb > best_mb:
                best_mb = mb
                best_name = item.get("Name", "Unknown GPU")
        if best_mb > 0:
            _gpu_vram_cache = (best_name, best_mb, best_mb)
            return _gpu_vram_cache
    except Exception:
        pass
    # 3. orchestrator detect_gpu
    try:
        from orchestrator.hardware_selector import detect_gpu
        gpu = detect_gpu() or {}
        if gpu.get("vram_total_mb", 0) > 0:
            _gpu_vram_cache = (gpu.get("name", "Unknown GPU"), int(gpu["vram_total_mb"]), int(gpu.get("vram_free_mb", gpu["vram_total_mb"])))
            return _gpu_vram_cache
    except Exception:
        pass
    _gpu_vram_cache = ("Unknown GPU", 0, 0)
    return _gpu_vram_cache


@app.get("/api/hardware")
def hardware_info():
    """Return detected GPU and system hardware specs."""
    import psutil, subprocess as _sp

    # GPU detection — use _detect_gpu_vram_mb() which tries nvidia-smi, WMI, orchestrator
    gpu_name, vram_total_mb, vram_free_mb = _detect_gpu_vram_mb()
    vram_total_gb = round(vram_total_mb / 1024, 1) if vram_total_mb else 0.0
    gpu = {"name": gpu_name, "vram_total_mb": vram_total_mb, "vram_free_mb": vram_free_mb,
           "detected": vram_total_mb > 0, "tier": 2 if vram_total_mb > 0 else 1}

    # System RAM — always works via psutil
    try:
        mem = psutil.virtual_memory()
        ram_total_gb = round(mem.total / (1024 ** 3), 1)
        ram_available_gb = round(mem.available / (1024 ** 3), 1)
        ram_used_gb = round((mem.total - mem.available) / (1024 ** 3), 1)
    except Exception:
        ram_total_gb = 0.0
        ram_available_gb = 0.0
        ram_used_gb = 0.0

    return {
        "gpu_name": gpu.get("name", "Unknown GPU"),
        "gpu_vram_total_gb": vram_total_gb,
        "gpu_vram_free_mb": gpu.get("vram_free_mb", 0),
        "gpu_tier": gpu.get("tier", 1),
        "gpu_detected": gpu.get("detected", False),
        "system_ram_total_gb": ram_total_gb,
        "system_ram_available_gb": ram_available_gb,
        "system_ram_used_gb": ram_used_gb,
    }


@app.get("/api/vram")
def vram_status():
    """Get current VRAM allocation status."""
    status = vram_manager.get_vram_status()
    status["comfyui_running"] = comfyui_bridge.is_comfyui_running()
    # Override vram_total_gb with our unified-memory-aware detection (cached)
    gpu_name, vram_total_mb, vram_free_mb = _detect_gpu_vram_mb()
    if vram_total_mb > 0:
        status["vram_total_gb"] = round(vram_total_mb / 1024, 1)
        status["gpu_name"] = gpu_name
    return status


# ---------- Skills API endpoints ----------


# ---------- ComfyUI remote config endpoints ----------

class ComfyUIConfigRequest(BaseModel):
    remote_url: str = ""


@app.get("/api/comfyui/config")
def get_comfyui_config():
    """Return current ComfyUI routing config and connectivity status."""
    remote = comfyui_bridge.get_remote_url()
    active = comfyui_bridge._resolve_active_url()
    running = comfyui_bridge.is_comfyui_running()
    return {
        "local_url": comfyui_bridge.COMFYUI_URL,
        "remote_url": remote,
        "active_url": active,
        "is_remote": bool(remote) and active == remote,
        "comfyui_running": running,
    }


@app.post("/api/comfyui/config")
def set_comfyui_config(req: ComfyUIConfigRequest):
    """Set or clear the remote ComfyUI URL. Empty string reverts to local."""
    url = req.remote_url.strip()
    # Basic URL validation
    if url and not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(400, "remote_url must start with http:// or https://")
    comfyui_bridge.set_remote_url(url)
    active = comfyui_bridge._resolve_active_url()
    running = comfyui_bridge.is_comfyui_running()
    return {
        "remote_url": url,
        "active_url": active,
        "is_remote": bool(url) and active == url.rstrip("/"),
        "comfyui_running": running,
        "message": f"ComfyUI routing updated → {active}",
    }


class NetworkNodeActivateRequest(BaseModel):
    network_pc_url: str
    run_setup_script: bool = False
    setup_script_path: str = ""


@app.post("/api/comfyui/network-node/activate")
def activate_network_node(req: NetworkNodeActivateRequest):
    """Point ComfyUI routing at a remote CPU network node and optionally run setup.

    Sets COMFYUI_REMOTE_URL at runtime so subsequent generate calls route to
    the network PC. Optionally launches the PowerShell setup script that copies
    GGUF model files and starts ComfyUI on the remote machine.

    Body:
      network_pc_url    — e.g. "http://192.168.1.50:8188"
      run_setup_script  — if true, executes setup_script_path in the background
      setup_script_path — absolute path to a .ps1 setup script (optional)
    """
    url = req.network_pc_url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(400, "network_pc_url must start with http:// or https://")
    comfyui_bridge.set_remote_url(url)
    script_status = "not_requested"
    if req.run_setup_script:
        script_path = req.setup_script_path.strip()
        if not script_path:
            raise HTTPException(400, "setup_script_path is required when run_setup_script is true")
        if not os.path.isfile(script_path):
            raise HTTPException(400, f"Setup script not found: {script_path}")
        if not script_path.lower().endswith(".ps1"):
            raise HTTPException(400, "Only .ps1 setup scripts are supported")
        import subprocess as _sp
        _sp.Popen(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", script_path],
            creationflags=_sp.DETACHED_PROCESS | _sp.CREATE_NEW_PROCESS_GROUP,
        )
        script_status = "launched"
    active = comfyui_bridge._resolve_active_url()
    running = comfyui_bridge.is_comfyui_running()
    return {
        "network_pc_url": url,
        "active_url": active,
        "comfyui_reachable": running,
        "setup_script_status": script_status,
        "message": f"Network node activated → {active}",
    }


# Known ComfyUI workflow IDs accessible via /api/comfyui/generate
_COMFYUI_WORKFLOW_IDS = {
    "text-to-image":          {"description": "FLUX txt2img (GPU local)", "mode": "local"},
    "text-to-image-gguf-cpu": {"description": "FLUX GGUF txt2img for CPU network node", "mode": "remote"},
}


class ComfyGenerateRequest(BaseModel):
    workflow_id: str
    prompt: str
    width: int = 512
    height: int = 512
    steps: int = 8
    network_pc_url: str = ""


@app.get("/api/comfyui/workflows")
def list_comfyui_workflows():
    """List registered ComfyUI workflow IDs that can be submitted via /api/comfyui/generate."""
    remote = comfyui_bridge.get_remote_url()
    return {
        "workflows": [
            {**{"id": wid}, **meta, "remote_url": remote if meta["mode"] == "remote" else ""}
            for wid, meta in _COMFYUI_WORKFLOW_IDS.items()
        ]
    }


@app.post("/api/comfyui/generate")
def comfyui_generate(req: ComfyGenerateRequest):
    """Submit an image generation job by workflow ID.

    workflow_id="text-to-image-gguf-cpu" routes to the configured remote CPU
    node. Optionally set network_pc_url in the request to override at call time.

    Supported workflow IDs: text-to-image, text-to-image-gguf-cpu
    """
    wid = req.workflow_id.strip().lower()
    if wid not in _COMFYUI_WORKFLOW_IDS:
        raise HTTPException(400, f"Unknown workflow_id '{wid}'. "
                                 f"Valid options: {list(_COMFYUI_WORKFLOW_IDS)}")
    if not req.prompt.strip():
        raise HTTPException(400, "prompt must not be empty")

    # Allow per-request remote URL override for CPU workflow
    if wid == "text-to-image-gguf-cpu":
        target_url = req.network_pc_url.strip() or comfyui_bridge.get_remote_url()
        if not target_url:
            raise HTTPException(400,
                "No remote ComfyUI URL configured for text-to-image-gguf-cpu. "
                "Call POST /api/comfyui/network-node/activate first, or pass network_pc_url in this request.")
        if req.network_pc_url.strip():
            comfyui_bridge.set_remote_url(req.network_pc_url.strip())
        result = comfyui_bridge.generate_gguf_cpu_image(
            req.prompt, width=req.width, height=req.height, steps=req.steps
        )
    else:
        result = comfyui_bridge.generate_image(
            req.prompt, width=req.width, height=req.height, steps=req.steps
        )

    if "error" in result:
        raise HTTPException(502, result["error"])
    return {"workflow_id": wid, **result}


@app.get("/api/comfyui/active-model")
def comfyui_get_active_model():
    """Return the name of the active ComfyUI checkpoint/model.

    Tries to discover the model from the most recent history entry that used a
    CheckpointLoaderSimple (or compatible) node. Falls back to the
    COMFYUI_DEFAULT_MODEL env var, then 'Wan 2.1'.
    """
    default_model = os.environ.get("COMFYUI_DEFAULT_MODEL", "Wan 2.1")
    try:
        base_url = comfyui_bridge._resolve_active_url().rstrip("/")
        history: dict = comfyui_bridge._get_json(f"{base_url}/history", timeout=4)
        # history is {prompt_id: {outputs, prompt, ...}}
        # Iterate entries newest-first looking for a checkpoint node
        for entry in reversed(list(history.values())):
            prompt_nodes = (entry.get("prompt") or [None, None, {}])[2] if isinstance(entry.get("prompt"), list) else {}
            for node in prompt_nodes.values():
                class_type = node.get("class_type", "")
                if "checkpoint" in class_type.lower() or "loader" in class_type.lower():
                    ckpt_name = (node.get("inputs") or {}).get("ckpt_name") or \
                                (node.get("inputs") or {}).get("model_name")
                    if ckpt_name:
                        # Strip path/extension for a clean display name
                        import pathlib
                        display = pathlib.Path(ckpt_name).stem
                        return {"model": display, "source": "history", "running": True}
    except Exception:
        pass
    running = comfyui_bridge.is_comfyui_running()
    return {"model": default_model, "source": "default", "running": running}


# ---------- Skills API endpoints ----------

from orchestrator.skills import get_registry as get_skill_registry

@app.get("/api/skills")
def list_skills(category: str = None, enabled_only: bool = False):
    """List all available skills."""
    import json as _json
    from fastapi.responses import JSONResponse as _JR
    registry = get_skill_registry()
    skills = registry.list_skills(category=category, enabled_only=enabled_only)
    # Sanitize surrogate characters that can break UTF-8 serialization
    safe = _json.loads(_json.dumps({"skills": skills, "total": len(skills)}, ensure_ascii=True))
    return _JR(content=safe)

@app.get("/api/skills/{skill_id}")
def get_skill_info(skill_id: str):
    """Get detailed skill info + config schema."""
    registry = get_skill_registry()
    skill = registry.get_skill(skill_id)
    if not skill:
        raise HTTPException(404, f"Skill not found: {skill_id}")
    meta = skill.get_metadata()
    return {
        **meta.to_dict(),
        "current_config": skill.config,
    }

@app.post("/api/skills/{skill_id}/execute")
async def execute_skill(skill_id: str, req: dict = Body(...)):
    """Execute a skill manually with a task dict."""
    registry = get_skill_registry()
    task = req.get("task", req)
    result = await registry.execute_skill(skill_id, task)
    return result

@app.post("/api/skills/{skill_id}/toggle")
def toggle_skill(skill_id: str, req: dict = Body(...)):
    """Enable or disable a skill."""
    registry = get_skill_registry()
    enabled = req.get("enabled", True)
    ok = registry.set_enabled(skill_id, enabled)
    if not ok:
        raise HTTPException(404, f"Skill not found: {skill_id}")
    return {"skill_id": skill_id, "enabled": enabled}

@app.post("/api/skills/{skill_id}/config")
def update_skill_config(skill_id: str, req: dict = Body(...)):
    """Update a skill's runtime config."""
    registry = get_skill_registry()
    ok = registry.update_config(skill_id, req)
    if not ok:
        raise HTTPException(404, f"Skill not found: {skill_id}")
    return {"skill_id": skill_id, "config": registry.get_skill(skill_id).config}

@app.post("/api/skills/reload")
def reload_skills():
    """Reload skill registry (rescan builtin + community folders)."""
    registry = get_skill_registry()
    registry.reload()
    return {"reloaded": True, "total": len(registry.skills)}


class GenerateSkillReq(BaseModel):
    description: str

@app.post("/api/skills/generate")
def api_generate_skill(req: GenerateSkillReq):
    """Generate and install a community skill from a natural-language description."""
    from orchestrator.skillify import generate_skill_from_prompt
    result = generate_skill_from_prompt(req.description)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    get_skill_registry().reload()
    return result

@app.delete("/api/skills/{skill_id}")
def api_delete_skill(skill_id: str):
    """Delete a custom (community) skill. Builtin skills cannot be deleted."""
    from orchestrator.skillify import delete_community_skill
    result = delete_community_skill(skill_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    get_skill_registry().reload()
    return result


# ---------- Agent API endpoints ----------

@app.get("/api/agents")
def list_agents():
    """List all agents — builtins + custom."""
    agents = agent_registry.list_agents()
    # Enrich with avatar info
    from orchestrator import avatar_storage
    for agent in agents:
        if agent.get("avatar_id"):
            avatar = avatar_storage.get_avatar(agent["avatar_id"])
            agent["avatar_name"] = avatar["name"] if avatar else None
            variant = avatar_storage.get_variant_path(agent["avatar_id"], "base")
            agent["avatar_image"] = f"/api/avatars/{agent['avatar_id']}/base" if variant else None
        else:
            agent["avatar_name"] = None
            agent["avatar_image"] = None
    return agents


@app.get("/api/agents/abilities")
def agent_abilities():
    """List ability definitions for UI checkboxes."""
    return agent_registry.get_ability_definitions()


@app.get("/api/agents/policies")
def agent_policies():
    """List content policies for UI dropdown."""
    return agent_registry.get_content_policies()


@app.get("/api/agents/runtimes")
def list_agent_runtimes():
    """List curated industry-standard agent runtimes available in this build."""
    return {
        "runtimes": agent_registry.list_agent_runtimes(enabled_only=True),
        "default": agent_registry.DEFAULT_RUNTIME_ID,
    }


@app.get("/api/agents/runtimes/{runtime_id}")
def get_agent_runtime(runtime_id: str):
    """Get details for a specific agent runtime."""
    rt = agent_registry.get_agent_runtime(runtime_id)
    if not rt:
        raise HTTPException(404, f"Runtime '{runtime_id}' not found")
    return rt


@app.post("/api/agents/runtimes/infer")
def infer_session_runtime(body: dict = Body(...)):
    """Infer which runtime label to show based on active mode and model."""
    mode = body.get("mode", "auto")
    model_id = body.get("model_id")
    return agent_registry.infer_session_runtime(mode, model_id)


@app.get("/api/agents/industry")
def list_industry_agents():
    """List all curated industry-standard agent brands (session provenance).

    The 'comfyui' entry's display_name and badge_text are patched in real-time
    with the name of the currently active ComfyUI checkpoint (if reachable).
    """
    import copy
    agents = copy.deepcopy(_industry_agents.list_agents())
    # Patch comfyui entry with live model name
    default_model = os.environ.get("COMFYUI_DEFAULT_MODEL", "Wan 2.1")
    live_model = default_model
    try:
        base_url = comfyui_bridge._resolve_active_url().rstrip("/")
        history: dict = comfyui_bridge._get_json(f"{base_url}/history", timeout=3)
        for entry in reversed(list(history.values())):
            prompt_nodes = (entry.get("prompt") or [None, None, {}])[2] if isinstance(entry.get("prompt"), list) else {}
            for node in prompt_nodes.values():
                class_type = node.get("class_type", "")
                if "checkpoint" in class_type.lower() or "loader" in class_type.lower():
                    ckpt = (node.get("inputs") or {}).get("ckpt_name") or \
                           (node.get("inputs") or {}).get("model_name")
                    if ckpt:
                        import pathlib
                        live_model = pathlib.Path(ckpt).stem
                        break
            if live_model != default_model:
                break
    except Exception:
        pass
    for agent in agents:
        if agent.get("id") == "comfyui":
            agent["display_name"] = live_model
            agent["badge_text"] = live_model
            agent["session_indicator"] = f"Powered by {live_model} via ComfyUI"
    return {
        "agents": agents,
        "count": len(_industry_agents.INDUSTRY_AGENTS),
    }


@app.get("/api/agents/industry/{agent_id}")
def get_industry_agent(agent_id: str):
    """Get a specific industry agent by ID."""
    agent = _industry_agents.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, f"Industry agent not found: {agent_id}")
    return agent


@app.post("/api/agents/session-brand")
def resolve_session_brand(body: dict = Body(...)):
    """Resolve the session-level brand / provenance indicator."""
    result = _industry_agents.resolve_session_brand(
        agent_id=body.get("agent_id"),
        muscle=body.get("muscle"),
        model_tag=body.get("model_tag"),
        cloud_provider=body.get("cloud_provider"),
    )
    return result


@app.get("/api/hermes/manifest")
def get_hermes_manifest():
    """Return Hermes Agent bridge availability and version info."""
    return hermes_bridge.get_manifest()


@app.get("/api/machine/openclaw/manifest")
def get_openclaw_bridge_manifest():
    """Return the OpenClaw bridge contract for THE MACHINE visuals."""
    return openclaw_bridge.get_bridge_manifest()


@app.get("/api/machine/openclaw/capabilities")
def get_openclaw_capability_map():
    """Return how OpenClaw capabilities map to existing THE MACHINE visuals."""
    return {"capabilities": openclaw_bridge.get_capability_map()}


@app.get("/api/machine/openclaw/events/schema")
def get_openclaw_event_schema():
    """Return canonical OpenClaw bridge events consumed by MACHINE visuals."""
    return {"events": openclaw_bridge.get_event_schema()}


@app.get("/api/machine/openclaw/settings/defaults")
def get_openclaw_default_settings():
    """Return default OpenClaw bridge runtime settings."""
    return openclaw_bridge.get_default_settings()


_OPENCLAW_TEMPLATE_RUNS: dict[str, dict] = {}


@app.get("/api/machine/openclaw/template")
def get_openclaw_canonical_template():
    """Return the canonical complex workflow template for wrapper integration."""
    return openclaw_bridge.get_canonical_workflow_template()


@app.get("/api/machine/openclaw/template/events")
def get_openclaw_canonical_template_events(project_id: str | None = None, flow_id: str | None = None, fail_node_id: str | None = None):
    """Return deterministic OpenClaw events for the canonical complex workflow."""
    events = openclaw_bridge.build_canonical_workflow_events(
        project_id=project_id,
        flow_id=flow_id,
        fail_node_id=fail_node_id,
    )
    return {
        "events": events,
        "validation": openclaw_bridge.validate_event_sequence(events),
    }


@app.post("/api/machine/openclaw/template/run")
def run_openclaw_canonical_template(body: dict | None = Body(default=None)):
    """Create a concrete template run payload that THE MACHINE UI can consume."""
    import uuid as _openclaw_uuid

    body = body or {}
    requested_project_id = body.get("project_id")
    requested_flow_id = body.get("flow_id")
    project_id = requested_project_id or f"openclaw-{str(_openclaw_uuid.uuid4())[:8]}"
    flow_id = requested_flow_id or f"flow-{project_id}"
    fail_node_id = body.get("fail_node_id")
    template = openclaw_bridge.get_canonical_workflow_template()
    template["project_id"] = project_id
    template["flow_id"] = flow_id
    events = openclaw_bridge.build_canonical_workflow_events(
        project_id=project_id,
        flow_id=flow_id,
        fail_node_id=fail_node_id,
    )
    validation = openclaw_bridge.validate_event_sequence(events)
    run = {
        "flow_id": flow_id,
        "project_id": project_id,
        "template": template,
        "events": events,
        "validation": validation,
        "status": "failed" if fail_node_id else "completed",
        "launch_url": f"/pages/the-machine-project-v2.html?id={project_id}&runtime=openclaw&flow_id={flow_id}",
        "created_at": time.time(),
    }
    _OPENCLAW_TEMPLATE_RUNS[flow_id] = run
    return run


@app.get("/api/machine/openclaw/runs/{flow_id}")
def get_openclaw_template_run(flow_id: str):
    """Return a previously created OpenClaw template run."""
    run = _OPENCLAW_TEMPLATE_RUNS.get(flow_id)
    if not run:
        raise HTTPException(404, f"OpenClaw run not found: {flow_id}")
    return run


@app.post("/api/machine/openclaw/events/validate")
def validate_openclaw_events(body: dict = Body(...)):
    """Validate a bridge event or event sequence against the OpenClaw schema."""
    if "events" in body:
        events = body.get("events") or []
        return openclaw_bridge.validate_event_sequence(events)
    if "event" in body:
        return openclaw_bridge.validate_event(body.get("event") or {})
    return openclaw_bridge.validate_event(body)


@app.get("/api/agents/{agent_id}")
def get_agent(agent_id: str):
    """Get a single agent's details."""
    agent = agent_registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return agent


class CreateAgentReq(BaseModel):
    name: str
    description: str = ""
    system_prompt: str = ""
    personality: dict | None = None
    avatar_id: str | None = None
    abilities: list[str] | None = None
    content_policy: str = "standard"
    routing_mode: str = "auto"


@app.post("/api/agents")
def create_agent(req: CreateAgentReq):
    """Create a new custom agent."""
    result = agent_registry.create_agent(
        name=req.name,
        description=req.description,
        system_prompt=req.system_prompt,
        personality=req.personality,
        avatar_id=req.avatar_id,
        abilities=req.abilities,
        content_policy=req.content_policy,
        routing_mode=req.routing_mode,
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


class UpdateAgentReq(BaseModel):
    name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    personality: dict | None = None
    avatar_id: str | None = None
    abilities: list[str] | None = None
    content_policy: str | None = None
    routing_mode: str | None = None


@app.put("/api/agents/{agent_id}")
def update_agent(agent_id: str, req: UpdateAgentReq):
    """Update a custom agent's fields."""
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    result = agent_registry.update_agent(agent_id, **updates)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.delete("/api/agents/{agent_id}")
def delete_agent(agent_id: str):
    """Delete a custom agent."""
    result = agent_registry.delete_agent(agent_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


class GeneratePersonalityReq(BaseModel):
    description: str


@app.post("/api/agents/generate")
def generate_personality(req: GeneratePersonalityReq):
    """AI-generate a personality from a description. Returns editable profile."""
    result = personality_engine.generate_personality(req.description)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


# ---------- Hardware / Model API endpoints ----------

@app.get("/api/registry")
def model_registry_info():
    """Get model registry summary."""
    return model_registry.registry_summary()


@app.get("/api/registry/{task}")
def models_for_task(task: str):
    """Get available models for a task, annotated with hardware compatibility."""
    return hardware_selector.get_available_for_task(task)


@app.post("/api/registry/reload")
def reload_model_registry():
    """Rescan data/models/ directory for user-added model.json configs.
    
    Drop a model.json into data/models/<provider>/<model-name>/ and call
    this endpoint — the model appears in every list, dropdown, and dashboard
    with zero code changes.
    """
    result = model_registry.reload_external_models()
    return {
        "status": "ok",
        **result,
    }


# ---------- Avatar serving ----------

@app.get("/api/avatars/{avatar_id}/{variant}")
def serve_avatar(avatar_id: str, variant: str = "base"):
    """Serve avatar variant images."""
    from orchestrator import avatar_storage
    path = avatar_storage.get_variant_path(avatar_id, variant)
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Avatar variant not found")
    return FileResponse(path)


# ---------- Cloud providers & API key management ----------

@app.get("/api/providers")
def list_providers():
    """List all cloud providers with API key status (never exposes actual keys)."""
    providers = api_key_manager.list_providers()
    # Enrich with model counts per provider
    for p in providers:
        p["model_count"] = len(model_registry.get_models_by_provider(p["provider"]))
    return providers


@app.post("/api/providers/{provider}/key")
def set_provider_key(provider: str, body: dict, _auth=Depends(require_api_key)):
    """Set an API key for a cloud provider.  Body: {"key": "sk-..."}"""
    key = body.get("key", "").strip()
    if not key:
        raise HTTPException(400, "key is required")
    api_key_manager.set_key(provider, key)
    # Auto-enable the backend now that it has a key
    backend_registry.update_backend(provider, {"enabled": True})
    print(f"[Backends] Auto-enabled '{provider}' backend (API key set)")
    # Auto-disable the backend since key is gone
    backend_registry.update_backend(provider, {"enabled": False})
    print(f"[Backends] Auto-disabled '{provider}' backend (API key removed)")
    return {"provider": provider, "removed": removed}


@app.get("/api/providers/{provider}/key-status")
def get_provider_key_status(provider: str):
    """Get masked key status for a provider (safe to expose)."""
    masked = api_key_manager.get_masked_key(provider)
    return {
        "provider": provider,
        "has_key": masked is not None,
        "masked": masked,
        "source": api_key_manager._key_source(provider),
    }


@app.post("/api/providers/{provider}/test")
def test_provider_connection(provider: str):
    """Test if a provider's API key works by attempting a health check."""
    provider = provider.lower()
    
    # Check if key exists
    if not api_key_manager.has_key(provider):
        return {
            "provider": provider,
            "status": "no_key",
            "message": "No API key configured for this provider",
            "online": False,
        }
    
    # Try to test the backend connection
    from orchestrator.backends import registry
    result = registry.health_check(provider)
    
    return {
        "provider": provider,
        "status": "success" if result.get("online") else "failed",
        "message": result.get("error", "Connection successful") if not result.get("online") else "Connection successful",
        "online": result.get("online", False),
        "latency_ms": result.get("latency_ms"),
    }


@app.get("/api/providers/{provider}/models")
def models_by_provider(provider: str):
    """Get all models from a specific cloud provider."""
    return model_registry.get_models_by_provider(provider)


# ---------- Cloud model filtering ----------

@app.get("/api/models/cloud")
def cloud_models_list(task: str | None = None):
    """List all cloud models, optionally filtered by task."""
    return model_registry.get_cloud_models(task)


@app.get("/api/models/local")
def local_models_list(task: str | None = None):
    """List all local models, optionally filtered by task."""
    return model_registry.get_local_models(task)


@app.get("/api/models/filter")
def filter_models_endpoint(
    task: str | None = None,
    location: str | None = None,
    provider: str | None = None,
    max_cost: float | None = None,
    min_context: int | None = None,
    specialization: str | None = None,
    status: str | None = None,
):
    """Filter models by multiple criteria (task, location, provider, cost, context, specialization)."""
    return model_registry.filter_models(
        task=task, location=location, provider=provider,
        max_cost_input=max_cost, min_context=min_context,
        specialization=specialization, status=status,
    )


@app.get("/api/models/specializations")
def list_specializations():
    """Get all unique specialization tags for filter dropdowns."""
    return model_registry.get_all_specializations()


@app.get("/api/models/all")
def all_models():
    """Get every model in the registry (local + cloud) with full metadata."""
    return list(model_registry.MODEL_REGISTRY.values())


# ---------- R&D Dashboard / Benchmark endpoints ----------

@app.get("/api/dashboard/benchmarks")
def dashboard_benchmarks():
    """Full benchmark database for all tracked models."""
    return model_benchmark_db.get_benchmarks()


@app.get("/api/dashboard/rankings")
def dashboard_rankings(category: str = "overall", location: str | None = None):
    """Rank models by score category. Optional location filter (local/cloud)."""
    return model_benchmark_db.get_rankings(category, location)


@app.get("/api/dashboard/recommend")
def dashboard_recommend(task: str = "overall", budget: str = "any"):
    """Top 3 model recommendations for a task + budget combo."""
    return model_benchmark_db.get_task_recommendation(task, budget)


@app.get("/api/dashboard/cost")
def dashboard_cost(tokens: int = 1000):
    """Cost comparison across all models for N output tokens."""
    return model_benchmark_db.get_cost_comparison(tokens)


@app.get("/api/dashboard/matrix")
def dashboard_matrix():
    """Feature heatmap matrix — all models × all categories."""
    return model_benchmark_db.get_feature_matrix()


@app.get("/api/dashboard/categories")
def dashboard_categories():
    """Available score categories for filtering/sorting."""
    return model_benchmark_db.get_categories()


@app.get("/api/dashboard/model/{model_id}")
def dashboard_model_detail(model_id: str):
    """Detailed benchmark data for a single model."""
    m = model_benchmark_db.get_model_detail(model_id)
    if not m:
        raise HTTPException(404, f"Model not found: {model_id}")
    return m


# ---------- Smart Model Selector endpoints ----------

@app.get("/api/smart/select")
def smart_select(
    task: str = "general",
    priority: str = "balanced",
    location: str = "local",
    vram_limit: float = 12.0,
):
    """Select the best model for a task + priority + location combo."""
    from orchestrator.smart_selector import select_best_model
    return select_best_model(task=task, priority=priority, location=location, vram_limit_gb=vram_limit)


@app.get("/api/smart/options")
def smart_options(
    task: str = "general",
    priority: str = "balanced",
    location: str = "any",
    vram_limit: float = 12.0,
):
    """Ranked list of all models for a task with scores."""
    from orchestrator.smart_selector import get_model_options
    return get_model_options(task=task, priority=priority, location=location, vram_limit_gb=vram_limit)


@app.get("/api/smart/explain")
def smart_explain(
    task: str = "general",
    priority: str = "balanced",
    location: str = "local",
):
    """Human-readable explanation of model ranking for a task."""
    from orchestrator.smart_selector import explain_selection
    return {"explanation": explain_selection(task, priority, location)}


@app.get("/api/smart/config")
def smart_config():
    """Current smart routing configuration."""
    from orchestrator.config import SMART_ROUTING, SMART_ROUTING_PRIORITY
    from orchestrator.smart_selector import TASK_SCORE_MAP, PRIORITY_SPEED_WEIGHT
    return {
        "enabled": SMART_ROUTING,
        "priority": SMART_ROUTING_PRIORITY,
        "task_types": list(TASK_SCORE_MAP.keys()),
        "priority_modes": list(PRIORITY_SPEED_WEIGHT.keys()),
    }


@app.post("/api/smart/config")
def update_smart_config(enabled: bool | None = None, priority: str | None = None):
    """Toggle smart routing on/off and set priority mode."""
    import orchestrator.config as cfg
    if enabled is not None:
        cfg.SMART_ROUTING = enabled
    if priority is not None:
        valid = ("quality", "balanced", "speed", "budget")
        if priority not in valid:
            raise HTTPException(400, f"priority must be one of {valid}")
        cfg.SMART_ROUTING_PRIORITY = priority
    return {"enabled": cfg.SMART_ROUTING, "priority": cfg.SMART_ROUTING_PRIORITY}


@app.get("/dashboard")
def serve_dashboard():
    """Serve the R&D Dashboard standalone page."""
    return FileResponse(
        os.path.join(FRONTEND_DIR, "dashboard.html"),
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# ---------- Multimodal Benchmark endpoints ----------

@app.get("/api/dashboard/multimodal")
def dashboard_multimodal():
    """Full multimodal benchmark database (image, video, voice, music, 3D)."""
    return multimodal_benchmark_db.get_multimodal_benchmarks()


@app.get("/api/dashboard/multimodal/rankings")
def dashboard_multimodal_rankings(
    category: str = "quality",
    modality: str | None = None,
    location: str | None = None,
):
    """Rank multimodal models by score, with modality + location filters."""
    return multimodal_benchmark_db.get_multimodal_rankings(category, modality, location)


@app.get("/api/dashboard/multimodal/modalities")
def dashboard_modalities():
    """Available modality types."""
    return multimodal_benchmark_db.get_modalities()


@app.get("/api/dashboard/multimodal/categories")
def dashboard_multimodal_categories():
    """Score categories for multimodal models."""
    return multimodal_benchmark_db.get_multimodal_categories()


@app.get("/api/dashboard/multimodal/matrix")
def dashboard_multimodal_matrix():
    """Heatmap matrix for all multimodal models."""
    return multimodal_benchmark_db.get_multimodal_matrix()


@app.get("/api/dashboard/multimodal/model/{model_id}")
def dashboard_multimodal_detail(model_id: str):
    """Detailed data for a single multimodal model."""
    m = multimodal_benchmark_db.get_multimodal_detail(model_id)
    if not m:
        raise HTTPException(404, f"Multimodal model not found: {model_id}")
    return m


@app.get("/api/dashboard/alerts")
def dashboard_alerts(modality: str | None = None):
    """Upcoming model news, leaks, and release signals."""
    return multimodal_benchmark_db.get_alerts(modality)


@app.get("/api/dashboard/freshness")
def dashboard_freshness():
    """Data freshness report — staleness per model + last sweep date."""
    return multimodal_benchmark_db.get_freshness_report()


@app.post("/api/dashboard/research/sweep")
def dashboard_research_sweep():
    """Run a full model research sweep (checks HuggingFace, Ollama, freshness + arXiv, GitHub)."""
    from orchestrator import model_research_agent
    return model_research_agent.run_sweep_for_api()


# ---------- Staging DB endpoints ----------

@app.get("/api/dashboard/staging")
def staging_list(status: str = "pending", modality: str = "", limit: int = 50):
    """List staging candidates."""
    from orchestrator.staging_db import get_db
    db = get_db()
    if status == "all":
        return db.get_all(limit=limit)
    if modality:
        return db.get_pending(modality=modality, limit=limit)
    return db.get_all(status=status, limit=limit)


@app.get("/api/dashboard/staging/stats")
def staging_stats():
    """Get staging candidate counts by status."""
    from orchestrator.staging_db import get_db
    return get_db().get_stats()


@app.post("/api/dashboard/staging/{candidate_id}/approve")
def staging_approve(candidate_id: int):
    """Approve a staging candidate for production."""
    from orchestrator.staging_db import get_db
    db = get_db()
    ok = db.approve(candidate_id)
    if not ok:
        raise HTTPException(404, "Candidate not found or already reviewed")
    return {"status": "approved", "id": candidate_id}


@app.post("/api/dashboard/staging/{candidate_id}/reject")
def staging_reject(candidate_id: int, body: dict = {}):
    """Reject a staging candidate."""
    from orchestrator.staging_db import get_db
    db = get_db()
    reason = body.get("reason", "")
    ok = db.reject(candidate_id, reason=reason)
    if not ok:
        raise HTTPException(404, "Candidate not found or already reviewed")
    return {"status": "rejected", "id": candidate_id}


@app.post("/api/dashboard/staging/{candidate_id}/defer")
def staging_defer(candidate_id: int):
    """Defer a staging candidate for later review."""
    from orchestrator.staging_db import get_db
    db = get_db()
    ok = db.defer(candidate_id)
    if not ok:
        raise HTTPException(404, "Candidate not found")
    return {"status": "deferred", "id": candidate_id}


# ---------- Task Stack endpoints ----------

@app.get("/api/stack")
def get_stack():
    """Return the full task stack — every role with its current model assignment."""
    return config.get_stack()


@app.put("/api/stack/{role}")
def update_stack(role: str, body: dict):
    """Update a single role in the task stack.
    Body: { model, location, ollama_tag?, cloud_model_id? }
    """
    if role not in config.TASK_STACK:
        raise HTTPException(404, f"Unknown role: {role}")
    ok = config.update_stack_role(
        role=role,
        model=body.get("model", ""),
        location=body.get("location", "local"),
        ollama_tag=body.get("ollama_tag"),
        cloud_model_id=body.get("cloud_model_id"),
    )
    if not ok:
        raise HTTPException(400, "Failed to update stack role")
    return {"status": "ok", "role": role, "updated": config.TASK_STACK[role]}


# ---------- Agentic Loop endpoints ----------

class AgenticReq(BaseModel):
    task: str
    agent: str = "gwen"
    max_iterations: int = 10
    think: bool = False
    system: str = ""
    include_history: bool = False


@app.post("/api/workflow/agentic_loop")
def run_agentic_loop(req: AgenticReq):
    """Run the Phase 3 agentic loop — iterative task solving."""
    system = req.system
    system += build_history_context(req.include_history)
    result = agentic_loop.run_loop(
        task=req.task,
        agent=req.agent,
        max_iterations=req.max_iterations,
        think=req.think,
        system_override=system,
    )
    return result


from fastapi.responses import StreamingResponse as _StreamingResponse

@app.post("/api/workflow/agentic_loop/stream")
async def run_agentic_loop_stream(req: AgenticReq):
    """
    SSE streaming version of /api/workflow/agentic_loop.
    Each step is emitted immediately as the agent completes it.

    Client receives:
        data: {"type": "step", "step": {iteration, action, text, tool_call?, tool_result?, ...}}\\n\\n
        data: {"type": "done", "status": str, "final_result": str, "iterations": int, ...}\\n\\n
        data: {"type": "error", "content": str}\\n\\n
    """
    system = req.system
    system += build_history_context(req.include_history)

    def _gen():
        for event in agentic_loop.run_loop_stream(
            task=req.task,
            agent=req.agent,
            max_iterations=req.max_iterations,
            think=req.think,
            system_override=system,
        ):
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return _StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/set-model")
async def set_muscle_model(request: Request):
    """
    Hot-swap the model used by a specific muscle without restarting.
    Body: {"muscle": "GWEN", "model": "qwen2.5-coder:14b"}
    Omit muscle to update CEO_LOCAL_MODEL (the primary router model).
    """
    body = await request.json()
    muscle = body.get("muscle", "").strip().upper()
    model_tag = body.get("model", "").strip()
    if not model_tag:
        return JSONResponse({"error": "model tag required"}, status_code=400)

    from orchestrator import muscles as _muscles
    from orchestrator.config import MUSCLES as _MUSCLES
    if muscle and muscle in _MUSCLES:
        _MUSCLES[muscle]["model"] = model_tag
        return {"ok": True, "muscle": muscle, "model": model_tag}
    else:
        # No muscle specified — update CEO_LOCAL_MODEL
        config.CEO_LOCAL_MODEL = model_tag
        return {"ok": True, "muscle": "CEO", "model": model_tag}


@app.get("/api/ollama/models")
def list_ollama_models():
    """List all models — merges live Ollama models with MODEL_REGISTRY local models."""
    import urllib.request as _urlreq
    from orchestrator.model_registry import MODEL_REGISTRY

    # 1. Fetch live Ollama models (these are actually pulled / ready to run)
    ollama_by_name: dict[str, dict] = {}
    try:
        req = _urlreq.Request(f"{config.OLLAMA_URL}/api/tags")
        with _urlreq.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        for m in data.get("models", []):
            name = m.get("name", "")
            ollama_by_name[name] = {
                "name": name,
                "size_gb": round(m.get("size", 0) / 1e9, 1),
                "modified": m.get("modified_at", ""),
                "family": m.get("details", {}).get("family", ""),
                "params": m.get("details", {}).get("parameter_size", ""),
                "source": "ollama",
                "pulled": True,
            }
    except Exception:
        pass

    # 2. Merge local/ollama models from MODEL_REGISTRY that aren't already pulled
    for mid, entry in MODEL_REGISTRY.items():
        if entry.get("backend") != "ollama":
            continue
        if entry.get("location") == "cloud":
            continue
        ollama_name = (entry.get("config") or {}).get("ollama_model", mid)
        # Skip if already present in live Ollama (live data is more accurate)
        already = any(
            oname == ollama_name or oname.startswith(ollama_name + ":")
            or ollama_name == oname.replace(":latest", "")
            for oname in ollama_by_name
        )
        if already:
            continue
        ollama_by_name[ollama_name] = {
            "name": ollama_name,
            "size_gb": round(entry.get("vram_mb", 0) / 1000, 1),
            "modified": "",
            "family": entry.get("provider") or "",
            "params": "",
            "source": "registry",
            "pulled": False,
            "registry_id": mid,
            "display_name": entry.get("name", ollama_name),
            "quality": entry.get("quality", 0),
            "task": entry.get("task", ""),
            "notes": entry.get("notes", ""),
        }

    # 3. Merge llamacpp-backend models (e.g. PrismML server)
    for mid, entry in MODEL_REGISTRY.items():
        if entry.get("backend") != "llamacpp":
            continue
        endpoint = (entry.get("config") or {}).get("endpoint", "")
        # Health-check: try to reach the server
        is_online = False
        if endpoint:
            try:
                hc_req = _urlreq.Request(f"{endpoint}/health")
                with _urlreq.urlopen(hc_req, timeout=3) as _hresp:
                    is_online = True
            except Exception:
                pass
        ollama_by_name[mid] = {
            "name": mid,
            "size_gb": round(entry.get("vram_mb", 0) / 1000, 1),
            "modified": "",
            "family": entry.get("provider") or "",
            "params": (entry.get("config") or {}).get("parameter_size", ""),
            "source": "llamacpp",
            "pulled": is_online,
            "registry_id": mid,
            "display_name": entry.get("name", mid),
            "quality": entry.get("quality", 0),
            "task": entry.get("task", ""),
            "notes": entry.get("notes", ""),
        }

    # Return pulled models first, then registry models sorted by quality
    pulled = sorted([m for m in ollama_by_name.values() if m.get("pulled")], key=lambda m: m["name"])
    registry = sorted([m for m in ollama_by_name.values() if not m.get("pulled")], key=lambda m: -m.get("quality", 0))
    return pulled + registry


# ---------- Persistent Memory (Structured) ----------

import uuid as _uuid

MEMORY_FILE = os.path.join(PROJECT_ROOT, "data", "memory.json")

# ── Category definitions for salience scoring ──
_HIGH_SALIENCE_CATEGORIES = {"identity", "preference", "instruction", "relationship", "ongoing_project"}
_MEDIUM_SALIENCE_CATEGORIES = {"personal", "technical", "context"}
_LOW_SALIENCE_CATEGORIES = {"casual", "temporary", "unknown"}

def _make_fact(text: str, category: str = "unknown", entity: str = "", attribute: str = "",
               confidence: float = 0.7, source_chat: str = "", scope: str = "user") -> dict:
    """Create a structured memory fact object."""
    now = time.time()
    # Auto-score salience based on category
    if category in _HIGH_SALIENCE_CATEGORIES:
        salience = 0.9
    elif category in _MEDIUM_SALIENCE_CATEGORIES:
        salience = 0.6
    else:
        salience = 0.3
    return {
        "id": str(_uuid.uuid4())[:8],
        "text": text.strip(),
        "scope": scope,
        "category": category,
        "entity": entity,
        "attribute": attribute,
        "confidence": round(confidence, 2),
        "salience": round(salience, 2),
        "source_chat": source_chat,
        "created_at": now,
        "updated_at": now,
        "last_used_at": None,
        "access_count": 0,
        "user_confirmed": False,
    }


def _load_memory() -> dict:
    """Load persistent memory from disk."""
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Migrate old flat facts to structured format
            migrated = False
            for i, f in enumerate(data.get("facts", [])):
                if isinstance(f, dict) and "id" not in f:
                    data["facts"][i] = _make_fact(
                        text=f.get("text", ""),
                        category="identity" if "name" in f.get("text", "").lower() else "unknown",
                        confidence=0.8,
                    )
                    migrated = True
            if migrated:
                _save_memory(data)
            return data
        except (json.JSONDecodeError, IOError):
            pass
    return {"facts": [], "updated_at": None}

def _save_memory(mem: dict):
    """Save persistent memory to disk."""
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    mem["updated_at"] = time.time()
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(mem, f, indent=2)


def _find_conflicting_fact(facts: list, new_entity: str, new_attribute: str) -> int | None:
    """Find index of existing fact with same entity+attribute (conflict resolution)."""
    if not new_entity or not new_attribute:
        return None
    ne = new_entity.lower().strip()
    na = new_attribute.lower().strip()
    for i, f in enumerate(facts):
        if f.get("entity", "").lower().strip() == ne and f.get("attribute", "").lower().strip() == na:
            return i
    return None


def _retrieve_relevant_facts(facts: list, query: str, max_facts: int = 15) -> list:
    """Retrieve only facts relevant to the current query (not full dump).
    Uses keyword overlap + salience + recency scoring."""
    if not facts or not query:
        return facts[:max_facts]  # fallback: return all up to limit

    query_words = set(re.findall(r'\w{3,}', query.lower()))
    if not query_words:
        return facts[:max_facts]

    scored = []
    now = time.time()
    for f in facts:
        text_words = set(re.findall(r'\w{3,}', f.get("text", "").lower()))
        entity_words = set(re.findall(r'\w{3,}', f.get("entity", "").lower()))
        attr_words = set(re.findall(r'\w{3,}', f.get("attribute", "").lower()))

        # Keyword relevance (0-1)
        all_fact_words = text_words | entity_words | attr_words
        overlap = len(query_words & all_fact_words)
        relevance = min(overlap / max(len(query_words), 1), 1.0)

        # Salience score from fact metadata (0-1)
        salience = f.get("salience", 0.5)

        # Recency bonus — facts used/created recently get a small boost
        age_days = (now - f.get("updated_at", f.get("created_at", now))) / 86400
        recency = max(0, 1.0 - (age_days / 90))  # decays over 90 days

        # High-salience facts (identity, preferences) always included even if no keyword match
        always_include = f.get("category", "") in _HIGH_SALIENCE_CATEGORIES

        # Combined score
        score = (relevance * 0.5) + (salience * 0.35) + (recency * 0.15)
        if always_include:
            score = max(score, 0.4)  # floor for important facts

        scored.append((score, f))

    # Sort by score descending, return top N
    scored.sort(key=lambda x: x[0], reverse=True)
    # Include facts above a minimum relevance threshold, or always-include facts
    threshold = 0.15
    return [f for score, f in scored[:max_facts] if score >= threshold]


@app.get("/api/memory")
def get_memory(_user=Depends(_auth_guard)):
    """Return all persistent memory facts."""
    return _load_memory()

@app.post("/api/memory")
def add_memory_facts(body: dict, _user=Depends(_auth_guard)):
    """Add structured facts to persistent memory.
    Body: {"facts": [{"text": "...", "category": "...", "entity": "...", "attribute": "...", "confidence": 0.8, "source_chat": "..."}]}
    Or simple: {"facts": ["my name is andrew", ...]}
    """
    new_facts = body.get("facts", [])
    if not new_facts:
        raise HTTPException(400, "No facts provided")
    mem = _load_memory()
    existing_texts = set(f.get("text", "").lower().strip() for f in mem["facts"])
    added = 0
    updated = 0
    for fact in new_facts:
        # Accept both string and structured dict
        if isinstance(fact, str):
            fact = {"text": fact.strip()}
        text = fact.get("text", "").strip()
        if not text:
            continue

        entity = fact.get("entity", "")
        attribute = fact.get("attribute", "")
        category = fact.get("category", "unknown")
        confidence = fact.get("confidence", 0.7)
        source_chat = fact.get("source_chat", "")

        # Check for conflict (same entity+attribute = update, not duplicate)
        conflict_idx = _find_conflicting_fact(mem["facts"], entity, attribute)
        if conflict_idx is not None:
            old = mem["facts"][conflict_idx]
            # Update existing fact with newer info
            old["text"] = text
            old["confidence"] = max(old.get("confidence", 0), confidence)
            old["updated_at"] = time.time()
            old["source_chat"] = source_chat or old.get("source_chat", "")
            if category != "unknown":
                old["category"] = category
            updated += 1
            continue

        # Dedup by text
        if text.lower() in existing_texts:
            continue

        # Create structured fact
        new_fact = _make_fact(
            text=text, category=category, entity=entity, attribute=attribute,
            confidence=confidence, source_chat=source_chat,
        )
        mem["facts"].append(new_fact)
        existing_texts.add(text.lower())
        added += 1

    if added or updated:
        _save_memory(mem)
    return {"status": "ok", "added": added, "updated": updated, "total": len(mem["facts"])}

@app.delete("/api/memory")
def clear_memory():
    """Clear all persistent memory facts."""
    _save_memory({"facts": [], "updated_at": None})
    return {"status": "cleared"}

@app.delete("/api/memory/fact")
def delete_memory_fact(body: dict):
    """Delete a specific fact by index or id. Body: {"index": 0} or {"id": "abc12345"}"""
    mem = _load_memory()
    fact_id = body.get("id")
    idx = body.get("index")
    if fact_id:
        # Find by id
        for i, f in enumerate(mem["facts"]):
            if f.get("id") == fact_id:
                gpu_name, vram_total_mb, vram_free_mb = _detect_gpu_vram_mb()
                vram_total_gb = round(vram_total_mb / 1024, 1) if vram_total_mb else 0.0
                gpu = {"name": gpu_name, "vram_total_mb": vram_total_mb, "vram_free_mb": vram_free_mb,
                       "detected": vram_total_mb > 0, "tier": 2 if vram_total_mb > 0 else 1}
        for i, f in enumerate(mem["facts"]):
            if f.get("id") == fact_id:
                idx = i
                break
        else:
            raise HTTPException(400, f"Fact id not found: {fact_id}")
    if idx is None or idx < 0 or idx >= len(mem["facts"]):
        raise HTTPException(400, "Invalid fact index")
    mem["facts"][idx]["user_confirmed"] = True
    mem["facts"][idx]["confidence"] = min(1.0, mem["facts"][idx].get("confidence", 0.7) + 0.2)
    mem["facts"][idx]["salience"] = min(1.0, mem["facts"][idx].get("salience", 0.5) + 0.2)
    _save_memory(mem)
    return {"status": "confirmed", "fact": mem["facts"][idx]}
    idx = body.get("index")
    mem = _load_memory()
    if idx is None or idx < 0 or idx >= len(mem["facts"]):
        raise HTTPException(400, "Invalid fact index")
    removed = mem["facts"].pop(idx)
    _save_memory(mem)
    return {"status": "removed", "fact": removed, "total": len(mem["facts"])}


# ---------- Project-Scoped Memory (Structured) ----------

def _get_project_memory_file(project_id: str) -> str:
    """Get path to project memory file."""
    return os.path.join(PROJECT_ROOT, "data", "projects", project_id, "memory.json")

def _load_project_memory(project_id: str) -> dict:
    """Load project-scoped memory from disk."""
    path = _get_project_memory_file(project_id)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Migrate old flat facts
            migrated = False
            for i, f_obj in enumerate(data.get("facts", [])):
                if isinstance(f_obj, dict) and "id" not in f_obj:
                    data["facts"][i] = _make_fact(
                        text=f_obj.get("text", ""), category="context",
                        confidence=0.7, scope="project",
                    )
                    migrated = True
            if migrated:
                _save_project_memory(project_id, data)
            return data
        except (json.JSONDecodeError, IOError):
            pass
    return {"facts": [], "project_id": project_id, "created_at": None, "updated_at": None}

def _save_project_memory(project_id: str, mem: dict):
    """Save project-scoped memory to disk."""
    path = _get_project_memory_file(project_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mem["project_id"] = project_id
    mem["updated_at"] = time.time()
    if not mem.get("created_at"):
        mem["created_at"] = time.time()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(mem, f, indent=2)

@app.get("/api/memory/project/{project_id}")
def get_project_memory(project_id: str):
    """Return project-scoped memory facts."""
    return _load_project_memory(project_id)

@app.post("/api/memory/project/{project_id}")
def add_project_memory_facts(project_id: str, body: dict):
    """Add structured facts to project memory. Accepts strings or structured dicts."""
    new_facts = body.get("facts", [])
    if not new_facts:
        raise HTTPException(400, "No facts provided")
    mem = _load_project_memory(project_id)
    existing_texts = set(f.get("text", "").lower().strip() for f in mem["facts"])
    added = 0
    updated = 0
    for fact in new_facts:
        if isinstance(fact, str):
            fact = {"text": fact.strip()}
        text = fact.get("text", "").strip()
        if not text:
            continue

        entity = fact.get("entity", "")
        attribute = fact.get("attribute", "")
        category = fact.get("category", "context")
        confidence = fact.get("confidence", 0.7)
        source_chat = fact.get("source_chat", "")

        # Conflict resolution
        conflict_idx = _find_conflicting_fact(mem["facts"], entity, attribute)
        if conflict_idx is not None:
            old = mem["facts"][conflict_idx]
            old["text"] = text
            old["confidence"] = max(old.get("confidence", 0), confidence)
            old["updated_at"] = time.time()
            if category != "unknown":
                old["category"] = category
            updated += 1
            continue

        if text.lower() in existing_texts:
            continue

        new_fact = _make_fact(
            text=text, category=category, entity=entity, attribute=attribute,
            confidence=confidence, source_chat=source_chat, scope="project",
        )
        mem["facts"].append(new_fact)
        existing_texts.add(text.lower())
        added += 1

    if added or updated:
        _save_project_memory(project_id, mem)
    return {"status": "ok", "added": added, "updated": updated, "total": len(mem["facts"]), "project_id": project_id}

@app.delete("/api/memory/project/{project_id}")
def clear_project_memory(project_id: str):
    """Clear all project memory facts."""
    _save_project_memory(project_id, {"facts": []})
    return {"status": "cleared", "project_id": project_id}

@app.delete("/api/memory/project/{project_id}/fact")
def delete_project_memory_fact(project_id: str, body: dict):
    """Delete a specific project fact by index or id."""
    mem = _load_project_memory(project_id)
    fact_id = body.get("id")
    idx = body.get("index")
    if fact_id:
        for i, f in enumerate(mem["facts"]):
            if f.get("id") == fact_id:
                idx = i
                break
        else:
            raise HTTPException(400, f"Fact id not found: {fact_id}")
    if idx is None or idx < 0 or idx >= len(mem["facts"]):
        raise HTTPException(400, "Invalid fact index")
    removed = mem["facts"].pop(idx)
    _save_project_memory(project_id, mem)
    return {"status": "removed", "fact": removed, "total": len(mem["facts"]), "project_id": project_id}


# ---------- File Management ----------

from fastapi import UploadFile, File, Form


@app.get("/api/workspace")
def get_workspace():
    """Get current workspace path and file listing."""
    return file_manager.list_files()


@app.put("/api/workspace")
def set_workspace(body: dict):
    """Set workspace folder path. Body: {"path": "C:/Users/..."}"""
    path = body.get("path", "").strip()
    if not path:
        raise HTTPException(400, "path is required")
    return file_manager.set_workspace(path)


@app.get("/api/workspace/files")
def list_workspace_files(subfolder: str = ""):
    """List files in workspace or a subfolder."""
    return file_manager.list_files(subfolder)


@app.post("/api/workspace/upload")
async def upload_file(file: UploadFile = File(...), subfolder: str = Form("")):
    """Upload a file to the workspace."""
    content = await file.read()
    result = file_manager.save_upload(file.filename, content, subfolder)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@app.post("/api/workspace/project")
def create_project_folder(body: dict):
    """Create a project subfolder in the workspace. Body: {"folder": "my_project"}"""
    folder = body.get("folder", "").strip()
    if not folder:
        raise HTTPException(400, "folder is required")
    ws = file_manager.get_workspace()
    project_path = os.path.normpath(os.path.join(ws, folder))
    if not project_path.startswith(os.path.normpath(ws)):
        raise HTTPException(403, "Path outside workspace")
    os.makedirs(project_path, exist_ok=True)
    return {"status": "created", "path": project_path}


# ---------- /api/upload — Chat attachment upload (Phase B: uploads/ folder + DB persistence) ----------

import uuid as _uuid
import shutil as _shutil

_UPLOAD_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_UPLOAD_ALLOWED_TYPES = {
    "text/plain", "text/markdown", "text/csv",
    "application/json", "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "image/png", "image/jpeg", "image/gif", "image/webp",
}

# Phase B: persisted uploads directory (relative to backend/)
_UPLOADS_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(_UPLOADS_DIR, exist_ok=True)

# Phase B: uploads table in validation.db
_UPLOADS_DB = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".dev", "validation", "validation.db")

def _uploads_db_init():
    """Create uploads table if it doesn't exist."""
    try:
        conn = sqlite3.connect(_UPLOADS_DB)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS uploads (
                file_id TEXT PRIMARY KEY,
                filename TEXT,
                content_type TEXT,
                size INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        conn.close()
    except Exception:
        pass  # DB unavailable — Phase B degrades gracefully

_uploads_db_init()


@app.post("/api/upload")
async def upload_attachment(file: UploadFile = File(...)):
    """Accept a file upload (Phase B): store in uploads/ folder, persist metadata to DB, return file_id.
    Max 10 MB. Whitelisted content types only.
    """
    # Content type check
    content_type = (file.content_type or "").split(";")[0].strip()
    if content_type not in _UPLOAD_ALLOWED_TYPES:
        raise HTTPException(415, f"Unsupported file type: {content_type}. Allowed: {sorted(_UPLOAD_ALLOWED_TYPES)}")

    # Read with size enforcement
    data = await file.read(_UPLOAD_MAX_BYTES + 1)
    if len(data) > _UPLOAD_MAX_BYTES:
        raise HTTPException(413, f"File too large. Max {_UPLOAD_MAX_BYTES // (1024 * 1024)} MB.")

    # Safe filename: strip path components
    safe_name = os.path.basename(file.filename or "upload")
    safe_name = re.sub(r"[^\w\-. ]", "_", safe_name)[:120]

    file_id = str(_uuid.uuid4())
    suffix = os.path.splitext(safe_name)[1] or ".bin"
    dest_path = os.path.join(_UPLOADS_DIR, f"{file_id}{suffix}")

    # Write to uploads/ folder
    with open(dest_path, "wb") as fout:
        fout.write(data)

    created_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    # Persist metadata to uploads table
    try:
        conn = sqlite3.connect(_UPLOADS_DB)
        conn.execute(
            "INSERT INTO uploads (file_id, filename, content_type, size, created_at) VALUES (?,?,?,?,?)",
            (file_id, safe_name, content_type, len(data), created_at)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # DB unavailable — file is still stored, metadata skipped

    return {
        "file_id": file_id,
        "filename": safe_name,
        "content_type": content_type,
        "size": len(data),
    }


@app.get("/api/upload/{file_id}")
def get_upload_info(file_id: str):
    """Get metadata for an uploaded file by file_id (Phase B: reads from DB)."""
    # Sanitize file_id — must be a valid UUID
    try:
        import uuid as _uuid_check
        _uuid_check.UUID(file_id)
    except ValueError:
        raise HTTPException(400, "Invalid file_id format")
    try:
        conn = sqlite3.connect(_UPLOADS_DB)
        row = conn.execute(
            "SELECT file_id, filename, content_type, size, created_at FROM uploads WHERE file_id=?",
            (file_id,)
        ).fetchone()
        conn.close()
        if row:
            return {"file_id": row[0], "filename": row[1], "content_type": row[2],
                    "size": row[3], "created_at": row[4]}
    except Exception:
        pass
    raise HTTPException(404, "file_id not found")


@app.delete("/api/workspace/files")
def delete_workspace_file(body: dict):
    """Delete a file. Body: {"path": "/full/path/to/file"}"""
    path = body.get("path", "").strip()
    if not path:
        raise HTTPException(400, "path is required")
    result = file_manager.delete_file(path)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@app.post("/api/workspace/read")
def read_workspace_file(body: dict):
    """Read text content from a file. Body: {"path": "/full/path"}"""
    path = body.get("path", "").strip()
    if not path:
        raise HTTPException(400, "path is required")
    result = file_manager.read_file_text(path)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@app.get("/api/workspace/serve/{filename:path}")
def serve_workspace_file(filename: str):
    """Serve a workspace file by name (for displaying images/media in UI)."""
    ws = file_manager.get_workspace()
    safe_path = os.path.normpath(os.path.join(ws, filename))
    if not safe_path.startswith(os.path.normpath(ws)):
        raise HTTPException(403, "Path outside workspace")
    if not os.path.isfile(safe_path):
        raise HTTPException(404, "File not found")
    return FileResponse(safe_path)


# ══════════════════════════════════════════════════════════════════════════════
# BACKENDS — Unified multi-backend management (Phase 19)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/backends")
def list_backends():
    """List all backends with config, status, and API key info."""
    return backend_registry.list_backends()


@app.get("/api/backends/health")
def backends_health():
    """Health check all enabled backends."""
    return backend_registry.health_check_all()


@app.get("/api/backends/routing")
def get_routing_config():
    """Get current routing preferences (smart_routing, priority_mode, etc.)."""
    return backend_registry.get_routing_config()


@app.post("/api/backends/routing")
def update_routing_config(body: dict):
    """Update routing preferences. Body: {smart_routing, priority_mode, local_first, fallback_enabled}."""
    return backend_registry.update_routing_config(body)


@app.get("/api/backends/{name}")
def get_backend(name: str):
    """Get config for a specific backend."""
    result = backend_registry.get_backend(name)
    if not result:
        raise HTTPException(404, f"Backend not found: {name}")
    return result


@app.post("/api/backends/{name}")
def update_backend(name: str, body: dict):
    """Update backend config. Body: {enabled, endpoint, priority, display_name, api_key}."""
    # Handle API key separately — not stored in backends_config.json
    api_key = body.pop("api_key", None)
    if api_key is not None:
        api_key = api_key.strip()
        if api_key:
            api_key_manager.set_key(name, api_key)
            # Auto-enable since key is now set
            if "enabled" not in body:
                body["enabled"] = True
                print(f"[Backends] Auto-enabled '{name}' backend (API key set via backend update)")
        # else: empty string = clear key (leave as-is for now)

    result = backend_registry.update_backend(name, body)
    if not result:
        raise HTTPException(404, f"Backend not found: {name}")
    return result


@app.get("/api/backends/{name}/health")
def backend_health(name: str):
    """Health check a specific backend."""
    return backend_registry.health_check(name)


@app.get("/api/backends/{name}/models")
def backend_models(name: str):
    """List models available on a specific backend."""
    return backend_registry.list_models(name)


@app.post("/api/backends/test")
def test_backend_connection(body: dict):
    """Test a backend connection before saving. Body: {backend, endpoint?, api_key?}."""
    backend_name = body.get("backend", "").strip()
    if not backend_name:
        raise HTTPException(400, "backend name is required")

    # Temporarily set key if provided (won't persist)
    temp_key = body.get("api_key", "").strip()
    if temp_key:
        api_key_manager.set_key(backend_name, temp_key, persist=False)

    # Override endpoint temporarily for test
    temp_endpoint = body.get("endpoint", "").strip()
    if temp_endpoint:
        original = backend_registry._config.get("backends", {}).get(backend_name, {}).get("endpoint")
        backend_registry._config.setdefault("backends", {}).setdefault(backend_name, {})["endpoint"] = temp_endpoint

    # Force-enable so health check runs
    was_enabled = backend_registry._config.get("backends", {}).get(backend_name, {}).get("enabled")
    backend_registry._config.setdefault("backends", {}).setdefault(backend_name, {})["enabled"] = True

    result = backend_registry.health_check(backend_name)

    # Restore original state (don't persist test changes)
    if temp_endpoint and original is not None:
        backend_registry._config["backends"][backend_name]["endpoint"] = original
    backend_registry._config.setdefault("backends", {}).setdefault(backend_name, {})["enabled"] = was_enabled or False

    return result


# ---------- Performance Tuning ----------

@app.get("/api/perf")
def get_perf():
    """Get current performance profile, targets, and presets."""
    return config.get_perf_profile()


@app.put("/api/perf/fast")
def toggle_fast_mode(body: dict):
    """Toggle fast mode. Body: {"enabled": true/false}"""
    enabled = body.get("enabled", False)
    config.set_fast_mode(enabled)
    return {"status": "ok", "fast_mode": config.FAST_MODE, "params": dict(config.PERF)}


@app.put("/api/perf/param")
def update_perf_param(body: dict):
    """Update a single perf param. Body: {"key": "num_ctx", "value": 8192}"""
    key = body.get("key", "")
    value = body.get("value")
    if not key:
        raise HTTPException(400, "key is required")
    ok = config.update_perf(key, value)
    if not ok:
        raise HTTPException(400, f"Unknown param: {key}")
    return {"status": "ok", "key": key, "value": value}


@app.get("/api/perf/benchmark")
def perf_benchmark(model: str = ""):
    """Quick benchmark: send a short prompt, measure tokens/sec.
    If model is given, benchmark that model.
    Otherwise benchmark whatever is currently loaded in VRAM.
    """
    import urllib.request as _urlreq
    # Determine which model to benchmark
    if not model:
        # Use whatever is currently in VRAM
        try:
            ps_req = _urlreq.Request(f"{config.OLLAMA_URL}/api/ps")
            with _urlreq.urlopen(ps_req, timeout=5) as resp:
                ps_data = json.loads(resp.read())
            running = ps_data.get("models", [])
            if running:
                model = running[0]["name"]
            else:
                return {"error": "No model loaded in VRAM. Mount a model first.", "tokens_per_sec": 0}
        except Exception as e:
            return {"error": f"Cannot reach Ollama: {e}", "tokens_per_sec": 0}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Say hello in exactly 5 words."}],
        "stream": False,
        "think": False,
        "options": config.get_ollama_options(),
    }
    data = json.dumps(payload).encode()
    t0 = time.time()
    try:
        req = _urlreq.Request(
            f"{config.OLLAMA_URL}/api/chat",
            data=data, headers={"Content-Type": "application/json"},
        )
        with _urlreq.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
        elapsed = time.time() - t0
        eval_count = result.get("eval_count", 0)
        eval_duration = result.get("eval_duration", 0)  # nanoseconds
        prompt_eval_count = result.get("prompt_eval_count", 0)
        prompt_eval_duration = result.get("prompt_eval_duration", 0)

        tokens_per_sec = (eval_count / (eval_duration / 1e9)) if eval_duration > 0 else 0
        prompt_tokens_per_sec = (prompt_eval_count / (prompt_eval_duration / 1e9)) if prompt_eval_duration > 0 else 0

        return {
            "model": model.replace(":latest", ""),
            "tokens_generated": eval_count,
            "tokens_per_sec": round(tokens_per_sec, 1),
            "prompt_tokens": prompt_eval_count,
            "prompt_tokens_per_sec": round(prompt_tokens_per_sec, 1),
            "total_time_sec": round(elapsed, 2),
            "eval_count": eval_count,
            "eval_duration": eval_duration,
            "meets_target": tokens_per_sec >= 16,
            "target_tokens_per_sec": 16,
            "response": result.get("message", {}).get("content", ""),
        }
    except Exception as e:
        return {"error": str(e), "tokens_per_sec": 0, "meets_target": False}


# ---------- /api/speculative — llama-server speculative decoding (step-001/002) ----------
_LLAMA_SERVER_PORT   = 11435
_LLAMA_SERVER_URL    = f"http://127.0.0.1:{_LLAMA_SERVER_PORT}"
_LLAMA_MODEL_MAIN    = r"C:\Users\Home\.ollama\models\blobs\sha256-6e9f90f02bb3b39b59e81916e8cfce9deb45aeaeb9a54a5be4414486b907dc1e"
_LLAMA_SERVER_EXE    = r"F:\LOCAL MODEL - MASTER\tools\prismml-llama\bin\llama-server.exe"


def _llama_server_alive() -> bool:
    """Return True if llama-server is responding on _LLAMA_SERVER_PORT."""
    try:
        import urllib.request as _ur
        import urllib.error as _ue
        try:
            _ur.urlopen(f"{_LLAMA_SERVER_URL}/health", timeout=2).read()
            return True
        except _ue.HTTPError:
            # 503 during model load still means the server is up.
            return True
    except Exception:
        return False


@app.post("/api/speculative")
async def speculative_inference(body: dict):
    """
    Run speculative decoding inference via llama-server on port 11435.
    Body: {"prompt": "...", "model": "deepseek-r1:14b", "draft_model": "deepseek-r1:1.5b",
           "max_tokens": 256, "temperature": 0.7}
    Returns: {"response": "...", "tokens_per_sec": N, "acceptance_rate": F,
              "route_time": ms, "exec_time": ms, "model_not_loaded": bool}
    """
    import time as _time
    import json as _json
    import urllib.request as _ur
    import urllib.error as _ue

    route_start = _time.monotonic()

    if not _llama_server_alive():
        return {
            "model_not_loaded": True,
            "message": "llama-server is not running on port 11435",
            "start_hint": {
                "exe": _LLAMA_SERVER_EXE,
                "model": _LLAMA_MODEL_MAIN,
                "example_cmd": (
                    f'"{_LLAMA_SERVER_EXE}" --model "{_LLAMA_MODEL_MAIN}" '
                    f"--port {_LLAMA_SERVER_PORT} --draft-n 16 --draft-p-min 0.75"
                )
            },
            "route_time": round((_time.monotonic() - route_start) * 1000),
        }

    prompt      = str(body.get("prompt", "")).strip()
    max_tokens  = int(body.get("max_tokens", 256))
    temperature = float(body.get("temperature", 0.7))

    if not prompt:
        raise HTTPException(400, "prompt is required")

    payload = _json.dumps({
        "prompt": prompt,
        "n_predict": max_tokens,
        "temperature": temperature,
        "stream": False,
    }).encode()

    exec_start = _time.monotonic()
    try:
        req = _ur.Request(
            f"{_LLAMA_SERVER_URL}/completion",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _ur.urlopen(req, timeout=120) as resp:
            result = _json.loads(resp.read().decode())
    except _ue.URLError as exc:
        raise HTTPException(502, f"llama-server error: {exc}")

    exec_time  = round((_time.monotonic() - exec_start) * 1000)
    route_time = round((_time.monotonic() - route_start) * 1000)

    timings         = result.get("timings", {})
    tokens_per_sec  = round(float(timings.get("predicted_per_second", 0)), 2)
    # llama.cpp draft stats field names vary by version; try multiple keys.
    acceptance_rate = float(
        timings.get("draft_p_mean",
        timings.get("draft_acceptance_rate",
        timings.get("speculative_acceptance_rate", 0)))
    )

    return {
        "model_not_loaded": False,
        "response":         result.get("content", ""),
        "tokens_per_sec":   tokens_per_sec,
        "acceptance_rate":  round(acceptance_rate, 4),
        "route_time":       route_time,
        "exec_time":        exec_time,
        "stop_type":        result.get("stop_type", ""),
        "tokens_predicted": result.get("tokens_predicted", 0),
    }


@app.get("/api/speculative/status")
def speculative_status():
    """Return whether llama-server is alive on port 11435."""
    alive = _llama_server_alive()
    return {
        "alive": alive,
        "port": _LLAMA_SERVER_PORT,
        "model_main": _LLAMA_MODEL_MAIN,
        "server_exe": _LLAMA_SERVER_EXE,
    }


@app.get("/api/ollama/running")
def ollama_running_models():
    """Get currently loaded Ollama models with VRAM usage details."""
    import urllib.request as _urlreq
    try:
        req = _urlreq.Request(f"{config.OLLAMA_URL}/api/ps")
        with _urlreq.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        models = []
        for m in data.get("models", []):
            total = m.get("size", 0)
            vram = m.get("size_vram", 0)
            # Ollama sometimes reports size_vram=0 even when fully GPU-loaded
            effective_vram = vram if vram > 0 else total
            models.append({
                "name": m.get("name", ""),
                "size_gb": round(total / 1e9, 1),
                "vram_gb": round(effective_vram / 1e9, 1),
                "expires": m.get("expires_at", ""),
                "processor": m.get("details", {}).get("processor", "gpu"),
            })
        return {"models": models}
    except Exception as e:
        return {"models": [], "error": str(e)}


# ---------- Model Management ----------
from fastapi.responses import StreamingResponse

@app.post("/api/ollama/pull")
def ollama_pull_model(body: dict):
    """Pull/download an Ollama model with SSE progress stream.
    Body: {"name": "qwen3:14b"}
    Returns: text/event-stream with JSON progress objects:
      {"status":"downloading","completed":N,"total":N,"percent":N}
      {"status":"done","model":"name"}
      {"status":"error","error":"msg"}
    """
    import urllib.request as _urlreq

    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(400, "name is required")

    payload = json.dumps({"name": name, "stream": True}).encode()

    def _event_stream():
        try:
            req = _urlreq.Request(
                f"{config.OLLAMA_URL}/api/pull",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with _urlreq.urlopen(req, timeout=1200) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    status = obj.get("status", "")
                    completed = obj.get("completed", 0)
                    total = obj.get("total", 0)
                    # Calculate percent safely
                    percent = round(completed / total * 100, 1) if total else 0
                    event = {
                        "status": status,
                        "model": name,
                        "completed": completed,
                        "total": total,
                        "percent": percent,
                        "digest": obj.get("digest", ""),
                    }
                    yield f"data: {json.dumps(event)}\n\n"
            yield f"data: {json.dumps({'status': 'done', 'model': name, 'percent': 100})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'status': 'error', 'model': name, 'error': str(e)})}\n\n"

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.delete("/api/ollama/model")
def ollama_delete_model(body: dict):
    """Delete an Ollama model. Body: {"name": "model:tag"}"""
    import urllib.request as _urlreq
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    payload = json.dumps({"name": name}).encode()
    try:
        req = _urlreq.Request(
            f"{config.OLLAMA_URL}/api/delete",
            data=payload, headers={"Content-Type": "application/json"},
            method="DELETE",
        )
        with _urlreq.urlopen(req, timeout=30) as resp:
            resp.read()
        return {"status": "deleted", "model": name}
    except Exception as e:
        return {"status": "error", "model": name, "error": str(e)}


@app.get("/api/ollama/model/{name:path}")
def ollama_model_info(name: str):
    """Get detailed info for an Ollama model."""
    import urllib.request as _urlreq
    payload = json.dumps({"name": name}).encode()
    try:
        req = _urlreq.Request(
            f"{config.OLLAMA_URL}/api/show",
            data=payload, headers={"Content-Type": "application/json"},
        )
        with _urlreq.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return {
            "name": name,
            "modelfile": data.get("modelfile", ""),
            "parameters": data.get("parameters", ""),
            "template": data.get("template", ""),
            "details": data.get("details", {}),
            "model_info": data.get("model_info", {}),
        }
    except Exception as e:
        return {"name": name, "error": str(e)}


# ---------- Help System ----------

THE_MACHINE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HELP_DIR = os.path.join(THE_MACHINE_DIR, "help")

@app.get("/api/help")
def help_index():
    """Return the help document index."""
    if not os.path.isdir(HELP_DIR):
        os.makedirs(HELP_DIR, exist_ok=True)
    docs = []
    for fname in sorted(os.listdir(HELP_DIR)):
        if fname.endswith(".md"):
            fpath = os.path.join(HELP_DIR, fname)
            with open(fpath, "r", encoding="utf-8") as f:
                first_line = f.readline().strip().lstrip("# ")
            docs.append({
                "id": fname.replace(".md", ""),
                "title": first_line or fname,
                "file": fname,
            })
    return {"docs": docs}


@app.get("/api/help/{doc_id}")
def help_doc(doc_id: str):
    """Return a single help document's content."""
    safe_id = os.path.basename(doc_id)
    fpath = os.path.join(HELP_DIR, f"{safe_id}.md")
    if not os.path.isfile(fpath):
        raise HTTPException(404, f"Help doc not found: {doc_id}")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return {"id": doc_id, "content": content}


# ---------- Phase 21: Reasoning Models, Agent Execution, Suggestions ----------

@app.get("/api/models/reasoning")
def list_reasoning_models():
    """List all reasoning models with availability status."""
    return {"models": reasoning_models.list_all()}


@app.get("/api/models/reasoning/installed")
def list_installed_reasoning():
    """List only installed/accessible reasoning models."""
    return {"models": reasoning_models.get_installed()}


@app.get("/api/models/reasoning/native")
def list_native_reasoning():
    """List reasoning models that support native deep thinking (capability-based filter).
    
    Returns models that have native_reasoning=True, regardless of whether they are
    local (e.g., DeepSeek-R1) or cloud (e.g., o3, o1-preview). Useful for dynamic
    UI hints about which models support 'Think' mode.
    """
    native_models = reasoning_models.get_native_reasoning_models()
    return {
        "models": native_models,
        "count": len(native_models),
        "description": "Models that support native deep reasoning (not orchestrated)",
    }


@app.post("/api/models/reasoning/suggest")
def suggest_reasoning_model(body: dict = Body(...)):
    """Suggest a reasoning model for a query."""
    query = body.get("query", "")
    if not query:
        raise HTTPException(400, "query is required")
    return suggestion_engine.suggest_for_query(query)


@app.post("/api/models/reasoning/tips")
def reasoning_model_tips(body: dict = Body(...)):
    """Get performance tips for a model, optionally tailored to a query."""
    model_id = body.get("model_id", "")
    query = body.get("query")
    if not model_id:
        raise HTTPException(400, "model_id is required")
    return {"tips": suggestion_engine.get_model_tips(model_id, query)}


@app.post("/api/models/reasoning/install")
def install_reasoning_model(body: dict = Body(...)):
    """Install a local reasoning model via Ollama pull with SSE progress stream."""
    model_id = body.get("model_id", "")
    model = reasoning_models.get_model(model_id)
    if not model:
        raise HTTPException(404, f"Unknown model: {model_id}")
    if model.get("location") != "local":
        raise HTTPException(400, "Only local models can be installed via Ollama")
    tag = model.get("ollama_tag", model_id)
    import urllib.request

    payload = json.dumps({"name": tag, "stream": True}).encode()

    def _event_stream():
        try:
            req = urllib.request.Request(
                f"{config.OLLAMA_URL}/api/pull",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=1200) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    completed = obj.get("completed", 0)
                    total = obj.get("total", 0)
                    percent = round(completed / total * 100, 1) if total else 0
                    event = {
                        "status": obj.get("status", ""),
                        "model": tag,
                        "completed": completed,
                        "total": total,
                        "percent": percent,
                        "digest": obj.get("digest", ""),
                    }
                    yield f"data: {json.dumps(event)}\n\n"
            yield f"data: {json.dumps({'status': 'done', 'model': tag, 'percent': 100})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'status': 'error', 'model': tag, 'error': str(e)})}\n\n"

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/backends/{name}/discover")
def discover_backend_models(name: str):
    """Discover models from a provider API (real-time fetch)."""
    models = backend_registry.discover_models(name)
    return {"backend": name, "models": models, "count": len(models)}


# ─────────────────────────────────────────────────────────────────────────────
# MODEL INVENTORY: full discovery + quarantine status
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/models/inventory")
def get_model_inventory():
    """Full model inventory: curated + auto-discovered Ollama models.

    Each entry carries:
      - ``discovery_source``: "curated" | "auto"
      - ``verification_status``: "verified" | "candidate" | "inferred"
      - ``routing_allowed``: bool — False = quarantined
      - ``hw_badge``: "fits" | "exceeds" | "cloud"
      - ``installed``: bool
    """
    inventory = reasoning_models.get_inventory()
    return {
        "models": inventory,
        "count": len(inventory),
        "curated": sum(1 for m in inventory if m.get("discovery_source") == "curated"),
        "discovered": sum(1 for m in inventory if m.get("discovery_source") == "auto"),
        "routable": sum(1 for m in inventory if m.get("routing_allowed")),
        "quarantined": sum(1 for m in inventory if not m.get("routing_allowed")),
    }


@app.get("/api/models/inventory/quarantined")
def list_quarantined_models():
    """Return only models in quarantine (discovered but not yet routing-eligible)."""
    return {"models": reasoning_models.get_discovered_unverified()}


@app.post("/api/models/inventory/{model_id}/promote")
def promote_model(model_id: str):
    """Mark a model as routing-eligible (after external capability profiling).

    This is a manual override — future versions will accept profiling
    results and update the underlying validation database.
    """
    from urllib.parse import unquote
    model_id = unquote(model_id)
    ok = reasoning_models.promote_to_verified(model_id)
    if not ok:
        raise HTTPException(404, f"Model not found: {model_id}")
    return {"promoted": True, "model_id": model_id, "routing_allowed": True}


@app.post("/api/models/inventory/{model_id}/quarantine")
def quarantine_model(model_id: str):
    """Re-quarantine a model (remove routing eligibility)."""
    from urllib.parse import unquote
    model_id = unquote(model_id)
    reasoning_models.demote_to_quarantine(model_id)
    return {"quarantined": True, "model_id": model_id, "routing_allowed": False}


@app.get("/api/models/inventory/{model_id}/status")
def model_inventory_status(model_id: str):
    """Return capability and routing status for a single model."""
    from urllib.parse import unquote
    model_id = unquote(model_id)
    for m in reasoning_models.get_inventory():
        if m["id"] == model_id or m.get("ollama_tag") == model_id:
            return m
    raise HTTPException(404, f"Model not found in inventory: {model_id}")


from fastapi.responses import StreamingResponse


def _resolve_agent_model(agent_id: str) -> tuple[str | None, dict | None]:
    """Resolve a reasoning_model override from an industry agent id.

    Returns:
        (model_tag, None)  — success: use this model
        (None, error_dict) — failure: surface this to the frontend

    Special cases:
        • claude_code → reject with use_claude_code_mode (has dedicated endpoint)
        • requires_api_key=True agents → reject with agent_requires_key
        • detection_method == "cli_executable" → check PATH for the CLI binary
        • agents with ollama_tags → match against live Ollama install list
        • no match found → reject with agent_not_installed
    """
    import shutil
    agent = _industry_agents.get_agent(agent_id)
    if not agent:
        return None, {"error": "agent_not_found", "message": f"Unknown agent id: {agent_id}"}

    # Claude Code has its own dedicated mode — don't route via execute-stream
    if agent_id == "claude_code":
        return None, {
            "error": "use_claude_code_mode",
            "message": "Claude Code has a dedicated mode. Select 'Claude Code' from the chat mode picker instead.",
        }

    # API-key-required agents cannot run locally
    if agent.get("requires_api_key"):
        return None, {
            "error": "agent_requires_key",
            "key_provider": agent.get("api_key_provider", ""),
            "display_name": agent.get("display_name", agent_id),
            "message": f"{agent.get('display_name', agent_id)} requires an API key from {agent.get('api_key_provider', 'the provider')}.",
        }

    # CLI-executable / bridge agents (e.g. Hermes Agent) — detected via the
    # bridge module, NOT by Ollama model tags. These are full agent frameworks.
    if agent.get("detection_method") == "cli_executable":
        cli_bin = agent.get("cli_executable", "")
        install_hint = agent.get("install_hint", f"Install {agent.get('display_name', agent_id)}")
        # Primary check: ask the dedicated bridge module (imports source directly)
        bridge_available = False
        if cli_bin == "hermes":
            try:
                manifest = hermes_bridge.get_manifest()
                bridge_available = manifest.get("available", False)
            except Exception:
                bridge_available = False
        # Fallback: check PATH for the CLI binary
        if not bridge_available:
            bridge_available = bool(cli_bin and shutil.which(cli_bin))
        if bridge_available:
            # Signal success with a "cli:" sentinel — downstream routing uses this
            # to delegate to the bridge instead of an Ollama model tag.
            return f"cli:{cli_bin}", None
        return None, {
            "error": "agent_not_installed",
            "display_name": agent.get("display_name", agent_id),
            "ollama_tags": [],
            "install_hint": install_hint,
            "message": f"{agent.get('display_name', agent_id)} not available. Install with: {install_hint}",
        }

    # Match against live Ollama models
    ollama_tags = agent.get("ollama_tags", [])
    if ollama_tags:
        try:
            import requests as _req
            resp = _req.get("http://localhost:11434/api/tags", timeout=3)
            if resp.ok:
                installed = {m.get("name", "").split(":")[0].lower() for m in resp.json().get("models", [])}
                for tag_pattern in ollama_tags:
                    if tag_pattern.lower() in installed:
                        # Find the full tag (with version suffix)
                        full_tags = [
                            m.get("name", "") for m in resp.json().get("models", [])
                            if m.get("name", "").lower().startswith(tag_pattern.lower())
                        ]
                        resolved = full_tags[0] if full_tags else tag_pattern
                        return resolved, None
        except Exception:
            pass
        return None, {
            "error": "agent_not_installed",
            "display_name": agent.get("display_name", agent_id),
            "ollama_tags": ollama_tags,
            "message": f"{agent.get('display_name', agent_id)} is not installed. Run: ollama pull {ollama_tags[0]}",
        }

    # Agent has api_model_ids but no local path — treat as cloud-only
    return None, {
        "error": "agent_cloud_only",
        "display_name": agent.get("display_name", agent_id),
        "message": f"{agent.get('display_name', agent_id)} is a cloud-only agent and cannot run locally.",
    }


@app.post("/api/agents/execute-stream")
async def execute_agent_stream(body: dict = Body(...)):
    """Execute agent with streaming SSE updates."""
    query = body.get("query", "")
    messages = body.get("messages", [])
    reasoning_model = body.get("reasoning_model", "")
    agent_id = str(body.get("agent_id", "")).strip()
    openclaw_agent_id = str(body.get("openclaw_agent_id", "")).strip()
    active_backend = str(body.get("active_backend", "")).strip()
    thinking = bool(body.get("thinking", False))
    mode = str(body.get("mode", "agent")).strip().lower()
    mode = {"native": "agent", "think": "agent", "autosolve": "agent"}.get(mode, mode)
    if mode not in {"agent", "plan"}:
        mode = "agent"
    skills = body.get("skills")
    strategy = body.get("strategy", "e-labs")
    session_id = str(body.get("session_id", "")).strip()

    if not query:
        raise HTTPException(400, "query is required")

    # Agent runtime override — resolve model from industry agent id if provided.
    # OpenCLAW is a gateway/workflow runtime, not a local Ollama model, so keep
    # the selected reasoning model and use it only for MACHINE handoff policy.
    original_agent_id = agent_id
    machine_allowed, machine_policy = _can_launch_machine_from_agent(mode, original_agent_id, active_backend)
    _pending_agent_error: dict | None = None  # surfaced as first SSE event if agent unavailable
    if agent_id and agent_id != "openclaw":
        resolved_model, _agent_err = _resolve_agent_model(agent_id)
        if _agent_err:
            # Store the error to emit as the first SSE event — do NOT silently swallow it.
            # The frontend 'agent_error' handler (processAgentEvent) will display the
            # correct message (e.g. "not installed", "requires API key") with install hints.
            _pending_agent_error = _agent_err
            agent_id = ""  # fall back to standard session brand
        else:
            reasoning_model = resolved_model  # override with agent's model

    if not reasoning_model:
        suggestion = reasoning_models.suggest_model(query or "", available_only=True)
        reasoning_model = suggestion.get("suggestion") or ""
    if not reasoning_model:
        raise HTTPException(400, "reasoning_model is required")

    # Resolve session brand for UI provenance indicator
    session_brand = _industry_agents.resolve_session_brand(
        agent_id=agent_id or None,
        model_tag=reasoning_model,
        muscle=mode,
    )

    from orchestrator.agents.executor import execute_agent

    async def event_stream():
        heartbeat_interval_s = 10.0
        idle_timeout_s = 45.0
        last_emit_at = time.time()

        # Emit session provenance as first event so the UI can update the indicator immediately
        yield f"event: session_brand\ndata: {json.dumps({'session_brand': session_brand})}\n\n"
        last_emit_at = time.time()

        # If the selected agent runtime could not be resolved (not installed, needs API key, etc.),
        # surface the error immediately so the UI shows the correct message instead of hanging.
        if _pending_agent_error:
            yield f"event: agent_error\ndata: {json.dumps(_pending_agent_error)}\n\n"
            yield f"event: done\ndata: {json.dumps({'content': '', 'method': 'agent_error', 'summary': _pending_agent_error.get('message', 'Agent unavailable.')})}\n\n"
            return

        # ── Hermes Agent runtime ──────────────────────────────────────────────
        # When the user selected "Hermes Agent" in the runtime dropdown and it
        # resolved to "cli:hermes", delegate entirely to the Hermes bridge which
        # runs the full NousResearch AIAgent loop and streams SSE events.
        if reasoning_model.startswith("cli:"):
            hermes_model_name = "gwen:latest"   # default to GWEN; resolved in bridge
            # Allow the user to have pre-selected a different model via reasoning_model
            # by passing it as cli:MODEL — strip the prefix.
            candidate = reasoning_model[4:].strip()
            if candidate and not candidate.startswith("hermes"):
                hermes_model_name = candidate

            # Pick the best available Ollama model for Hermes to use — prefer
            # qwen3.6 (high reasoning) if installed, fall back to gwen.
            try:
                import requests as _req
                _tags_resp = _req.get("http://localhost:11434/api/tags", timeout=3)
                if _tags_resp.ok:
                    _installed_names = [m.get("name", "") for m in _tags_resp.json().get("models", [])]
                    _prefer = ["qwen3.6:35b-a3b-q8_0", "gwen:latest", "qwen3:4b"]
                    for _pref in _prefer:
                        if _pref in _installed_names:
                            hermes_model_name = _pref
                            break
            except Exception:
                pass

            yield f"event: step_start\ndata: {json.dumps({'step': 0, 'label': f'Hermes Agent starting (model: {hermes_model_name})', 'tool': ''})}\n\n"

            async for update in hermes_bridge.execute_hermes(
                query=query,
                messages=messages,
                model=hermes_model_name,
                session_id=session_id,
            ):
                yield f"event: {update.get('type', 'error')}\ndata: {json.dumps(update)}\n\n"
                last_emit_at = time.time()
            return

        # Agent-mode handoff: bind chat session to a MACHINE project and emit launch metadata.
        # Policy: only The Machine/OpenCLAW runtime may launch MACHINE workflows.
        if mode == "agent" and session_id and machine_allowed:
            yield f"event: machine_handoff_pending\ndata: {json.dumps({'session_id': session_id, 'runtime': 'openclaw', 'openclaw_agent_id': openclaw_agent_id or None, 'active_backend': active_backend or 'standard_local', 'runtime_status': machine_policy})}\n\n"
            project_id = _agent_machine_projects.get(session_id)
            project = machine_engine.get_project(project_id) if project_id else None
            if project_id and not project:
                _agent_machine_projects.pop(session_id, None)

            if not project:
                project = machine_engine.create_project(query, session_id=session_id)
                project_id = project.get("project_id")
                persisted_project = machine_engine.get_project(project_id) if project_id else None
                if project.get("clarification_needed") or not persisted_project:
                    project = machine_engine.create_project(query, session_id=session_id, tasks=[query])
                    project_id = project.get("project_id")
                    persisted_project = machine_engine.get_project(project_id) if project_id else None
                if persisted_project and project_id:
                    _agent_machine_projects[session_id] = project_id
                    project = persisted_project
                else:
                    yield f"event: machine_handoff_blocked\ndata: {json.dumps({'reason': 'MACHINE project manifest was not created', 'mode': mode, 'agent_id': original_agent_id or 'openclaw', 'runtime_status': machine_policy})}\n\n"
                    project = None

            if project and project.get("project_id"):
                launch_url = f"/pages/the-machine-project-v2.html?id={project['project_id']}"
                should_auto_open = session_id not in _agent_machine_auto_opened
                if should_auto_open:
                    _agent_machine_auto_opened.add(session_id)
                handoff_payload = {
                    "project_id": project["project_id"],
                    "launch_url": launch_url,
                    "auto_open": should_auto_open,
                    "session_id": session_id,
                    "runtime": "openclaw",
                    "openclaw_agent_id": openclaw_agent_id or None,
                    "active_backend": active_backend or "standard_local",
                    "selected_backend": active_backend or "standard_local",
                    "handoff_reason": "agent_first_task" if should_auto_open else "agent_followup",
                    "openclaw_bridge": openclaw_bridge.get_bridge_manifest().get("runtime_id", "openclaw"),
                    "runtime_status": machine_policy,
                }
                yield f"event: machine_handoff\ndata: {json.dumps(handoff_payload)}\n\n"
        elif mode == "agent" and session_id and original_agent_id:
            yield f"event: machine_handoff_blocked\ndata: {json.dumps(machine_policy)}\n\n"

        stream = execute_agent(query, messages, reasoning_model, mode, skills, strategy)
        try:
            while True:
                try:
                    update = await asyncio.wait_for(stream.__anext__(), timeout=heartbeat_interval_s)
                except asyncio.TimeoutError:
                    idle_for = time.time() - last_emit_at
                    if idle_for >= idle_timeout_s:
                        stalled = {
                            "type": "error",
                            "error": "Agent stream stalled",
                            "details": f"No stream events for {int(idle_for)}s",
                            "session_brand": session_brand.get("session_indicator"),
                        }
                        yield f"event: error\ndata: {json.dumps(stalled)}\n\n"
                        break

                    heartbeat = {
                        "type": "heartbeat",
                        "status": "alive",
                        "idle_seconds": round(idle_for, 1),
                        "session_brand": session_brand.get("session_indicator"),
                    }
                    yield f"event: heartbeat\ndata: {json.dumps(heartbeat)}\n\n"
                    continue
                except StopAsyncIteration:
                    break

                # Inject provenance into each update so re-connects can restore the label
                update.setdefault("session_brand", session_brand.get("session_indicator"))
                event_type = update.get("type", "error")
                event_name = getattr(event_type, "value", event_type)
                if not thinking and event_name == "thinking":
                    continue
                yield f"event: {event_name}\ndata: {json.dumps(update)}\n\n"
                last_emit_at = time.time()
        finally:
            try:
                await stream.aclose()
            except Exception:
                pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/chat/stream")
async def chat_stream(body: dict = Body(...)):
    """Stream LLM response tokens via SSE for any muscle."""
    prompt = body.get("prompt", "")
    muscle = body.get("muscle", "GWEN")
    system_prompt = body.get("system", "")
    history = body.get("history", [])
    model_override = body.get("model_override", "")

    if not prompt:
        raise HTTPException(400, "prompt is required")

    def event_stream():
        for event in muscles.stream_muscle(
            muscle, prompt,
            system=system_prompt,
            model_override=model_override,
            history=history,
        ):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Cloud SDK Chat Stream (GitHub Copilot + Claude Code) ──────────────────────

@app.post("/chat/stream")
async def cloud_chat_stream(body: dict = Body(...)):
    """SSE stream for GitHub Copilot SDK and Claude Code CLI chat modes.
    Expects: {prompt, history?, model, think?}
    Emits: data: {type: "token", content: "..."} / {type: "done", full_text, eval_count} / {type: "error", content}
    """
    prompt = body.get("prompt", "")
    model  = body.get("model", "")
    history = body.get("history", [])

    if not prompt:
        raise HTTPException(400, "prompt is required")

    def _sse(obj):
        return f"data: {json.dumps(obj)}\n\n"

    if model.startswith("copilot-"):
        # ── GitHub Copilot SDK path ──
        tier = model.replace("copilot-", "") or "nemo"

        # Inject persistent memory into Copilot context (same as local muscle path)
        _mem = _load_memory()
        _all_facts = _mem.get("facts", [])
        _relevant_facts = _retrieve_relevant_facts(_all_facts, prompt)
        _mem_system = ""
        if _relevant_facts:
            _fact_lines = []
            for f in _relevant_facts:
                cat = f.get("category", "")
                cat_label = f" [{cat}]" if cat and cat != "unknown" else ""
                _fact_lines.append(f"- {f.get('text', '')}{cat_label}")
            _mem_system = (
                "[Saved Memory — persistent facts about the user]\n"
                "IMPORTANT: These are confirmed facts the user previously told you. "
                "Use them naturally when relevant.\n"
                + "\n".join(_fact_lines) + "\n[End saved memory]\n"
            )
            # Touch access stats
            for rf in _relevant_facts:
                for af in _all_facts:
                    if af.get("id") == rf.get("id"):
                        af["last_used_at"] = time.time()
                        af["access_count"] = af.get("access_count", 0) + 1
            _save_memory(_mem)
            print(f"[Memory/Copilot] Injecting {len(_relevant_facts)} facts for: {prompt[:80]!r}")

        async def copilot_stream():
            try:
                from orchestrator.copilot_integration import get_bridge
                import asyncio as _asyncio

                system_parts = []
                if _mem_system:
                    system_parts.append(_mem_system)
                for msg in history:
                    if msg.get("role") == "system":
                        system_parts.append(msg.get("content", ""))
                system = "\n".join(system_parts) if system_parts else ""

                bridge = get_bridge()
                # copilot_wrapper is sync — run in a thread so we don't block the event loop
                result = await _asyncio.to_thread(bridge.send_blocking, prompt, system=system, tier=tier)

                if result.success:
                    yield _sse({"type": "token", "content": result.text})
                    yield _sse({"type": "done", "full_text": result.text, "eval_count": len(result.text.split())})
                else:
                    yield _sse({"type": "error", "content": result.error or "Copilot call failed"})
            except Exception as e:
                yield _sse({"type": "error", "content": str(e)})

        return StreamingResponse(
            copilot_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    elif model == "claude-code" or model.startswith("claude"):
        # ── Claude Code CLI path ──
        import shutil, subprocess as _sp

        async def claude_stream():
            cli = shutil.which("claude")
            if not cli:
                yield _sse({"type": "error", "content": "Claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code"})
                return
            try:
                workdir = config.CLAUDE_CODE_WORKDIR or os.getcwd()
                proc = _sp.Popen(
                    [cli, "-p", prompt, "--output-format", "stream-json"],
                    stdout=_sp.PIPE, stderr=_sp.PIPE,
                    cwd=workdir, text=True,
                    timeout=None,
                )
                full_text = ""
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        if ev.get("type") == "assistant" and "content" in ev:
                            for block in ev["content"]:
                                if block.get("type") == "text":
                                    full_text += block["text"]
                                    yield _sse({"type": "token", "content": block["text"]})
                        elif ev.get("type") == "result":
                            full_text = ev.get("result", full_text)
                    except json.JSONDecodeError:
                        full_text += line
                        yield _sse({"type": "token", "content": line})
                proc.wait(timeout=10)
                yield _sse({"type": "done", "full_text": full_text, "eval_count": len(full_text.split())})
            except Exception as e:
                yield _sse({"type": "error", "content": str(e)})

        return StreamingResponse(
            claude_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    else:
        raise HTTPException(400, f"Unsupported model for /chat/stream: {model}. Use copilot-* or claude-code.")


# ── Artifact Builder endpoints ────────────────────────────────────────────────

class ArtifactBuildReq(BaseModel):
    prompt: str
    model: str | None = None

class ArtifactRefineReq(BaseModel):
    manifest: dict
    feedback: str
    model: str | None = None

@app.post("/api/artifacts/build")
def artifact_build(req: ArtifactBuildReq):
    """Build a new artifact from a natural language prompt."""
    t0 = time.time()
    result = artifact_builder.build_artifact(req.prompt, model=req.model)
    elapsed = time.time() - t0
    if result["success"]:
        return {
            "success": True,
            "bundled_html": result.get("bundled_html", ""),
            "manifest": result.get("manifest"),
            "build_time": result.get("build_time", 0),
            "total_time": round(elapsed, 1),
        }
    raise HTTPException(422, detail=result.get("error", "Artifact build failed"))

@app.post("/api/artifacts/refine")
def artifact_refine(req: ArtifactRefineReq):
    """Refine an existing artifact with user feedback."""
    t0 = time.time()
    result = artifact_builder.refine_artifact(req.manifest, req.feedback, model=req.model)
    elapsed = time.time() - t0
    if result["success"]:
        return {
            "success": True,
            "bundled_html": result.get("bundled_html", ""),
            "manifest": result.get("manifest"),
            "build_time": result.get("build_time", 0),
            "total_time": round(elapsed, 1),
        }
    raise HTTPException(422, detail=result.get("error", "Artifact refinement failed"))


# ── Auto-Solve endpoints ──────────────────────────────────────────────────────

class AutoSolveReq(BaseModel):
    prompt: str
    task_type: str = ""           # Optional: coding, writing, research. Empty = let CEO decide.
    session_id: str = ""
    include_history: bool = False
    strategy: str = ""            # Loop strategy override: e-labs | cot | reflexion | direct. Empty = use saved setting.

@app.post("/api/auto-solve")
def run_auto_solve(req: AutoSolveReq):
    """Run the auto-solve wrapper — autonomous multi-step task solving."""
    # If task_type not provided, ask CEO to classify
    task_type = req.task_type
    if not task_type:
        try:
            routing = ceo.route(req.prompt)
            muscle = routing.get("muscle", "NEMOTRON")
            task_type = {"MAX": "writing", "GWEN": "coding", "NEMOTRON": "research"}.get(muscle, "coding")
        except Exception:
            task_type = "coding"

    result = model_wrapper.auto_solve(
        task=req.prompt,
        task_type=task_type,
        session_id=req.session_id,
        strategy=req.strategy or config.get_auto_solve_settings().get("AUTO_SOLVE_LOOP_STRATEGY", "e-labs"),
    )

    # Save to conversation history if session provided
    if req.session_id and result.status == "complete":
        try:
            conversation_manager.add_message(req.session_id, "user", req.prompt)
            conversation_manager.add_message(req.session_id, "assistant", result.final_result)
        except Exception:
            pass

    return result.to_dict()


@app.get("/api/auto-solve/settings")
def get_auto_solve_settings():
    """Return all auto-solve feature toggle settings."""
    return {
        "settings": config.get_auto_solve_settings(),
        "defaults": dict(config.AUTO_SOLVE_DEFAULTS),
    }


@app.put("/api/auto-solve/settings")
def update_auto_solve_settings(body: dict):
    """Update one or more auto-solve settings.
    Body: {"AUTO_SOLVE_ENABLED": false, "AUTO_SOLVE_MAX_ITERATIONS": 5}"""
    applied = config.update_auto_solve_bulk(body)
    if not applied:
        raise HTTPException(400, "No valid settings keys provided")
    return {"status": "ok", "applied": applied, "current": config.get_auto_solve_settings()}


# ── Feature Flags ─────────────────────────────────────────────────────────────

@app.get("/api/features")
def get_features():
    """Return all feature flags and their current states."""
    from orchestrator.features import get_all_flags, get_enabled_flags
    return {
        "flags": get_all_flags(),
        "enabled": get_enabled_flags(),
    }


@app.put("/api/features/{name}")
def set_feature_flag(name: str, body: dict):
    """Enable or disable a feature flag.
    Body: {"enabled": true}"""
    from orchestrator.features import set_feature, feature as check_feature
    enabled = body.get("enabled", False)
    if not set_feature(name, enabled):
        raise HTTPException(400, f"Unknown feature flag: {name}")
    return {"name": name, "enabled": check_feature(name)}


@app.put("/api/features")
def bulk_update_features(body: dict):
    """Update multiple feature flags at once.
    Body: {"BASH_TOOL": true, "FILE_EDIT_TOOL": true}"""
    from orchestrator.features import bulk_update_flags
    applied = bulk_update_flags(body)
    if not applied:
        raise HTTPException(400, "No valid feature flag names provided")
    from orchestrator.features import get_all_flags
    return {"status": "ok", "applied": applied, "current": get_all_flags()}


# ── Swarm / MACHINE Settings ─────────────────────────────────────────────────

@app.get("/api/swarm/settings")
def get_swarm_settings_api():
    """Return all SWARM pipeline settings with defaults and descriptions."""
    from orchestrator.config import SWARM, SWARM_DEFAULTS
    # Build rich response with metadata for UI rendering
    _SWARM_META = {
        "plan_model":            {"label": "Plan Model",             "group": "models",   "type": "model",  "help": "Local model used for the PLAN phase (analysis/decomposition). Smaller models are faster but less accurate."},
        "exec_model":            {"label": "Execute Model",          "group": "models",   "type": "model",  "help": "Model/muscle for the EXEC phase (code generation). Use 'FLEET' for cloud SDK or a local Ollama model name."},
        "verify_model":          {"label": "Verify Model",           "group": "models",   "type": "model",  "help": "Model/muscle for the VERIFY phase (code review). Use 'FLEET' for cloud SDK or a local model name."},
        "consolidation_model":   {"label": "Consolidation Model",    "group": "models",   "type": "model",  "help": "Model used to synthesize multiple agent findings into a ranked plan."},
        "fleet_model":           {"label": "Fleet Cloud Model",      "group": "models",   "type": "text",   "help": "Which cloud model to use for FLEET calls via Copilot SDK (e.g. gpt-5-mini, gpt-4.1-mini)."},
        "plan_workers":          {"label": "Plan Workers",           "group": "workers",  "type": "number", "help": "Number of parallel agents during the PLAN phase. More workers = broader analysis but higher VRAM/API usage.", "min": 1, "max": 10},
        "exec_workers":          {"label": "Execute Workers",        "group": "workers",  "type": "number", "help": "Number of parallel implementers during EXEC. Each gets a priority from the plan.", "min": 1, "max": 10},
        "verify_workers":        {"label": "Verify Workers",         "group": "workers",  "type": "number", "help": "Number of parallel reviewers during VERIFY. Reviews code from all EXEC workers.", "min": 1, "max": 10},
        "source_file_max_chars": {"label": "Source File Context",    "group": "context",  "type": "number", "help": "Max characters per source file injected into EXEC prompts. Higher = better code quality but uses more context window. Set to 0 for full file injection.", "min": 0, "max": 100000, "unit": "chars"},
        "source_files_max_count":{"label": "Max Source Files",       "group": "context",  "type": "number", "help": "Maximum number of source files to include in EXEC context. More files = more awareness of codebase.", "min": 1, "max": 50},
        "plan_synthesis_chars":  {"label": "Plan Synthesis Budget",   "group": "context",  "type": "number", "help": "Character budget for the consolidated plan text passed to EXEC workers.", "min": 500, "max": 50000, "unit": "chars"},
        "source_block_chars":    {"label": "Source Block Budget",     "group": "context",  "type": "number", "help": "Total character budget for all source file blocks in EXEC prompts.", "min": 1000, "max": 100000, "unit": "chars"},
        "plan_compaction_chars": {"label": "Plan Compaction",        "group": "context",  "type": "number", "help": "Max chars for compacted plan summaries.", "min": 500, "max": 10000, "unit": "chars"},
        "impl_compaction_chars": {"label": "Impl Compaction",        "group": "context",  "type": "number", "help": "Max chars for compacted implementation summaries passed to verifiers.", "min": 500, "max": 10000, "unit": "chars"},
        "spec_compaction_chars": {"label": "Spec Compaction",        "group": "context",  "type": "number", "help": "Max chars for compacted spec summaries.", "min": 500, "max": 10000, "unit": "chars"},
        "context_per_task_chars":{"label": "Context Per Task",       "group": "context",  "type": "number", "help": "Character budget for context appended to each task prompt.", "min": 500, "max": 50000, "unit": "chars"},
        "file_read_max_chars":   {"label": "File Read Limit",        "group": "context",  "type": "number", "help": "Maximum chars when reading files for context injection.", "min": 1000, "max": 200000, "unit": "chars"},
        "plan_timeout":          {"label": "Plan Timeout",           "group": "timeouts", "type": "number", "help": "Maximum seconds for the entire PLAN phase.", "min": 30, "max": 3600, "unit": "seconds"},
        "exec_timeout":          {"label": "Execute Timeout",        "group": "timeouts", "type": "number", "help": "Maximum seconds for the entire EXEC phase.", "min": 60, "max": 3600, "unit": "seconds"},
        "verify_timeout":        {"label": "Verify Timeout",         "group": "timeouts", "type": "number", "help": "Maximum seconds for the entire VERIFY phase.", "min": 60, "max": 3600, "unit": "seconds"},
        "scope_timeout":         {"label": "Scope Timeout",          "group": "timeouts", "type": "number", "help": "Maximum seconds for the initial SCOPE analysis.", "min": 30, "max": 1800, "unit": "seconds"},
        "fleet_polling_deadline":{"label": "Fleet Polling Deadline", "group": "fleet",    "type": "number", "help": "Maximum seconds to wait for a FLEET API response before timing out.", "min": 30, "max": 1200, "unit": "seconds"},
        "fleet_max_retries":     {"label": "Fleet Max Retries",      "group": "fleet",    "type": "number", "help": "How many times to retry a failed FLEET call.", "min": 0, "max": 10},
        "fleet_retry_delay":     {"label": "Fleet Retry Delay",      "group": "fleet",    "type": "number", "help": "Seconds to wait between FLEET retry attempts.", "min": 1, "max": 30, "unit": "seconds"},
        "fleet_min_response_chars":{"label": "Fleet Min Response",   "group": "fleet",    "type": "number", "help": "Minimum characters for a FLEET response to be considered valid. Responses shorter than this trigger retries.", "min": 10, "max": 1000, "unit": "chars"},
        "fleet_thread_timeout":  {"label": "Fleet Thread Timeout",   "group": "fleet",    "type": "number", "help": "Maximum seconds for a FLEET thread to complete.", "min": 30, "max": 600, "unit": "seconds"},
        "apply_min_file_chars":  {"label": "Min File Size to Apply", "group": "apply",    "type": "number", "help": "Files shorter than this are skipped during apply (likely empty/stub).", "min": 0, "max": 1000, "unit": "chars"},
        "apply_security_gates":  {"label": "Security Gates",         "group": "apply",    "type": "boolean","help": "When enabled, generated files are security-reviewed before applying. Files that fail security review go to a staging directory instead."},
        "apply_require_pass_verdict":{"label": "Require PASS Verdict","group": "apply",   "type": "boolean","help": "When enabled, only files with a PASS verdict from verifiers are applied. When disabled, files with valid syntax are applied regardless of verdict."},
        "exec_strategy":         {"label": "Execution Strategy",     "group": "strategy", "type": "select", "help": "How EXEC workers run. 'parallel' launches all at once. 'sequential' runs one at a time (useful for models with limited context or VRAM).", "options": ["parallel", "sequential"]},
    }
    settings = {}
    for key, default_val in SWARM_DEFAULTS.items():
        meta = _SWARM_META.get(key, {"label": key, "group": "other", "type": "text", "help": ""})
        settings[key] = {
            "value": SWARM.get(key, default_val),
            "default": default_val,
            **meta,
        }
    return {"settings": settings}


@app.put("/api/swarm/settings")
def update_swarm_settings_api(body: dict):
    """Update one or more SWARM settings.
    Body: {"source_file_max_chars": 8000, "exec_workers": 5}"""
    from orchestrator.config import update_swarm_bulk, SWARM, SWARM_DEFAULTS
    valid_keys = set(SWARM_DEFAULTS.keys())
    updates = {k: v for k, v in body.items() if k in valid_keys}
    if not updates:
        raise HTTPException(400, "No valid SWARM setting keys provided")
    update_swarm_bulk(updates)
    return {"status": "ok", "applied": list(updates.keys()), "current": {k: SWARM[k] for k in SWARM}}


@app.put("/api/swarm/settings/{key}")
def update_single_swarm_setting(key: str, body: dict):
    """Update a single SWARM setting.
    Body: {"value": 8000}"""
    from orchestrator.config import update_swarm, SWARM, SWARM_DEFAULTS
    if key not in SWARM_DEFAULTS:
        raise HTTPException(404, f"Unknown SWARM setting: {key}")
    value = body.get("value")
    if value is None:
        raise HTTPException(400, "Missing 'value' in request body")
    update_swarm(key, value)
    return {"key": key, "value": SWARM[key]}


@app.post("/api/swarm/settings/reset")
def reset_swarm_settings_api():
    """Reset all SWARM settings to defaults."""
    from orchestrator.config import SWARM, SWARM_DEFAULTS, _save_swarm
    SWARM.clear()
    SWARM.update(SWARM_DEFAULTS)
    _save_swarm()
    return {"status": "ok", "current": dict(SWARM)}


@app.get("/api/swarm/models")
def get_swarm_model_options():
    """Return available models for SWARM pipeline, with context/speed metadata."""
    models = []
    # Local Ollama models
    try:
        import urllib.request, json as _json
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5) as resp:
            data = _json.loads(resp.read())
        for m in data.get("models", []):
            name = m.get("name", "")
            size_gb = round(m.get("size", 0) / 1e9, 1)
            # Estimate context window from known models
            ctx = _estimate_context_window(name)
            models.append({
                "name": name,
                "backend": "ollama",
                "size_gb": size_gb,
                "context_window": ctx,
                "speed": _estimate_speed_tier(size_gb),
                "available": True,
            })
    except Exception:
        pass
    # FLEET (cloud) — routes to the fleet_model setting
    models.append({
        "name": "FLEET",
        "backend": "copilot-sdk",
        "size_gb": 0,
        "context_window": 128000,
        "speed": "medium",
        "available": True,
        "note": "Cloud model via Copilot SDK (uses fleet_model setting)",
    })
    # Specific SDK cloud models
    for sdk_name, sdk_note in [
        ("gpt-5-mini", "Free tier — 0x multiplier"),
        ("gpt-5.1", "Standard tier — 1x multiplier"),
        ("claude-opus-4.6", "Deep tier — 3x multiplier"),
    ]:
        models.append({
            "name": sdk_name,
            "backend": "copilot-sdk",
            "size_gb": 0,
            "context_window": 128000,
            "speed": "medium",
            "available": True,
            "note": f"Cloud: {sdk_note}",
        })
    # llamacpp models from registry
    try:
        from orchestrator.model_registry import MODEL_REGISTRY
        for mid, entry in MODEL_REGISTRY.items():
            if entry.get("backend") == "llamacpp":
                models.append({
                    "name": mid,
                    "backend": "llamacpp",
                    "size_gb": round(entry.get("vram_mb", 0) / 1024, 1),
                    "context_window": entry.get("config", {}).get("context_window", 4096),
                    "speed": entry.get("speed_rating", "medium"),
                    "available": True,
                    "note": entry.get("notes", ""),
                })
    except Exception:
        pass
    return {"models": models}


def _estimate_context_window(model_name: str) -> int:
    """Estimate context window for known Ollama models."""
    name = model_name.lower()
    _KNOWN = {
        "qwen2.5:1.5b": 32768, "qwen3:14b": 32768, "qwen3:8b": 32768,
        "gwen": 32768, "nemotron-agent": 131072, "deepseek-r1:14b": 65536,
        "gemma3:4b": 8192, "llama3.2:3b": 131072, "phi-4-mini": 16384,
    }
    for known, ctx in _KNOWN.items():
        if known in name:
            return ctx
    return 4096  # conservative default


def _estimate_speed_tier(size_gb: float) -> str:
    """Estimate speed tier from model size."""
    if size_gb < 2:
        return "fast"
    elif size_gb < 8:
        return "medium"
    else:
        return "slow"


# ── Permissions ───────────────────────────────────────────────────────────────

@app.get("/api/permissions")
def get_permissions():
    """Return current permission mode and custom rules."""
    from orchestrator.permissions import get_permission_state
    return get_permission_state()


@app.put("/api/permissions/mode")
def set_permission_mode(body: dict):
    """Set the permission mode.
    Body: {"mode": "default"|"accept_edits"|"bypass"|"deny"}"""
    from orchestrator.permissions import set_mode, PermissionMode, get_permission_state
    mode_str = body.get("mode", "default")
    try:
        set_mode(PermissionMode(mode_str))
    except ValueError:
        raise HTTPException(400, f"Invalid mode: {mode_str}. Valid: default, accept_edits, bypass, deny")
    return get_permission_state()


@app.post("/api/permissions/rules")
def add_permission_rule(body: dict):
    """Add a custom permission rule.
    Body: {"tool_name": "bash", "behavior": "deny", "pattern": "rm -rf *"}"""
    from orchestrator.permissions import add_rule, get_permission_state
    tool_name = body.get("tool_name")
    behavior = body.get("behavior")
    if not tool_name or not behavior:
        raise HTTPException(400, "tool_name and behavior are required")
    if behavior not in ("allow", "deny", "ask"):
        raise HTTPException(400, f"Invalid behavior: {behavior}. Valid: allow, deny, ask")
    add_rule(tool_name, behavior, body.get("pattern", "*"), body.get("source", "user"))
    return get_permission_state()


@app.delete("/api/permissions/rules/{index}")
def delete_permission_rule(index: int):
    """Remove a custom permission rule by index."""
    from orchestrator.permissions import remove_rule, get_permission_state
    if not remove_rule(index):
        raise HTTPException(404, f"Rule index {index} not found")
    return get_permission_state()


# ── Sprint 3: Bash Tool, File Tools, Context Collection ──────────────────────

@app.post("/api/tools/bash")
def api_bash_execute(req: dict = Body(...)):
    """Execute a shell command via the Bash Tool."""
    from orchestrator.tools.bash_tool import bash_execute
    command = req.get("command", "")
    if not command:
        raise HTTPException(400, "command is required")
    timeout = req.get("timeout", 30)
    working_dir = req.get("working_dir", ".")
    result = bash_execute(command, timeout=timeout, working_dir=working_dir)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@app.post("/api/tools/file-write")
def api_file_write(req: dict = Body(...)):
    """Create or overwrite a file via the File Write Tool."""
    from orchestrator.tools.file_write_tool import file_write
    file_path = req.get("file_path", "")
    content = req.get("content", "")
    if not file_path:
        raise HTTPException(400, "file_path is required")
    create_dirs = req.get("create_dirs", True)
    result = file_write(file_path, content, create_dirs=create_dirs)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@app.post("/api/tools/file-edit")
def api_file_edit(req: dict = Body(...)):
    """Make a targeted string replacement in a file via the File Edit Tool."""
    from orchestrator.tools.file_edit_tool import file_edit
    file_path = req.get("file_path", "")
    old_string = req.get("old_string", "")
    new_string = req.get("new_string", "")
    if not file_path or not old_string:
        raise HTTPException(400, "file_path and old_string are required")
    replace_all = req.get("replace_all", False)
    result = file_edit(file_path, old_string, new_string, replace_all=replace_all)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@app.get("/api/context")
def api_get_context():
    """Get the current workspace context collection."""
    from orchestrator.context_collector import ContextCollector
    collector = ContextCollector(workspace_root=PROJECT_ROOT)
    context_block = collector.collect()
    return {
        "context": context_block,
        "cached": False,
        "ttl": 60,
    }


# ── Sprint 4: Streaming Generate, Pagination, Cost Tracking ─────────────────

@app.post("/api/generate/stream")
async def generate_stream(body: dict = Body(...), _user=Depends(_auth_guard)):
    """Stream tokens from /api/generate via SSE — works with any muscle or direct model.

    Replaces the blocking /api/generate for modes that support streaming.
    Falls back to single-event response for modes that don't (multi, toolcaller, artifact).

    Enhanced:
    - Injects global + project persistent memory (same tiers as blocking /api/generate)
    - Persists streamed responses to conversation session
    - Blocks CEO from routing to MACHINE/ARTIFACT/TOOLCALLER in chat mode (keep it conversational)
    """
    prompt = body.get("prompt", "")
    mode = body.get("mode", "auto").strip().lower()
    system_prompt = body.get("system", "")
    history = body.get("messages") or body.get("history") or []
    model_override = body.get("model_override", "")
    session_id = body.get("session_id")
    project_id = body.get("project_id")
    cloud_model_id = body.get("cloud_model_id")

    if not prompt:
        raise HTTPException(400, "prompt is required")

    # ── Resolve memory tiers (same logic as blocking /api/generate) ──────────
    mem = _load_memory()
    all_facts = mem.get("facts", [])
    relevant_facts = _retrieve_relevant_facts(all_facts, prompt)
    mem_system = ""
    if relevant_facts:
        fact_lines = []
        for f in relevant_facts:
            cat = f.get("category", "")
            cat_label = f" [{cat}]" if cat and cat != "unknown" else ""
            fact_lines.append(f"- {f.get('text', '')}{cat_label}")
        mem_system = (
            "[Saved Memory — persistent facts about the user]\n"
            "IMPORTANT: These are confirmed facts the user previously told you. "
            "Use them naturally when relevant.\n"
            + "\n".join(fact_lines) + "\n[End saved memory]\n"
        )
        # Touch last_used_at
        for rf in relevant_facts:
            for af in all_facts:
                if af.get("id") == rf.get("id"):
                    af["last_used_at"] = time.time()
                    af["access_count"] = af.get("access_count", 0) + 1
        _save_memory(mem)

    proj_mem_system = ""
    if project_id:
        proj_mem = _load_project_memory(project_id)
        proj_facts = proj_mem.get("facts", [])
        relevant_proj = _retrieve_relevant_facts(proj_facts, prompt)
        if relevant_proj:
            proj_lines = [f"- {f.get('text', '')}" for f in relevant_proj]
            proj_mem_system = "[Project Memory — facts specific to this project only]\n" + "\n".join(proj_lines) + "\n[End project memory]\n"
        for rf in relevant_proj:
            for pf in proj_facts:
                if pf.get("id") == rf.get("id"):
                    pf["last_used_at"] = time.time()
                    pf["access_count"] = pf.get("access_count", 0) + 1
        if relevant_proj:
            _save_project_memory(project_id, proj_mem)

    combined_mem = mem_system + proj_mem_system

    # Merge memory into system prompt
    if combined_mem:
        system_prompt = combined_mem + ("\n" + system_prompt if system_prompt else "")
        n_facts = len(relevant_facts)
        print(f"[chat/stream] Injecting {n_facts} memory facts for: {prompt[:60]!r}")

    # Persist user message to session
    if session_id:
        if not conversation_manager.session_exists(session_id):
            conversation_manager.create_session()
        conversation_manager.add_message(session_id, "user", prompt)

    def event_stream():
        full_text = ""
        muscle_used = mode.upper()

        # ── Direct model streaming (e.g. direct:qwen3:14b) ──────────────────
        if mode.startswith("direct:"):
            raw_model = mode[7:]
            muscle_used = raw_model.replace(":latest", "").upper()
            # Build full system (memory + any frontend system msgs + tool instructions)
            sys_msgs = [m for m in history if m.get("role") == "system"]
            chat_hist = [m for m in history if m.get("role") != "system"]
            full_sys = system_prompt
            for sm in sys_msgs:
                full_sys += "\n" + sm.get("content", "")
            full_sys += "\n\n" + build_tool_instructions()
            for event_line in _stream_model(raw_model, prompt, full_sys.strip(), chat_hist):
                ev = json.loads(event_line[6:]) if event_line.startswith("data: ") else None
                if ev:
                    if ev.get("type") == "token":
                        full_text += ev.get("content", "")
                    elif ev.get("type") == "done":
                        full_text = ev.get("full_text", full_text)
                        ev["muscle"] = muscle_used
                yield event_line
            if session_id and full_text:
                conversation_manager.add_message(session_id, "assistant", full_text)
            return

        # ── Named muscle streaming ────────────────────────────────────────────
        if mode in ("max", "gwen", "nemotron"):
            muscle_name = mode.upper()
            sys_prompt = system_prompt or config.MUSCLES.get(muscle_name, {}).get("system_prompt", "")
            sys_prompt += "\n\n" + build_tool_instructions()
            sys_prompt += build_smart_guidance(muscle_name, "chat")
            for event in muscles.stream_muscle(
                muscle_name, prompt,
                system=sys_prompt,
                model_override=model_override,
                history=history,
            ):
                ev_str = f"data: {json.dumps(event)}\n\n"
                if event.get("type") == "token":
                    full_text += event.get("content", "")
                elif event.get("type") == "done":
                    full_text = event.get("full_text", full_text)
                    event["muscle"] = muscle_name
                yield ev_str
            if session_id and full_text:
                conversation_manager.add_message(session_id, "assistant", full_text)
            return

        # ── Cloud model streaming ─────────────────────────────────────────────
        if cloud_model_id:
            entry = model_registry.get_model(cloud_model_id)
            if not entry:
                yield f"data: {json.dumps({'type': 'error', 'content': f'Cloud model not found: {cloud_model_id}'})}\n\n"
                return
            result = cloud_models.call_cloud_model(
                entry, prompt=prompt,
                messages=[{"role": "user", "content": prompt}],
            )
            resp_text = result.get("text", result.get("error", "Error"))
            if session_id:
                conversation_manager.add_message(session_id, "assistant", resp_text)
            # Emit as tokens then done (cloud models don't stream, send as single burst)
            chunk_size = 80
            cloud_provider = entry.get("provider", "").upper()
            for i in range(0, len(resp_text), chunk_size):
                yield f"data: {json.dumps({'type': 'token', 'content': resp_text[i:i+chunk_size]})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'full_text': resp_text, 'muscle': 'CLOUD:' + cloud_provider})}\n\n"
            return

        # ── Auto/CEO mode — route then stream ────────────────────────────────
        # Route through CEO but NEVER send to MACHINE/ARTIFACT/TOOLCALLER in chat stream
        try:
            routing = ceo.smart_route(prompt)
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
            return

        muscle_name = routing.get("muscle", "NEMOTRON")
        smart_model = routing.get("smart_ollama_tag", "")

        # Redirect non-streamable or non-conversational routes to a sane muscle
        if muscle_name in ("MACHINE", "ARTIFACT", "TOOLCALLER"):
            # Force to GWEN for chat mode — do NOT launch projects or artifact builds
            muscle_name = "GWEN"
            smart_model = ""
            yield f"data: {json.dumps({'type': 'routing', 'muscle': 'GWEN', 'reasoning': 'Chat mode: redirected from ' + routing.get('muscle', '?') + ' to GWEN for direct response'})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'routing', 'muscle': muscle_name, 'reasoning': routing.get('reasoning', ''), 'model': smart_model})}\n\n"

        sys_prompt = system_prompt or config.MUSCLES.get(muscle_name, {}).get("system_prompt", "")
        sys_prompt += "\n\n" + build_tool_instructions()
        sys_prompt += build_smart_guidance(muscle_name, "chat")

        for event in muscles.stream_muscle(
            muscle_name, prompt,
            system=sys_prompt,
            model_override=smart_model,
            history=history,
        ):
            ev_str = f"data: {json.dumps(event)}\n\n"
            if event.get("type") == "token":
                full_text += event.get("content", "")
            elif event.get("type") == "done":
                full_text = event.get("full_text", full_text)
                event["muscle"] = muscle_name
            yield ev_str

        if session_id and full_text:
            conversation_manager.add_message(session_id, "assistant", full_text)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )



def _stream_model(model: str, prompt: str, system: str, history: list):
    """Helper: stream raw tokens from Ollama (or llamacpp) for a given model."""
    import urllib.request as _urlreq

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    if history:
        for m in history[-20:]:
            if m.get("role") in ("user", "assistant", "system") and m.get("content"):
                messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": prompt})

    # Check if model uses llamacpp backend
    from orchestrator.muscles import _get_llamacpp_endpoint, _stream_llamacpp
    llamacpp_ep = _get_llamacpp_endpoint(model)
    if llamacpp_ep:
        for event in _stream_llamacpp(llamacpp_ep, messages):
            yield f"data: {json.dumps(event)}\n\n"
        return

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "stream": True,
        "keep_alive": "30m",
        "options": config.get_ollama_options(),
    }).encode()

    req = _urlreq.Request(
        f"{config.OLLAMA_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    full_text = ""
    eval_count = 0

    try:
        with _urlreq.urlopen(req, timeout=300) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                chunk = json.loads(line)
                token = chunk.get("message", {}).get("content", "")
                full_text += token

                if token:
                    yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"

                if chunk.get("done"):
                    eval_count = chunk.get("eval_count", 0)

        yield f"data: {json.dumps({'type': 'done', 'full_text': full_text, 'eval_count': eval_count})}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"


@app.get("/api/sessions/{session_id}/messages")
def get_session_messages_paginated(session_id: str, cursor: int = 0, limit: int = 50):
    """Paginated message retrieval for a session.

    Args:
        session_id: Session ID.
        cursor: Starting message index (0-based, from most recent).
        limit: Number of messages to return (max 200).

    Returns:
        messages: List of messages for this page.
        next_cursor: Next cursor value (null if no more).
        total: Total message count.
    """
    limit = min(limit, 200)
    if not conversation_manager.session_exists(session_id):
        raise HTTPException(404, f"Session not found: {session_id}")

    all_msgs = conversation_manager.get_messages(session_id)
    total = len(all_msgs)

    # Slice from cursor
    page = all_msgs[cursor:cursor + limit]
    next_cursor = cursor + limit if cursor + limit < total else None

    return {
        "messages": page,
        "cursor": cursor,
        "next_cursor": next_cursor,
        "limit": limit,
        "total": total,
    }


@app.get("/api/cost")
def get_cost_tracking():
    """Get current session cost/token tracking data."""
    from orchestrator.cost_tracker import get_tracker
    tracker = get_tracker()
    return {
        "session": tracker.session_total(),
        "last_turn": tracker.last_turn(),
    }


@app.post("/api/cost/reset")
def reset_cost_tracking():
    """Reset the session cost tracker."""
    from orchestrator.cost_tracker import get_tracker
    tracker = get_tracker()
    tracker.reset()
    return {"status": "reset", "session": tracker.session_total()}


# ── Sprint 5: Background Tasks API ───────────────────────────────────────────

@app.post("/api/tasks/submit")
async def submit_task(request: Request):
    """Submit a background task (placeholder — real tasks submitted via code)."""
    from orchestrator.task_runner import get_runner
    body = await request.json()
    name = body.get("name", "unnamed")
    # For now, return guidance — real task submission happens internally
    return {"info": "Tasks are submitted programmatically. Use GET /api/tasks to list.", "name": name}


@app.get("/api/tasks")
def list_tasks(state: str = None):
    """List all background tasks, optionally filtered by state."""
    from orchestrator.task_runner import get_runner
    runner = get_runner()
    return {"tasks": runner.list_tasks(state_filter=state)}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    """Get status of a specific background task."""
    from orchestrator.task_runner import get_runner
    runner = get_runner()
    status = runner.get_status(task_id)
    if status is None:
        return JSONResponse(status_code=404, content={"error": "Task not found"})
    return status


@app.delete("/api/tasks/{task_id}")
def cancel_task(task_id: str):
    """Cancel a running background task."""
    from orchestrator.task_runner import get_runner
    runner = get_runner()
    cancelled = runner.cancel(task_id)
    if not cancelled:
        return JSONResponse(status_code=404, content={"error": "Task not found or not running"})
    return {"cancelled": True, "task_id": task_id}


@app.post("/api/coordinator/run")
async def run_coordinator(request: Request):
    """Run the multi-agent coordinator on a complex request."""
    from orchestrator.coordinator import Coordinator, should_use_coordinator
    from orchestrator.features import is_enabled
    import urllib.request as urllib_req

    if not is_enabled("COORDINATOR_MODE"):
        return JSONResponse(status_code=400, content={"error": "COORDINATOR_MODE feature flag is disabled"})

    body = await request.json()
    prompt = body.get("prompt", "")
    if not prompt:
        return JSONResponse(status_code=400, content={"error": "prompt is required"})

    async def call_model(p: str, model: str) -> str:
        from orchestrator.config import OLLAMA_URL, get_ollama_options
        payload = {
            "model": model, "messages": [{"role": "user", "content": p}],
            "stream": False, "think": False, "keep_alive": "30m",
            "options": get_ollama_options(),
        }
        import json as _json
        data = _json.dumps(payload).encode()
        req = urllib_req.Request(
            f"{OLLAMA_URL}/api/chat", data=data,
            headers={"Content-Type": "application/json"},
        )
        loop = asyncio.get_event_loop()
        resp_data = await loop.run_in_executor(None, lambda: urllib_req.urlopen(req, timeout=120).read())
        result = _json.loads(resp_data)
        return result.get("message", {}).get("content", "")

    coord = Coordinator(call_model)
    result = await coord.run(prompt)
    return result


@app.post("/api/verify/code")
async def verify_code(request: Request):
    """Verify generated code by running it."""
    from orchestrator.skills.builtin.verify.verify_skill import verify_code_sync
    from orchestrator.features import is_enabled

    if not is_enabled("SELF_VERIFICATION"):
        return JSONResponse(status_code=400, content={"error": "SELF_VERIFICATION feature flag is disabled"})

    body = await request.json()
    code = body.get("code", "")
    language = body.get("language", "python")
    timeout = min(body.get("timeout", 10), 30)  # Cap at 30s

    if not code:
        return JSONResponse(status_code=400, content={"error": "code is required"})

    result = verify_code_sync(code, language, timeout)
    return result


# ── Sprint 6: Bridge Mode ─────────────────────────────────────────────────────

@app.get("/api/bridge/sessions")
def list_bridge_sessions():
    """List active bridge sessions."""
    from orchestrator.bridge_mode import get_bridge_manager
    from orchestrator.features import feature as feat
    if not feat("BRIDGE_MODE"):
        return JSONResponse(status_code=400, content={"error": "BRIDGE_MODE feature flag is disabled"})
    mgr = get_bridge_manager()
    return {"sessions": mgr.list_sessions()}


@app.post("/api/bridge/sessions")
def create_bridge_session():
    """Create a new bridge session."""
    from orchestrator.bridge_mode import get_bridge_manager
    from orchestrator.features import feature as feat
    if not feat("BRIDGE_MODE"):
        return JSONResponse(status_code=400, content={"error": "BRIDGE_MODE feature flag is disabled"})
    mgr = get_bridge_manager()
    session = mgr.create_session()
    return session.to_dict()


@app.post("/api/bridge/sessions/{session_id}/message")
async def bridge_message(session_id: str, request: Request):
    """Send a message to a bridge session."""
    from orchestrator.bridge_mode import get_bridge_manager
    from orchestrator.features import feature as feat
    if not feat("BRIDGE_MODE"):
        return JSONResponse(status_code=400, content={"error": "BRIDGE_MODE feature flag is disabled"})
    mgr = get_bridge_manager()
    session = mgr.get_session(session_id)
    if not session:
        return JSONResponse(status_code=404, content={"error": "Session not found"})
    body = await request.json()
    result = mgr.handle_message(session_id, body)
    if result is None:
        return {"status": "ok"}
    return result.to_dict()


# ── Sprint 6: MCP ─────────────────────────────────────────────────────────────

@app.get("/api/mcp/servers")
def list_mcp_servers():
    """List connected MCP servers."""
    from orchestrator.mcp_client import get_mcp_client
    from orchestrator.features import feature as feat
    if not feat("MCP_CLIENT"):
        return JSONResponse(status_code=400, content={"error": "MCP_CLIENT feature flag is disabled"})
    client = get_mcp_client()
    return {"servers": client.list_servers()}


@app.get("/api/mcp/tools")
def list_mcp_tools():
    """List all tools from connected MCP servers."""
    from orchestrator.mcp_client import get_mcp_client
    from orchestrator.features import feature as feat
    if not feat("MCP_CLIENT"):
        return JSONResponse(status_code=400, content={"error": "MCP_CLIENT feature flag is disabled"})
    client = get_mcp_client()
    return {"tools": client.list_tools()}


@app.post("/api/mcp/tools/call")
async def call_mcp_tool(request: Request):
    """Call a tool on an MCP server."""
    from orchestrator.mcp_client import get_mcp_client
    from orchestrator.features import feature as feat
    if not feat("MCP_CLIENT"):
        return JSONResponse(status_code=400, content={"error": "MCP_CLIENT feature flag is disabled"})
    body = await request.json()
    tool_name = body.get("tool", "")
    arguments = body.get("arguments", {})
    if not tool_name:
        return JSONResponse(status_code=400, content={"error": "tool is required"})
    client = get_mcp_client()
    result = client.call_tool(tool_name, arguments)
    return result


@app.get("/api/mcp/server/tools")
def list_exposed_tools():
    """List tools exposed by our MCP server."""
    from orchestrator.mcp_server import get_mcp_server
    from orchestrator.features import feature as feat
    if not feat("MCP_SERVER"):
        return JSONResponse(status_code=400, content={"error": "MCP_SERVER feature flag is disabled"})
    server = get_mcp_server()
    return {"tools": server.list_tools()}


# ── Sprint 6: Plugin System ───────────────────────────────────────────────────

@app.get("/api/plugins")
def list_plugins():
    """List all loaded plugins with tool contributions."""
    from orchestrator.skills.skill_registry import get_registry
    reg = get_registry()
    return reg.get_plugin_info()


@app.get("/api/plugins/tools")
def list_contributed_tools():
    """List all tools contributed by plugins."""
    from orchestrator.skills.skill_registry import get_registry
    reg = get_registry()
    return {"tools": reg.get_contributed_tools()}


@app.post("/api/plugins/{skill_id}/reload")
def reload_plugin(skill_id: str):
    """Hot-reload a single plugin."""
    from orchestrator.skills.skill_registry import get_registry
    reg = get_registry()
    ok = reg.hot_reload_skill(skill_id)
    if not ok:
        return JSONResponse(status_code=404, content={"error": f"Skill not found or reload failed: {skill_id}"})
    return {"status": "reloaded", "skill_id": skill_id}


@app.delete("/api/plugins/{skill_id}")
def unload_plugin(skill_id: str):
    """Unload a plugin."""
    from orchestrator.skills.skill_registry import get_registry
    reg = get_registry()
    ok = reg.unload_skill(skill_id)
    if not ok:
        return JSONResponse(status_code=404, content={"error": f"Skill not found: {skill_id}"})
    return {"status": "unloaded", "skill_id": skill_id}


# ═══════════════════════════════════════════════════════════════════════════
# SPRINT 7 — Buddy Companion, Voice Input, Artifact Templates, Auth
# ═══════════════════════════════════════════════════════════════════════════

# ── Buddy Companion ───────────────────────────────────────────────────────

@app.get("/api/companion/roll/{user_id}")
def companion_roll(user_id: str):
    """Roll or retrieve deterministic companion for a user ID."""
    from orchestrator.features import feature
    if not feature("BUDDY_COMPANION"):
        return JSONResponse(status_code=403, content={"error": "BUDDY_COMPANION flag disabled"})
    from orchestrator.companion import get_companion
    companion = get_companion(user_id)
    return companion.to_dict()


@app.get("/api/companion/sprite/{user_id}")
def companion_sprite(user_id: str, frame: int = 0):
    """Get ASCII sprite for a user's companion."""
    from orchestrator.features import feature
    if not feature("BUDDY_COMPANION"):
        return JSONResponse(status_code=403, content={"error": "BUDDY_COMPANION flag disabled"})
    from orchestrator.companion import roll, render_sprite, render_face, sprite_frame_count
    bones = roll(user_id)
    return {
        "sprite": render_sprite(bones, frame),
        "face": render_face(bones),
        "species": bones.species,
        "frame_count": sprite_frame_count(bones.species),
    }


class CompanionNameReq(BaseModel):
    name: str
    personality: str = ""

@app.post("/api/companion/{user_id}/name")
def companion_set_name(user_id: str, req: CompanionNameReq):
    """Set companion name and personality (soul)."""
    from orchestrator.features import feature
    if not feature("BUDDY_COMPANION"):
        return JSONResponse(status_code=403, content={"error": "BUDDY_COMPANION flag disabled"})
    from orchestrator.companion import save_soul, CompanionSoul
    soul = CompanionSoul(name=req.name, personality=req.personality)
    save_soul(user_id, soul)
    return {"status": "saved", "name": req.name}


# ── Voice Input ───────────────────────────────────────────────────────────

@app.get("/api/voice/capabilities")
def voice_capabilities():
    """Check voice input dependencies."""
    from orchestrator.features import feature
    if not feature("VOICE_INPUT"):
        return JSONResponse(status_code=403, content={"error": "VOICE_INPUT flag disabled"})
    from orchestrator.voice_input import check_capabilities
    return check_capabilities().to_dict()


class VoiceTranscribeReq(BaseModel):
    audio_path: str
    language: str = "en"

@app.post("/api/voice/transcribe")
def voice_transcribe(req: VoiceTranscribeReq):
    """Transcribe an audio file using Whisper."""
    from orchestrator.features import feature
    if not feature("VOICE_INPUT"):
        return JSONResponse(status_code=403, content={"error": "VOICE_INPUT flag disabled"})
    from orchestrator.voice_input import transcribe_file
    try:
        result = transcribe_file(req.audio_path, req.language)
        return result.to_dict()
    except FileNotFoundError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/voice/languages")
def voice_languages():
    """Get supported transcription languages."""
    from orchestrator.voice_input import get_supported_languages
    return {"languages": get_supported_languages()}


# ── Artifact Templates ────────────────────────────────────────────────────

@app.get("/api/templates")
def list_templates(category: str = None):
    """List artifact templates."""
    from orchestrator.features import feature
    if not feature("ARTIFACT_TEMPLATES"):
        return JSONResponse(status_code=403, content={"error": "ARTIFACT_TEMPLATES flag disabled"})
    from orchestrator.artifact_templates import get_template_store
    store = get_template_store()
    return {"templates": store.list_templates(category), "categories": store.get_categories()}


@app.get("/api/templates/{template_id}")
def get_template(template_id: str):
    """Get a specific template."""
    from orchestrator.features import feature
    if not feature("ARTIFACT_TEMPLATES"):
        return JSONResponse(status_code=403, content={"error": "ARTIFACT_TEMPLATES flag disabled"})
    from orchestrator.artifact_templates import get_template_store
    tmpl = get_template_store().get_template(template_id)
    if not tmpl:
        return JSONResponse(status_code=404, content={"error": "Template not found"})
    return tmpl.to_dict()


class TemplateCreateReq(BaseModel):
    name: str
    description: str
    language: str
    category: str
    content: str
    variables: list = []

@app.post("/api/templates")
def create_template(req: TemplateCreateReq):
    """Create a custom template."""
    from orchestrator.features import feature
    if not feature("ARTIFACT_TEMPLATES"):
        return JSONResponse(status_code=403, content={"error": "ARTIFACT_TEMPLATES flag disabled"})
    from orchestrator.artifact_templates import get_template_store
    tmpl = get_template_store().create_template(
        name=req.name, description=req.description, language=req.language,
        category=req.category, content=req.content, variables=req.variables,
    )
    return tmpl.to_dict()


class TemplateRenderReq(BaseModel):
    variables: dict = {}

@app.post("/api/templates/{template_id}/render")
def render_template(template_id: str, req: TemplateRenderReq):
    """Render a template with variables."""
    from orchestrator.features import feature
    if not feature("ARTIFACT_TEMPLATES"):
        return JSONResponse(status_code=403, content={"error": "ARTIFACT_TEMPLATES flag disabled"})
    from orchestrator.artifact_templates import get_template_store
    result = get_template_store().render_template(template_id, req.variables)
    if result is None:
        return JSONResponse(status_code=404, content={"error": "Template not found"})
    return {"rendered": result, "template_id": template_id}


@app.delete("/api/templates/{template_id}")
def delete_template(template_id: str):
    """Delete a custom template."""
    from orchestrator.features import feature
    if not feature("ARTIFACT_TEMPLATES"):
        return JSONResponse(status_code=403, content={"error": "ARTIFACT_TEMPLATES flag disabled"})
    from orchestrator.artifact_templates import get_template_store
    ok = get_template_store().delete_template(template_id)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "Template not found or is builtin"})
    return {"status": "deleted", "template_id": template_id}


@app.get("/api/templates/editor/config")
def template_editor_config(language: str = "html"):
    """Get Monaco editor configuration for a language."""
    from orchestrator.artifact_templates import get_template_store
    return get_template_store().get_editor_config(language)


# ── Auth & Rate Limiting ──────────────────────────────────────────────────

class AuthTokenReq(BaseModel):
    subject: str
    scopes: list = ["*"]
    expiry_seconds: int = 86400

@app.post("/api/auth/token")
def auth_create_token(req: AuthTokenReq):
    """Create a JWT token."""
    from orchestrator.features import feature
    if not feature("API_AUTH"):
        return JSONResponse(status_code=403, content={"error": "API_AUTH flag disabled"})
    from orchestrator.auth import create_token
    token = create_token({"sub": req.subject, "scopes": req.scopes}, expiry_seconds=req.expiry_seconds)
    return {"token": token, "expires_in": req.expiry_seconds}


class AuthValidateReq(BaseModel):
    token: str

@app.post("/api/auth/validate")
def auth_validate_token(req: AuthValidateReq):
    """Validate a JWT token."""
    from orchestrator.auth import validate_token
    payload = validate_token(req.token)
    if payload:
        return {"valid": True, "payload": payload}
    return {"valid": False, "payload": None}


class ApiKeyCreateReq(BaseModel):
    name: str
    scopes: list = ["*"]

@app.post("/api/auth/keys")
def auth_create_api_key(req: ApiKeyCreateReq):
    """Create an API key."""
    from orchestrator.features import feature
    if not feature("API_AUTH"):
        return JSONResponse(status_code=403, content={"error": "API_AUTH flag disabled"})
    from orchestrator.auth import create_api_key
    return create_api_key(req.name, req.scopes)


@app.get("/api/auth/keys")
def auth_list_api_keys():
    """List all API keys."""
    from orchestrator.features import feature
    if not feature("API_AUTH"):
        return JSONResponse(status_code=403, content={"error": "API_AUTH flag disabled"})
    from orchestrator.auth import list_api_keys
    return {"keys": list_api_keys()}


@app.get("/api/ratelimit/status")
def ratelimit_status(client_id: str = "default"):
    """Check rate limit status for a client."""
    from orchestrator.auth import get_rate_limiter
    limiter = get_rate_limiter()
    return limiter.get_status(client_id)


# ── System Context Verification ───────────────────────────────────────────────

@app.get("/api/system-context/verify")
def verify_system_context():
    """Verify that capabilities context is properly injected into system prompts.
    
    Returns metadata about each system prompt and capability context state.
    """
    try:
        from orchestrator.capabilities_context import get_all_contexts
        from orchestrator import ceo, agentic_loop
        from orchestrator.prompts import auto_solve_system
        
        contexts = get_all_contexts()
        
        # Verify contexts are injected
        routing_injected = "You are the CEO" in ceo.ROUTING_SYSTEM
        agent_injected = "You are an autonomous AI agent" in agentic_loop.AGENT_SYSTEM
        
        # Build a test auto-solve prompt to verify
        test_tool_section = "Available tools: generate_image, read_file"
        test_prompt = auto_solve_system.build_system_prompt(test_tool_section)
        autosolve_injected = "autonomous" in test_prompt
        
        return {
            "status": "verified",
            "timestamp": time.time(),
            "contexts": {
                "routing": contexts.get("routing", "")[:100] + "...",
                "agent": contexts.get("agent", "")[:100] + "...",
                "autosolve": contexts.get("autosolve", "")[:100] + "...",
                "capabilities": contexts.get("capabilities", "")[:100] + "...",
            },
            "injection_status": {
                "routing_system": routing_injected,
                "agent_system": agent_injected,
                "autosolve_system": autosolve_injected,
                "all_injected": routing_injected and agent_injected and autosolve_injected,
            },
            "token_estimate": {
                "routing_context": len(contexts.get("routing", "").split()) * 1.3,  # Rough token estimate
                "agent_context": len(contexts.get("agent", "").split()) * 1.3,
                "autosolve_context": len(contexts.get("autosolve", "").split()) * 1.3,
                "total_overhead": (
                    len(contexts.get("routing", "").split()) +
                    len(contexts.get("agent", "").split()) +
                    len(contexts.get("autosolve", "").split())
                ) * 1.3,
            },
            "help_coverage": {
                "total_help_files": 10,  # help/01-getting-started through help/10-system-architecture
                "new_help_files": ["07-agent-modes.md", "08-auto-solve.md", "09-reasoning-models.md", "10-system-architecture.md"],
            },
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "timestamp": time.time(),
        }


# ═══════════════════════════════════════════════════════════════════════════
# SPRINT 8 — Skillify, DreamTask, Key Encryption
# ═══════════════════════════════════════════════════════════════════════════

# ── Skillify (6A) ────────────────────────────────────────────────────────

class SkillifyReq(BaseModel):
    description: str
    messages: list = []
    generate_code: bool = False

@app.post("/api/skillify")
def api_skillify(req: SkillifyReq):
    from orchestrator.features import feature
    if not feature("SKILLIFY"):
        return {"error": "SKILLIFY feature flag is disabled"}
    from orchestrator.skillify import skillify
    result = skillify(req.description, req.messages, req.generate_code)
    return result.to_dict()

@app.get("/api/skillify/list")
def api_skillify_list():
    from orchestrator.features import feature
    if not feature("SKILLIFY"):
        return {"error": "SKILLIFY feature flag is disabled"}
    from orchestrator.skillify import list_generated_skills
    return {"skills": list_generated_skills()}

@app.delete("/api/skillify/{skill_id}")
def api_skillify_delete(skill_id: str):
    from orchestrator.features import feature
    if not feature("SKILLIFY"):
        return {"error": "SKILLIFY feature flag is disabled"}
    from orchestrator.skillify import delete_generated_skill
    success = delete_generated_skill(skill_id)
    if not success:
        return {"error": f"Skill '{skill_id}' not found"}
    return {"deleted": skill_id}


class GenerateSkillReq_old(BaseModel):  # kept for reference; active one is above
    pass

# ── DreamTask (6B) ───────────────────────────────────────────────────────

@app.get("/api/dream/status")
def api_dream_status():
    from orchestrator.features import feature
    if not feature("DREAM_TASK"):
        return {"error": "DREAM_TASK feature flag is disabled"}
    from orchestrator.dream_task import get_dreamer
    return get_dreamer().get_status()

@app.post("/api/dream/trigger")
async def api_dream_trigger():
    from orchestrator.features import feature
    if not feature("DREAM_TASK"):
        return {"error": "DREAM_TASK feature flag is disabled"}
    from orchestrator.dream_task import get_dreamer
    result = await get_dreamer().trigger()
    return result.to_dict()

@app.post("/api/dream/start")
def api_dream_start():
    from orchestrator.features import feature
    if not feature("DREAM_TASK"):
        return {"error": "DREAM_TASK feature flag is disabled"}
    from orchestrator.dream_task import get_dreamer
    dreamer = get_dreamer()
    dreamer.start()
    return {"started": True, "phase": str(dreamer.phase)}

@app.post("/api/dream/stop")
def api_dream_stop():
    from orchestrator.features import feature
    if not feature("DREAM_TASK"):
        return {"error": "DREAM_TASK feature flag is disabled"}
    from orchestrator.dream_task import get_dreamer
    dreamer = get_dreamer()
    dreamer.stop()
    return {"stopped": True, "phase": str(dreamer.phase)}

@app.get("/api/dream/history")
def api_dream_history():
    from orchestrator.features import feature
    if not feature("DREAM_TASK"):
        return {"error": "DREAM_TASK feature flag is disabled"}
    from orchestrator.dream_task import get_dreamer
    return {"history": get_dreamer().get_history()}

@app.post("/api/dream/activity")
def api_dream_activity():
    from orchestrator.features import feature
    if not feature("DREAM_TASK"):
        return {"error": "DREAM_TASK feature flag is disabled"}
    from orchestrator.dream_task import get_dreamer
    get_dreamer().record_activity()
    return {"recorded": True}

# ── Key Encryption (7B) ─────────────────────────────────────────────────

@app.get("/api/keys/encryption/status")
def api_key_encryption_status():
    from orchestrator.key_encryption import get_encryption_status
    return get_encryption_status()

@app.post("/api/keys/encryption/migrate")
def api_key_encryption_migrate():
    from orchestrator.features import feature
    if not feature("API_KEY_ENCRYPTION"):
        return {"error": "API_KEY_ENCRYPTION feature flag is disabled"}
    from orchestrator.key_encryption import migrate_keys_to_keyring
    return migrate_keys_to_keyring()

@app.get("/api/keys/encrypted/list")
def api_keys_encrypted_list():
    from orchestrator.key_encryption import encrypted_list_providers
    return {"providers": encrypted_list_providers()}

class EncryptedKeySetReq(BaseModel):
    provider: str
    key: str

@app.post("/api/keys/encrypted/set")
def api_keys_encrypted_set(req: EncryptedKeySetReq):
    from orchestrator.key_encryption import encrypted_set_key
    return encrypted_set_key(req.provider, req.key)

@app.delete("/api/keys/encrypted/{provider}")
def api_keys_encrypted_remove(provider: str):
    from orchestrator.key_encryption import encrypted_remove_key
    return encrypted_remove_key(provider)


# ---------- Stress Test Endpoints ----------

@app.post("/api/stress-test/run")
async def stress_test_run(body: dict = Body(...)):
    """Run stress tests on specified models (or all if not specified)."""
    models = body.get("models", None)
    
    runner = stress_test.StressTestRunner()
    results = await runner.run_all_tests(models)
    report = runner.generate_report()
    
    return {
        "status": "complete",
        "total_tests": len(results),
        "report": report,
        "raw_results": [asdict(r) for r in results[:10]]  # First 10 for preview
    }


@app.post("/api/stress-test/run-single")
async def stress_test_run_single(body: dict = Body(...)):
    """Run a single test case on a single model for quick feedback."""
    model_name = body.get("model", "max")
    category = body.get("category", "code")
    prompt = body.get("prompt", "")
    
    if not prompt:
        raise HTTPException(400, "prompt is required")
    
    runner = stress_test.StressTestRunner()
    test_case = stress_test.TestCase(
        id="custom_test",
        category=category,
        prompt=prompt,
        expected_outcome="",
        quality_rubric="Functional and complete"
    )
    
    result = await runner._run_single_test(test_case, model_name)
    
    return {
        "status": "complete",
        "result": asdict(result),
        "recommendation": f"Quality score: {result.quality_score:.1f}/100, Passed: {result.passed}"
    }


@app.get("/api/stress-test/suggestions")
def stress_test_get_suggestions():
    """Get auto-tuning suggestions from the last test run."""
    runner = stress_test.StressTestRunner()
    tuner = stress_test.AutoTuner()
    
    # Generate dummy report for now (would use cached results in production)
    # This would be pulled from memory in a real impl
    return {
        "status": "ready",
        "message": "Run /api/stress-test/run first to generate suggestions",
        "next_step": "POST /api/stress-test/run to execute tests"
    }


@app.get("/api/stress-test/test-cases")
def stress_test_list_tests():
    """List all available stress test cases."""
    runner = stress_test.StressTestRunner()
    tests = [
        {
            "id": t.id,
            "category": t.category,
            "prompt_preview": t.prompt[:100],
            "rubric": t.quality_rubric
        }
        for t in runner.test_cases
    ]
    return {
        "total": len(tests),
        "tests": tests
    }


# ── THE MACHINE API endpoints ────────────────────────────────────────────────

class MachineCreateReq(BaseModel):
    prompt: str
    session_id: str | None = None
    linked_dir: str | None = None
    tasks: list[str] | None = None
    ceo_speed: str | None = None          # fast | balanced | reasoning
    allowed_muscles: list[str] | None = None  # e.g. ["GWEN", "MAX"]

class MachineUpdatePromptReq(BaseModel):
    prompt: str

class MachineFileWriteReq(BaseModel):
    filepath: str
    content: str

class MachineLinkDirReq(BaseModel):
    dir_path: str

@app.post("/api/machine/projects")
async def machine_create_project(req: MachineCreateReq):
    """Create a new MACHINE project from a prompt. CEO decomposes into subtasks."""
    import asyncio
    loop = asyncio.get_event_loop()
    project = await loop.run_in_executor(
        None,
        lambda: machine_engine.create_project(
            req.prompt, session_id=req.session_id, linked_dir=req.linked_dir,
            tasks=req.tasks, ceo_speed=req.ceo_speed, allowed_muscles=req.allowed_muscles,
        )
    )
    pid = (project or {}).get("project_id") or (project or {}).get("id")
    return {"success": True, "project": project}


class BlueprintCreateReq(BaseModel):
    prompt: str
    nodes: list[dict]
    execution_mode: str = "pipeline_with_parallel"
    session_id: str | None = None


@app.post("/api/machine/projects/from-blueprint")
def machine_create_from_blueprint(req: BlueprintCreateReq):
    """Create a MACHINE project directly from blueprint nodes — bypasses CEO decomposition."""
    from orchestrator.workflow_templates import _create_from_blueprint
    manifest = _create_from_blueprint(
        prompt=req.prompt,
        nodes=req.nodes,
        execution_mode=req.execution_mode,
        session_id=req.session_id,
    )
    return {"success": True, "project": manifest}


@app.get("/api/machine/projects")
def machine_list_projects(limit: int = 100):
    """List MACHINE projects (most recent first, capped at limit)."""
    return {"projects": machine_engine.list_projects(limit=limit)}

@app.get("/api/machine/projects/{project_id}")
def machine_get_project(project_id: str):
    """Get a MACHINE project manifest."""
    project = machine_engine.get_project(project_id)
    if not project:
        raise HTTPException(404, f"Project not found: {project_id}")
    return project

@app.delete("/api/machine/projects/{project_id}")
def machine_delete_project(project_id: str):
    """Delete a MACHINE project."""
    if machine_engine.delete_project(project_id):
        return {"status": "deleted", "project_id": project_id}
    raise HTTPException(404, f"Project not found: {project_id}")

@app.post("/api/machine/projects/{project_id}/run")
def machine_run_project(project_id: str):
    """Execute all pending nodes in a MACHINE project sequentially."""
    # Start filesystem watcher for live node spawning
    _start_project_watcher(project_id)
    result = machine_engine.run_project(project_id)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result

class MachineSwarmReq(BaseModel):
    swarm_model: str | None = None

@app.post("/api/machine/projects/{project_id}/swarm")
def machine_swarm_project(project_id: str, req: MachineSwarmReq = MachineSwarmReq()):
    """Execute all pending nodes in PARALLEL (swarm mode). Returns immediately; poll for progress."""
    # Start filesystem watcher for live node spawning
    _start_project_watcher(project_id)
    result = machine_engine.run_project_swarm(project_id, swarm_model=req.swarm_model)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result

@app.get("/api/machine/vram")
def machine_vram_status():
    """Get current VRAM usage from Ollama for monitoring."""
    import urllib.request, json as _json
    try:
        ollama_url = getattr(config, 'OLLAMA_URL', 'http://localhost:11434')
        req = urllib.request.Request(f"{ollama_url}/api/ps")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read())
        models = []
        total_vram = 0
        for m in data.get("models", []):
            size_vram = m.get("size_vram", m.get("size", 0))
            total_vram += size_vram
            models.append({
                "name": m.get("name", "?"),
                "size_vram_gb": round(size_vram / (1024**3), 2),
                "size_gb": round(m.get("size", 0) / (1024**3), 2),
                "expires_at": m.get("expires_at", ""),
                "details": m.get("details", {}),
            })
        _gname, _vtotal_mb, _vfree_mb = _detect_gpu_vram_mb()
        _vtotal_gb = round(_vtotal_mb / 1024, 1) if _vtotal_mb else 0.0
        _vfree_gb = round(_vfree_mb / 1024, 1) if _vfree_mb else round(_vtotal_gb - total_vram / (1024**3), 2)
        _used_gb = round(total_vram / (1024**3), 2)
        return {
            "models": models,
            "total_vram_used_gb": _used_gb,
            "gpu_total_gb": _vtotal_gb,
            "gpu_free_gb": round(_vtotal_gb - _used_gb, 2),
            "utilization_pct": round(_used_gb / _vtotal_gb * 100, 1) if _vtotal_gb > 0 else 0,
        }
    except Exception as e:
        _gname2, _vtotal_mb2, _vfree_mb2 = _detect_gpu_vram_mb()
        _vtotal_gb2 = round(_vtotal_mb2 / 1024, 1) if _vtotal_mb2 else 0.0
        return {"models": [], "total_vram_used_gb": 0, "gpu_total_gb": _vtotal_gb2, "gpu_free_gb": _vtotal_gb2, "utilization_pct": 0, "error": str(e)}

@app.get("/api/machine/ceo-options")
def machine_ceo_options():
    """Return available CEO model options with capability descriptions."""
    from orchestrator.ceo import get_ceo_options
    return {"options": get_ceo_options()}

@app.post("/api/machine/projects/{project_id}/nodes/{node_id}/run")
def machine_run_node(project_id: str, node_id: int):
    """Execute a single node in a MACHINE project."""
    result = machine_engine.execute_node(project_id, node_id)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result

@app.put("/api/machine/projects/{project_id}/prompt")
def machine_update_prompt(project_id: str, req: MachineUpdatePromptReq):
    """Update a project's prompt and re-decompose into new subtasks."""
    result = machine_engine.update_prompt(project_id, req.prompt)
    if not result:
        raise HTTPException(404, f"Project not found: {project_id}")
    return result


# ── Node steering endpoints ──────────────────────────────────────

class MachineNodeEditReq(BaseModel):
    task: str | None = None
    muscle: str | None = None
    depends_on: list[str] | None = None
    tier: str | None = None
    model_override: str | None = None
    model: str | None = None
    agent_framework: str | None = None
    capabilities_required: list[str] | None = None
    agent_framework: str | None = None
    model: str | None = None
    model_override: str | None = None
    capabilities_required: list[str] | None = None

class MachineNodeAddReq(BaseModel):
    task: str
    muscle: str = "NEMOTRON"
    depends_on: list[str] | None = None
    tier: str = "fast"
    action: str = "generate"
    cmd: str = ""
    agent_framework: str = "openclaw"
    model: str | None = None
    model_override: str | None = None
    capabilities_required: list[str] | None = None

@app.patch("/api/machine/projects/{project_id}/nodes/{node_id}")
def machine_edit_node(project_id: str, node_id: int, req: MachineNodeEditReq):
    """Edit a pending/error node's task, runtime assignment, dependencies, or tier."""
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "No fields to update")
    result = machine_engine.edit_node(project_id, node_id, updates)
    if not result:
        raise HTTPException(404, f"Node {node_id} not found in project {project_id}")
    if "error" in result:
        raise HTTPException(409, result["error"])
    return result

@app.post("/api/machine/projects/{project_id}/nodes")
def machine_add_node(project_id: str, req: MachineNodeAddReq):
    """Add a new node to an existing project."""
    result = machine_engine.add_node(
        project_id, req.task, req.muscle,
        depends_on=req.depends_on, tier=req.tier,
        action=req.action, cmd=req.cmd,
        agent_framework=req.agent_framework,
        model=req.model,
        model_override=req.model_override,
        capabilities_required=req.capabilities_required,
    )
    if not result:
        raise HTTPException(404, f"Project not found: {project_id}")
    return result

@app.post("/api/machine/projects/{project_id}/nodes/{node_id}/reset")
def machine_reset_node(project_id: str, node_id: int):
    """Reset a completed/errored node back to pending so it can be re-run."""
    result = machine_engine.reset_node(project_id, node_id)
    if not result:
        raise HTTPException(404, f"Node {node_id} not found in project {project_id}")
    if "error" in result:
        raise HTTPException(409, result["error"])
    return result

@app.delete("/api/machine/projects/{project_id}/nodes/{node_id}")
def machine_remove_node(project_id: str, node_id: int):
    """Remove a node from the project (not running nodes)."""
    if machine_engine.remove_node(project_id, node_id):
        return {"status": "removed", "node_id": node_id}
    raise HTTPException(404, f"Cannot remove node {node_id}")

@app.post("/api/machine/projects/{project_id}/dag")
def machine_dag_project(project_id: str, req: MachineSwarmReq = MachineSwarmReq()):
    """Execute nodes respecting dependency graph (DAG mode). Independent nodes run parallel."""
    _start_project_watcher(project_id)
    result = machine_engine.run_project_dag(project_id, swarm_model=req.swarm_model)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


# ── Project Scheduling API ────────────────────────────────────────────────────

class MachineScheduleReq(BaseModel):
    run_at: str  # ISO-8601 datetime string, must be in the future
    execution_mode: str = "swarm"  # "swarm" | "dag" | "pipeline"
    swarm_model: str | None = None

@app.post("/api/machine/projects/{project_id}/schedule")
def machine_schedule_project(project_id: str, req: MachineScheduleReq):
    """
    Schedule a project to execute at a future time.
    Sets status='scheduled' and fires automatically when run_at passes.
    """
    result = machine_engine.schedule_project(
        project_id,
        run_at=req.run_at,
        execution_mode=req.execution_mode,
        swarm_model=req.swarm_model,
    )
    if result is None:
        raise HTTPException(404, f"Project not found: {project_id}")
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result

@app.delete("/api/machine/projects/{project_id}/schedule")
def machine_cancel_schedule(project_id: str):
    """Cancel a scheduled run, returning the project to 'ready' status."""
    result = machine_engine.cancel_schedule(project_id)
    if result is None:
        raise HTTPException(404, f"Project not found: {project_id}")
    if "error" in result:
        raise HTTPException(409, result["error"])
    return result

@app.get("/api/machine/schedule")
def machine_list_scheduled():
    """List all projects with status='scheduled' and their scheduled run times."""
    return {"scheduled": machine_engine.get_scheduled_projects()}


class ArchitectReq(BaseModel):
    topic: str = ""
    muscle_a: str = "GWEN"
    muscle_b: str = "MAX"

@app.post("/api/machine/architect")
def machine_create_architect(req: ArchitectReq):
    """Create and start an architect mode project — two agents collaborate on a live canvas."""
    topic = req.topic or "a creative animated scene"
    manifest = machine_engine.create_architect_project(topic, req.muscle_a, req.muscle_b)
    if "error" in manifest:
        raise HTTPException(500, manifest["error"])
    project_id = manifest["project_id"]
    _start_project_watcher(project_id)
    result = machine_engine.run_project_architect(project_id)
    if "error" in result:
        raise HTTPException(500, result["error"])
    return result

@app.post("/api/machine/projects/{project_id}/architect")
def machine_run_architect(project_id: str):
    """Start architect mode on an existing project with 2 nodes."""
    _start_project_watcher(project_id)
    result = machine_engine.run_project_architect(project_id)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result

@app.post("/api/machine/city")
def machine_create_city(req: ArchitectReq):
    """Create and start a city-building architect project (friendly alias for /architect)."""
    topic = req.topic or "a sprawling futuristic city at night"
    manifest = machine_engine.create_architect_project(topic, req.muscle_a, req.muscle_b)
    if "error" in manifest:
        raise HTTPException(500, manifest["error"])
    project_id = manifest["project_id"]
    _start_project_watcher(project_id)
    result = machine_engine.run_project_architect(project_id)
    if "error" in result:
        raise HTTPException(500, result["error"])
    return result


@app.get("/api/machine/projects/{project_id}/files")
def machine_list_files(project_id: str):
    """List files in a project's workspace."""
    files = machine_engine.list_workspace_files(project_id)
    return {"files": files}

@app.get("/api/machine/projects/{project_id}/files/{filepath:path}")
def machine_read_file(project_id: str, filepath: str):
    """Read a file from a project's workspace."""
    content = machine_engine.read_workspace_file(project_id, filepath)
    if content is None:
        raise HTTPException(404, f"File not found: {filepath}")
    return {"filepath": filepath, "content": content}

@app.get("/api/machine/projects/{project_id}/raw/{filepath:path}")
def machine_serve_raw(project_id: str, filepath: str):
    """Serve a workspace file as raw bytes (HTML, JS, etc.) for direct iframe src."""
    from fastapi.responses import Response as _FastAPIResponse
    import mimetypes as _mt
    content = machine_engine.read_workspace_file(project_id, filepath)
    if content is None:
        raise HTTPException(404, f"File not found: {filepath}")
    mime, _ = _mt.guess_type(filepath)
    if mime is None:
        mime = "text/html"
    return _FastAPIResponse(content=content, media_type=mime, headers={
        "Cache-Control": "no-store, no-cache",
    })

@app.post("/api/machine/projects/{project_id}/files")
def machine_write_file(project_id: str, req: MachineFileWriteReq):
    """Write a file to a project's workspace."""
    if machine_engine.write_workspace_file(project_id, req.filepath, req.content):
        return {"status": "written", "filepath": req.filepath}
    raise HTTPException(400, "Failed to write file (project not found or path invalid)")


# ── Filesystem watcher + SSE for live node spawning ──────────────────────────

def _start_project_watcher(project_id: str):
    """Start a filesystem watcher for a project's workspace and linked dir (idempotent)."""
    try:
        from orchestrator.workspace_watcher import start_watching
        manifest = machine_engine.get_project(project_id)
        if manifest and manifest.get("workspace_dir"):
            start_watching(project_id, manifest["workspace_dir"])
        if manifest and manifest.get("linked_dir"):
            # Build slug→node_id mapping so linked_dir files map to parent nodes
            slug_map = {}
            for node in manifest.get("nodes", []):
                task = node.get("task", f"node_{node['id']}")
                slug = re.sub(r'[^a-z0-9]+', '_', task.lower()).strip('_')[:60]
                slug_map[slug] = node["id"]
            start_watching(project_id + "_linked", manifest["linked_dir"],
                           broadcast_as=project_id, slug_to_node=slug_map)
    except Exception:
        pass  # Non-critical — watcher is optional


@app.get("/api/machine/projects/{project_id}/events")
async def machine_project_events(project_id: str):
    """SSE stream of filesystem events for a project workspace."""
    manifest = machine_engine.get_project(project_id)
    if not manifest:
        raise HTTPException(404, f"Project not found: {project_id}")

    # Ensure watcher is running
    _start_project_watcher(project_id)

    from orchestrator.workspace_watcher import event_stream as ws_event_stream

    return StreamingResponse(
        ws_event_stream(project_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/machine/projects/{project_id}/summary")
def machine_get_summary(project_id: str):
    """Get project summary — reads _summary.md from workspace, or generates it."""
    content = machine_engine.read_workspace_file(project_id, "_summary.md")
    if content is None:
        # Try to generate if project is complete
        machine_engine.generate_summary(project_id)
        content = machine_engine.read_workspace_file(project_id, "_summary.md")
    if content is None:
        raise HTTPException(404, "Summary not available yet")
    # Also return per-node summaries for UI
    manifest = machine_engine.get_project(project_id)
    node_summaries = []
    if manifest:
        for n in manifest["nodes"]:
            node_summaries.append({
                "id": n["id"],
                "label": n["label"],
                "icon": n["icon"],
                "role": n["role"],
                "muscle": n["muscle"],
                "task": n["task"],
                "status": n["status"],
                "output_file": n.get("output_file"),
                "preview": (n.get("result") or "")[:200],
                "tokens_used": n.get("tokens_used", 0),
                "model_used": n.get("model_used"),
            })
    return {"summary": content, "nodes": node_summaries}

@app.get("/api/machine/projects/{project_id}/status")
def machine_get_status(project_id: str):
    """Get execution status of project and models."""
    manifest = machine_engine.get_project(project_id)
    if not manifest:
        raise HTTPException(404, f"Project not found: {project_id}")
    
    # Count nodes by status
    total = len(manifest.get("nodes", []))
    done = len([n for n in manifest.get("nodes", []) if n.get("status") in ("done", "complete")])
    running = len([n for n in manifest.get("nodes", []) if n.get("status") == "running"])
    pending = len([n for n in manifest.get("nodes", []) if n.get("status") in ("pending", None)])
    
    # Get VRAM status
    import urllib.request, json as _json
    _gn, _vt_mb, _vf_mb = _detect_gpu_vram_mb()
    vram_info = {"total": round(_vt_mb / 1024, 1) if _vt_mb else 0.0, "used": 0.0, "models": []}
    try:
        ollama_url = getattr(config, 'OLLAMA_URL', 'http://localhost:11434')
        req = urllib.request.Request(f"{ollama_url}/api/ps")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read())
        total_vram = 0
        for m in data.get("models", []):
            size_vram = m.get("size_vram", m.get("size", 0))
            total_vram += size_vram
            vram_info["models"].append(m.get("name", "?"))
        vram_info["used"] = round(total_vram / (1024**3), 2)
    except:
        pass
    
    return {
        "project_id": project_id,
        "status": manifest.get("status"),
        "nodes": {"total": total, "done": done, "running": running, "pending": pending},
        "vram": vram_info,
    }

@app.post("/api/machine/projects/{project_id}/stop")
def machine_stop_project(project_id: str):
    """Stop execution and stop all models (unload from VRAM)."""
    manifest = machine_engine.get_project(project_id)
    if not manifest:
        raise HTTPException(404, f"Project not found: {project_id}")
    
    # Mark any running nodes as stopped
    for node in manifest.get("nodes", []):
        if node.get("status") == "running":
            node["status"] = "stopped"
    
    manifest["status"] = "stopped"
    machine_engine._save_manifest(os.path.join(machine_engine.PROJECTS_DIR, project_id), manifest)
    
    return {
        "status": "stopped",
        "message": "Project execution stopped. Models will be unloaded on next cycle.",
        "project_id": project_id,
    }

@app.get("/api/machine/projects/{project_id}/tree")
def machine_browse_tree(project_id: str, subpath: str = ""):
    """Browse the linked directory tree of a project."""
    tree = machine_engine.browse_directory(project_id, subpath=subpath)
    if tree is None:
        raise HTTPException(404, "No linked directory or project not found")
    return tree

@app.get("/api/machine/projects/{project_id}/linked/{filepath:path}")
def machine_read_linked_file(project_id: str, filepath: str):
    """Read a file from the project's linked directory."""
    content = machine_engine.read_linked_file(project_id, filepath)
    if content is None:
        raise HTTPException(404, f"File not found or not accessible: {filepath}")
    return {"filepath": filepath, "content": content}

@app.put("/api/machine/projects/{project_id}/link")
def machine_link_directory(project_id: str, req: MachineLinkDirReq):
    """Link or update a project's linked directory."""
    result = machine_engine.link_directory(project_id, req.dir_path)
    if result is None:
        raise HTTPException(400, "Project not found or directory does not exist")
    return {"status": "linked", "linked_dir": result["linked_dir"]}

@app.post("/api/machine/open-file")
def machine_open_file(req: dict = Body(...)):
    """Open a file or directory in the platform's default application."""
    import platform
    import subprocess
    
    path = req.get("path")
    if not path:
        raise HTTPException(400, "No path provided")
    
    # Normalize and resolve to prevent directory traversal
    resolved = os.path.normpath(os.path.abspath(path))
    
    if not os.path.exists(resolved):
        # For node folders, try opening the parent workspace if node folder doesn't exist
        parent = os.path.dirname(resolved)
        if os.path.exists(parent):
            resolved = parent
        else:
            raise HTTPException(404, f"Path not found: {resolved}")
    
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(resolved)
        elif system == "Darwin":  # macOS
            subprocess.Popen(["open", resolved])
        else:  # Linux
            subprocess.Popen(["xdg-open", resolved])
        
        return {"status": "opened", "path": resolved}
    except Exception as e:
        raise HTTPException(500, f"Failed to open: {str(e)}")


# ── Node Runtime: PATCH, Proof, and Agent Discovery ──────────────────────────

class NodePatchReq(BaseModel):
    model: str | None = None
    model_override: str | None = None
    agent_framework: str | None = None
    capabilities_required: list[str] | None = None
    tools_profile_hint: list[str] | None = None
    action: str | None = None
    cmd: str | None = None
    timeout_seconds: int | None = None


@app.patch("/api/machine/projects/{project_id}/nodes/{node_id}")
def patch_machine_node(project_id: str, node_id: int, req: NodePatchReq):
    """
    Update a node's runtime assignment before or between executions.
    Allowed fields: model, model_override, agent_framework,
    capabilities_required, tools_profile_hint, action, cmd, timeout_seconds.
    Re-runs the runtime resolver with the new values and returns updated decision.
    """
    manifest = machine_engine.get_project(project_id)
    if not manifest:
        raise HTTPException(404, f"Project {project_id} not found")

    node = next((n for n in manifest["nodes"] if n["id"] == node_id), None)
    if not node:
        raise HTTPException(404, f"Node {node_id} not found in project {project_id}")

    # Apply non-None patch fields
    patch_data = req.model_dump(exclude_none=True)
    for k, v in patch_data.items():
        node[k] = v

    # Re-resolve runtime with updated node fields
    try:
        from orchestrator.node_runtime_resolver import resolve as _resolve
        rd = _resolve(
            node,
            prefer_framework=node.get("agent_framework"),
            model_override=node.get("model_override") or node.get("model"),
        )
        node["runtime_decision"] = rd.to_dict()
        node["agent_framework"] = rd.framework
        node["tools_profile"] = rd.tools_profile
    except Exception:
        pass  # Keep existing decision if resolver fails

    # Persist
    project_dir = os.path.join(machine_engine.PROJECTS_DIR, project_id)
    try:
        machine_engine._save_manifest(project_dir, manifest)
    except Exception as e:
        raise HTTPException(500, f"Failed to save: {e}")

    return {
        "project_id": project_id,
        "node_id": node_id,
        "runtime_decision": node.get("runtime_decision"),
        "agent_framework": node.get("agent_framework"),
        "tools_profile": node.get("tools_profile", []),
        "model": node.get("model"),
        "model_override": node.get("model_override"),
    }


@app.get("/api/machine/projects/{project_id}/nodes/{node_id}/proof")
def get_node_runtime_proof(project_id: str, node_id: int):
    """Return the runtime_proof for a completed node — shows what agent tech was used."""
    manifest = machine_engine.get_project(project_id)
    if not manifest:
        raise HTTPException(404, f"Project {project_id} not found")
    node = next((n for n in manifest["nodes"] if n["id"] == node_id), None)
    if not node:
        raise HTTPException(404, f"Node {node_id} not found")
    return {
        "project_id": project_id,
        "node_id": node_id,
        "status": node.get("status"),
        "runtime_decision": node.get("runtime_decision"),
        "runtime_proof": node.get("runtime_proof"),
    }


@app.get("/api/machine/agents/discover")
def discover_agent_frameworks():
    """
    Return all available agent frameworks with capability profiles and availability status.
    Used by the frontend to populate the node runtime selector and agent discovery panel.
    """
    try:
        from orchestrator.node_runtime_resolver import discover_available_frameworks
        frameworks = discover_available_frameworks()
    except Exception as e:
        raise HTTPException(500, f"Discovery failed: {e}")
    return {
        "frameworks": frameworks,
        "default_framework": "openclaw",
        "default_reason": "OpenClaw is the native runtime with full terminal, file, and tool-calling capabilities.",
    }


@app.get("/api/machine/projects/{project_id}/nodes/{node_id}/assignment")
def explain_node_assignment(project_id: str, node_id: int):
    """Explain why a node was assigned its current runtime framework."""
    manifest = machine_engine.get_project(project_id)
    if not manifest:
        raise HTTPException(404, f"Project {project_id} not found")
    node = next((n for n in manifest["nodes"] if n["id"] == node_id), None)
    if not node:
        raise HTTPException(404, f"Node {node_id} not found")
    try:
        from orchestrator.node_runtime_resolver import explain_node_assignment as _explain
        return _explain(node)
    except Exception as e:
        raise HTTPException(500, f"Explanation failed: {e}")


# ── Workflow Templates ────────────────────────────────────────────────────────

from orchestrator import workflow_templates as _wf_templates

class WorkflowSaveReq(BaseModel):
    name: str | None = None
    tags: list[str] | None = None

class WorkflowLaunchReq(BaseModel):
    prompt_override: str | None = None
    session_id: str | None = None

@app.get("/api/machine/workflows")
def list_workflows():
    """List all saved workflow templates."""
    return _wf_templates.list_workflows()

@app.get("/api/machine/workflows/{template_id}")
def get_workflow(template_id: str):
    """Get a single workflow template with full node blueprints."""
    t = _wf_templates.get_workflow(template_id)
    if not t:
        raise HTTPException(404, f"Workflow template not found: {template_id}")
    return t

@app.post("/api/machine/projects/{project_id}/save-workflow")
def save_as_workflow(project_id: str, req: WorkflowSaveReq):
    """Save a project's node structure as a reusable workflow template."""
    manifest = machine_engine.get_project(project_id)
    if not manifest:
        raise HTTPException(404, f"Project not found: {project_id}")
    template = _wf_templates.save_workflow(manifest, name=req.name, tags=req.tags)
    return {"status": "saved", "template": template}

@app.post("/api/machine/workflows/{template_id}/launch")
def launch_workflow(template_id: str, req: WorkflowLaunchReq):
    """Launch a new project from a saved workflow template."""
    result = _wf_templates.launch_workflow(
        template_id,
        prompt_override=req.prompt_override,
        session_id=req.session_id,
    )
    if result.get("error"):
        raise HTTPException(404, result["error"])
    return result

@app.delete("/api/machine/workflows/{template_id}")
def delete_workflow(template_id: str):
    """Delete a saved workflow template."""
    ok = _wf_templates.delete_workflow(template_id)
    if not ok:
        raise HTTPException(404, f"Workflow template not found: {template_id}")
    return {"status": "deleted", "template_id": template_id}


# ── Research Loop Control endpoints ─────────────────────────────────────────

import subprocess as _subprocess
import signal as _signal

_RESEARCH_LOOP_PROC: "_subprocess.Popen | None" = None
_RESEARCH_STOP_FLAG = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "_research_stop.flag")
_RESEARCH_LOOP_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "research_loop.py")
_RESEARCH_STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "research_reports", "loop_state.json")


@app.post("/api/research/start")
def research_start(cooldown_seconds: int = 0, max_cycles: int = 0):
    """Start the R&D research loop as a background subprocess."""
    global _RESEARCH_LOOP_PROC
    # Remove stale stop flag if present
    if os.path.exists(_RESEARCH_STOP_FLAG):
        os.remove(_RESEARCH_STOP_FLAG)
    # If already running, return current status
    if _RESEARCH_LOOP_PROC is not None and _RESEARCH_LOOP_PROC.poll() is None:
        return {"status": "already_running", "pid": _RESEARCH_LOOP_PROC.pid}
    cmd = [sys.executable, _RESEARCH_LOOP_SCRIPT]
    if cooldown_seconds:
        cmd += ["--cooldown-seconds", str(cooldown_seconds)]
    if max_cycles:
        cmd += ["--max-cycles", str(max_cycles)]
    _RESEARCH_LOOP_PROC = _subprocess.Popen(
        cmd,
        cwd=os.path.dirname(_RESEARCH_LOOP_SCRIPT),
        stdout=_subprocess.PIPE,
        stderr=_subprocess.STDOUT,
        text=True,
    )
    webbrowser.open("http://127.0.0.1:8001/pages/research-loop.html")
    return {"status": "started", "pid": _RESEARCH_LOOP_PROC.pid}


@app.post("/api/research/stop")
def research_stop():
    """Signal the research loop to stop after the current cycle completes."""
    # Write stop flag — loop_gate node checks for this
    with open(_RESEARCH_STOP_FLAG, "w", encoding="utf-8") as f:
        f.write(f"stop requested at {__import__('datetime').datetime.now().isoformat()}\n")
    return {"status": "stop_flag_written", "flag_path": _RESEARCH_STOP_FLAG}


@app.get("/api/research/status")
def research_status():
    """Return current research loop state."""
    global _RESEARCH_LOOP_PROC
    is_running = _RESEARCH_LOOP_PROC is not None and _RESEARCH_LOOP_PROC.poll() is None
    pid = _RESEARCH_LOOP_PROC.pid if _RESEARCH_LOOP_PROC else None
    stop_pending = os.path.exists(_RESEARCH_STOP_FLAG)
    loop_state: dict = {}
    if os.path.exists(_RESEARCH_STATE_FILE):
        try:
            with open(_RESEARCH_STATE_FILE, "r", encoding="utf-8") as f:
                loop_state = json.load(f)
        except Exception:
            pass
    return {
        "is_running": is_running,
        "pid": pid,
        "stop_pending": stop_pending,
        "cycle_count": loop_state.get("cycle_count", 0),
        "last_project_id": loop_state.get("last_project_id"),
        "last_completed_at": loop_state.get("last_completed_at"),
        "total_errors": loop_state.get("total_errors", 0),
    }


# ── Forever Loop Control endpoints ──────────────────────────────────────────

_ELABS_PROD = Path(__file__).parent.parent.parent
_FOREVER_LOOP_STOP_FLAG = _ELABS_PROD / "Conjoined" / "data" / "_loop_stop.flag"
_FOREVER_LOOP_LOG       = _ELABS_PROD / "Conjoined" / "data" / "forever_loop.log"
_FOREVER_LOOP_CYCLES    = _ELABS_PROD / "Conjoined" / "simulation_results" / "forever_loop_cycles.jsonl"


@app.get("/api/loop/status")
def loop_status():
    """Return backend health + forever loop last-cycle stats."""
    stop_pending = _FOREVER_LOOP_STOP_FLAG.exists()
    last_grade, last_cycle, last_ts = "?", 0, None
    try:
        text = _FOREVER_LOOP_CYCLES.read_text(encoding="utf-8", errors="replace")
        lines = [l for l in text.splitlines() if l.strip()]
        if lines:
            last = json.loads(lines[-1])
            last_grade = last.get("grade", "?")
            last_cycle = last.get("cycle", 0)
            last_ts    = last.get("ts")
    except Exception:
        pass
    return {
        "backend_ok":  True,
        "stop_pending": stop_pending,
        "last_grade":  last_grade,
        "last_cycle":  last_cycle,
        "last_ts":     last_ts,
    }


@app.get("/api/loop/log")
def loop_log(lines: int = 80):
    """Return last N lines from the forever_loop.log file."""
    try:
        text = _FOREVER_LOOP_LOG.read_text(encoding="utf-8", errors="replace")
        tail = text.splitlines()[-lines:]
        return {"lines": tail}
    except Exception:
        return {"lines": []}


@app.post("/api/loop/stop")
def loop_stop():
    """Write the stop flag so the forever loop halts after the current cycle."""
    _FOREVER_LOOP_STOP_FLAG.parent.mkdir(parents=True, exist_ok=True)
    _FOREVER_LOOP_STOP_FLAG.write_text(
        f"stop requested at {__import__('datetime').datetime.now().isoformat()}\n"
    )
    return {"status": "stop_flag_written"}


# ── Machine Project Chat endpoint ────────────────────────────────────────────

# ── Research Loop data-read endpoints (used by research-loop.html panel) ────

_AUDIT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "research_reports", "audit")
_REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "research_reports")


def _read_jsonl_tail(path: str, limit: int) -> list[dict]:
    """Read last `limit` lines from a .jsonl file, parsing each as JSON."""
    if not os.path.exists(path):
        return []
    lines: list[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except Exception:
        return []
    tail = lines[-limit:] if limit > 0 else lines
    result = []
    for ln in tail:
        try:
            result.append(json.loads(ln))
        except Exception:
            pass
    return result


@app.get("/api/research/audit-log")
def research_audit_log(limit: int = 20):
    entries = _read_jsonl_tail(os.path.join(_AUDIT_DIR, "audit_log.jsonl"), limit)
    return {"entries": entries, "count": len(entries)}


@app.get("/api/research/disagreements")
def research_disagreements(limit: int = 20):
    entries = _read_jsonl_tail(os.path.join(_AUDIT_DIR, "disagreement_log.jsonl"), limit)
    return {"entries": entries, "count": len(entries)}


@app.get("/api/research/benchmark-runs")
def research_benchmark_runs(limit: int = 10):
    entries = _read_jsonl_tail(os.path.join(_AUDIT_DIR, "benchmark_runs.jsonl"), limit)
    return {"entries": entries, "count": len(entries)}


@app.get("/api/research/baseline")
def research_baseline():
    path = os.path.join(_REPORTS_DIR, "benchmark_baseline.json")
    if not os.path.exists(path):
        raise HTTPException(404, "No baseline file found")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@app.get("/api/research/reports")
def research_reports_list(limit: int = 20):
    if not os.path.exists(_REPORTS_DIR):
        return {"reports": []}
    reports = []
    for fname in sorted(os.listdir(_REPORTS_DIR), reverse=True):
        if fname.endswith(".md") and fname != "FINAL_SUMMARY.md":
            fpath = os.path.join(_REPORTS_DIR, fname)
            mtime = os.path.getmtime(fpath)
            import datetime as _dt
            reports.append({
                "filename": fname,
                "modified": _dt.datetime.fromtimestamp(mtime).isoformat(),
            })
        if len(reports) >= limit:
            break
    return {"reports": reports}


@app.get("/api/research/report-file")
def research_report_file(name: str):
    """Serve a research report markdown file as plain text."""
    # Security: restrict to research_reports/ directory, no path traversal
    safe_name = os.path.basename(name)
    fpath = os.path.join(_REPORTS_DIR, safe_name)
    if not os.path.exists(fpath) or not fpath.endswith(".md"):
        raise HTTPException(404, "Report not found")
    from fastapi.responses import PlainTextResponse
    with open(fpath, encoding="utf-8") as f:
        return PlainTextResponse(f.read())


@app.get("/api/research/known-frameworks")
def research_known_frameworks():
    """Return the list of frameworks already explored (exclusion list)."""
    path = os.path.join(_REPORTS_DIR, "known_frameworks.json")
    if not os.path.exists(path):
        return {"frameworks": []}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        frameworks = data if isinstance(data, list) else []
        return {"frameworks": frameworks, "count": len(frameworks)}
    except Exception:
        return {"frameworks": []}


@app.get("/api/research/proposals")
def research_proposals(limit: int = 20):
    """Return all cycle proposals from research_reports/proposals/*.json."""
    proposals_dir = os.path.join(_REPORTS_DIR, "proposals")
    if not os.path.exists(proposals_dir):
        return {"proposals": []}
    proposals = []
    for fname in sorted(os.listdir(proposals_dir), reverse=True):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(proposals_dir, fname)
        try:
            with open(fpath, encoding="utf-8") as f:
                data = json.load(f)
            data["_filename"] = fname
            proposals.append(data)
        except Exception:
            pass
        if len(proposals) >= limit:
            break
    return {"proposals": proposals, "count": len(proposals)}


@app.get("/api/research/memory-status")
def research_memory_status():
    """Return counts from the memory_store (canonical frameworks + contradictions)."""
    _MEM_ROOT = os.path.join(_REPORTS_DIR, "memory_store")
    _CANON_FILE = os.path.join(_MEM_ROOT, "canonical", "frameworks.json")
    _CONTRA_FILE = os.path.join(_MEM_ROOT, "contradictions", "contradiction_log.json")
    _EPISODES_DIR = os.path.join(_MEM_ROOT, "episodes")

    canonical_count = 0
    if os.path.exists(_CANON_FILE):
        try:
            with open(_CANON_FILE, encoding="utf-8") as f:
                canon = json.load(f)
            canonical_count = len(canon) if isinstance(canon, dict) else 0
        except Exception:
            pass

    contradiction_count = 0
    if os.path.exists(_CONTRA_FILE):
        try:
            with open(_CONTRA_FILE, encoding="utf-8") as f:
                contras = json.load(f)
            contradiction_count = len(contras) if isinstance(contras, list) else 0
        except Exception:
            pass

    episode_count = 0
    if os.path.exists(_EPISODES_DIR):
        try:
            episode_count = len([f for f in os.listdir(_EPISODES_DIR) if f.endswith(".json")])
        except Exception:
            pass

    return {
        "canonical_count": canonical_count,
        "contradiction_count": contradiction_count,
        "episode_count": episode_count,
        "memory_store_exists": os.path.exists(_MEM_ROOT),
    }


@app.get("/api/research/audit-log/stream")
def research_audit_log_stream(after: int = 0):
    """
    SSE endpoint — streams new audit_log.jsonl entries as they appear.
    Win 4 (§24.5): replaces 4-second polling with push delivery.
    Client sends ?after=N (byte offset it last received).
    Server sends data:{json}\\n\\n lines, then keeps connection open.
    """
    from fastapi.responses import StreamingResponse
    import time as _time

    _audit_path = os.path.join(_AUDIT_DIR, "audit_log.jsonl")

    def _event_generator():
        # On first connect, fast-forward to `after` offset
        byte_pos = after
        while True:
            try:
                if not os.path.exists(_audit_path):
                    yield f"data: {{\"type\":\"ping\"}}\n\n"
                    _time.sleep(2)
                    continue
                with open(_audit_path, encoding="utf-8") as _fh:
                    _fh.seek(byte_pos)
                    new_data = _fh.read()
                    byte_pos_new = _fh.tell()
                if new_data.strip():
                    for line in new_data.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            payload = json.dumps({"type": "entry", "data": entry, "offset": byte_pos_new})
                            yield f"data: {payload}\n\n"
                        except Exception:
                            pass
                    byte_pos = byte_pos_new
                else:
                    yield f"data: {{\"type\":\"ping\",\"offset\":{byte_pos}}}\n\n"
                _time.sleep(1.5)
            except GeneratorExit:
                break
            except Exception:
                _time.sleep(2)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


import re as _re

# Patterns that indicate user wants a NEW workflow/project, not Q&A about current one
_NEW_WORKFLOW_RE = _re.compile(
    r'\b(create|build|make|generate|start|set\s*up|design|spin\s*up)\b'
    r'.*\b(workflow|project|pipeline|app|application|program|script|tool|system|simulation|game)\b',
    _re.IGNORECASE,
)

# Pattern for architect mode requests
_ARCHITECT_RE = _re.compile(
    r'\b(architect|canvas|live\s*build|collaborate|collab|two\s*agents?\s*build|'
    r'agents?\s*discuss|sprite|shared\s*canvas|keep\s*building|keep\s*improving|'
    r'build.*until.*stop|iterate.*forever|infinite.*loop|build.*city|city.*builder|'
    r'build.*town|agents?.*city|city.*agents?|simulate.*agents?|agents?.*collaborate)\b',
    _re.IGNORECASE,
)

class MachineChatReq(BaseModel):
    message: str
    history: list[dict] | None = None
    confirmed: bool = False  # True when user explicitly confirms creating a new project

@app.post("/api/machine/projects/{project_id}/chat")
def machine_project_chat(project_id: str, req: MachineChatReq):
    """Chat within a MACHINE project context.

    The chat is project-aware: it knows the manifest, nodes, workspace files,
    and can execute commands like running nodes, changing settings, and reading
    workspace files. Uses the project's session_id for conversation persistence.
    """
    manifest = machine_engine.get_project(project_id)
    if not manifest:
        raise HTTPException(404, f"Project not found: {project_id}")

    # ── Architect steering fast-path ─────────────────────────────────────
    # When the architect loop is running, the LLM is busy. Instead of calling it
    # (which times out), store the steering message in the manifest so the
    # next agent cycle picks it up, then return immediately.
    is_architect = manifest.get("execution_mode") == "architect"
    is_running   = manifest.get("status") in ("running", "swarming")
    if is_architect and is_running:
        try:
            project_dir = _os.path.join(machine_engine.PROJECTS_DIR, project_id)
            manifest["steering_msg"] = req.message.strip()
            machine_engine._save_manifest(project_dir, manifest)
        except Exception as _e:
            print(f"[Architect steering] save failed: {_e}")
        return {
            "response": (
                f"🎯 **Steering received!**\n\n"
                f"Your direction: *\"{req.message[:120]}\"*\n\n"
                f"The agents will pick this up on the next build cycle. "
                f"Watch the canvas for changes — they'll respond to your direction."
            ),
            "muscle": "STEERING",
            "session_id": manifest.get("session_id") or project_id,
            "project_id": project_id,
            "exec_time": 0,
            "tokens": 0,
        }

    # ── Detect architect mode requests ──
    # "create an architect with...", "two agents build together on a canvas", etc.
    if _ARCHITECT_RE.search(req.message):
        try:
            session_id = manifest.get("session_id") or project_id
            # Extract the topic from the request
            topic = req.message.strip()
            new_project = machine_engine.create_architect_project(topic, session_id=session_id)
            new_pid = new_project["project_id"]
            _start_project_watcher(new_pid)
            machine_engine.run_project_architect(new_pid)
            print(f"[Chat→Architect] Created {new_pid}: {topic[:80]}")
            return {
                "response": (
                    f"🏗️ **Architect Mode started!**\n\n"
                    f"Two agents are now collaborating on a shared canvas.\n"
                    f"- **Agent ALPHA** ({new_project['nodes'][0]['muscle']}): Builder\n"
                    f"- **Agent BETA** ({new_project['nodes'][1]['muscle']}): Reviewer\n\n"
                    f"The canvas will appear as a live viewer overlay.\n"
                    f"Hit **Stop** when you want them to stop iterating.\n\n"
                    f"Redirecting to the architect project..."
                ),
                "muscle": "MACHINE",
                "session_id": session_id,
                "project_id": new_pid,
                "new_project_id": new_pid,
                "exec_time": 0,
                "tokens": 0,
            }
        except Exception as e:
            print(f"[Chat→Architect] Failed: {e}")
            # Fall through

    # ── Detect new workflow requests ──
    # If the user says "create a workflow that...", require explicit confirmation first
    if _NEW_WORKFLOW_RE.search(req.message):
        if not req.confirmed:
            # Return a confirmation prompt — do NOT create anything yet
            return {
                "needs_confirmation": True,
                "response": f"This will **create a new project** based on your request. The current project will stay open.\n\n> {req.message[:200]}{'...' if len(req.message) > 200 else ''}\n\nConfirm to proceed.",
                "muscle": "MACHINE",
                "session_id": manifest.get("session_id") or project_id,
                "project_id": project_id,
                "exec_time": 0,
                "tokens": 0,
            }
        try:
            session_id = manifest.get("session_id") or project_id
            new_project = machine_engine.create_project(req.message, session_id=session_id)
            new_pid = new_project["project_id"]
            total = new_project["total_nodes"]
            print(f"[Chat→NewProject] Created {new_pid} with {total} nodes from chat request")
            return {
                "response": f"New workflow created with {total} nodes! Redirecting...",
                "muscle": "MACHINE",
                "session_id": session_id,
                "project_id": new_pid,
                "new_project_id": new_pid,
                "exec_time": 0,
                "tokens": 0,
            }
        except Exception as e:
            print(f"[Chat→NewProject] Failed: {e}")
            # Fall through to regular chat on failure

    session_id = manifest.get("session_id") or project_id

    # ── Fast-path: if project is running and user asks about status, respond locally ──
    # This avoids calling Ollama (which is busy processing nodes) and timing out
    _STATUS_QUESTION_RE = _re.compile(
        r'\b(what.*happening|status|progress|which.*node|are you.*running|confirm|current|stuck|where are)'
        r'|happening\s*(right\s*now|now|currently)',
        _re.IGNORECASE,
    )
    # Always respond to status questions locally (avoids calling busy Ollama)
    if _STATUS_QUESTION_RE.search(req.message):
        nodes = manifest.get("nodes", [])
        total = len(nodes)
        done = [n for n in nodes if n.get("status") in ("complete", "done")]
        running = [n for n in nodes if n.get("status") == "running"]
        pending = [n for n in nodes if n.get("status") in ("pending", None, "ready")]
        errored = [n for n in nodes if n.get("status") == "error"]

        lines = [f"**Project Status: {len(done)}/{total} nodes complete**\n"]
        if running:
            for n in running:
                elapsed = ""
                if n.get("started_at"):
                    try:
                        from datetime import datetime
                        started = datetime.fromisoformat(n["started_at"])
                        elapsed = f" ({int((datetime.now() - started).total_seconds())}s elapsed)"
                    except Exception:
                        pass
                lines.append(f"🔄 **Running** — Node {n['id']}: {n.get('task', '?')[:80]} [{n.get('muscle', '?')}]{elapsed}")
        if done:
            for n in done:
                lines.append(f"✅ Node {n['id']}: {n.get('task', '?')[:60]} [{n.get('muscle', '?')}]")
        if pending:
            for n in pending:
                deps = n.get("depends_on", [])
                dep_str = f" (waiting on: {', '.join(str(d) for d in deps)})" if deps else ""
                lines.append(f"⏳ Node {n['id']}: {n.get('task', '?')[:60]} [{n.get('muscle', '?')}]{dep_str}")
        if errored:
            for n in errored:
                lines.append(f"❌ Node {n['id']}: {n.get('task', '?')[:60]} — {(n.get('result') or 'error')[:80]}")

        lines.append(f"\n_Note: Model is busy processing nodes — this is a live status snapshot, not an LLM response._")

        return {
            "response": "\n".join(lines),
            "muscle": "STATUS",
            "session_id": session_id,
            "project_id": project_id,
            "exec_time": 0,
            "tokens": 0,
        }

    # ── Detect modify-project requests (add/remove/insert step/node) ──
    _MODIFY_PROJECT_RE = _re.compile(
        r'\b(add|insert|include|append|put|attach|create)\b'
        r'.*\b(step|node|stage|command|task|action|phase)\b',
        _re.IGNORECASE,
    )
    _REMOVE_NODE_RE = _re.compile(
        r'\b(remove|delete|drop)\b.*\b(step|node|stage|task)\b',
        _re.IGNORECASE,
    )

    if _MODIFY_PROJECT_RE.search(req.message) and not _NEW_WORKFLOW_RE.search(req.message):
        try:
            nodes = manifest.get("nodes", [])
            nodes_desc = "\n".join(
                f"  Node {n['id']} (task_id={n.get('task_id','t'+str(n['id']))}): "
                f"[{n.get('status','?')}] {n.get('task','')}"
                for n in nodes
            )
            last_node = nodes[-1] if nodes else None
            last_tid = last_node.get("task_id", f"t{last_node['id']}") if last_node else None

            # Focused LLM call to extract structured node details
            _ADD_NODE_SYSTEM = (
                "You are a project planner. The user wants to add a new step to an existing project.\n"
                "Given the user's request and the current project nodes, return ONLY a JSON object:\n"
                "{\n"
                '  "task": "short description of what the new node should do",\n'
                '  "action": "generate" or "bash" or "verify",\n'
                '  "cmd": "shell command if action is bash, otherwise empty string",\n'
                '  "muscle": "GWEN" or "MAX" or "NEMOTRON",\n'
                '  "depends_on": ["t1"] or [] — task_ids this node must wait for,\n'
                '  "after_node_id": 2 — integer ID of the node this should run after (or null)\n'
                "}\n\n"
                "Rules:\n"
                "- action='bash' when the user wants to run a shell command, open a file, execute something\n"
                "- action='verify' when the user wants to check/validate something exists or works\n"
                "- action='generate' when the user wants the AI to write/create content\n"
                "- For bash commands on Windows, use PowerShell syntax\n"
                "- muscle='GWEN' for code tasks, 'MAX' for writing tasks, 'NEMOTRON' for research\n"
                "- depends_on should reference task_ids of nodes that must complete first\n"
                "- If user says 'after it has been created' or 'after node X', set appropriate depends_on\n"
                "- Return ONLY valid JSON, no markdown fences, no explanation\n"
            )

            user_prompt = (
                f"## Current Project Nodes:\n{nodes_desc}\n\n"
                f"## User Request:\n{req.message}\n\n"
                f"Return the JSON for the new node to add."
            )

            from orchestrator import muscles as _muscles
            result = _muscles.call_muscle("GWEN", user_prompt, system=_ADD_NODE_SYSTEM)
            raw_response = result.get("response", "")

            # Extract JSON from response
            import json as _json
            # Strip markdown fences if present
            cleaned = raw_response.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1]
                if "```" in cleaned:
                    cleaned = cleaned[:cleaned.rfind("```")]
            cleaned = cleaned.strip()

            node_spec = _json.loads(cleaned)
            task = node_spec.get("task", req.message)
            action = node_spec.get("action", "generate")
            cmd = node_spec.get("cmd", "")
            muscle = node_spec.get("muscle", "GWEN")
            depends_on = node_spec.get("depends_on")
            after_id = node_spec.get("after_node_id")

            # Default: depend on last node if no explicit dependency
            if not depends_on and not after_id and last_tid:
                depends_on = [last_tid]

            new_node = machine_engine.add_node(
                project_id, task, muscle,
                depends_on=depends_on,
                action=action, cmd=cmd,
                after_node_id=after_id,
            )

            if new_node:
                nid = new_node["id"]
                deps_str = ", ".join(str(d) for d in new_node.get("depends_on", [])) or "none"
                action_str = action
                if action == "bash" and cmd:
                    action_str = f"bash: `{cmd[:120]}`"

                resp_lines = [
                    f"**Added Node {nid}** to your project!\n",
                    f"- **Task:** {task}",
                    f"- **Action:** {action_str}",
                    f"- **Agent:** {muscle}",
                    f"- **Depends on:** {deps_str}",
                    f"\nHit **DAG Run** to execute the updated workflow. The new node will run after its dependencies complete.",
                ]
                print(f"[Chat→AddNode] Added node {nid} to {project_id}: {task[:80]}")
                return {
                    "response": "\n".join(resp_lines),
                    "muscle": "MACHINE",
                    "session_id": session_id,
                    "project_id": project_id,
                    "exec_time": 0,
                    "tokens": 0,
                }
        except Exception as e:
            print(f"[Chat→AddNode] Failed: {e}")
            # Fall through to regular chat on failure

    if _REMOVE_NODE_RE.search(req.message):
        try:
            # Extract node ID from message like "remove node 3" or "delete step 2"
            id_match = _re.search(r'\b(?:node|step|stage)\s*#?(\d+)', req.message, _re.IGNORECASE)
            if id_match:
                node_id = int(id_match.group(1))
                removed = machine_engine.remove_node(project_id, node_id)
                if removed:
                    print(f"[Chat→RemoveNode] Removed node {node_id} from {project_id}")
                    return {
                        "response": f"**Removed Node {node_id}** from the project. Any dependencies on it have been cleared.",
                        "muscle": "MACHINE",
                        "session_id": session_id,
                        "project_id": project_id,
                        "exec_time": 0,
                        "tokens": 0,
                    }
                else:
                    return {
                        "response": f"Could not remove Node {node_id} — it may not exist or is currently running.",
                        "muscle": "MACHINE",
                        "session_id": session_id,
                        "project_id": project_id,
                        "exec_time": 0,
                        "tokens": 0,
                    }
        except Exception as e:
            print(f"[Chat→RemoveNode] Failed: {e}")
            # Fall through to regular chat

    # ── Build rich project context ──
    nodes_summary = []
    for n in manifest.get("nodes", []):
        entry = f"  Node {n['id']}: [{n.get('status','?')}] {n.get('task','')}"
        if n.get("muscle"):
            entry += f" (muscle: {n['muscle']})"
        nodes_summary.append(entry)

    workspace_files = machine_engine.list_workspace_files(project_id) or []
    file_list = ", ".join(
        (f.get("name", str(f)) if isinstance(f, dict) else str(f))
        for f in workspace_files[:20]
    ) if workspace_files else "none"

    # Collect enabled feature flags
    from orchestrator.features import get_enabled_flags
    enabled_flags = get_enabled_flags()

    system_prompt = (
        "You are THE MACHINE — an AI project orchestrator with full access to this project's workspace.\n\n"
        f"## Project\n"
        f"Prompt: {manifest.get('prompt', '')}\n"
        f"Status: {manifest.get('status', 'unknown')}\n"
        f"Project ID: {project_id}\n"
        f"Workspace: {manifest.get('workspace_dir', 'N/A')}\n"
        f"Linked Directory: {manifest.get('linked_dir', 'none')}\n\n"
        f"## Nodes ({len(manifest.get('nodes', []))} total)\n"
        + "\n".join(nodes_summary) + "\n\n"
        f"## Workspace Files\n{file_list}\n\n"
        f"## Enabled Features\n{', '.join(enabled_flags) if enabled_flags else 'none'}\n\n"
        "## Capabilities\n"
        "You can help the user with:\n"
        "- Explaining project status, node results, and workspace files\n"
        "- Adding new steps/nodes to the project (just ask: 'add a step that...')\n"
        "- Removing nodes (just ask: 'remove node 3')\n"
        "- Suggesting next steps based on node outputs\n"
        "- Answering questions about the codebase in the workspace\n"
        "- Providing technical guidance for the project's goals\n"
        "IMPORTANT: When the user asks to add steps, commands, or tasks to the project, "
        "do NOT run them directly. The system will automatically add them as new DAG nodes. "
        "Be concise and technical. Reference specific nodes and files when relevant."
    )

    # Build message history for the LLM
    history = []
    if req.history:
        for m in req.history[-20:]:
            if m.get("role") in ("user", "assistant") and m.get("content"):
                history.append({"role": m["role"], "content": m["content"]})

    # Route through the standard generate path with project context
    prompt_req = PromptReq(
        prompt=req.message,
        mode="chat",
        session_id=session_id,
        project_id=project_id,
        messages=[{"role": "system", "content": system_prompt}] + history,
    )
    result = generate(prompt_req)

    return {
        "response": result.get("response", ""),
        "muscle": result.get("muscle", ""),
        "session_id": session_id,
        "project_id": project_id,
        "exec_time": result.get("exec_time", 0),
        "tokens": result.get("tokens", 0),
    }


@app.get("/api/machine/projects/{project_id}/workspace-path")
def machine_get_workspace_path(project_id: str):
    """Return the absolute filesystem path for a project's workspace directory."""
    manifest = machine_engine.get_project(project_id)
    if not manifest:
        raise HTTPException(404, f"Project not found: {project_id}")
    workspace_dir = manifest.get("workspace_dir", "")
    linked_dir = manifest.get("linked_dir", "")
    project_dir = os.path.join(machine_engine.PROJECTS_DIR, project_id)
    return {
        "project_id": project_id,
        "project_dir": project_dir,
        "workspace_dir": workspace_dir or os.path.join(project_dir, "workspace"),
        "linked_dir": linked_dir or None,
    }


# ── Design Loop + Checkpoint endpoints ───────────────────────────────────────

@app.get("/api/machine/projects/{project_id}/checkpoints")
def machine_list_checkpoints(project_id: str):
    """List all checkpoints for a project."""
    from orchestrator.checkpoint import list_checkpoints
    project_dir = os.path.join(machine_engine.PROJECTS_DIR, project_id)
    if not os.path.isdir(project_dir):
        raise HTTPException(404, f"Project not found: {project_id}")
    return list_checkpoints(project_dir)


@app.post("/api/machine/projects/{project_id}/checkpoints")
def machine_save_checkpoint(project_id: str, req: dict = Body(...)):
    """Save a checkpoint of the current project state."""
    from orchestrator.checkpoint import save_checkpoint
    project_dir = os.path.join(machine_engine.PROJECTS_DIR, project_id)
    manifest = machine_engine.get_project(project_id)
    if not manifest:
        raise HTTPException(404, f"Project not found: {project_id}")
    meta = save_checkpoint(
        project_dir, manifest,
        reason=req.get("reason", "manual"),
        loop_step=req.get("loop_step", "manual"),
    )
    return meta


@app.post("/api/machine/projects/{project_id}/checkpoints/{checkpoint_id}/restore")
def machine_restore_checkpoint(project_id: str, checkpoint_id: str):
    """Restore a project to a previous checkpoint."""
    from orchestrator.checkpoint import restore_checkpoint
    project_dir = os.path.join(machine_engine.PROJECTS_DIR, project_id)
    manifest = restore_checkpoint(project_dir, checkpoint_id)
    if manifest is None:
        raise HTTPException(404, f"Checkpoint not found: {checkpoint_id}")
    return manifest


@app.get("/api/machine/projects/{project_id}/checkpoints/{checkpoint_id}/diff")
def machine_diff_checkpoint(project_id: str, checkpoint_id: str):
    """Diff current project state against a checkpoint."""
    from orchestrator.checkpoint import diff_checkpoint
    project_dir = os.path.join(machine_engine.PROJECTS_DIR, project_id)
    return diff_checkpoint(project_dir, checkpoint_id)


@app.post("/api/machine/projects/{project_id}/improve")
def machine_run_improvement_loop(project_id: str, req: dict = Body(default={})):
    """
    Run the self-improvement design loop on a project.
    
    Body params:
      - goal (str): specific improvement goal
      - max_iterations (int): max loop cycles (default 3)
      - review_tier (str): nemo/standard/deep (default nemo)
      - validate_tier (str): nemo/standard/deep (default nemo)
    """
    import threading
    from orchestrator.design_loop import run_improvement_loop

    manifest = machine_engine.get_project(project_id)
    if not manifest:
        raise HTTPException(404, f"Project not found: {project_id}")

    # Run async in background thread (loop is long-running)
    def _run():
        try:
            run_improvement_loop(
                project_id,
                goal=req.get("goal", ""),
                max_iterations=req.get("max_iterations", 3),
                review_tier=req.get("review_tier", "nemo"),
                validate_tier=req.get("validate_tier", "nemo"),
            )
        except Exception as e:
            log.error("Improvement loop failed for %s: %s", project_id, e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    return {
        "status": "started",
        "project_id": project_id,
        "message": "Improvement loop started. Poll project status and checkpoints for progress.",
    }


@app.get("/api/machine/projects/{project_id}/debug")
def machine_project_debug(project_id: str):
    """Full diagnostic dump for automated testing — agent states, VRAM, execution logs."""
    manifest = machine_engine.get_project(project_id)
    if not manifest:
        raise HTTPException(404, f"Project not found: {project_id}")

    # Node details
    nodes = []
    for n in manifest.get("nodes", []):
        nodes.append({
            "id": n["id"],
            "muscle": n.get("muscle"),
            "status": n.get("status"),
            "model_used": n.get("model_used"),
            "tokens_used": n.get("tokens_used", 0),
            "started_at": n.get("started_at"),
            "completed_at": n.get("completed_at"),
            "result_length": len(n.get("result") or ""),
            "output_file": n.get("output_file"),
            "task": (n.get("task") or "")[:100],
        })

    # VRAM from Ollama
    vram_status = vram_manager.get_vram_status()

    # Execution log tail
    logs_dir = os.path.join(machine_engine.PROJECTS_DIR, project_id, "logs")
    log_tail = []
    log_file = os.path.join(logs_dir, "execution.jsonl")
    if os.path.exists(log_file):
        import json as _json
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in lines[-50:]:
                try:
                    log_tail.append(_json.loads(line.strip()))
                except Exception:
                    pass
        except Exception:
            pass

    # Workspace file list
    ws_dir = os.path.join(machine_engine.PROJECTS_DIR, project_id, "workspace")
    ws_files = []
    if os.path.isdir(ws_dir):
        for root, dirs, files in os.walk(ws_dir):
            for f in files:
                fp = os.path.join(root, f)
                rel = os.path.relpath(fp, ws_dir)
                ws_files.append({"path": rel, "size": os.path.getsize(fp)})

    return {
        "project_id": project_id,
        "status": manifest.get("status"),
        "execution_mode": manifest.get("execution_mode"),
        "created_at": manifest.get("created_at"),
        "updated_at": manifest.get("updated_at"),
        "total_tokens": manifest.get("total_tokens", 0),
        "total_time": manifest.get("total_time", 0),
        "nodes": nodes,
        "vram": vram_status,
        "execution_log_tail": log_tail,
        "workspace_files": ws_files,
        "prompt": (manifest.get("prompt") or "")[:500],
    }


@app.get("/api/machine/copilot/status")
def machine_copilot_status():
    """Check if Copilot SDK is authenticated and available."""
    try:
        from orchestrator.copilot_integration import get_bridge, _get_or_create_loop
        bridge = get_bridge()
        loop = _get_or_create_loop()
        is_auth = loop.run_until_complete(bridge.is_authenticated())
        models = loop.run_until_complete(bridge.list_models())
        return {
            "authenticated": is_auth,
            "models_available": len(models),
            "models": models,
        }
    except Exception as e:
        return {
            "authenticated": False,
            "error": str(e),
            "models_available": 0,
        }


# ── Claude Code CLI Integration ───────────────────────────────────────────────

_COPILOT_TIERS = {
    "nemo":     {"model": "gpt-5-mini",      "multiplier": 0, "label": "Nemo (free)"},
    "standard": {"model": "gpt-5.1",         "multiplier": 1, "label": "Standard (1x)"},
    "deep":     {"model": "claude-opus-4.6", "multiplier": 3, "label": "Deep (3x)"},
}


@app.get("/claude-code/status")
def claude_code_status():
    config._load_backend_settings()  # always reflect persisted state
    import shutil, subprocess as _sp
    cli_found = shutil.which("claude") is not None
    version = None
    authenticated = cli_found  # claude CLI self-manages auth; treat found=authenticated
    if cli_found:
        try:
            r = _sp.run(["claude", "--version"], capture_output=True, text=True, timeout=5)
            version = (r.stdout.strip() or r.stderr.strip()) or None
        except Exception:
            pass
    return {
        "enabled":   config.CLAUDE_CODE_ENABLED,
        "cli_found": cli_found,
        "version":   version,
        "workdir":   config.CLAUDE_CODE_WORKDIR,
        "timeout":   config.CLAUDE_CODE_TIMEOUT,
        "authenticated": authenticated,
        "active":    False,
    }


@app.post("/claude-code/settings")
async def claude_code_settings_endpoint(request: Request):
    import shutil
    body = await request.json()
    if "enabled"  in body: config.CLAUDE_CODE_ENABLED  = bool(body["enabled"])
    if "workdir"  in body: config.CLAUDE_CODE_WORKDIR  = str(body["workdir"]).strip()
    if "timeout"  in body: config.CLAUDE_CODE_TIMEOUT  = max(10, int(body["timeout"]))
    config.save_backend_settings()
    return {
        "ok":        True,
        "enabled":   config.CLAUDE_CODE_ENABLED,
        "cli_found": shutil.which("claude") is not None,
        "workdir":   config.CLAUDE_CODE_WORKDIR,
        "timeout":   config.CLAUDE_CODE_TIMEOUT,
    }


@app.post("/claude-code/auth")
def claude_code_auth():
    """Launch 'claude /login' in a new visible terminal window and return immediately.
    The frontend should poll /claude-code/status until authenticated."""
    import shutil, subprocess as _sp
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return JSONResponse(status_code=400, content={"ok": False, "error": "claude CLI not found"})
    try:
        _sp.Popen(
            [claude_bin, "/login"],
            creationflags=_sp.CREATE_NEW_CONSOLE,
            close_fds=True,
        )
        return {"ok": True, "terminal_opened": True}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


@app.get("/claude-code/models")
def claude_code_models_endpoint():
    if not config.CLAUDE_CODE_ENABLED:
        return {"enabled": False, "models": []}
    return {"enabled": True, "models": [{"id": "claude-code", "name": "Claude Code CLI", "type": "claude-code"}]}


# ── GitHub Copilot SDK Integration ────────────────────────────────────────────

@app.get("/copilot-sdk/status")
def copilot_sdk_status():
    config._load_backend_settings()  # always reflect persisted state
    try:
        from copilot import CopilotClient  # noqa: F401
        sdk_importable = True
    except ImportError:
        sdk_importable = False

    sdk_version = None
    if sdk_importable:
        try:
            import importlib.metadata
            sdk_version = importlib.metadata.version("copilot")
        except Exception:
            pass

    authenticated = False
    auth_error = None
    if sdk_importable:
        try:
            from orchestrator.copilot_integration import check_auth_sync
            result = check_auth_sync()
            authenticated = result.get("authenticated", False)
            auth_error = result.get("error")
        except Exception as e:
            auth_error = str(e)

    return {
        "enabled":       config.COPILOT_SDK_ENABLED,
        "sdk_installed": sdk_importable,
        "sdk_version":   sdk_version,
        "authenticated": authenticated,
        "auth_error":    auth_error,
        "default_tier":  config.COPILOT_SDK_DEFAULT_TIER,
        "tiers":         _COPILOT_TIERS,
    }


@app.post("/copilot-sdk/settings")
async def copilot_sdk_settings_endpoint(request: Request):
    body = await request.json()
    if "enabled"       in body: config.COPILOT_SDK_ENABLED      = bool(body["enabled"])
    if "default_tier"  in body and body["default_tier"] in _COPILOT_TIERS:
        config.COPILOT_SDK_DEFAULT_TIER = body["default_tier"]
    config.save_backend_settings()
    return {
        "ok":           True,
        "enabled":      config.COPILOT_SDK_ENABLED,
        "default_tier": config.COPILOT_SDK_DEFAULT_TIER,
    }


# ── RotorQuant / llama.cpp KV settings ───────────────────────────────────────

@app.get("/api/llamacpp/settings")
def llamacpp_settings_get():
    """Return current RotorQuant/llama.cpp KV settings and server reachability."""
    server_reachable = False
    try:
        import urllib.request as _ur
        with _ur.urlopen("http://127.0.0.1:8095/health", timeout=2) as _r:
            server_reachable = _r.status == 200
    except Exception:
        pass
    return {
        "enabled":          config.LLAMACPP_KV_ENABLED,
        "default_tier":     config.LLAMACPP_KV_DEFAULT_TIER,
        "server_reachable": server_reachable,
        "server_url":       "http://127.0.0.1:8095",
        "tiers": {
            "1": "Tier 1 — q8_0 KV (~40K ctx, works now)",
            "2": "Tier 2 — RotorQuant iso3 (~200K ctx, needs CUDA 12.4 + fork build)",
        },
    }


@app.post("/api/llamacpp/settings")
async def llamacpp_settings_post(request: Request):
    """Update RotorQuant/llama.cpp KV settings at runtime and persist to disk."""
    body = await request.json()
    if "enabled"      in body: config.LLAMACPP_KV_ENABLED      = bool(body["enabled"])
    if "default_tier" in body and body["default_tier"] in ("1", "2"):
        config.LLAMACPP_KV_DEFAULT_TIER = body["default_tier"]
    config.save_backend_settings()
    return {
        "ok":           True,
        "enabled":      config.LLAMACPP_KV_ENABLED,
        "default_tier": config.LLAMACPP_KV_DEFAULT_TIER,
    }


@app.post("/copilot-sdk/auth")
def copilot_sdk_auth():
    """Launch 'copilot login' in a new visible terminal window and return immediately.
    The frontend polls /copilot-sdk/status until authenticated."""
    import shutil, subprocess as _sp, sys as _sys
    copilot_bin = shutil.which("copilot")
    cmd = [copilot_bin, "login"] if copilot_bin else [_sys.executable, "-m", "copilot", "login"]
    try:
        _sp.Popen(
            cmd,
            creationflags=_sp.CREATE_NEW_CONSOLE,
            close_fds=True,
        )
        return {"ok": True, "terminal_opened": True}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


@app.post("/copilot-sdk/install")
def copilot_sdk_install():
    import subprocess as _sp, sys as _sys
    try:
        result = _sp.run(
            [_sys.executable, "-m", "pip", "install", "--upgrade", "copilot"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            try:
                import importlib, importlib.metadata
                importlib.invalidate_caches()
                version = importlib.metadata.version("copilot")
            except Exception:
                version = None
            return {"ok": True, "sdk_version": version, "output": result.stdout[-500:]}
        else:
            return JSONResponse(status_code=500, content={"ok": False, "error": (result.stderr or result.stdout or "pip failed")[-600:]})
    except _sp.TimeoutExpired:
        return JSONResponse(status_code=504, content={"ok": False, "error": "pip install timed out after 120s"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/copilot-sdk/models")
def copilot_sdk_models_endpoint():
    if not config.COPILOT_SDK_ENABLED:
        return {"enabled": False, "models": []}
    models = [
        {"id": f"copilot-{tier}", "name": info["label"], "model": info["model"],
         "multiplier": info["multiplier"], "type": "copilot-sdk"}
        for tier, info in _COPILOT_TIERS.items()
    ]
    return {
        "enabled":      True,
        "default_tier": config.COPILOT_SDK_DEFAULT_TIER,
        "default_id":   f"copilot-{config.COPILOT_SDK_DEFAULT_TIER}",
        "models":       models,
    }


# ---------- Observability: /api/status and /api/metrics ----------

_APP_START_TIME = time.time()

@app.get("/api/status")
def api_status():
    """Lightweight health check: db, model endpoint, uptime."""
    uptime_s = int(time.time() - _APP_START_TIME)

    # DB reachability
    db_ok = False
    try:
        _VAL_DB = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".dev", "validation", "validation.db")
        _VAL_DB = os.path.normpath(_VAL_DB)
        if os.path.exists(_VAL_DB):
            import sqlite3 as _sq
            with _sq.connect(_VAL_DB, timeout=2) as _c:
                _c.execute("SELECT 1").fetchone()
            db_ok = True
    except Exception:
        db_ok = False

    # Ollama reachability (quick TCP check)
    model_ok = False
    try:
        import socket as _sock
        with _sock.create_connection(("127.0.0.1", 11434), timeout=1):
            model_ok = True
    except Exception:
        model_ok = False

    return {
        "ok": True,
        "uptime_seconds": uptime_s,
        "db": db_ok,
        "model_endpoint": model_ok,
        "version": "1.0",
    }


@app.get("/api/metrics")
def api_metrics():
    """Aggregate metrics from validation.db sim_results (last 24h)."""
    try:
        _VAL_DB = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".dev", "validation", "validation.db")
        _VAL_DB = os.path.normpath(_VAL_DB)
        if not os.path.exists(_VAL_DB):
            return {"error": "metrics db not found", "metrics": []}
        import sqlite3 as _sq
        with _sq.connect(_VAL_DB, timeout=3) as _c:
            rows = _c.execute("""
                SELECT
                  COUNT(*) AS total_runs,
                  SUM(passed) AS total_passed,
                  ROUND(AVG(CAST(passed AS FLOAT)) * 100, 1) AS pass_rate_pct,
                  ROUND(AVG(latency_ms), 0) AS avg_latency_ms,
                  ROUND(AVG(quality_score), 3) AS avg_quality,
                  MAX(run_at) AS last_run_at,
                  run_session
                FROM sim_results
                WHERE run_at >= datetime('now', '-24 hours')
                GROUP BY run_session
                ORDER BY last_run_at DESC
                LIMIT 20
            """).fetchall()
            cols = ["total_runs", "total_passed", "pass_rate_pct", "avg_latency_ms", "avg_quality", "last_run_at", "session"]
            sessions = [dict(zip(cols, r)) for r in rows]

            summary = _c.execute("""
                SELECT COUNT(*) AS total, SUM(passed) AS passed,
                       ROUND(AVG(quality_score), 3) AS avg_quality,
                       ROUND(AVG(latency_ms), 0) AS avg_latency_ms
                FROM sim_results WHERE run_at >= datetime('now', '-24 hours')
            """).fetchone()

        return {
            "period": "last_24h",
            "summary": dict(zip(["total", "passed", "avg_quality", "avg_latency_ms"], summary or [0,0,0,0])),
            "by_session": sessions,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e), "metrics": []})


# ══════════════════════════════════════════════════════════════════════════════
# OPENCLAW BRIDGE — Gateway integration endpoints
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# OPENCLAW BRIDGE — Gateway integration endpoints
# ══════════════════════════════════════════════════════════════════════════════

import importlib.util as _oc_ilu
_oc_spec = _oc_ilu.spec_from_file_location(
    "openclaw_bridge",
    os.path.join(os.path.dirname(__file__), "openclaw_bridge.py"),
)
_oc_bridge = _oc_ilu.module_from_spec(_oc_spec)
_oc_spec.loader.exec_module(_oc_bridge)
from dataclasses import asdict as _dc_asdict

# Runtime settings store (in-memory, persisted to disk separately if needed)
_oc_settings: dict = dict(_oc_bridge.DEFAULT_SETTINGS)


@app.get("/api/openclaw/config")
def openclaw_get_config():
    """Return gateway URL and token for frontend WebSocket client initialization."""
    gateway = os.environ.get("OPENCLAW_URL", _oc_settings.get("gateway", "http://127.0.0.1:18789"))
    token = os.environ.get("HOST_OPENCLAW_TOKEN", _oc_settings.get("token", ""))
    return {
        "gateway": gateway.rstrip("/"),
        "token": token,
        "enabled": _oc_settings.get("enabled", True),
        "default_agent": _oc_settings.get("default_agent", "main"),
    }


@app.get("/api/openclaw/settings")
def openclaw_get_settings():
    """Return current OpenClaw integration settings."""
    return {
        "settings": _oc_settings,
        "schema": _oc_bridge.EVENT_SCHEMA,
        "capability_map": [_dc_asdict(c) for c in _oc_bridge.CAPABILITY_MAP],
    }


@app.put("/api/openclaw/settings")
def openclaw_update_settings(body: dict):
    """Update OpenClaw integration settings.
    Body: any subset of DEFAULT_SETTINGS keys.
    """
    allowed = set(_oc_bridge.DEFAULT_SETTINGS.keys())
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(400, "No valid settings keys provided")
    _oc_settings.update(updates)
    return {"status": "ok", "settings": _oc_settings}


@app.post("/api/openclaw/settings/reset")
def openclaw_reset_settings():
    """Reset OpenClaw settings to defaults."""
    global _oc_settings
    _oc_settings = dict(_oc_bridge.DEFAULT_SETTINGS)
    return {"status": "reset", "settings": _oc_settings}


@app.post("/api/openclaw/validate-event")
def openclaw_validate_event(body: dict):
    """Validate an OpenClaw event payload against the canonical schema."""
    return _oc_bridge.validate_event(body)


@app.get("/api/openclaw/template")
def openclaw_get_template():
    """Return the canonical complex workflow template."""
    return _oc_bridge.get_canonical_workflow_template()


@app.get("/api/openclaw/health")
async def openclaw_health_check():
    """Proxy a health check to the configured OpenClaw gateway."""
    import urllib.request as _urlreq
    gateway = _oc_settings.get("gateway", "http://127.0.0.1:18789").rstrip("/")
    try:
        req = _urlreq.Request(f"{gateway}/")
        with _urlreq.urlopen(req, timeout=4) as resp:
            status_code = resp.status
        return {
            "gateway": gateway,
            "reachable": True,
            "status_code": status_code,
            "integration_enabled": _oc_settings.get("enabled", False),
        }
    except Exception as exc:
        return {
            "gateway": gateway,
            "reachable": False,
            "error": str(exc),
            "integration_enabled": _oc_settings.get("enabled", False),
        }


@app.get("/api/openclaw/agents")
def openclaw_list_agents():
    """Return installed OpenClaw agents using gateway /tools/invoke (agents_list)."""
    import urllib.request as _urlreq

    gateway = str(os.environ.get("OPENCLAW_URL", _oc_settings.get("gateway", "http://127.0.0.1:18789"))).rstrip("/")
    if gateway.startswith("ws://"):
        gateway = "http://" + gateway[len("ws://"):]
    elif gateway.startswith("wss://"):
        gateway = "https://" + gateway[len("wss://"):]

    token = str(os.environ.get("HOST_OPENCLAW_TOKEN", _oc_settings.get("token", ""))).strip()
    payload = json.dumps({
        "tool": "agents_list",
        "action": "json",
        "args": {},
        "sessionKey": _oc_settings.get("default_agent", "main") or "main",
    }).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = _urlreq.Request(f"{gateway}/tools/invoke", data=payload, headers=headers, method="POST")
    try:
        with _urlreq.urlopen(req, timeout=4) as resp:
            raw = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception as exc:
        return {
            "ok": False,
            "gateway": gateway,
            "agents": [],
            "defaultId": "main",
            "error": str(exc),
        }

    if not isinstance(raw, dict) or not raw.get("ok"):
        return {
            "ok": False,
            "gateway": gateway,
            "agents": [],
            "defaultId": "main",
            "error": (raw or {}).get("error") if isinstance(raw, dict) else "invalid OpenClaw response",
        }

    result = raw.get("result") if isinstance(raw.get("result"), dict) else {}
    nested = result.get("result") if isinstance(result.get("result"), dict) else result
    agents = nested.get("agents") if isinstance(nested.get("agents"), list) else []
    requester = str(nested.get("requester") or "").strip()
    default_id = requester or "main"
    normalized_agents = []
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        aid = str(agent.get("id") or "").strip()
        if not aid:
            continue
        normalized_agents.append({
            "id": aid,
            "name": str(agent.get("name") or aid),
            "configured": bool(agent.get("configured", False)),
        })

    if not normalized_agents:
        normalized_agents = [{"id": default_id, "name": default_id, "configured": True}]

    return {
        "ok": True,
        "gateway": gateway,
        "defaultId": default_id,
        "agents": normalized_agents,
    }


@app.post("/api/openclaw/dispatch-event")
def openclaw_dispatch_event(body: dict):
    """Validate and log an inbound OpenClaw event (bridge ingestion point).
    In hybrid_bridge mode this records the event; production routing
    is handled by openclaw-visual-bridge.mjs on the frontend via WebSocket.
    """
    result = _oc_bridge.validate_event(body)
    if not result["valid"]:
        raise HTTPException(400, f"Invalid event: missing fields {result['missing_fields']}")
    # Log for debugging
    log.info(f"[openclaw] event received: type={body.get('type')} flow={body.get('flow_id','?')}")
    return {"status": "accepted", "validation": result}


# Serve frontend

# Serve help docs as static HTML
_HELP_STATIC_DIR = os.path.join(FRONTEND_DIR, "help")
if os.path.isdir(_HELP_STATIC_DIR):
    app.mount("/help", StaticFiles(directory=_HELP_STATIC_DIR, html=True), name="help")

# Serve pages (THE MACHINE project page, etc.)
PAGES_DIR = os.path.join(FRONTEND_DIR, "pages")
if os.path.isdir(PAGES_DIR):
    app.mount("/pages", StaticFiles(directory=PAGES_DIR, html=True), name="pages")

@app.get("/")
def serve_index():
    return FileResponse(
        os.path.join(FRONTEND_DIR, "index.html"),
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )
