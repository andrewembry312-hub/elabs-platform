"""OpenClaw bridge metadata for THE MACHINE visual workflow integration.

This module defines the first stable contract between the existing MACHINE
visuals and an OpenClaw-style task-flow runtime. It is intentionally side-effect
free so endpoints can expose the contract before execution routing is switched.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from copy import deepcopy
from typing import Literal

OpenClawEventType = Literal[
    "flow_created",
    "task_queued",
    "task_running",
    "task_progress",
    "task_output",
    "task_failed",
    "task_completed",
    "flow_paused",
    "flow_resumed",
    "flow_cancelled",
    "resource_update",
    "model_update",
    "file_update",
    "flow_completed",
]

EVENT_SCHEMA: list[dict[str, object]] = [
    {
        "type": "flow_created",
        "visual_state": "workflow_initialized",
        "required_fields": ["flow_id", "project_id", "nodes"],
        "description": "A MACHINE project has been projected into an OpenClaw task flow.",
    },
    {
        "type": "task_queued",
        "visual_state": "node_pending",
        "required_fields": ["flow_id", "task_id", "node_id", "dependencies"],
        "description": "A visual node has a corresponding OpenClaw task waiting on dependencies or capacity.",
    },
    {
        "type": "task_running",
        "visual_state": "node_active",
        "required_fields": ["flow_id", "task_id", "node_id", "agent_id", "model_id"],
        "description": "OpenClaw started executing the task represented by a visual node.",
    },
    {
        "type": "task_progress",
        "visual_state": "node_active",
        "required_fields": ["flow_id", "task_id", "node_id", "percent", "message"],
        "description": "Progress update for node glow, status text, and log panels.",
    },
    {
        "type": "task_output",
        "visual_state": "node_active",
        "required_fields": ["flow_id", "task_id", "node_id", "artifact_paths"],
        "description": "Task produced files or structured output that should appear in the file tree/inspector.",
    },
    {
        "type": "task_failed",
        "visual_state": "node_failed",
        "required_fields": ["flow_id", "task_id", "node_id", "error"],
        "description": "Task failed and the visual node should show failure state with diagnostic details.",
    },
    {
        "type": "task_completed",
        "visual_state": "node_completed",
        "required_fields": ["flow_id", "task_id", "node_id", "artifact_paths"],
        "description": "Task completed and the visual node should show completion state.",
    },
    {
        "type": "flow_paused",
        "visual_state": "workflow_paused",
        "required_fields": ["flow_id", "reason"],
        "description": "OpenClaw paused the task flow, usually awaiting approval/input or capacity.",
    },
    {
        "type": "flow_resumed",
        "visual_state": "workflow_running",
        "required_fields": ["flow_id"],
        "description": "Paused OpenClaw flow resumed execution.",
    },
    {
        "type": "flow_cancelled",
        "visual_state": "workflow_cancelled",
        "required_fields": ["flow_id", "reason"],
        "description": "Flow was cancelled from UI or runtime policy.",
    },
    {
        "type": "resource_update",
        "visual_state": "resource_panel_update",
        "required_fields": ["ram_used_gb", "vram_used_gb", "reserved_gb", "running_tasks"],
        "description": "RAM/VRAM and queue telemetry for HUD/resource panels.",
    },
    {
        "type": "model_update",
        "visual_state": "model_panel_update",
        "required_fields": ["model_id", "status", "task_id"],
        "description": "Model load/unload/switch/fallback telemetry for local model indicators.",
    },
    {
        "type": "file_update",
        "visual_state": "file_tree_refresh",
        "required_fields": ["project_id", "path", "operation"],
        "description": "Workspace file tree should refresh for OpenClaw-produced artifacts or edits.",
    },
    {
        "type": "flow_completed",
        "visual_state": "workflow_completed",
        "required_fields": ["flow_id", "project_id", "artifact_paths"],
        "description": "All OpenClaw tasks completed and THE MACHINE workflow can show final success state.",
    },
]


@dataclass(frozen=True)
class CapabilityMapping:
    capability: str
    existing_visual: str
    current_backend_surface: str
    openclaw_surface: str
    integration_action: str
    status: str


CAPABILITY_MAP: list[CapabilityMapping] = [
    CapabilityMapping(
        capability="node_task_graph",
        existing_visual="the-machine-project-v2 node graph + nodes.js status states",
        current_backend_surface="/api/machine/projects/{project_id}/dag and /events",
        openclaw_surface="task-flow create/run/resume/cancel lifecycle",
        integration_action="map each visual node to an OpenClaw task_id and preserve dependency edges",
        status="bridge_required",
    ),
    CapabilityMapping(
        capability="agent_execution",
        existing_visual="agents.js load/travel/executing/returning phases",
        current_backend_surface="MACHINE agent/muscle routing and VRAM manager",
        openclaw_surface="runtime task assignment and agent context",
        integration_action="project OpenClaw task assignment into existing agent phase animations",
        status="bridge_required",
    ),
    CapabilityMapping(
        capability="file_management",
        existing_visual="project file tree, open/reveal/copy artifact actions",
        current_backend_surface="/files, /tree, /raw/{filepath}, /link endpoints",
        openclaw_surface="task output artifacts and workspace mutations",
        integration_action="emit file_update and task_output events, then reuse current tree refresh flow",
        status="extend_existing",
    ),
    CapabilityMapping(
        capability="ram_vram_tracking",
        existing_visual="VRAM HUD, queue indicators, loaded model chips",
        current_backend_surface="/api/machine/vram and orchestrator.vram_manager",
        openclaw_surface="runtime resource telemetry projection",
        integration_action="merge OpenClaw resource_update with existing VRAM panel data model",
        status="extend_existing",
    ),
    CapabilityMapping(
        capability="local_model_tracking",
        existing_visual="topbar model indicator and per-agent model state",
        current_backend_surface="reasoning/model inventory endpoints and model registry",
        openclaw_surface="model_update event for load/unload/switch/fallback",
        integration_action="surface model_update events in current model indicators and node inspector",
        status="extend_existing",
    ),
    CapabilityMapping(
        capability="settings",
        existing_visual="settings panels and runtime selector",
        current_backend_surface="industry agents, model settings, runtime options",
        openclaw_surface="gateway mode, timeout, retry, concurrency, sandbox, policy profile",
        integration_action="add OpenClaw settings section backed by persistent backend config",
        status="new_settings_needed",
    ),
    CapabilityMapping(
        capability="pause_resume_cancel",
        existing_visual="play/pause/reset/stop controls",
        current_backend_surface="/run, /dag, /swarm, /stop endpoints",
        openclaw_surface="flow pause/resume/cancel actions",
        integration_action="wire UI controls to bridge actions and emit flow_paused/resumed/cancelled",
        status="bridge_required",
    ),
]

DEFAULT_SETTINGS: dict[str, object] = {
    "enabled": True,
    "mode": "hybrid_bridge",
    "gateway": "http://127.0.0.1:18789",
    "timeout_seconds": 300,
    "retry_limit": 1,
    "max_concurrent_tasks": 2,
    "file_sandbox": "project_workspace",
    "policy_profile": "standard",
    "fallback_to_machine_executor": True,
}


CANONICAL_WORKFLOW_TEMPLATE: dict[str, object] = {
    "template_id": "openclaw_complex_workflow_v1",
    "name": "Complex Product Documentation Workflow",
    "description": "A realistic four-stage MACHINE/OpenClaw workflow used as the wrapper integration template.",
    "prompt": "Build a polished documentation site for a local AI orchestration platform, including architecture overview, API reference, examples, and publish-ready static assets.",
    "flow_id": "openclaw-template-flow",
    "project_id": "openclaw-template-project",
    "nodes": [
        {
            "node_id": "analysis",
            "task_id": "task-analysis",
            "label": "Architecture Review",
            "agent_id": "agent-nemotron-analysis",
            "model_id": "nemotron",
            "capability": "research_analysis",
            "dependencies": [],
            "inputs": ["user_prompt", "repo_tree", "existing_api_routes"],
            "outputs": ["docs/architecture.md", "docs/api-surface.json"],
            "visual_phase": "load -> travel -> executing -> returning",
        },
        {
            "node_id": "content",
            "task_id": "task-content",
            "label": "Narrative Content",
            "agent_id": "agent-max-writing",
            "model_id": "max",
            "capability": "content_generation",
            "dependencies": ["analysis"],
            "inputs": ["docs/architecture.md"],
            "outputs": ["docs/index.md", "docs/examples.md"],
            "visual_phase": "load -> travel -> executing -> returning",
        },
        {
            "node_id": "assembly",
            "task_id": "task-assembly",
            "label": "Site Assembly",
            "agent_id": "agent-gwen-code",
            "model_id": "gwen",
            "capability": "code_generation",
            "dependencies": ["analysis", "content"],
            "inputs": ["docs/index.md", "docs/examples.md", "docs/api-surface.json"],
            "outputs": ["site/index.html", "site/assets/app.js", "site/assets/styles.css"],
            "visual_phase": "load -> travel -> executing -> returning",
        },
        {
            "node_id": "verify_publish",
            "task_id": "task-verify-publish",
            "label": "Verify and Package",
            "agent_id": "agent-toolcaller-verify",
            "model_id": "toolcaller",
            "capability": "tool_execution",
            "dependencies": ["assembly"],
            "inputs": ["site/index.html", "site/assets/app.js", "site/assets/styles.css"],
            "outputs": ["site/build-report.json", "site/dist.zip"],
            "visual_phase": "load -> executing -> returning",
        },
    ],
    "edges": [
        {"from": "analysis", "to": "content"},
        {"from": "analysis", "to": "assembly"},
        {"from": "content", "to": "assembly"},
        {"from": "assembly", "to": "verify_publish"},
    ],
    "settings": {
        "max_concurrent_tasks": 2,
        "fallback_to_machine_executor": True,
        "file_sandbox": "project_workspace",
        "policy_profile": "standard",
    },
    "integration_points": [
        "flow_created hydrates the visual DAG",
        "task_* events update node status and agent travel animations",
        "resource_update and model_update feed the HUD/model panels",
        "task_output and file_update refresh artifacts and file tree",
        "flow_completed enables final workflow success affordances",
    ],
}


def _schema_by_type() -> dict[str, dict[str, object]]:
    return {str(item["type"]): item for item in EVENT_SCHEMA}


def get_canonical_workflow_template() -> dict[str, object]:
    """Return the canonical complex workflow template used by the wrapper."""
    return deepcopy(CANONICAL_WORKFLOW_TEMPLATE)


def validate_event(event: dict[str, object]) -> dict[str, object]:
    """Validate a bridge event against the canonical schema."""
    event_type = str(event.get("type", ""))
    schema = _schema_by_type().get(event_type)
    missing = []
    if schema:
        missing = [field for field in schema.get("required_fields", []) if field not in event]
    return {
        "valid": bool(schema) and not missing,
        "type": event_type,
        "known_type": bool(schema),
        "missing_fields": missing,
        "visual_state": schema.get("visual_state") if schema else None,
    }


def validate_event_sequence(events: list[dict[str, object]]) -> dict[str, object]:
    """Validate a generated or runtime OpenClaw event sequence."""
    validations = [validate_event(event) for event in events]
    return {
        "valid": all(item["valid"] for item in validations),
        "event_count": len(events),
        "invalid_events": [item for item in validations if not item["valid"]],
    }


def build_canonical_workflow_events(
    project_id: str | None = None,
    flow_id: str | None = None,
    fail_node_id: str | None = None,
) -> list[dict[str, object]]:
    """Build deterministic OpenClaw events for the canonical complex workflow."""
    template = get_canonical_workflow_template()
    project_id = project_id or str(template["project_id"])
    flow_id = flow_id or str(template["flow_id"])
    nodes = deepcopy(template["nodes"])
    artifact_paths: list[str] = []
    events: list[dict[str, object]] = [
        {
            "type": "flow_created",
            "flow_id": flow_id,
            "project_id": project_id,
            "template_id": template["template_id"],
            "nodes": nodes,
            "edges": deepcopy(template["edges"]),
        },
        {
            "type": "resource_update",
            "flow_id": flow_id,
            "project_id": project_id,
            "ram_used_gb": 7.8,
            "vram_used_gb": 10.4,
            "reserved_gb": 3.0,
            "running_tasks": 0,
        },
    ]

    for node in nodes:
        outputs = list(node.get("outputs", []))
        artifact_paths.extend(str(path) for path in outputs)
        events.extend([
            {
                "type": "task_queued",
                "flow_id": flow_id,
                "project_id": project_id,
                "task_id": node["task_id"],
                "node_id": node["node_id"],
                "dependencies": node["dependencies"],
            },
            {
                "type": "model_update",
                "flow_id": flow_id,
                "project_id": project_id,
                "model_id": node["model_id"],
                "status": "loaded",
                "task_id": node["task_id"],
            },
            {
                "type": "task_running",
                "flow_id": flow_id,
                "project_id": project_id,
                "task_id": node["task_id"],
                "node_id": node["node_id"],
                "agent_id": node["agent_id"],
                "model_id": node["model_id"],
            },
            {
                "type": "task_progress",
                "flow_id": flow_id,
                "project_id": project_id,
                "task_id": node["task_id"],
                "node_id": node["node_id"],
                "percent": 50,
                "message": f"{node['label']} is producing artifacts",
            },
        ])
        if fail_node_id and fail_node_id == node["node_id"]:
            events.append({
                "type": "task_failed",
                "flow_id": flow_id,
                "project_id": project_id,
                "task_id": node["task_id"],
                "node_id": node["node_id"],
                "error": "Simulated OpenClaw wrapper failure for integration testing.",
            })
            return events
        events.extend([
            {
                "type": "task_output",
                "flow_id": flow_id,
                "project_id": project_id,
                "task_id": node["task_id"],
                "node_id": node["node_id"],
                "artifact_paths": outputs,
                "summary": f"{node['label']} produced {len(outputs)} artifact(s).",
            },
            {
                "type": "file_update",
                "flow_id": flow_id,
                "project_id": project_id,
                "path": outputs[0] if outputs else "",
                "operation": "upsert",
                "task_id": node["task_id"],
            },
            {
                "type": "task_completed",
                "flow_id": flow_id,
                "project_id": project_id,
                "task_id": node["task_id"],
                "node_id": node["node_id"],
                "artifact_paths": outputs,
            },
            {
                "type": "resource_update",
                "flow_id": flow_id,
                "project_id": project_id,
                "ram_used_gb": 8.2,
                "vram_used_gb": 9.6,
                "reserved_gb": 2.0,
                "running_tasks": 0,
            },
        ])

    events.append({
        "type": "flow_completed",
        "flow_id": flow_id,
        "project_id": project_id,
        "artifact_paths": artifact_paths,
    })
    return events


def get_capability_map() -> list[dict[str, str]]:
    """Return visual-to-OpenClaw capability mappings."""
    return [asdict(item) for item in CAPABILITY_MAP]


def get_event_schema() -> list[dict[str, object]]:
    """Return canonical OpenClaw bridge events for MACHINE visuals."""
    return EVENT_SCHEMA


def get_default_settings() -> dict[str, object]:
    """Return default OpenClaw bridge settings."""
    return dict(DEFAULT_SETTINGS)


def get_bridge_manifest() -> dict[str, object]:
    """Return the current OpenClaw bridge contract manifest."""
    return {
        "runtime_id": "openclaw",
        "display_name": "OpenClaw",
        "integration_mode": "hybrid_bridge",
        "status": "template_runtime_ready",
        "event_schema": get_event_schema(),
        "capabilities": get_capability_map(),
        "default_settings": get_default_settings(),
        "canonical_template_id": CANONICAL_WORKFLOW_TEMPLATE["template_id"],
        "template_endpoints": [
            "/api/machine/openclaw/template",
            "/api/machine/openclaw/template/events",
            "/api/machine/openclaw/template/run",
        ],
    }
