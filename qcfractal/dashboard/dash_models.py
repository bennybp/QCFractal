import dash_bootstrap_components as dbc
import dash_core_components as dcc
import dash_html_components as html
from dash.dependencies import Input, Output
import pandas as pd
import plotly.graph_objs as go
from plotly.colors import DEFAULT_PLOTLY_COLORS

import dash_coreui_components as coreui
from flask import current_app

from .connection import get_socket

_default_margin = {"t": 5, "b": 5, "r": 5, "l": 5}


def manager_graph(status=None, modified_after=None):
    socket = get_socket()

    managers = socket.get_managers(status=status, modified_after=modified_after)
    df = pd.DataFrame(managers["data"])

    bars = []
    if df.shape[0] > 0:
        data = df.groupby("cluster")[["completed", "submitted", "failures"]].sum()
        data["error"] = data["failures"]
        data["running"] = data["submitted"] - data["completed"]

        bar_iter = [("error", DEFAULT_PLOTLY_COLORS[3]), ("running", DEFAULT_PLOTLY_COLORS[2]),
                    ("completed", DEFAULT_PLOTLY_COLORS[0])]

        data.sort_values("completed", inplace=True, ascending=False)
        bars = []
        for status, color in bar_iter:
            bars.append(go.Bar(name=status.title(), x=data.index, y=data[status], marker_color=color))

    return go.Figure(data=bars,
                     layout={
                         # "yaxis_type": "log",
                         "barmode": "stack",
                         "yaxis": {
                             "title": "Completed Tasks"
                         },
                         "xaxis": {
                             "title": "Cluster"
                         },
                         "margin": _default_margin
                     })


def task_graph():

    socket = get_socket()

    cnts = socket.custom_query("task", "counts")
    df = pd.DataFrame(cnts["data"])

    df.loc[df["tag"].isna(), "tag"] = "None"
    order = df.groupby('tag')['count'].sum().sort_values(ascending=False).index

    bar_iter = [("waiting", DEFAULT_PLOTLY_COLORS[0]), ("running", DEFAULT_PLOTLY_COLORS[2]),
                ("error", DEFAULT_PLOTLY_COLORS[3])]

    bars = []
    for status, color in bar_iter:
        bar_data = []
        for tag in order:
            matches = df[(df["status"] == status) & (df["tag"] == tag)]
            bar_data.append(matches['count'].sum())

        bars.append(go.Bar(name=status.title(), x=order, y=bar_data, marker_color=color))

    fig = go.Figure(data=bars,
                    layout={
                        "barmode": "stack",
                        "yaxis_type": "log",
                        "yaxis": {
                            "title": "nTasks"
                        },
                        "xaxis": {
                            "title": "Tag"
                        },
                        "margin": _default_margin
                    })

    return fig
