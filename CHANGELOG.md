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
Unreleased set contains new features, so the next release will be a minor bump.)

## [Unreleased]

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
