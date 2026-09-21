from __future__ import annotations

from typing import Any


# This is a registry of simulated capabilities, not a gateway to a school system.
TOOL_REGISTRY: tuple[dict[str, Any], ...] = (
    {
        "tool_id": "search_service_items",
        "tool_version": "v1",
        "risk_class": "read_only",
        "allowed_scopes": ["service_user", "evaluation_engineer"],
        "real_integration": False,
    },
    {
        "tool_id": "check_service_rules",
        "tool_version": "v1",
        "risk_class": "calculation",
        "allowed_scopes": ["service_user", "evaluation_engineer"],
        "real_integration": False,
    },
    {
        "tool_id": "check_virtual_availability",
        "tool_version": "v1",
        "risk_class": "read_only",
        "allowed_scopes": ["service_user", "evaluation_engineer"],
        "real_integration": False,
    },
    {
        "tool_id": "create_simulation_draft",
        "tool_version": "v1",
        "risk_class": "virtual_write",
        "allowed_scopes": ["service_user", "evaluation_engineer"],
        "real_integration": False,
        "requires_confirmation": False,
    },
    {
        "tool_id": "precheck_simulation_materials",
        "tool_version": "v1",
        "risk_class": "calculation",
        "allowed_scopes": ["service_user", "evaluation_engineer"],
        "real_integration": False,
    },
    {
        "tool_id": "confirm_simulation_submission",
        "tool_version": "v1",
        "risk_class": "virtual_write",
        "allowed_scopes": ["service_user", "evaluation_engineer"],
        "real_integration": False,
        "requires_confirmation": True,
    },
    {
        "tool_id": "reassess_impacted_task",
        "tool_version": "v1",
        "risk_class": "calculation",
        "allowed_scopes": ["evaluation_engineer"],
        "real_integration": False,
    },
    {
        "tool_id": "record_feedback",
        "tool_version": "v1",
        "risk_class": "virtual_write",
        "allowed_scopes": ["service_user", "evaluation_engineer"],
        "real_integration": False,
        "requires_confirmation": False,
    },
)


def list_registered_tools() -> list[dict[str, Any]]:
    return [dict(tool) for tool in TOOL_REGISTRY]


def registered_tool_ids() -> list[str]:
    return [tool["tool_id"] for tool in TOOL_REGISTRY]
