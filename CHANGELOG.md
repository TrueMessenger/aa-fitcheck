# Changelog

All notable changes to **aa-fitcheck** are recorded here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the versioning rules
are described below.

## Versioning

This project follows [Semantic Versioning](https://semver.org/)
(`MAJOR.MINOR.PATCH`). `__version__` lives in `fitcheck/__init__.py` (Hatch reads
it via `[tool.hatch.version]` in `pyproject.toml`, so that single string drives
the published package version).

The published version means **"what's released," not "what's in the working
tree."** Changes accumulate under **[Unreleased]** as they merge; day-to-day PRs
add a line here and do **not** touch `__version__`. The number is bumped once,
at release/tag time, to the **highest level** the accumulated batch requires:

- **MAJOR** (`x.0.0`) — a backward-incompatible change to the data model, public
  API, or documented behaviour.
- **MINOR** (`1.x.0`) — new, backward-compatible functionality.
- **PATCH** (`1.0.x`) — backward-compatible bug fixes only.

A bump resets the lower fields (a minor bump sets patch to 0; a major bump sets
minor and patch to 0). At release: scan everything under Unreleased, pick the
single highest applicable level, bump once, move the Unreleased entries under
the new version heading with the date, and tag the commit to match. (The current
Unreleased set contains fixes and the removal of a non-functional setting, so
the next release will be a patch bump.)

Pushing the `vX.Y.Z` tag triggers `publish.yml`, which builds and uploads to
PyPI — it does **not** create a GitHub Release. After the tag lands, also run
`gh release create vX.Y.Z --notes-file <path>` (notes = that version's
CHANGELOG section) so the GitHub Releases page — a separate feature from git
tags — stays in sync with PyPI instead of silently freezing at whichever
version last got one.

## [Unreleased]

### Added
- Member Inventory scans now show an upfront notice when corptools is absent or the asset
  source is forced to live ESI, explaining that the scan is limited to the configured
  budget (`member_scan_esi_budget`, default 25 pilots per page load). README documentation
  emphasizes that corptools is strongly recommended at alliance scale (#47).

### Fixed
- Bulk audits now surface a warning when the per-ship abyssal lookup cap truncated
  verification, and the compliance finding distinguishes cap-skipped rolls from missing
  data (#48).

## [1.10.0] - 2026-07-06

**Upgrade notes:** run `python manage.py migrate` (adds migration 0032), then
`collectstatic` and restart the web workers as usual. No new beat task, permission, or
setting is required. Scan behaviour changes out of the box: the Member Inventory scan now
covers **all** corptools-synced members (previously it silently stopped at the first 200
alphabetically), and at most 25 members without a corptools sync are fetched live from ESI
per scan — raise that under **Settings → Scan & Result Limits** if you need more.

### Added
- **Scan & Result Limits** settings page (Settings tab, `manage_policies`): the previously
  hard-coded scan/result bounds are now admin-tunable in-app, each with an explanation of
  the impact of raising it — the Member Inventory live-ESI fallback budget (default 25),
  ships graded per audit click (default 50), abyssal-verification lookups per ship
  (default 25), and the page size of the paginated lists (default 50). Migration 0032.

### Fixed
- Member Inventory scanned only the first 200 members alphabetically and silently hid the
  rest (#50). corptools-synced members are now all scanned via a bulk read (a handful of
  queries at any alliance size); only the live-ESI full-tree fallback is budgeted (see the
  new Scan & Result Limits page — the old code could attempt up to 200 multi-second live
  fetches in one page load), skipped pilots are announced on the page, the skipped-pilot
  banners list at most 10 names, and the duplicate roster-wide token query is gone (#49).
- Fittings & Standards page: the hull-class filter listed each ship class once per fitting
  instead of once; doctrine filter pills now render in the app's rounded chip style matching
  the Doctrines tab; filter order is now Category, Hull class, then search.

## [1.9.0] - 2026-07-06

**Upgrade notes:** no new migration this release. Run `collectstatic` and restart the web
workers as usual. To activate the new import slot lint immediately, run
`python manage.py fitcheck_load_sde --force` once — otherwise it stays dormant until CCP's
next build triggers the daily SDE reload. If you are upgrading Alliance Auth to 5.2.x,
upgrade aa-fitcheck to this version **first or at the same time** — older aa-fitcheck
versions crash on AA 5.2 (see Fixed below).

### Added
- Fittings & Standards page: doctrine/hull-class/category filters, name search, sortable
  columns, standalone-fit filter, and pagination (50/page).
- Import-time slot lint: importing, updating, or re-syncing a fitting standard now warns when
  the fit exceeds the hull's slot layout (e.g. nine low-slot modules on a six-low hull) —
  warn-only, nothing is rejected; Strategic Cruisers are exempt (subsystem-modified layouts).
  Powered by hull slot/hardpoint attributes now kept in the local SDE mirror. **Upgrade note:**
  run `python manage.py fitcheck_load_sde --force` once after upgrading to populate the hull
  attributes and activate the lint immediately — otherwise it stays silently dormant until
  CCP's next build triggers the daily SDE reload (#67).

### Fixed
- Alliance Auth 5.2 compatibility: reviewer notifications, the review digest, and
  compliance-snapshot audience resolution crashed with `AttributeError: 'Permission'
  object has no attribute 'state_set'` under AA 5.2, whose new `Permission` proxy model
  breaks `app_utils.users_with_permission`. Permission-holder resolution is now done
  locally with forward lookups that work on both AA 5.1 and 5.2.

## [1.8.0] - 2026-07-03

**Upgrade notes:** run `python manage.py migrate` (new migration `0031` — staleness version
ladders and the grace-period setting; all defaults are behavior-preserving, no submission
changes staleness state on upgrade) and `collectstatic`, then restart the web and Celery
workers. No new beat task and no new permission this release. The staleness grace period
defaults to 0 (compliance expires immediately, as before) — set it on **Settings →
Enforcement Settings** if you want pilots to keep Secure Groups access for a few days after
a fit change while they re-verify.

### Added
- **Staleness grace period** on the Enforcement Settings page: for the configured number of
  days after a fit or policy change, stale passing submissions keep counting as compliant
  for the Python API and Secure Groups, giving pilots time to re-verify before group
  membership lapses. Default 0 preserves the previous immediate expiry; the stale badge and
  pilot notifications are always immediate (#13).
- A `fitcheck.signals.compliance_changed` Django signal fires on first grading, re-check,
  and reviewer decision, carrying the submission, user, fit/doctrine, old→new verdict and
  status, and the acting user — so other plugins can react to compliance changes without
  polling the Python API. Documented in the README Integration section (#68).
- Sandbox check on the "Test a Fit" bench: a **Check only (don't save)** button grades an
  EFT paste and shows the full findings without creating a submission — nothing lands in
  the review queue or audit log. Ideal for theorycrafting and pre-purchase checks (#66).
- Pilots are now notified when "Recheck Stale" re-grades their stale submission — including
  a compact old→new module diff when the fit's BOM changed — and holders of approved
  submissions are warned once per fit version that the fit moved on (never re-graded). The
  submission page shows the same diff on stale submissions. Toggle with
  `FITCHECK_NOTIFY_PILOTS_STALE` (#62).

### Changed
- **Staleness is now scoped to what actually changed** (migration 0031): editing one
  doctrine's policy snapshot stales only that doctrine's submissions, source-policy edits
  stale only submissions graded against the fit's defaults, and BOM / fit-wide settings
  changes stale everything — previously any edit staled every submission for the fit. The
  stale badge, Recheck Stale, pilot notifications, the Python API, and Secure Groups all
  share the scoped definition, so unrelated edits no longer expire compliance or notify
  unaffected pilots (#13).
- Internationalization pass: task notification texts (reviewer alerts, review digest,
  decision and re-check notices) are now translation-wrapped, the app-level `locale/`
  scaffolding is in place, and CI verifies translation extraction stays clean (#6).

## [1.7.0] - 2026-07-02

### Added
- **Reports tab** (gated by the `view_compliance_reports` permission, which now
  does something): an org-wide **compliance overview** — every active doctrine's
  audience size, compliant / substitute / non-compliant / never-submitted split,
  ready-%, and a 14-day sparkline, filterable by category — and a per-doctrine
  **drill-down** with a member-by-member readiness list (state filter, name
  search, pagination), a compliance **trend chart** rendered from the snapshot
  history (inline SVG, no new assets), and **CSV exports** on both pages that
  honour the active filters.
- **Failure and substitution analytics** on the doctrine drill-down: the top
  failing modules (missing / not allowed / quantity short, with distinct pilot
  counts) and the most-used substitutions. Counted over each pilot's latest
  submission per fit so frequent resubmitters aren't over-weighted; the new
  `FITCHECK_REPORT_ANALYTICS_WINDOW_DAYS` setting (default 90, `0` = unlimited)
  bounds how far back submissions are considered.

## [1.6.0] - 2026-07-02

### Added
- **Compliance snapshot collection for trend reporting** (migration 0030). A new
  daily beat task, `fitcheck.tasks.take_compliance_snapshots`, records one
  aggregate row per active doctrine per day — audience size and how it splits
  into compliant / compliant-via-substitutes / non-compliant / never-submitted —
  over the doctrine's target audience (basic-access holders admitted by its
  categories). Trend history cannot be backfilled, so schedule the task from day
  one (deploy check `fitcheck.W003` warns when collection looks unscheduled or
  stalled). The **Diagnostics & Health** page gains a reporting-data panel
  (row counts, covered doctrines, history range, last run, schedule status) and
  operator controls — take a snapshot now, purge rows older than a chosen
  window, or purge everything — so the collected data can be managed entirely
  without database access. `FITCHECK_SNAPSHOT_RETENTION_DAYS` (default 365,
  `0` = keep forever) auto-prunes after each run.
- **Composite review-queue index.** `FitSubmission` gains an index on
  (status, verdict, created_at), matching the review queue's combined
  status + verdict filter so it no longer scans within the status slice on
  large installs.

### Removed
- **The non-functional "Min Meta" input on the slot-group policy editor**, along
  with the underlying `min_meta_level` fields (migration 0029). The numeric
  meta-level floor was superseded by the explicit meta-group allow-list before
  1.0; the grading engine never read the value, so the input silently did
  nothing. Meta-based substitution control lives in the per-item "Allowed Meta
  Groups" checkboxes; "Equal to or greater" continues to compare dogma
  attributes within the allowed groups.

### Fixed
- **"Connect ESI access" buttons and the grant banner now appear only when a
  character actually needs them.** My Ships and Pilot Fittings previously showed
  the connect/refresh prompts unconditionally — even when every character on the
  account already held all required scopes (e.g. granted through a full-scope
  Auth login or another app; tokens are shared). The UI is now hidden once all
  owned characters have a valid token carrying the full pilot scope set, and
  reappears if a character or scope is missing or a token expires.

## [1.5.0] - 2026-07-02

### Added
- **Pagination on the review queue and the pilot's validation history.** Both
  lists previously stopped at a silent hard cap (300 / 200 rows); they now page
  at 50 rows with filter-preserving page links and a "Showing X–Y of Z" total,
  so no submission is ever invisible on a large install.

### Fixed
- **My Ships self-audit no longer re-fetches per ship.** Grading several
  selected ships used to pull the character's entire ESI asset tree, the ship
  names, and the active-clone implants once **per ship** (up to 25× each);
  they are now fetched once **per character** and shared across that
  character's selected ships. Ship selections naming a character outside the
  requester's own Auth ownerships are now dropped outright instead of being
  looked up.
- The doctrine fitting picker's search endpoint no longer runs one extra
  database count per result row (the doctrine count is annotated into the
  search query).
- Save-to-EVE failures show a generic error message instead of echoing the
  raw ESI exception text to the page; the detail stays in the server log.
- **Static-data (SDE) loading works on MySQL/MariaDB.** `fitcheck_load_sde` and
  the scheduled `update_sde_data` task crashed with `NotSupportedError` on
  MySQL/MariaDB-backed installs — those backends reject naming the unique
  target in a bulk upsert, which Postgres/SQLite require. The loader now adapts
  to the backend's capabilities; behaviour is unchanged everywhere.
- **My Ships no longer shows an empty list for pilots with many ships docked in
  private structures (Citadels).** The listing used to resolve each Citadel's
  name live, trying every structure-scoped token — and every "no docking
  access" response counted against EVE's shared API error budget, so a pilot
  with a large fleet spread across inaccessible Citadels tripped the rate limit
  and the whole scan aborted to a blank page (even with corptools serving the
  assets perfectly). Structure locations now always come from the local
  structure-name cache (like the bulk member scan since 1.3.0), with unseen
  Citadels queued for the out-of-band `refresh_structure_names` task; only the
  cheap batched custom-ship-name lookup stays live. A scan interrupted by the
  rate limit now also shows a clear warning banner instead of silently
  rendering an empty page.

## [1.4.0] - 2026-06-29

### Added
- **Admin "Diagnostics & Health" page** (Settings tab, plugin-admin only). A
  read-only panel showing app-critical health — Fit Check / corptools versions,
  static-data (SDE) build, load date and type counts, live deploy-check warnings,
  the structure-name cache, enforcement modes, and content/queue counts — plus a
  web **inventory doctor** that explains, per character, why their ships do or
  don't surface in My Ships. Reads the local DB + corptools cache only (no ESI),
  so "my ships" issues can be diagnosed without server/Docker access. Shares its
  logic with the `fitcheck_inventory_doctor` CLI command.

## [1.3.1] - 2026-06-29

### Added
- **`fitcheck_inventory_doctor <character_id>` management command** — a read-only
  diagnostic that reports, per character, exactly what each layer of the My Ships
  pipeline returns (corptools detected, audit found, assets-synced time,
  corptools ship rows with singleton/SDE-whitelist breakdown, token presence, and
  an optional live-ESI count behind `--esi`). Makes no ESI calls by default. Aids
  triage of "0 ships" reports without settings changes or live debugging.

### Internal
- Corptools asset-read tests now run against **real** stub models (registered
  under app_label `corptools`) so the actual ORM filtering (`singleton` /
  `type_id__in` / `character`) is exercised; the previous duck-typed fakes ignored
  `filter()` kwargs and could mask query regressions.

## [1.3.0] - 2026-06-29

### Changed
- **Doctrine category selection is now a searchable dropdown with coloured pills**
  instead of a long checkbox list. On the doctrine create wizard and the doctrine
  Edit panel, each selected/assigned category renders as its own coloured pill
  (matching the Categories tab and the rest of the app), and the dropdown options
  show as coloured badges. Inline "Add category" adds the new category straight into
  the picker. Reuses the bundled tom-select assets; no new dependency.

### Fixed
- **Member Inventory ("Browse Member Ships") no longer trips EVE's ESI rate limit
  on a real alliance.** The listing phase used to resolve a private-structure
  (Citadel) name for every ship over ESI, trying each one against *every* pilot's
  structure token — and each "no docking access" 403 drained the shared ESI error
  budget, so a moderate alliance scan returned only a few ships with a rate-limit
  warning. The bulk scan now makes **no live ESI calls** for ship names or Citadel
  locations: it reads them from a new local cache (`StructureNameCache`), and a new
  periodic task **`fitcheck.tasks.refresh_structure_names`** resolves/refreshes those
  names out-of-band with a bounded, paced, negatively-cached fan-out. Schedule it via
  `CELERYBEAT_SCHEDULE` (see the README); `FITCHECK_STRUCTURE_CACHE_TTL` (default 24h)
  caps how stale a cached name can get. Self-inventory ("My Ships", small N) is
  unchanged and still resolves names live. Migration `0028`; deploy check
  `fitcheck.W002` warns when the task looks unscheduled.
- **"Validate my ships" no longer silently shows zero ships when the static-data
  mirror hasn't been loaded.** On a fresh install, before `fitcheck_load_sde` has
  run (or its scheduled task has fired), the ship inventory filtered every asset
  out and showed an empty list as if the pilot owned nothing. Now:
  - **Ship listing falls back to eveuniverse** (a hard dependency) to classify the
    pilot's owned ships, so My Ships / "Validate ships from my inventory" work even
    before the local mirror is populated. Grading still uses the mirror.
  - **The mirror self-heals:** an empty mirror triggers a one-off background load
    (once per 10-minute window), and the My Ships / Pilot Fittings pages show a
    "game data is still loading" notice. A Django system check (`fitcheck.W001`)
    also warns at deploy time, and the README marks `fitcheck_load_sde` as required.
  - **A doctrine-hull scan no longer needs the mirror at all** — it already knows
    the hull type it's looking for.
- **The hull pre-filter banner shows the ship name, not a raw type_id.** Clicking
  "Validate My Ships" for a hull you don't own showed *"Pre-filtered to 12032"*;
  it now resolves and shows the hull name (e.g. *"Pre-filtered to Nightmare"*) with
  a clearer empty state.

## [1.2.0] - 2026-06-26

### Fixed
- **Table columns no longer resize as chip/badge columns grow.** On pages with a
  column that holds a wrapping set of chips — the policy editor's Exceptions, the
  Fittings & Standards and stale-recheck "Doctrines" lists, a fitting's
  Alternatives, and the Categories group lists — adding more chips used to widen
  that column and squeeze all the others, so the layout jumped around. Those
  tables now use a fixed column layout (a shared convention), so columns keep
  consistent, aligned widths and the chips simply wrap to more rows.
- **The module-policy "Add Exceptions" finder no longer hides its results.** When
  searching for a module to allow/forbid as an exception, the results list was
  clipped by the section panel, so anything past the first match — and the list's
  scrollbar — was cut off. The list now shows in full, floating over the panel
  edge. (The "Allowed Meta Groups" column header also drops its redundant
  "(Checked = Allowed)" suffix.)
- **Capital jump fuel is now counted wherever it sits.** A doctrine's fuel-bay
  requirement is now satisfied by isotopes the pilot holds in the **fuel bay, the
  cargo hold, or the fleet/freight hangar** (the last two both arrive as cargo) —
  pooled together. Fuel in the bay reads as a clean pass; fuel carried in cargo /
  the fleet hangar passes flagged **"carried in cargo or the fleet hangar (not in
  the fuel bay)"** (a refit pass, like implants/boosters carried in cargo), so it
  counts even under the strict Reject fuel mode. Previously only fuel physically
  in the fuel bay counted, so a capital carrying its jump fuel in cargo showed a
  false shortfall. Also, on an SDE reload the loader now re-sections stale
  **doctrine** isotope rows left in Cargo (created before isotopes were classified
  as fuel) into the Fuel Bay. No schema change.

### Changed
- **Module Policies only offers the meta groups (and the Abyssal toggle) that can
  actually exist for each item.** The "Allowed Meta Groups" checkboxes used to show
  the same six tiers (Tech I/II, Storyline, Faction, Officer, Deadspace) for every
  module, even when most are impossible — a rig has no Officer variant, ammo has no
  Deadspace tier. Now each item offers only the meta groups of its **actual
  substitutes** (the variant-family members other than the item itself): a rig
  shows Tech I/II, a structure module its Structure tiers, and **a module with no
  variant substitutes shows no checkboxes at all** (just a short "no substitutes"
  note). The **Abyssal (mutated)** checkbox likewise only appears when the item has
  an abyssal/mutaplasmid variant — it's gone for drones, boosters, ammo, and other
  modules that can't be abyssal. Impossible groups are rejected on save, and any
  left over from the old all-tiers default are dropped the next time a fit's
  policies are saved. Computed from the local SDE mirror (the variant family + each
  type's meta group) — no extra ESI calls, no schema change. Grading results are
  unchanged (impossible groups never matched anything anyway).
- **Multi-select pickers are now readable and consistently styled everywhere.**
  The tokenized multi-select dropdowns (category group/doctrine/fitting pickers,
  the FEB "Allowed" picker, module exception pickers) now render with a **white
  options panel and black text** on every page — previously only the FEB picker
  carried that styling, so other pickers showed dark, hard-to-read dropdowns on
  the dark theme. Selected items render as **teal pills** (matching the
  Save/confirm buttons) app-wide. The styling now lives once in the base template
  instead of being copied per page.
- **One-click ESI access instead of per-scope token prompts.** Pilots used to be
  asked for each ESI scope separately (assets, then implants, then re-grant for
  structure names). Now a single **Connect ESI access** button — on the Pilot
  Fittings page and the My Ships inventory page — grants everything a pilot's
  audit features use in **one SSO consent**: assets + private-structure names,
  active-clone implants, and Save-to-EVE. Scopes the pilot already granted to
  another Auth app, or that **corptools** supplies, are reused, so the grant only
  asks for what's genuinely missing. No schema change.
- **Member inventory is now select-to-audit, not audit-everything.** Opening a
  fitting's **Member Inventory** page used to fetch every in-scope pilot's entire
  asset tree and immediately grade every matching hull — writing one submission
  per ship on every page load, and holding all of it in memory at once (an
  alliance with thousands of audited pilots is millions of asset rows). It now
  **lists** the matching ships cheaply (a narrow read — ships only, not the whole
  hangar) and grades only the ships you **tick and "Audit selected"**. Submissions
  are created only for ships you actually audit, so the review queue no longer
  fills with auto-generated proactive checks. When the optional **corptools**
  cache backs the read, both the listing and the per-ship grade are narrow DB
  queries; with live ESI, listing keeps only the ship rows and grading fetches a
  selected pilot's contents once. No schema change.

### Added
- **Settings tab.** A new **Settings** tab gathers the manager-facing
  configuration that was scattered across other pages. It has two sections:
  **Fittings import** (the ways to bring fittings in — manual EFT paste, the
  colcrunch Fittings plugin, and an *in-game saved fittings (ESI)* method marked
  *Planned*) and **Enforcement & global settings** (the site-wide enforcement
  modes, which used to live behind a button on the Policies page). The dashboard's
  import button and empty-state now point here. Each section is shown only to
  managers who hold the matching permission (`manage_doctrines` for import,
  `manage_policies` for enforcement). No schema change.
- **FEB ship-class quick-add.** The Frigate Escape Bay "Allowed" picker on the
  fit settings page now has a companion **"Add a whole ship class"** selector:
  pick e.g. *Assault Frigate* and every frigate of that class is folded into the
  Allowed list, instead of ticking ~75 hulls one at a time. The classes expand
  into individual frigate type ids on save, so storage and the compliance engine
  are unchanged. No schema change.
- **Policy template drift detection + re-sync.** A fit's per-module policy is a
  *template*; each doctrine the fit belongs to keeps an independent copy that
  audits actually grade against, cloned when the fit is attached. Editing the
  template never propagated to those copies, so leeway/policy edits could appear
  to do nothing. Now: the per-(doctrine, fit) editor shows a **"differs from
  template"** warning (and per-row badges) when a copy has drifted, with a
  **Re-sync from template** button; the fit's policy page lists **"Used in N
  doctrines"** with each copy's in-sync/differs status and re-sync; and a
  submission page gives managers an **Edit policy for this combination** jump-link
  to the exact copy that graded it. No schema change.

### Removed
- **"Import my saved fittings" (member-side saved-fittings audit).** A pilot's
  in-game *saved fittings* are a plan, not proof of what they actually own;
  grading them gave false assurance. The button and its page are gone — the
  inventory-based self-audit (**Validate ships from my inventory** / My Ships) is
  the member path, since what we audit is the pilot's real hangar. The ESI
  saved-fittings read plumbing is retained internally for the planned admin-side
  alliance-fittings import.

## [1.1.0] - 2026-06-18

### Added
- **Secure Groups smart filter** (`FitComplianceFilter`) — auto-manage an
  Alliance Auth group from doctrine compliance: membership requires the pilot to
  be compliant with a chosen doctrine (any fit) or a specific fit. Optional
  feature, enabled by installing the `securegroups` extra
  (`allianceauth-securegroups`); the plugin no-ops cleanly when it is absent.
  Built on the compliance API below.
- **corptools asset read-through** (optional): when aa-corptools (Corp Tools) is
  installed and has synced a character's assets, fitcheck reads the ship
  inventory from corptools' local DB instead of calling ESI live — the heaviest,
  least time-sensitive call. An alliance-wide member-inventory scan then needs
  **no fitcheck token at all** (the player already granted the scope to
  corptools). Falls back to live ESI when corptools is absent or hasn't synced
  the character. New `FITCHECK_ASSET_SOURCE` setting (`auto` default / `esi` /
  `corptools`). corptools is a soft dependency — fitcheck no-ops without it.
- **Authoritative category sync from the fittings plugin**: importing or pulling
  updates from colcrunch `fittings` now also syncs its **Categories** — colour,
  Auth-group visibility (colcrunch `groups` → Selected-OR), and doctrine/fit
  membership — into `DoctrineCategory`. The fittings plugin is the source of
  truth, so a re-sync overwrites those fields each run; purely-local hand-made
  categories (no plugin source link) are never touched. Adds
  `DoctrineCategory.source_plugin_pk` (migration 0027).
- **Public compliance Python API** (`fitcheck.services.api`) for cross-plugin
  use: `is_user_compliant`, `get_qualifying_submission`, `get_user_compliance`,
  and a single-query bulk `iter_user_compliance`. Targets a doctrine (any fit) or
  a specific fit; honours staleness and reviewer rejection, with an optional
  `require_approved`. Backs the Secure Groups smart filter.
- FEB picker: multi-select the accepted Frigate Escape Bay frigates by name as
  removable pills, replacing the single Type-ID box; eligibility is enforced
  server-side (a non-frigate POST is rejected). (#44)

### Changed
- Inventory scans reuse scopes the player already granted to **any** Auth app
  (tokens are shared) and no longer flag a character as "needs access" when
  corptools can serve its assets — so no redundant consent prompt. New
  `esi_assets.existing_token` helper documents the silent-reuse primitive.
- The FEB picker now renders **only on hulls that carry a Frigate Escape Bay**
  (battleship-class: Battleship / Black Ops / Marauder); supercapitals,
  capitals, destroyers and frigates hide the field entirely. (#45)
- FEB field label renamed "Frigate Escape Bay Frigates" → **"Frigate Escape Bay
  - Allowed"**; pills now match the green Save button and the options dropdown is
  white-background / black-text on the dark theme. (#45)
- Title-case sweep of the view-supplied page headings and four card-header
  section titles that the earlier UI-chrome sweep missed. (#45)

### Fixed
- A No-Enforcement slot no longer consumes a module the doctrine also wants
  carried in cargo, which had wrongly failed the cargo line `QTY_SHORT`. (#42)

## Released

The **v1.0.0** public release is documented in
[RELEASE_NOTES_v1.0.md](RELEASE_NOTES_v1.0.md). The incremental patch releases
**1.0.1 – 1.0.4** (policy disable/enable + entity dates; submission-grading
fixes; the Strict / Standard / Flexible policy-label rename; FEB findings-panel
layout) predate this changelog. Everything from `1.1.0` onward is tracked here.
