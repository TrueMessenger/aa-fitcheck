"""Settings an installation can override in `local.py`."""

from app_utils.app_settings import clean_setting

# Source for static data: "ccp" (official JSONL) or "fuzzwork" (CSV fallback).
FITCHECK_SDE_SOURCE = clean_setting("FITCHECK_SDE_SOURCE", "ccp")

# Override the static-data archive URL (defaults to the official CCP JSONL bundle).
FITCHECK_SDE_SOURCE_URL = clean_setting(
    "FITCHECK_SDE_SOURCE_URL",
    "https://developers.eveonline.com/static-data/eve-online-static-data-latest-jsonl.zip",
)

# Build-number pointer used to detect new releases (documented automation endpoint).
FITCHECK_SDE_LATEST_URL = clean_setting(
    "FITCHECK_SDE_LATEST_URL",
    "https://developers.eveonline.com/static-data/tranquility/latest.jsonl",
)

# Optional URL for the mutaplasmid dataset (dynamicItemAttributes). Empty = use bundled default.
FITCHECK_SDE_DYNAMIC_ITEMS_URL = clean_setting("FITCHECK_SDE_DYNAMIC_ITEMS_URL", "")

# Notify users with review permission when a new submission arrives.
FITCHECK_NOTIFY_REVIEWERS = clean_setting("FITCHECK_NOTIFY_REVIEWERS", True)

# When True, skip per-submission reviewer notifications; schedule
# fitcheck.tasks.send_review_digest instead for a periodic summary.
FITCHECK_REVIEWER_DIGEST = clean_setting("FITCHECK_REVIEWER_DIGEST", False)

# Contact email embedded in the ESI/SDE User-Agent header (required by CCP guidelines).
FITCHECK_ESI_CONTACT = clean_setting("FITCHECK_ESI_CONTACT", "", required_type=str)

# Failure/substitution analytics on the Reports drill-down only consider each
# pilot's latest submission per (fit, doctrine) made within this many days.
# 0 = no time limit.
FITCHECK_REPORT_ANALYTICS_WINDOW_DAYS = clean_setting(
    "FITCHECK_REPORT_ANALYTICS_WINDOW_DAYS", 90
)

# Days of compliance-snapshot history the fitcheck.tasks.take_compliance_snapshots
# beat task keeps; older rows are pruned automatically after each run. 0 disables
# the auto-prune (keep forever). The Diagnostics page offers manual purge controls
# either way.
FITCHECK_SNAPSHOT_RETENTION_DAYS = clean_setting("FITCHECK_SNAPSHOT_RETENTION_DAYS", 365)

# A cached private-structure (Citadel) name is considered stale after this many
# seconds; the fitcheck.tasks.refresh_structure_names beat task re-resolves stale
# rows. The default (24h) means a renamed Citadel shows the old name for at most
# ~1 day. The member-inventory scan itself never calls ESI for these names.
FITCHECK_STRUCTURE_CACHE_TTL = clean_setting("FITCHECK_STRUCTURE_CACHE_TTL", 86400)
