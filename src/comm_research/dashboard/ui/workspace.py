"""Session registry plus URL-backed persistence for browser reloads."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import date
from typing import Any

import streamlit as st

MAX_PANELS = 12
VIEWS = ("Tabs view", "Split 1×2", "Split 2×2")
TIMEZONES = ("UTC", "America/Los_Angeles", "America/New_York")


def panel_key(dataset: str, field: str) -> str:
    identifier = hashlib.sha256(dataset.encode()).hexdigest()[:16]
    return f"panel:{identifier}:{field}"


def decode_workspace(value: str) -> dict[str, Any]:
    if len(value) > 16_000:
        return {}
    try:
        result = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if not isinstance(result, dict) or result.get("version") != 1:
            return {}
        active = result.get("active", [])
        if not isinstance(active, list) or any(
            not isinstance(key, str) for key in active
        ):
            return {}
        result["active"] = list(dict.fromkeys(active))[:MAX_PANELS]
        if not isinstance(result.get("panels", {}), dict):
            result["panels"] = {}
        return result
    except (ValueError, TypeError, UnicodeDecodeError):
        return {}


def initialize_workspace(available: tuple[str, ...]) -> None:
    if "active_panels" in st.session_state:
        return
    restored = decode_workspace(st.query_params.get("workspace", ""))
    defaults = [key for key in available if key.endswith(("/dam_lmp", "/rtm_lmp"))][:2]
    active = restored.get("active", defaults or list(available[:1]))
    st.session_state["active_panels"] = active
    st.session_state["panel_preferences"] = restored.get("panels", {})
    st.session_state["desk_theme"] = (
        restored.get("theme") if restored.get("theme") in ("Light", "Dark") else "Dark"
    )
    st.session_state["desk_view"] = (
        restored.get("view") if restored.get("view") in VIEWS else VIEWS[0]
    )
    st.session_state["focus_panel"] = (
        restored.get("focus") if restored.get("focus") in active else None
    )
    st.session_state["pending_focus"] = st.session_state["focus_panel"]
    page = restored.get("grid_page", 0)
    st.session_state["grid_page"] = (
        page if isinstance(page, int) and 0 <= page < MAX_PANELS else 0
    )


def open_panel(key: str) -> bool:
    active = st.session_state["active_panels"]
    if key not in active:
        if len(active) >= MAX_PANELS:
            return False
        st.session_state["active_panels"] = [*active, key]
    st.session_state["focus_panel"] = key
    st.session_state["pending_focus"] = key
    view = st.session_state.get("desk_view", VIEWS[0])
    if view != VIEWS[0]:
        capacity = 2 if view == VIEWS[1] else 4
        st.session_state["grid_page"] = (
            st.session_state["active_panels"].index(key) // capacity
        )
    save_workspace()
    return True


def close_panel(key: str) -> None:
    st.session_state["active_panels"] = [
        item for item in st.session_state["active_panels"] if item != key
    ]
    save_workspace()
    st.rerun(scope="app")


def preferences(key: str) -> dict[str, Any]:
    value = st.session_state["panel_preferences"].get(key, {})
    return value if isinstance(value, dict) else {}


def store_preferences(key: str, settings: dict[str, Any]) -> None:
    st.session_state["panel_preferences"][key] = settings
    save_workspace()


def save_workspace() -> None:
    active = st.session_state.get("active_panels", [])
    payload = {
        "version": 1,
        "active": active,
        "theme": st.session_state.get("desk_theme", "Dark"),
        "view": st.session_state.get("desk_view", VIEWS[0]),
        "focus": st.session_state.get("focus_panel"),
        "grid_page": st.session_state.get("grid_page", 0),
        "panels": {
            key: st.session_state.get("panel_preferences", {}).get(key, {})
            for key in active
        },
    }
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(payload, default=_json_value, separators=(",", ":")).encode()
        )
        .decode()
        .rstrip("=")
    )
    # URLs hold preferences and relative feed names, never source rows or secrets.
    if len(encoded) <= 16_000 and st.query_params.get("workspace") != encoded:
        st.query_params["workspace"] = encoded


def _json_value(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"Unsupported workspace value: {type(value).__name__}")
