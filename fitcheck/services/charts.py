"""Server-rendered SVG chart geometry. Pure functions, no DB, no JS.

One static trend line and per-row sparklines don't justify vendoring a chart
library (collectstatic churn, license file, upgrade docs); native SVG
``<title>`` elements give hover tooltips for free and ``currentColor`` follows
the Auth theme. Revisit only if a later feature needs interactive charts.
"""

from __future__ import annotations


def sparkline_points(
    values: list[float], *, width: int = 120, height: int = 28, pad: int = 2
) -> str:
    """SVG polyline ``points`` string for a series of 0-100 values. Empty input
    returns ``""``; a single value renders a flat line across the width."""
    if not values:
        return ""
    if len(values) == 1:
        values = [values[0], values[0]]
    inner_w = width - 2 * pad
    inner_h = height - 2 * pad
    step = inner_w / (len(values) - 1)
    points = []
    for i, value in enumerate(values):
        clamped = max(0.0, min(100.0, float(value)))
        x = pad + i * step
        y = pad + inner_h * (1 - clamped / 100.0)
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def build_trend_chart(snapshots, *, width: int = 760, height: int = 240) -> dict | None:
    """Geometry for the doctrine trend chart from ordered ``ComplianceSnapshot``
    rows. Returns ``None`` under 2 points (nothing worth drawing). The y axis is
    fixed 0-100% so charts stay comparable across doctrines."""
    if len(snapshots) < 2:
        return None

    pad_left, pad_right, pad_top, pad_bottom = 44, 12, 12, 26
    inner_w = width - pad_left - pad_right
    inner_h = height - pad_top - pad_bottom
    step = inner_w / (len(snapshots) - 1)

    def ready_pct(s) -> float:
        if not s.audience_count:
            return 0.0
        return round(
            (s.compliant_count + s.compliant_subs_count) * 100.0 / s.audience_count, 1
        )

    points = []
    coords = []
    for i, snap in enumerate(snapshots):
        pct = ready_pct(snap)
        x = pad_left + i * step
        y = pad_top + inner_h * (1 - pct / 100.0)
        coords.append((x, y))
        points.append(
            {
                "x": round(x, 1),
                "y": round(y, 1),
                "date": snap.date,
                "ready_pct": pct,
                "audience": snap.audience_count,
                "compliant": snap.compliant_count,
                "subs": snap.compliant_subs_count,
                "non_compliant": snap.non_compliant_count,
                "no_submission": snap.no_submission_count,
            }
        )

    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    baseline = pad_top + inner_h
    area = (
        f"M {coords[0][0]:.1f},{baseline:.1f} "
        + " ".join(f"L {x:.1f},{y:.1f}" for x, y in coords)
        + f" L {coords[-1][0]:.1f},{baseline:.1f} Z"
    )

    y_ticks = [
        {"y": round(pad_top + inner_h * (1 - pct / 100.0), 1), "label": f"{pct}%"}
        for pct in (0, 25, 50, 75, 100)
    ]
    # At most ~6 x labels, always including the first and last day.
    label_every = max(1, (len(snapshots) - 1) // 5)
    x_ticks = [
        {"x": p["x"], "label": p["date"].strftime("%b %d")}
        for i, p in enumerate(points)
        if i % label_every == 0 or i == len(points) - 1
    ]

    return {
        "width": width,
        "height": height,
        "line": line,
        "area": area,
        "baseline": round(baseline, 1),
        "pad_left": pad_left,
        "inner_right": round(pad_left + inner_w, 1),
        "points": points,
        "y_ticks": y_ticks,
        "x_ticks": x_ticks,
    }
