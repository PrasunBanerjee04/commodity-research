"""Reusable analytic panel, compact controls, metrics and Plotly traces."""

from __future__ import annotations

import html
import math
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import plotly.graph_objects as go
import polars as pl
import streamlit as st

from comm_research.dashboard.config.taxonomy import display_name
from comm_research.dashboard.data.loader import (
    Dataset,
    FilterSet,
    LakeError,
    SeriesBundle,
    dimension_options,
    inspect_dataset,
    load_raw_page,
    load_series,
    raw_row_count,
)
from comm_research.dashboard.ui.theme import Theme
from comm_research.dashboard.ui.workspace import (
    TIMEZONES,
    close_panel,
    panel_key,
    preferences,
    store_preferences,
)

FREQUENCIES = {"Native": "native", "5 minutes": "5m", "Hourly": "1h", "Daily": "1d"}
CHART_CONFIG = {
    "displaylogo": False,
    "scrollZoom": True,
    "responsive": True,
    "modeBarButtonsToRemove": ["lasso2d", "select2d", "autoScale2d"],
    "toImageButtonOptions": {"format": "png", "filename": "desk-analytics", "scale": 2},
}


def build_chart(
    bundle: SeriesBundle, theme: Theme, zone: str, height: int = 360
) -> go.Figure:
    figure = go.Figure()
    style_counts: dict[str, int] = {}
    styles = {
        "LMP": theme.accent,
        "Energy": "#8191A8",
        "Congestion": theme.red,
        "Losses": theme.muted,
        "GHG": theme.green,
    }
    for index, trace in enumerate(
        bundle.plot.partition_by("series", maintain_order=True)
    ):
        name = trace["series"][0]
        component = name.split(" · ")[0]
        repetition = style_counts.get(component, 0)
        style_counts[component] = repetition + 1
        color = styles.get(
            component, (theme.accent, theme.muted, theme.green, theme.red)[index % 4]
        )
        local_times = (
            trace["timestamp"]
            .dt.convert_time_zone(zone)
            .dt.strftime("%Y-%m-%d %H:%M %Z")
            .to_list()
        )
        unit = trace["unit"][0]
        figure.add_trace(
            go.Scattergl(
                x=trace["timestamp"].to_list(),
                y=trace["value"].to_list(),
                name=html.escape(name),
                mode="lines",
                connectgaps=False,
                line={
                    "color": color,
                    "width": 1.3,
                    "dash": ("solid", "dot", "dash", "dashdot")[repetition % 4],
                },
                customdata=local_times,
                hovertemplate=f"%{{customdata}}<br>%{{y:,.4f}} {html.escape(unit)}<extra>%{{fullData.name}}</extra>",
            )
        )
    units = bundle.plot["unit"].unique().to_list() if not bundle.plot.is_empty() else []
    figure.update_layout(
        height=height,
        margin={"l": 30, "r": 20, "t": 30, "b": 20},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={
            "family": "ui-monospace, SFMono-Regular, Consolas, monospace",
            "size": 10,
            "color": theme.text,
        },
        hovermode="x unified",
        hoverdistance=100,
        spikedistance=-1,
        hoverlabel={
            "bgcolor": theme.panel,
            "bordercolor": theme.border,
            "font_size": 11,
        },
        legend={
            "orientation": "h",
            "y": 1.14,
            "yanchor": "bottom",
            "x": 1,
            "xanchor": "right",
            "font_size": 9,
        },
        uirevision=f"{zone}:{','.join(bundle.plot['series'].unique().to_list())}"
        if not bundle.plot.is_empty()
        else zone,
        xaxis={
            "type": "date",
            "title": {"text": "UTC", "font_size": 9},
            "gridcolor": theme.grid,
            "zeroline": False,
            "showline": True,
            "linecolor": theme.border,
            "showspikes": True,
            "spikemode": "across",
            "spikesnap": "cursor",
            "spikethickness": 1,
            "spikedash": "dot",
            "spikecolor": theme.muted,
            "rangeslider": {
                "visible": True,
                "thickness": 0.045,
                "bgcolor": theme.panel,
                "bordercolor": theme.border,
            },
            "rangeselector": {
                "bgcolor": theme.panel,
                "activecolor": theme.border,
                "font": {"color": theme.text, "size": 9},
                "x": 0,
                "y": 1.03,
                "buttons": [
                    {"count": 1, "label": "1D", "step": "day", "stepmode": "backward"},
                    {"count": 5, "label": "5D", "step": "day", "stepmode": "backward"},
                    {
                        "count": 1,
                        "label": "1M",
                        "step": "month",
                        "stepmode": "backward",
                    },
                    {"count": 1, "label": "YTD", "step": "year", "stepmode": "todate"},
                    {"label": "ALL", "step": "all"},
                ],
            },
        },
        yaxis={
            "title": {
                "text": units[0] if len(units) == 1 else "source units",
                "font_size": 9,
            },
            "gridcolor": theme.grid,
            "zerolinecolor": theme.border,
            "showspikes": True,
            "spikesnap": "cursor",
            "spikecolor": theme.muted,
            "spikethickness": 1,
            "tickformat": ",.2f",
        },
    )
    return figure


