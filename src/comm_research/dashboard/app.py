"""Institutional commodities workstation. Run `streamlit run app.py` at repo root."""

from __future__ import annotations

import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import streamlit as st

from comm_research.dashboard.config.taxonomy import display_name
from comm_research.dashboard.data.loader import Dataset, clear_caches, discover_lake
from comm_research.dashboard.ui.components import render_panel
from comm_research.dashboard.ui.theme import Theme, inject_theme
from comm_research.dashboard.ui.workspace import (
    VIEWS,
    close_panel,
    initialize_workspace,
    open_panel,
    save_workspace,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LAKE_ROOT = PROJECT_ROOT / "data/lake"


def _navigation_tree(datasets: tuple[Dataset, ...]) -> dict[str, Any]:
    tree: dict[str, Any] = {}
    for dataset in datasets:
        branch = tree
        for token in dataset.tokens[:-1]:
            branch = branch.setdefault(token, {})
        branch.setdefault("__datasets", []).append(dataset)
    return tree


def _navigation(branch: dict[str, Any], path: tuple[str, ...] = ()) -> None:
    active = st.session_state["active_panels"]
    for token in sorted(key for key in branch if key != "__datasets"):
        prefix = "/".join((*path, token)) + "/"
        expanded = any(key.startswith(prefix) for key in active)
        with st.expander(display_name(token), expanded=expanded):
            _navigation(branch[token], (*path, token))
    for dataset in branch.get("__datasets", []):
        is_open = dataset.key in active
        label = ("• " if is_open else "+ ") + dataset.name
        if st.button(
            label,
            key=f"open:{dataset.key}",
            width="stretch",
            type="primary" if is_open else "secondary",
            help=f"{dataset.key} · {len(dataset.files)} files",
        ):
            if open_panel(dataset.key):
                st.rerun()
            else:
                st.warning(
                    "The workspace has 12 panels. Close a panel before adding another."
                )


def _sidebar(datasets: tuple[Dataset, ...], root: str) -> None:
    with st.sidebar:
        st.markdown("**LAKE EXPLORER**")
        search = st.text_input(
            "Find a feed",
            key="lake_search",
            placeholder="Market, region, dataset…",
            label_visibility="collapsed",
        )
        filtered = tuple(
            dataset
            for dataset in datasets
            if search.casefold()
            in " ".join(
                (*dataset.tokens, *(display_name(token) for token in dataset.tokens))
            ).casefold()
        )
        st.caption(f"{len(filtered)} / {len(datasets)} FEEDS · LOCAL FILESYSTEM")
        _navigation(_navigation_tree(filtered))
        if not filtered:
            st.caption("No matching datasets.")
        with st.expander("Lake source", expanded=not datasets):
            st.code(root, language=None)
            candidate = st.text_input(
                "Lake directory", value=root, key="lake_directory_input"
            )
            actions = st.columns(2)
            with actions[0]:
                if st.button("Apply source", key="apply_lake", width="stretch"):
                    path = Path(candidate).expanduser().resolve()
                    if path.is_dir():
                        st.session_state["lake_root"] = str(path)
                        clear_caches()
                        st.rerun()
                    else:
                        st.error("Choose an existing local directory.")
            with actions[1]:
                if st.button("Rescan", key="rescan_lake", width="stretch"):
                    clear_caches()
                    st.rerun()
        st.caption(
            "Select feeds to append panels. Each panel keeps its own dates, signals and filters."
        )


def _tabs(active: list[str], lookup: dict[str, Dataset], theme: Theme) -> None:
    labels = [lookup[key].title if key in lookup else key for key in active]
    # Include the relative key if two venues expose identically named feeds.
    labels = [
        f"{label} [{key}]" if labels.count(label) > 1 else label
        for key, label in zip(active, labels, strict=True)
    ]
    focus = st.session_state.pop("pending_focus", None)
    if focus in active:
        st.session_state["workspace_tabs"] = labels[active.index(focus)]
    if st.session_state.get("workspace_tabs") not in labels:
        st.session_state["workspace_tabs"] = labels[0]
    tabs = st.tabs(labels, key="workspace_tabs", on_change="rerun")
    for key, tab in zip(active, tabs, strict=True):
        if tab.open:
            with tab:
                _dataset_panel(key, lookup, theme, compact=False)
    st.session_state["focus_panel"] = active[
        labels.index(st.session_state["workspace_tabs"])
    ]


def _dataset_panel(
    key: str, lookup: dict[str, Dataset], theme: Theme, compact: bool
) -> None:
    if key in lookup:
        render_panel(lookup[key], theme, compact)
    else:
        with st.container(border=True):
            st.warning(f"This feed is no longer present in the selected lake: {key}")
            if st.button("Close unavailable feed", key=f"close_missing:{key}"):
                close_panel(key)


def _grid(
    active: list[str], lookup: dict[str, Dataset], theme: Theme, capacity: int
) -> None:
    page_count = math.ceil(len(active) / capacity)
    if st.session_state.get("grid_page", 0) >= page_count:
        st.session_state["grid_page"] = 0
    if page_count > 1:
        st.selectbox(
            "Grid page",
            range(page_count),
            format_func=lambda page: (
                f"Panels {page * capacity + 1}–{min((page + 1) * capacity, len(active))} / {len(active)}"
            ),
            key="grid_page",
        )
    offset = st.session_state.get("grid_page", 0) * capacity
    visible = active[offset : offset + capacity]
    for start in range(0, len(visible), 2):
        row = visible[start : start + 2]
        for key, column in zip(row, st.columns(min(len(row), 2)), strict=True):
            with column:
                _dataset_panel(key, lookup, theme, compact=True)


def main() -> None:
    st.set_page_config(
        page_title="Commodities | Desk Analytics",
        page_icon="▥",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    root = st.session_state.setdefault(
        "lake_root",
        str(
            Path(os.environ.get("COMMODITY_LAKE_ROOT", DEFAULT_LAKE_ROOT))
            .expanduser()
            .resolve()
        ),
    )
    try:
        datasets = discover_lake(root)
    except OSError as error:
        st.error(f"Unable to discover this lake: {error}")
        datasets = ()
    initialize_workspace(tuple(dataset.key for dataset in datasets))
    _sidebar(datasets, root)
    heading, layout, appearance = st.columns([3.6, 2.3, 0.8])
    with heading:
        st.markdown(
            '<div class="desk-masthead"><span class="desk-wordmark"><b>▥</b> COMMODITIES</span>'
            '<span class="desk-tag">DESK ANALYTICS / LOCAL LAKE</span></div>',
            unsafe_allow_html=True,
        )
    with layout:
        view = st.segmented_control(
            "Workspace view",
            VIEWS,
            key="desk_view",
            required=True,
            label_visibility="collapsed",
            width="stretch",
        )
    with appearance:
        mode = st.selectbox(
            "Appearance",
            ("Dark", "Light"),
            key="desk_theme",
            label_visibility="collapsed",
        )
    theme = inject_theme(mode)
    active = st.session_state["active_panels"]
    now = datetime.now(ZoneInfo("America/New_York"))
    st.markdown(
        f'<div class="desk-rule"><span>{len(datasets):02d} FEEDS / {len(active):02d} OPEN PANELS / PARQUET + ARROW</span>'
        f"<span>{now:%d %b %Y · %H:%M %Z}</span></div>",
        unsafe_allow_html=True,
    )
    lookup = {dataset.key: dataset for dataset in datasets}
    if not active:
        st.markdown(
            '<div class="desk-empty">WORKSPACE EMPTY<br><br>Select a feed from the lake explorer to open an analytic panel.</div>',
            unsafe_allow_html=True,
        )
    elif view == VIEWS[0]:
        _tabs(active, lookup, theme)
    else:
        _grid(active, lookup, theme, 2 if view == VIEWS[1] else 4)
    if not datasets:
        st.info(
            "No Parquet or Arrow files found. Select the directory containing your local lake."
        )
    save_workspace()
    st.markdown(
        '<div class="desk-footer">LOCAL LAKE / SOURCE OBSERVATIONS · 24H CHANGE REQUIRES A MATCHING OBSERVATION · RANGE BUTTONS ZOOM THE SELECTED HORIZON</div>',
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
