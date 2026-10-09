"""Sharp, dense workstation tokens and Streamlit widget styling."""

from dataclasses import dataclass

import streamlit as st


@dataclass(frozen=True)
class Theme:
    background: str
    panel: str
    text: str
    muted: str
    border: str
    grid: str
    accent: str = "#2962FF"
    green: str = "#089981"
    red: str = "#F23645"


THEMES = {
    "Dark": Theme("#131722", "#1E222D", "#D1D4DC", "#959BA8", "#2D3139", "#242733"),
    "Light": Theme("#FFFFFF", "#F8F9FA", "#131722", "#667085", "#D0D5DD", "#EAECEF"),
}


def inject_theme(mode: str) -> Theme:
    theme = THEMES[mode]
    st.markdown(
        f"""
<style>
:root, .stApp {{
 --desk-bg:{theme.background}; --desk-panel:{theme.panel}; --desk-text:{theme.text};
 --desk-muted:{theme.muted}; --desk-border:{theme.border}; --desk-accent:{theme.accent};
 --background-color:{theme.background}; --secondary-background-color:{theme.panel};
 --text-color:{theme.text}; --primary-color:{theme.accent}; color-scheme:{mode.lower()};
 --gdg-bg-cell:{theme.panel}; --gdg-bg-header:{theme.background};
 --gdg-text-dark:{theme.text}; --gdg-border-color:{theme.border};
}}
html, body, .stApp, [data-testid="stAppViewContainer"] {{background:var(--desk-bg); color:var(--desk-text)}}
[data-testid="stMarkdownContainer"] {{color:inherit}}
[data-testid="stText"], [data-testid="stText"] pre, [data-testid="stCode"] {{color:var(--desk-text)!important; background:transparent!important}}
[data-testid="stHeader"] {{background:var(--desk-bg)!important; height:28px!important; min-height:0!important; padding:0!important; pointer-events:none}}
[data-testid="stSidebarCollapsedControl"], [data-testid="stSidebarCollapsedControl"] button, [data-testid="stExpandSidebarButton"] {{pointer-events:auto}}
[data-testid="stAppDeployButton"] {{display:none}}
[data-testid="stSidebarHeader"] {{height:28px!important; min-height:0!important; padding:0!important}}
[data-testid="stToolbar"] {{height:28px!important; min-height:0!important; padding:0!important; background:transparent!important}}
[data-testid="stToolbarActions"], [data-testid="stMainMenu"] {{display:none!important}}
[data-testid="stMainBlockContainer"] {{max-width:none; padding:2.2rem 1rem .8rem}}
[data-testid="stSidebar"] {{background:var(--desk-panel); border-right:1px solid var(--desk-border)}}
[data-testid="stSidebarUserContent"] {{padding:1rem .7rem}}
[data-testid="stVerticalBlock"] {{gap:8px}}
[data-testid="stHorizontalBlock"] {{gap:8px}}
[data-testid="stElementContainer"] {{margin:0}}
body, p, label, button, input, select, textarea {{font-family:Inter,Arial,sans-serif!important; font-size:12px!important}}
p {{margin:0 0 4px}}
small, [data-testid="stCaptionContainer"] {{color:var(--desk-muted)}}
h1,h2,h3 {{font-size:15px!important; font-weight:600!important; margin:0!important; padding:0!important}}
button, input, textarea, [data-baseweb="select"] > div,
[data-testid="stDateInput"] > div, [data-testid="stExpander"],
[data-testid="stVerticalBlockBorderWrapper"] {{border-radius:0!important; box-shadow:none!important}}
button {{min-height:28px!important; padding:3px 8px!important; border-color:var(--desk-border)!important}}
button[kind="secondary"], button[kind="tertiary"] {{background:var(--desk-panel)!important; color:var(--desk-text)!important}}
button:hover {{border-color:var(--desk-accent)!important; color:var(--desk-accent)!important}}
input, textarea, [data-baseweb="select"] > div {{background:var(--desk-panel)!important; color:var(--desk-text)!important; border-color:var(--desk-border)!important; min-height:30px!important}}
[data-baseweb="select"] input {{min-height:20px!important}}
[data-baseweb="select"] span, [data-baseweb="select"] svg {{color:var(--desk-text)!important}}
[data-baseweb="tag"] {{border-radius:0!important; background:var(--desk-bg)!important; color:var(--desk-text)!important; border:1px solid var(--desk-border); margin:1px!important}}
[data-baseweb="popover"], [data-baseweb="menu"], [role="listbox"] {{background:var(--desk-panel)!important; color:var(--desk-text)!important}}
[data-baseweb="menu"] li {{background:var(--desk-panel)!important; color:var(--desk-text)!important}}
.react-aria-ComboBox > [role="group"], [data-testid="stDateInputField"] {{background:var(--desk-panel)!important; color:var(--desk-text)!important; border:1px solid var(--desk-border)!important; border-radius:0!important}}
.react-aria-ComboBox button {{background:var(--desk-panel)!important; color:var(--desk-text)!important; border-radius:0!important}}
[data-testid="stDateInput"] [data-rac], [data-testid="stDateInputFieldsScroller"] {{background:transparent!important; color:var(--desk-text)!important}}
[data-testid="stMultiSelectTagsContainer"] input {{background:transparent!important}}
[data-tag] {{border-radius:0!important; background:var(--desk-bg)!important; color:var(--desk-text)!important; border:1px solid var(--desk-border)!important}}
[data-tag] button {{background:transparent!important; color:var(--desk-muted)!important; min-height:16px!important; padding:0!important}}
button[data-variant="segmented_control"] {{background:var(--desk-panel)!important; color:var(--desk-muted)!important}}
button[data-variant="segmented_control"][aria-checked="true"] {{background:var(--desk-bg)!important; color:var(--desk-accent)!important; border-color:var(--desk-accent)!important}}
[role="option"] {{background:var(--desk-panel)!important; color:var(--desk-text)!important}}
[role="option"][data-focused], [role="option"][aria-selected="true"] {{background:var(--desk-bg)!important; color:var(--desk-accent)!important}}
[data-testid="stTooltipIcon"], [data-testid="stSidebarHeader"] span {{color:var(--desk-muted)!important}}
[data-testid="stWidgetLabel"] p {{font-size:10px!important; color:var(--desk-muted); text-transform:uppercase; letter-spacing:.4px}}
[data-testid="stExpander"] {{border:1px solid var(--desk-border)}}
[data-testid="stExpander"] summary {{background:var(--desk-panel); color:var(--desk-text); padding:4px 8px!important; min-height:28px}}
[data-testid="stExpanderDetails"] {{padding:6px!important}}
[data-testid="stVerticalBlockBorderWrapper"] > div {{border-color:var(--desk-border)!important}}
[data-testid="stTabs"] [role="tablist"] {{gap:0; border-bottom:1px solid var(--desk-border)}}
[data-testid="stTabs"] [role="tab"] {{background:var(--desk-panel); color:var(--desk-muted); border-right:1px solid var(--desk-border); padding:5px 12px; height:32px; border-radius:0}}
[data-testid="stTabs"] [role="tab"][aria-selected="true"] {{color:var(--desk-text); background:var(--desk-bg)}}
[data-testid="stTabs"] [data-baseweb="tab-highlight"] {{background:var(--desk-accent)}}
[data-testid="stDataFrame"] {{border:1px solid var(--desk-border)}}
[data-testid="stAlert"] {{border-radius:0; border:1px solid var(--desk-border); padding:8px}}
.desk-masthead {{height:32px; display:flex; align-items:center; gap:14px; border-bottom:1px solid var(--desk-border)}}
.desk-wordmark {{font-size:16px; letter-spacing:1.7px; font-weight:700; color:var(--desk-text)}}
.desk-wordmark b {{color:var(--desk-accent)}}
.desk-tag {{font:10px ui-monospace,SFMono-Regular,Consolas,monospace; color:var(--desk-muted); letter-spacing:1px}}
.desk-rule {{display:flex; justify-content:space-between; padding:5px 0; border-bottom:1px solid var(--desk-border); color:var(--desk-muted); font:10px ui-monospace,SFMono-Regular,Consolas,monospace}}
.desk-panel-title {{font-weight:600; font-size:13px; letter-spacing:.25px; padding:4px 0}}
.desk-ticker {{display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); border-top:1px solid var(--desk-border); border-bottom:1px solid var(--desk-border); background:var(--desk-panel)}}
.desk-tick {{padding:5px 7px; border-right:1px solid var(--desk-border); overflow:hidden}}
.desk-tick:last-child {{border-right:0}}
.desk-tick-label {{font-size:9px; letter-spacing:.6px; color:var(--desk-muted); text-transform:uppercase; white-space:nowrap}}
.desk-tick-value {{font:600 clamp(13px,1.3vw,20px) ui-monospace,SFMono-Regular,Consolas,monospace; color:var(--desk-text); line-height:1.7; white-space:nowrap}}
.desk-empty {{border:1px solid var(--desk-border); padding:24px 12px; color:var(--desk-muted); font:12px ui-monospace,Consolas,monospace}}
.desk-footer {{font:10px ui-monospace,Consolas,monospace; color:var(--desk-muted); border-top:1px solid var(--desk-border); padding:6px 0}}
@media(max-width:700px) {{[data-testid="stMainBlockContainer"]{{padding-left:8px;padding-right:8px}} .desk-wordmark{{font-size:13px}} .desk-tag{{display:none}} .desk-tick{{padding:4px}}}}
</style>
""",
        unsafe_allow_html=True,
    )
    return theme
