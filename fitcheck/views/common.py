"""View helpers shared across the member / manage / review / reports modules."""

from __future__ import annotations


def paginate(request, object_list, per_page=None):
    """Page a queryset or list plus the bits the shared pagination partial needs:
    the Page object, an elided page range, and the current querystring minus
    `page` (so active filters survive page navigation). Out-of-range /
    non-numeric `page` values fall back to a valid page via `get_page`.

    `per_page` defaults to the admin-tunable ScanParameters page size
    (Settings -> Scan & Result Limits)."""
    from django.core.paginator import Paginator

    if per_page is None:
        from ..models import ScanParameters

        per_page = max(1, ScanParameters.current().results_per_page)
    paginator = Paginator(object_list, per_page)
    page_obj = paginator.get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    elided_range = list(
        paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1)
    )
    return page_obj, elided_range, params.urlencode()
