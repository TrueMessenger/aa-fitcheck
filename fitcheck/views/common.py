"""View helpers shared across the member / manage / review / reports modules."""

from __future__ import annotations


def paginate(request, object_list, per_page=50):
    """Page a queryset or list plus the bits the shared pagination partial needs:
    the Page object, an elided page range, and the current querystring minus
    `page` (so active filters survive page navigation). Out-of-range /
    non-numeric `page` values fall back to a valid page via `get_page`."""
    from django.core.paginator import Paginator

    paginator = Paginator(object_list, per_page)
    page_obj = paginator.get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    elided_range = list(
        paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1)
    )
    return page_obj, elided_range, params.urlencode()
