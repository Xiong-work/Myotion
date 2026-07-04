import os as _os
import base64 as _base64

import plotly.express as px
import plotly.graph_objects as go
import pandas as pd
import numpy as np

from .summary_reader import METRIC_LABELS


def _load_watermark_b64():
    """Base64-encode the branding watermark once; empty string if missing."""
    path = _os.path.normpath(_os.path.join(
        _os.path.dirname(__file__), "..", "..", "myotion_resources", "myotion_logo_origin.png"
    ))
    try:
        with open(path, "rb") as f:
            return _base64.b64encode(f.read()).decode("ascii")
    except OSError:
        return ""


_WATERMARK_B64 = _load_watermark_b64()

# Group palette — consistent with Dracula theme used by the app
GROUP_PALETTE = [
    "#6272a4",  # Group 1 — blue
    "#ff9f43",  # Group 2 — orange
    "#26de81",  # Group 3 — green
    "#fc5c65",  # Group 4 — red
    "#a55eea",  # Group 5 — purple
    "#45aaf2",  # Group 6 — sky
]

_DARK_LAYOUT = dict(
    template="plotly_dark",
    paper_bgcolor="#282a36",
    plot_bgcolor="#1e1f28",
    font=dict(color="#f8f8f2", size=12),
    margin=dict(l=60, r=20, t=50, b=80),
    legend=dict(
        orientation="h", yanchor="bottom", y=-0.30, xanchor="center", x=0.5,
        bgcolor="rgba(40,42,54,0.6)", font=dict(color="#f8f8f2"),
    ),
)


def _color_map(groups: list) -> dict:
    return {g: GROUP_PALETTE[i % len(GROUP_PALETTE)] for i, g in enumerate(sorted(groups))}


def _metric_label(metric: str) -> str:
    return METRIC_LABELS.get(metric, metric)


def build_chart(
    df: pd.DataFrame,
    metric: str,
    channels: list,
    chart_type: str,
    group_col: str = "Group",
) -> go.Figure | None:
    """
    Build a plotly Figure for the given parameters.
    Returns None if there is no data to plot.
    """
    if df is None or df.empty or metric not in df.columns:
        return None

    plot_df = df[df[group_col].notna() & (df[group_col] != "None")].copy()
    if channels:
        plot_df = plot_df[plot_df["Channel"].isin(channels)]
    if plot_df.empty:
        return None

    cmap = _color_map(plot_df[group_col].unique().tolist())
    ylabel = _metric_label(metric)

    if chart_type == "Box":
        fig = px.box(
            plot_df, x="Channel", y=metric, color=group_col,
            color_discrete_map=cmap, points="all",
            labels={metric: ylabel},
        )
    elif chart_type == "Bar (Mean±SD)":
        fig = _bar_mean_sd(plot_df, metric, group_col, cmap, ylabel)
    elif chart_type == "Violin":
        fig = px.violin(
            plot_df, x="Channel", y=metric, color=group_col,
            color_discrete_map=cmap, box=True, points="all",
            labels={metric: ylabel},
        )
    elif chart_type == "Strip":
        fig = px.strip(
            plot_df, x="Channel", y=metric, color=group_col,
            color_discrete_map=cmap,
            labels={metric: ylabel},
        )
    else:
        fig = px.box(
            plot_df, x="Channel", y=metric, color=group_col,
            color_discrete_map=cmap, points="all",
            labels={metric: ylabel},
        )

    fig.update_layout(**_DARK_LAYOUT)
    fig.update_layout(title=f"{ylabel} by Channel and Group", yaxis_title=ylabel)
    fig.update_xaxes(showgrid=False, tickfont=dict(color="#f8f8f2"),
                     title_font=dict(color="#f8f8f2"), linecolor="#44475a")
    fig.update_yaxes(showgrid=False, tickfont=dict(color="#f8f8f2"),
                     title_font=dict(color="#f8f8f2"), linecolor="#44475a")
    return fig


def build_scatter(
    df: pd.DataFrame,
    metric_x: str,
    metric_y: str,
    channels: list,
    group_col: str = "Group",
) -> go.Figure | None:
    """Scatter plot of metric_x vs metric_y, one point per participant-channel."""
    if df is None or df.empty:
        return None
    if metric_x not in df.columns or metric_y not in df.columns:
        return None

    plot_df = df[df[group_col].notna() & (df[group_col] != "None")].copy()
    if channels:
        plot_df = plot_df[plot_df["Channel"].isin(channels)]
    if plot_df.empty:
        return None

    cmap = _color_map(plot_df[group_col].unique().tolist())
    fig = px.scatter(
        plot_df, x=metric_x, y=metric_y, color=group_col,
        color_discrete_map=cmap, hover_data=["Participant", "Channel"],
        trendline="ols",
        labels={metric_x: _metric_label(metric_x), metric_y: _metric_label(metric_y)},
    )
    fig.update_layout(**_DARK_LAYOUT)
    fig.update_layout(title=f"{_metric_label(metric_x)} vs {_metric_label(metric_y)}")
    fig.update_xaxes(showgrid=False, tickfont=dict(color="#f8f8f2"), linecolor="#44475a")
    fig.update_yaxes(showgrid=False, tickfont=dict(color="#f8f8f2"), linecolor="#44475a")
    return fig


