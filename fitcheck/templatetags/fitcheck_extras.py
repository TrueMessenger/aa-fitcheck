"""Template filters for fitcheck."""

from django import template

register = template.Library()


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