def _number(value: float | None, signed: bool = False) -> str:
    if value is None or not math.isfinite(value):
        return "—"
    return format(value, "+,.2f" if signed else ",.2f")


def metric_ticker(row: dict[str, Any] | None, theme: Theme) -> None:
    row = row or {}
    cells: list[str] = []
    for label, field in (
        ("Last", "last"),
        ("24h change %", "change_24h"),
        ("Min", "min"),
        ("Max", "max"),
        ("Period mean", "mean"),
        ("Std dev", "std"),
    ):
        value = row.get(field)
        color = theme.text
        if field == "change_24h" and value is not None:
            color = theme.green if value > 0 else theme.red if value < 0 else theme.text
        cells.append(
            f'<div class="desk-tick"><div class="desk-tick-label">{label}</div>'
            f'<div class="desk-tick-value" style="color:{color}">{_number(value, field == "change_24h")}</div></div>'
        )
    st.markdown(
        '<div class="desk-ticker">' + "".join(cells) + "</div>", unsafe_allow_html=True
    )


def _date_preference(value: Any, fallback: date, earliest: date, latest: date) -> date:
    try:
        value = date.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(value, date) or isinstance(value, datetime):
            return fallback
        return min(latest, max(earliest, value))
    except ValueError:
        return fallback


def _choice(key: str, options: tuple[str, ...], default: str) -> None:
    if st.session_state.get(key) not in options:
        st.session_state[key] = default if default in options else options[0]


def _selection(key: str, options: tuple[str, ...], default: list[str]) -> None:
    previous = st.session_state.get(key, default)
    if not isinstance(previous, list):
        previous = default
    st.session_state[key] = [value for value in previous if value in options]


def _raw_table(
    dataset: Dataset,
    start: date | None,
    end: date | None,
    zone: str,
    filters: FilterSet,
    theme: Theme,
) -> None:
    count = raw_row_count(dataset, start, end, zone, filters)
    size_key = panel_key(dataset.key, "page_size")
    if st.session_state.get(size_key) not in (100, 250, 500):
        st.session_state[size_key] = 250
    controls = st.columns([1, 0.7, 0.7, 1.8])
    with controls[0]:
        page_size = st.selectbox("Rows / page", (100, 250, 500), key=size_key)
    pages = max(1, math.ceil(count / page_size))
    page_key = panel_key(dataset.key, "page")
    page = min(max(0, st.session_state.get(page_key, 0)), pages - 1)
    with controls[1]:
        st.caption("PREVIOUS")
        if st.button(
            "←",
            key=panel_key(dataset.key, "previous"),
            disabled=page == 0,
            width="stretch",
        ):
            page -= 1
    with controls[2]:
        st.caption("NEXT")
        if st.button(
            "→",
            key=panel_key(dataset.key, "next"),
            disabled=page + 1 == pages,
            width="stretch",
        ):
            page += 1
    with controls[3]:
        st.caption("SOURCE ROWS")
        st.text(f"{count:,} rows · page {page + 1:,} / {pages:,}")
    st.session_state[page_key] = page
    frame = load_raw_page(dataset, start, end, zone, filters, page, page_size)
    st.dataframe(
        frame.to_pandas().style.set_properties(
            **{"background-color": theme.panel, "color": theme.text}
        ),
        hide_index=True,
        row_height=24,
        height=320,
        width="stretch",
        key=panel_key(dataset.key, "raw_table"),
    )
    st.download_button(
        "Export this page · CSV",
        frame.write_csv().encode(),
        file_name=f"{dataset.tokens[-1]}-page-{page + 1}.csv",
        mime="text/csv",
        key=panel_key(dataset.key, "export"),
    )


