"""Tiny inline-SVG helpers — no charting library, just enough to draw a
sparkline trend line for a pooler's point history."""


def sparkline_points(values: list[float], w: int = 90, h: int = 26, pad: int = 3) -> str:
    if not values:
        return ""
    if len(values) == 1:
        values = [values[0], values[0]]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    n = len(values)
    step = (w - 2 * pad) / (n - 1)
    pts = []
    for i, v in enumerate(values):
        x = pad + i * step
        y = pad + (h - 2 * pad) * (1 - (v - lo) / span)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def sparkline_trend(values: list[float]) -> str:
    if len(values) < 2:
        return "flat"
    return "up" if values[-1] >= values[0] else "down"
