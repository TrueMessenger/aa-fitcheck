"""Template filters for fitcheck."""

from django import template
from django.utils.html import format_html

register = template.Library()


@register.simple_tag
def sparkline(values, width=120, height=28):
    """Inline SVG sparkline for a list of 0-100 values (empty-safe). Strokes
    with ``currentColor`` so it follows the surrounding text/theme colour."""
    from ..services.charts import sparkline_points

    points = sparkline_points(list(values or []), width=width, height=height)
    if not points:
        return ""
    return format_html(
        '<svg viewBox="0 0 {} {}" width="{}" height="{}" aria-hidden="true" '
        'style="vertical-align: middle">'
        '<polyline points="{}" fill="none" stroke="currentColor" '
        'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/></svg>',
        width,
        height,
        width,
        height,
        points,
    )


@register.filter
def sig3(value):
    """Round a number to 3 significant figures, trimming trailing zeros. Whole
    numbers and short decimals pass through unchanged; non-numbers are returned
    as-is. Used to tame long abyssal roll values like 21.373750576376914 → 21.4."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if number == 0:
        return 0
    from decimal import Decimal

    # 3 significant figures.
    rounded = float(f"{number:.3g}")
    # Render without trailing zeros / scientific notation for typical magnitudes.
    text = format(Decimal(str(rounded)).normalize(), "f")
    return text
