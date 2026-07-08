from django.contrib import admin

from .models import (
    CompliancePolicy,
    ComplianceFinding,
    Doctrine,
    DoctrineCategory,
    DoctrineFit,
    DoctrineFitItem,
    FitItemOverride,
    FitSubmission,
    PolicySlotRule,
    SdeLoadRecord,
    SdeType,
    SubmissionActionLog,
)
from .models.securegroups import SECUREGROUPS_INSTALLED

if SECUREGROUPS_INSTALLED:
    from .models import FitComplianceFilter


@admin.register(DoctrineCategory)
class DoctrineCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "color")
    search_fields = ("name",)
    filter_horizontal = ("selected_groups", "required_groups", "reviewer_groups", "fits")


@admin.register(Doctrine)
class DoctrineAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "created_by", "updated_at")
    list_filter = ("is_active", "categories")
    search_fields = ("name",)
    filter_horizontal = ("categories",)


class FitItemOverrideInline(admin.TabularInline):
    model = FitItemOverride
    extra = 0
    raw_id_fields = ("alt_type",)


class DoctrineFitItemInline(admin.TabularInline):
    model = DoctrineFitItem
    extra = 0
    raw_id_fields = ("module_type", "charge_type")
    fields = (
        "section",
        "module_type",
        "quantity",
        "policy",
        "allowed_meta_groups",
        "min_quantity_pct",
        "allow_mutated",
    )


@admin.register(DoctrineFit)
class DoctrineFitAdmin(admin.ModelAdmin):
    list_display = ("name", "ship_type", "version", "is_active", "compliance_policy")
    list_filter = ("doctrines", "is_active")
    search_fields = ("name",)
    raw_id_fields = ("ship_type",)
    filter_horizontal = ("doctrines",)
    inlines = [DoctrineFitItemInline]


@admin.register(DoctrineFitItem)
class DoctrineFitItemAdmin(admin.ModelAdmin):
    list_display = ("fit", "section", "module_type", "quantity", "policy", "min_quantity_pct")
    list_filter = ("section", "policy")
    raw_id_fields = ("fit", "module_type", "charge_type")
    inlines = [FitItemOverrideInline]


class PolicySlotRuleInline(admin.TabularInline):
    model = PolicySlotRule
    extra = 0


@admin.register(CompliancePolicy)
class CompliancePolicyAdmin(admin.ModelAdmin):
    list_display = ("name", "created_by", "updated_at")
    search_fields = ("name",)
    inlines = [PolicySlotRuleInline]


class ComplianceFindingInline(admin.TabularInline):
    model = ComplianceFinding
    extra = 0
    can_delete = False
    readonly_fields = ("section", "code", "expected_type", "actual_type", "message")
    fields = readonly_fields


class SubmissionActionLogInline(admin.TabularInline):
    model = SubmissionActionLog
    extra = 0
    can_delete = False
    readonly_fields = ("action", "actor", "comment", "created_at")
    fields = readonly_fields


@admin.register(FitSubmission)
class FitSubmissionAdmin(admin.ModelAdmin):
    list_display = ("pk", "user", "doctrine_fit", "verdict", "status", "source", "created_at")
    list_filter = ("verdict", "status", "source")
    raw_id_fields = ("user", "character", "doctrine_fit", "ship_type", "reviewed_by")
    inlines = [ComplianceFindingInline, SubmissionActionLogInline]


@admin.register(SdeType)
class SdeTypeAdmin(admin.ModelAdmin):
    list_display = (
        "type_id",
        "name",
        "category_id",
        "slot_kind",
        "meta_group_id",
        "meta_level",
        "variation_parent_type_id",
        "published",
    )
    list_filter = ("category_id", "slot_kind", "published")
    search_fields = ("name", "type_id")


@admin.register(SdeLoadRecord)
class SdeLoadRecordAdmin(admin.ModelAdmin):
    list_display = ("sde_build", "loaded_at", "type_count")


if SECUREGROUPS_INSTALLED:

    @admin.register(FitComplianceFilter)
    class FitComplianceFilterAdmin(admin.ModelAdmin):
        list_display = (
            "name",
            "doctrine",
            "fit",
            "require_approved",
            "require_current",
            "enforce_from",
        )
        list_select_related = ("doctrine", "fit")
        search_fields = ("name",)
        raw_id_fields = ("doctrine", "fit")