@st.fragment
def render_panel(dataset: Dataset, theme: Theme, compact: bool = False) -> None:
    """Widget reruns stay inside their panel; registry changes rerun the workspace."""
    with st.container(border=True, key=panel_key(dataset.key, "container")):
        title, close = st.columns([6, 1])
        with title:
            st.markdown(
                f'<div class="desk-panel-title">{html.escape(dataset.title)}</div>',
                unsafe_allow_html=True,
            )
        with close:
            if st.button("Close", key=panel_key(dataset.key, "close"), width="stretch"):
                close_panel(dataset.key)
        try:
            _panel_body(dataset, theme, compact)
        except (LakeError, OSError, pl.exceptions.PolarsError) as error:
            st.error(f"This panel could not read the selected data: {error}")
            st.caption(
                "Other workspace panels remain open. Rescan the lake after a source refresh."
            )


def _panel_body(dataset: Dataset, theme: Theme, compact: bool) -> None:
    metadata = inspect_dataset(dataset)
    saved = preferences(dataset.key)
    zone_key = panel_key(dataset.key, "zone")
    _choice(zone_key, TIMEZONES, saved.get("zone", "UTC"))
    zone = st.session_state[zone_key]
    dated = metadata.earliest is not None and metadata.latest is not None
    fallback = datetime.now(ZoneInfo("America/New_York")).date()
    earliest = (
        metadata.earliest.astimezone(ZoneInfo(zone)).date() if dated else fallback
    )
    latest = metadata.latest.astimezone(ZoneInfo(zone)).date() if dated else fallback
    default_start = max(earliest, latest - timedelta(days=29))
    for field, default in (("start", default_start), ("end", latest)):
        key = panel_key(dataset.key, field)
        st.session_state[key] = _date_preference(
            st.session_state.get(key, saved.get(field)), default, earliest, latest
        )
    choices = tuple(signal.key for signal in metadata.signals)
    labels = {signal.key: signal.label for signal in metadata.signals}
    signals_key = panel_key(dataset.key, "signals")
    _selection(signals_key, choices, saved.get("signals", list(choices[:1])))
    strip = st.columns([1, 1, 1.7])
    with strip[0]:
        start = st.date_input(
            "Start date",
            min_value=earliest,
            max_value=latest,
            key=panel_key(dataset.key, "start"),
            disabled=not dated,
        )
    with strip[1]:
        end = st.date_input(
            "End date",
            min_value=earliest,
            max_value=latest,
            key=panel_key(dataset.key, "end"),
            disabled=not dated,
        )
    with strip[2]:
        selected = st.multiselect(
            "Signals",
            choices,
            format_func=labels.__getitem__,
            key=signals_key,
            max_selections=8,
            placeholder="Choose signals",
            disabled=not choices,
        )
    options = st.columns([1.7, 1.2, 1, 1])
    filters: list[tuple[str, tuple[str, ...]]] = []
    saved_filters = (
        saved.get("filters", {}) if isinstance(saved.get("filters", {}), dict) else {}
    )
    primary = metadata.dimensions[0] if metadata.dimensions else None

    def dimension_control(column: str) -> None:
        values = dimension_options(dataset, column)
        key = panel_key(dataset.key, f"filter:{column}")
        default = saved_filters.get(column, list(values[:1]))
        previous = st.session_state.get(key, default)
        if isinstance(previous, (list, tuple)):
            values = tuple(
                dict.fromkeys(
                    (*values, *(value for value in previous if isinstance(value, str)))
                )
            )
        _selection(key, values, default)
        chosen = st.multiselect(
            display_name(column),
            values,
            key=key,
            max_selections=8,
            accept_new_options=True,
            placeholder="Choose series",
            help="An empty selection returns no rows. For more than 5,000 identifiers, type the exact identifier.",
        )
        filters.append((column, tuple(chosen)))

    with options[0]:
        if primary:
            dimension_control(primary)
        else:
            st.caption("SERIES")
            st.text("All observations")
    with options[1]:
        zone = st.selectbox(
            "Date zone",
            TIMEZONES,
            key=zone_key,
            help="Date boundaries and hover timestamps use this zone. The time axis stays UTC across DST.",
        )
    frequency_key, aggregation_key = (
        panel_key(dataset.key, "frequency"),
        panel_key(dataset.key, "aggregation"),
    )
    _choice(frequency_key, tuple(FREQUENCIES), saved.get("frequency", "Native"))
    _choice(aggregation_key, ("Mean", "Last"), saved.get("aggregation", "Mean"))
    with options[2]:
        frequency = st.selectbox("Frequency", tuple(FREQUENCIES), key=frequency_key)
    with options[3]:
        aggregation = st.selectbox(
            "Aggregation",
            ("Mean", "Last"),
            key=aggregation_key,
            help="Exact duplicates are removed. Mean averages repeated observations in each interval; Last uses the final observation in time/source order, not a verified publication revision.",
        )
    secondary = metadata.dimensions[1:]
    if secondary:
        with st.expander("Series filters", expanded=False):
            for offset in range(0, len(secondary), 3):
                for column, area in zip(
                    secondary[offset : offset + 3], st.columns(3), strict=False
                ):
                    with area:
                        dimension_control(column)
    view_key = panel_key(dataset.key, "view")
    _choice(
        view_key,
        ("Chart", "Raw data"),
        saved.get("view", "Chart" if choices and dated else "Raw data"),
    )
    view = st.segmented_control(
        "Inspection",
        ("Chart", "Raw data"),
        key=view_key,
        required=True,
        label_visibility="collapsed",
    )
    store_preferences(
        dataset.key,
        {
            "start": start,
            "end": end,
            "zone": zone,
            "signals": selected,
            "frequency": frequency,
            "aggregation": aggregation,
            "view": view,
            "filters": dict(filters),
        },
    )
    if start > end:
        st.error("Start date must be on or before end date.")
        return
    if view == "Raw data":
        _raw_table(
            dataset,
            start if dated else None,
            end if dated else None,
            zone,
            tuple(filters),
            theme,
        )
        with st.expander("Field types"):
            st.dataframe(
                pl.DataFrame(
                    {
                        "Field": [name for name, _ in metadata.schema],
                        "Type": [dtype for _, dtype in metadata.schema],
                    }
                )
                .to_pandas()
                .style.set_properties(
                    **{"background-color": theme.panel, "color": theme.text}
                ),
                hide_index=True,
                height=220,
                row_height=24,
                width="stretch",
            )
        return
    if not dated or not selected:
        metric_ticker(None, theme)
        st.info("Select a numeric signal and a dated dataset, or inspect raw data.")
        return
    with st.spinner("Scanning selected intervals…", show_time=True):
        bundle = load_series(
            dataset,
            start,
            end,
            zone,
            tuple(selected),
            tuple(filters),
            FREQUENCIES[frequency],
            aggregation,
        )
    if bundle.statistics.is_empty():
        metric_ticker(None, theme)
        st.info("No observations match these dates and series filters.")
        return
    summary_key = panel_key(dataset.key, "summary")
    series_names = tuple(bundle.statistics["series"].to_list())
    _choice(summary_key, series_names, saved.get("summary", series_names[0]))
    summary_name = st.selectbox("Summary series", series_names, key=summary_key)
    row = bundle.statistics.filter(pl.col("series") == summary_name).row(0, named=True)
    metric_ticker(row, theme)
    st.plotly_chart(
        build_chart(bundle, theme, zone, 340 if compact else 430),
        theme=None,
        config=CHART_CONFIG,
        key=panel_key(dataset.key, "plot"),
        width="stretch",
    )
    coverage = f"{bundle.observations:,} observations · {bundle.points:,} intervals · {frequency.lower()} / {aggregation.lower()}"
    if bundle.downsampled:
        coverage += " · extrema preserved in display reduction"
    st.caption(
        f"{coverage} · last {row['as_of'].astimezone(ZoneInfo(zone)):%Y-%m-%d %H:%M %Z}"
    )
    stored = preferences(dataset.key)
    store_preferences(dataset.key, {**stored, "summary": summary_name})