def build_generic_chart(
    df: pd.DataFrame,
    dv: str,
    x_col: str,
    color_col: str | None,
    chart_type: str,
) -> go.Figure | None:
    """
    Build a plotly Figure for imported external data, using whatever column
    names the user chose (within/between factors) instead of the fixed
    Channel/Group columns build_chart() assumes.
    """
    if df is None or df.empty or dv not in df.columns or x_col not in df.columns:
        return None

    plot_df = df.dropna(subset=[dv, x_col])
    if plot_df.empty:
        return None

    common = dict(x=x_col, y=dv, labels={dv: dv})
    if color_col and color_col in plot_df.columns:
        cmap = _color_map(plot_df[color_col].dropna().unique().tolist())
        common["color"] = color_col
        common["color_discrete_map"] = cmap

    if chart_type == "Box":
        fig = px.box(plot_df, points="all", **common)
    elif chart_type == "Bar (Mean±SD)":
        group_cols = [x_col] + ([color_col] if color_col else [])
        summary = plot_df.groupby(group_cols)[dv].agg(mean="mean", std="std").reset_index()
        summary["std"] = summary["std"].fillna(0)
        fig = go.Figure()
        if color_col:
            for level in sorted(summary[color_col].dropna().unique().tolist()):
                sub = summary[summary[color_col] == level]
                fig.add_trace(go.Bar(name=str(level), x=sub[x_col], y=sub["mean"],
                                      error_y=dict(type="data", array=sub["std"].tolist())))
            fig.update_layout(barmode="group")
        else:
            fig.add_trace(go.Bar(x=summary[x_col], y=summary["mean"],
                                  error_y=dict(type="data", array=summary["std"].tolist())))
        fig.update_layout(yaxis_title=dv)
    elif chart_type == "Violin":
        fig = px.violin(plot_df, box=True, points="all", **common)
    elif chart_type == "Strip":
        fig = px.strip(plot_df, **common)
    else:
        fig = px.box(plot_df, points="all", **common)

    fig.update_layout(**_DARK_LAYOUT)
    fig.update_layout(title=f"{dv} by {x_col}" + (f" and {color_col}" if color_col else ""),
                      yaxis_title=dv)
    fig.update_xaxes(showgrid=False, tickfont=dict(color="#f8f8f2"),
                     title_font=dict(color="#f8f8f2"), linecolor="#44475a")
    fig.update_yaxes(showgrid=False, tickfont=dict(color="#f8f8f2"),
                     title_font=dict(color="#f8f8f2"), linecolor="#44475a")
    return fig


def _bar_mean_sd(df, metric, group_col, cmap, ylabel):
    summary = (
        df.groupby([group_col, "Channel"])[metric]
        .agg(mean="mean", std="std")
        .reset_index()
    )
    summary["std"] = summary["std"].fillna(0)
    fig = go.Figure()
    for group in sorted(cmap):
        gdf = summary[summary[group_col] == group]
        fig.add_trace(go.Bar(
            name=group,
            x=gdf["Channel"],
            y=gdf["mean"],
            error_y=dict(type="data", array=gdf["std"].tolist()),
            marker_color=cmap[group],
        ))
    fig.update_layout(barmode="group", yaxis_title=ylabel,
                      xaxis=dict(tickfont=dict(color="#f8f8f2")),
                      yaxis=dict(tickfont=dict(color="#f8f8f2")))
    return fig


def figure_to_html(fig: go.Figure) -> str:
    """Convert a plotly figure to a self-contained HTML string."""
    import plotly.io as pio
    return pio.to_html(fig, full_html=True, include_plotlyjs=True)


def empty_html(message: str = "") -> str:
    # Faint, centered logo watermark behind the message — visible enough to brand
    # the empty state without competing with the "nothing to plot yet" text.
    watermark = (
        "background-image:linear-gradient(rgba(40,42,54,0.9),rgba(40,42,54,0.9)),"
        f"url(data:image/png;base64,{_WATERMARK_B64});"
        "background-repeat:no-repeat,no-repeat;"
        "background-position:center,center;"
        "background-size:cover,200px 200px;"
    ) if _WATERMARK_B64 else "background:#282a36;"
    return (
        "<!DOCTYPE html><html style='height:100%'><body style='"
        f"{watermark}color:#6272a4;font-family:sans-serif;"
        "display:flex;align-items:center;justify-content:center;height:100%;margin:0'>"
        f"<p style='font-size:14px;position:relative'>{message}</p></body></html>"
    )
