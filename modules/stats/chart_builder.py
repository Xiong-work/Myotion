import plotly.express as px
import plotly.graph_objects as go
import pandas as pd
import numpy as np

from .summary_reader import METRIC_LABELS

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
    return (
        "<!DOCTYPE html><html><body style='"
        "background:#282a36;color:#6272a4;font-family:sans-serif;"
        "display:flex;align-items:center;justify-content:center;height:100%;margin:0'>"
        f"<p style='font-size:14px'>{message}</p></body></html>"
    )
