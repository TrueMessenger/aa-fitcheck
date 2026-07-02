from django.urls import path

from .views import manage, member, review

app_name = "fitcheck"

urlpatterns = [
    # Doctrines tab (member view + inline management)
    path("", member.index, name="index"),
    path("doctrine/add/", manage.doctrine_create, name="doctrine_create"),
    path("doctrine/<int:doctrine_pk>/", member.doctrine_detail, name="doctrine_detail"),
    path("doctrine/<int:doctrine_pk>/edit/", manage.doctrine_edit, name="doctrine_edit"),
    path("doctrine/<int:doctrine_pk>/delete/", manage.doctrine_delete, name="doctrine_delete"),
    path(
        "doctrine/<int:doctrine_pk>/fits/add/",
        manage.doctrine_assign_fit,
        name="doctrine_assign_fit",
    ),
    path(
        "doctrine/<int:doctrine_pk>/fits/add-bulk/",
        manage.doctrine_assign_fits_bulk,
        name="doctrine_assign_fits_bulk",
    ),
    path("fitting-search/", manage.fitting_search, name="fitting_search"),
    path("ship-group-list/", manage.ship_group_list, name="ship_group_list"),
    path(
        "doctrine/<int:doctrine_pk>/fits/<int:fit_pk>/remove/",
        manage.doctrine_remove_fit,
        name="doctrine_remove_fit",
    ),
    path(
        "doctrine/<int:doctrine_pk>/import/",
        manage.standard_import,
        name="manage_fit_import",
    ),
    path("categories/add/", manage.category_add, name="category_add"),
    path("categories/", manage.category_list, name="category_list"),
    path("categories/new/", manage.category_edit, name="category_create"),
    path("categories/<int:category_pk>/edit/", manage.category_edit, name="category_edit"),
    path("categories/<int:category_pk>/delete/", manage.category_delete, name="category_delete"),
    path("ship-search/", manage.ship_search, name="ship_search"),
    path("fittings-import/", manage.fittings_plugin_import, name="fittings_plugin_import"),
    path(
        "doctrine/<int:doctrine_pk>/resync/",
        manage.doctrine_resync_from_plugin,
        name="doctrine_resync_from_plugin",
    ),
    # Fitting detail + staff test bench
    path("fit/<int:fit_pk>/", member.fit_detail, name="fit_detail"),
    path("fit/<int:fit_pk>/submit/", member.submit_eft, name="submit_eft"),
    path(
        "fit/<int:fit_pk>/members/",
        manage.member_inventory_for_fit,
        name="member_inventory_for_fit",
    ),
    # Pilot Fittings tab (own submissions + ESI ship validation)
    path("pilot/", member.pilot_fittings, name="pilot_fittings"),
    path("pilot/inventory/", member.ship_inventory, name="ship_inventory"),
    # One SSO consent for every pilot ESI scope (assets/structures/implants/
    # fittings-write), replacing the per-scope grant flows.
    path("pilot/connect-esi/", member.grant_all_esi, name="grant_all_esi"),
    path(
        "pilot/fittings-write-token/",
        member.add_fittings_write_token,
        name="add_fittings_write_token",
    ),
    path(
        "fit/<int:fit_pk>/save-to-eve/",
        member.save_fit_to_eve_view,
        name="save_fit_to_eve",
    ),
    path(
        "pilot/submissions/delete/",
        member.submissions_delete_bulk,
        name="submissions_delete_bulk",
    ),
    path("submission/<int:submission_pk>/", member.submission_detail, name="submission_detail"),
    path(
        "submission/<int:submission_pk>/delete/",
        member.submission_delete,
        name="submission_delete",
    ),
    path(
        "submission/<int:submission_pk>/recheck/",
        member.submission_recheck,
        name="submission_recheck",
    ),
    # Fittings & Standards tab
    path("standards/", manage.standards_list, name="standards_list"),
    path("standards/import/", manage.standard_import, name="standard_import"),
    path("standards/recheck/", manage.stale_recheck_page, name="stale_recheck_page"),
    path(
        "standards/fit/<int:fit_pk>/doctrines/",
        manage.fit_set_doctrines,
        name="fit_set_doctrines",
    ),
    path("standards/fit/<int:fit_pk>/settings/", manage.fit_settings, name="manage_fit_settings"),
    path("standards/fit/<int:fit_pk>/update/", manage.fit_update_bom, name="manage_fit_update"),
    path("standards/fit/<int:fit_pk>/history/", manage.fit_archives, name="fit_archives"),
    path("standards/fit/<int:fit_pk>/items/", manage.fit_items, name="manage_fit_items"),
    path(
        "assignment/<int:assignment_pk>/items/",
        manage.assignment_items,
        name="manage_assignment_items",
    ),
    path(
        "assignment/<int:assignment_pk>/resync/",
        manage.assignment_resync,
        name="manage_assignment_resync",
    ),
    path("standards/fit/<int:fit_pk>/delete/", manage.fit_delete, name="fit_delete"),
    path(
        "standards/fit/<int:fit_pk>/apply-policy/",
        manage.fit_apply_policy,
        name="fit_apply_policy",
    ),
    path(
        "standards/fit/<int:fit_pk>/recheck/",
        manage.fit_recheck_stale,
        name="fit_recheck_stale",
    ),
    path("manage/module-search/", manage.module_search, name="module_search"),
    path("manage/item/<int:item_pk>/override/add/", manage.override_add, name="override_add"),
    path(
        "manage/item/<int:item_pk>/override/add-bulk/",
        manage.override_add_bulk,
        name="override_add_bulk",
    ),
    path(
        "manage/override/<int:override_pk>/remove/",
        manage.override_remove,
        name="override_remove",
    ),
    path(
        "manage/item/<int:item_pk>/attributes/",
        manage.attribute_policy_save,
        name="attribute_policy_save",
    ),
    path(
        "manage/item/<int:item_pk>/attribute-candidates/",
        manage.attribute_candidates,
        name="attribute_candidates",
    ),
    # Per-assignment (doctrine, fit) twins of the override/attribute endpoints
    path(
        "manage/aitem/<int:item_pk>/override/add/",
        manage.assignment_override_add,
        name="assignment_override_add",
    ),
    path(
        "manage/aitem/<int:item_pk>/override/add-bulk/",
        manage.assignment_override_add_bulk,
        name="assignment_override_add_bulk",
    ),
    path(
        "manage/aoverride/<int:override_pk>/remove/",
        manage.assignment_override_remove,
        name="assignment_override_remove",
    ),
    path(
        "manage/aitem/<int:item_pk>/attributes/",
        manage.assignment_attribute_policy_save,
        name="assignment_attribute_policy_save",
    ),
    path(
        "manage/aitem/<int:item_pk>/attribute-candidates/",
        manage.assignment_attribute_candidates,
        name="assignment_attribute_candidates",
    ),
    # Submissions (review) tab
    path("review/", review.queue, name="review_queue"),
    path(
        "review/delete/",
        review.submissions_delete_bulk,
        name="review_submissions_delete_bulk",
    ),
    path("review/<int:submission_pk>/decide/", review.decide, name="review_decide"),
    # Settings hub (fittings import + enforcement / global settings)
    path("settings/", manage.settings_home, name="settings_home"),
    path("settings/diagnostics/", manage.diagnostics, name="diagnostics"),
    path(
        "settings/diagnostics/snapshots/run/",
        manage.snapshot_run_now,
        name="snapshot_run_now",
    ),
    path(
        "settings/diagnostics/snapshots/purge/",
        manage.snapshot_purge,
        name="snapshot_purge",
    ),
    # Policy editor (plugin admins)
    path("policies/", manage.policy_list, name="policy_list"),
    path("policies/enforcement/", manage.enforcement_settings, name="enforcement_settings"),
    path("policies/add/", manage.policy_edit, name="policy_create"),
    path("policies/<int:policy_pk>/", manage.policy_edit, name="policy_edit"),
    path("policies/<int:policy_pk>/delete/", manage.policy_delete, name="policy_delete"),
    path(
        "policies/<int:policy_pk>/toggle-disabled/",
        manage.policy_toggle_disabled,
        name="policy_toggle_disabled",
    ),
]
