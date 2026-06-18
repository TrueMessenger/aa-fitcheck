# Fit Check (aa-fitcheck)

Doctrine ship-fit compliance for [Alliance Auth](https://gitlab.com/allianceauth/allianceauth),
with **module substitutions as a first-class concept**.

The engine knows every EVE module's variant family (the data behind Pyfa's right-click
"Variations" menu) and grades each substitution against the policy your fitting team sets.
A doctrine that requires a Heat Sink II can still pass when the pilot fits an Imperial Navy
Heat Sink — because the engine knows both modules belong to the same family and your policy
allows it. Mutated/abyssal modules are compared attribute-by-attribute using EVE's own
`highIsGood` semantics. No existing Alliance Auth plugin does this level of fit verification.

---

## Major Features

### Substitution Engine

Three substitution policies per module slot, assigned with bipartite matching so overlapping
candidate sets are never mis-allocated:

| Policy | Description |
|--------|-------------|
| **Strict** (exact) | Only the listed module passes. |
| **Standard** (variant family) | Any module in the SDE variant family, filtered by allowed meta groups (e.g. "Tech II or Faction only"). |
| **Flexible** (meet-or-beat) | Any module — including mutated/abyssal — whose EVE attributes meet or exceed the doctrine baseline in every required dimension. |

**Explicit overrides** (include/exclude) per slot let admins allow specific cross-family swaps or
ban specific modules regardless of the general policy.

### Abyssal / Mutated Module Support

Abyssal modules are handled transparently across all intake paths:

- **Pyfa EFT export** — mutation blocks parsed automatically from the extended EFT format.
- **Manual entry** — a guided stats form, flagged *Self-reported* to reviewers.
- **ESI-verified** — rolled attribute values fetched from the pilot's actual in-game assets
  via the public dynamic-items endpoint, matched per asset `item_id`.

Per-attribute pass/fail tables appear inline in the compliance verdict. Admins can set minimum-
attribute windows per module (e.g. "propmod must roll at least 120% signature radius bonus") via
an abyssal-bounds modal with dual-handle range sliders.

### Per-Doctrine Policy Snapshots

A fit can belong to several doctrines, each with its own independent policy rules:

- Attaching a fit to a doctrine **clones** its item policies into a snapshot owned by that assignment.
- Editing one doctrine's policy never affects another's or the source fit's defaults.
- A full override + abyssal-attribute editor is available per (doctrine, fit) assignment, directly
  from the fit page.

### ESI Integration

| Capability | ESI Scope |
|------------|-----------|
| Pilot inventory validation + Frigate Escape Bay content | `esi-assets.read_assets.v1` |
| Structure names + location (system / region) | `esi-universe.read_structures.v1` |
| Implant verification | `esi-clones.read_implants.v1` |
| Saved-fittings intake (import fits saved in EVE) | `esi-fittings.read_fittings.v1` |
| Save-to-EVE (push a fit into the in-game Fittings panel) | `esi-fittings.write_fittings.v1` |
| Mutated/abyssal roll verification | Public dynamic-items endpoint (unauthenticated) |

> **Enable all five scopes on your EVE application** at
> [developers.eveonline.com/applications](https://developers.eveonline.com/applications) — the same
> app whose Client ID/Secret Auth uses for SSO. A scope that isn't enabled on the application fails
> with `invalid_scope` at login. Pilots then grant the scopes per feature via SSO (assets +
> structures are bundled into one grant; implants and saved-fittings are granted on demand);
> structure names additionally require the character to have docking access.

Pilots validate their ships directly from their hangar without pasting any text. ESI-sourced
submissions carry the real asset `item_id` so the engine always re-pulls current data on re-check.

### Proactive Member Fit Checks

Fleet leadership can scan hangars before a fleet forms rather than waiting for pilots to submit:

- **Alliance-wide**: see every member's ships alongside per-ship compliance verdicts.
- **Corp-scoped**: corps-only leaders see only their own corporation.
- Two permission levels keep scanning rights granular without custom roles.

### Category-Driven Visibility & Group Gating

`DoctrineCategory` controls access to both doctrines and fits:

- **Selected-OR** groups: pilot qualifies if they hold any one of the selected groups.
- **Required-AND** groups: pilot must hold every required group simultaneously.
- Both conditions combine with OR; items with no categories are public.
- Managers and reviewers bypass all category gates.

Categories carry a custom color for visual identification and appear as filterable chips on the
Doctrines tab.

### colcrunch `fittings` Integration

If your alliance already uses [colcrunch `fittings`](https://gitlab.com/colcrunch/fittings):

- **One-click import** of existing doctrine and fit libraries (doctrines, fits, BOMs).
- **On-demand "Pull updates"** re-syncs BOMs when the fittings team publishes changes, while
  preserving every compliance policy and override.
- `fittings` is a soft dependency: all code paths no-op cleanly when it is not installed.

### Review Workflow

- Filterable review queue (by pilot, doctrine, verdict, status).
- **Approve** (comment optional) or **reject** (comment required — pilot sees exactly what to fix).
- Stale badges flag submissions graded against an outdated fit version.
- Full audit log per submission with actor, action, and comment.
- AA notifications on decision (immediate or periodic digest); Discord delivery is automatic via
  `aa-discordnotify` when installed.

---

## Additional Features

### Pilot Quality-of-Life

- **Copy as EFT** — one-click clipboard export of the doctrine fit in EFT format.
- **Copy Buy All** — aggregated multibuy list (hull + all modules, charges, and bay contents,
  grouped and summed) ready to paste into EVE's Multibuy window.
- **Save to EVE** — push the doctrine fit directly into the pilot's in-game Fittings panel via SSO.
- **Missing Modules** — the review form generates a paste-ready deficit multibuy list of everything
  a pilot still needs.

### Compliance Sections

Every part of a ship fit is checked independently. Slot sections match exactly (order never
matters); bay and cargo sections require "at least N":

| Section | Behaviour |
|---------|-----------|
| High / Mid / Low / Rig / Subsystem | Exact quantity, any slot order |
| Drone Bay / Fighter Bay | At least N of each type per squadron |
| Cargo | Refit spares with full substitution; implant requirements declared here |
| Implants | ESI-verified from pilot clone; required implants carried in cargo or the fleet hangar pass as a refit; unverifiable submissions warn but never auto-fail |
| Boosters | Warn-only — never a hard fail regardless of mode; boosters carried in cargo or the fleet hangar pass as a refit |
| Fuel Bay | Isotope quantity check for capitals, warn-only |
| Frigate Escape Bay | ESI-verified content; configurable enforcement mode |

Loaded charges are pooled into cargo on both sides: a doctrine specifying "4 Artillery Cannons
needing 4 crystals" passes whether the crystals are loaded or in the hold.

### Policy Editor

No Django admin needed for day-to-day policy tuning:

- Per-item: policy mode, allowed meta groups, quantity leeway (Qty%), notes.
- Override chips: cross-family includes or per-slot excludes, with a search-filtered picker.
- Abyssal bounds modal: per-attribute acceptance windows for mutated modules.
- Named **Compliance Policies** apply a slot-group ruleset to multiple fits in one action.
  - **Pre-built policies** ship ready to use — *Strict*, *Standard*, *Flexible*, and *No Enforcement*
    (ten slot rules each). Built-ins are seeded automatically and are superuser-only to edit and can
    never be deleted, so managers can apply them without risk of losing a shared baseline.
  - **Disable / Enable** any policy (built-in or custom): disabled policies drop out of the "apply"
    picker while fits that already use them keep their per-module settings.
  - Created / updated / disabled dates show on the policies list and edit page; fits and doctrines
    show their created and updated dates too.
- Every policy change bumps the fit version and flags existing submissions as stale.

### Site-Wide Enforcement Settings

Global REJECT / POLICY / WARN / IGNORE modes for each optional section, configured in-app:

- Implants
- Boosters
- Fuel Bay
- Frigate Escape Bay

POLICY mode defers to the per-item policy editor; the other modes override it site-wide.

### Always-Current Game Data

- Local SDE mirror: ~9,400 types, ~130k attribute values, ~5,600 mutaplasmid mappings.
- Includes CCP's official `dynamicItemAttributes` data — no community supplement required.
- Daily Celery beat task checks CCP's official build pointer after downtime and refreshes
  automatically when a new build ships.

---

## Installation

1. Install into your Auth venv:

   ```bash
   pip install aa-fitcheck
   ```

2. Add `"eveuniverse"` and `"fitcheck"` to `INSTALLED_APPS` in `local.py`.

3. Add the ESI contact email CCP requires for third-party apps (skip if already set for django-esi):

   ```python
   ESI_USER_CONTACT_EMAIL = "you@example.com"
   ```

4. Run migrations and load the static data:

   ```bash
   python manage.py migrate
   python manage.py fitcheck_load_sde
   ```

5. Add the SDE refresh task to `local.py`:

   ```python
   CELERYBEAT_SCHEDULE["fitcheck_update_sde_data"] = {
       "task": "fitcheck.tasks.update_sde_data",
       "schedule": crontab(minute="30", hour="12"),  # daily, after CCP's 11:00 downtime
   }
   ```

6. Restart Auth and assign permissions.

### Optional: Secure Groups smart filter

To auto-manage Auth groups from doctrine compliance, install with the
`securegroups` extra (requires [allianceauth-securegroups](https://apps.allianceauth.org/apps/detail/allianceauth-securegroups)):

```bash
pip install aa-fitcheck[securegroups]
```

Add `"securegroups"` to `INSTALLED_APPS`, run `python manage.py migrate`, and a
**Smart Filter: Fit Compliance** becomes available in Secure Groups. The plugin
works normally without it — the filter simply isn't offered.

---

## Permissions

| Permission | Purpose |
|---|---|
| `fitcheck.basic_access` | See the app, visible doctrines, submit own fits, validate own ships, Save-to-EVE |
| `fitcheck.manage_doctrines` | Create/edit doctrines, import fits, set policies, assign fits to doctrines |
| `fitcheck.review_submissions` | Review queue, approve/reject any submission |
| `fitcheck.secure_group_management` | Review and approve submissions only — cannot edit doctrines or standards |
| `fitcheck.manage_policies` | Create/edit named compliance policies |
| `fitcheck.view_compliance_reports` | Org-wide compliance reports (planned) |
| `fitcheck.view_member_inventory` | Browse alliance-wide members' ships and run proactive fit checks |
| `fitcheck.view_own_corp_inventory` | Browse own corporation members' ships (corp-scoped proactive checks) |

---

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `FITCHECK_SDE_SOURCE_URL` | Official CCP JSONL bundle | Static-data archive URL |
| `FITCHECK_NOTIFY_REVIEWERS` | `True` | Notify reviewers on new submissions |
| `FITCHECK_REVIEWER_DIGEST` | `False` | Periodic digest instead of per-submission pings (schedule `fitcheck.tasks.send_review_digest`) |
| `FITCHECK_ESI_CONTACT` | `ESI_USER_CONTACT_EMAIL` | Contact email in the ESI User-Agent header |

Section-level enforcement modes (Implants, Boosters, Fuel Bay, Frigate Escape Bay) are managed
through the in-app **Enforcement Settings** page — no `local.py` changes required.

---

## Integration (Python API)

Other plugins can query a user's doctrine compliance through the stable Python API in
`fitcheck.services.api` — no REST layer, no reaching into internals. A user is *compliant*
when they have a submission whose engine verdict passes (`COMPLIANT` / `COMPLIANT_SUBS`); by
default the check also requires the submission to be **current** (not stale) and **not
reviewer-rejected**. Pass `require_approved=True` to additionally require a reviewer's approval.

```python
from fitcheck.services import api

# Is this user compliant with a doctrine (any one of its fits) or a specific fit?
api.is_user_compliant(user, doctrine=doctrine)        # -> bool
api.is_user_compliant(user, fit=fit, require_approved=True)

# The submission that proves it (newest qualifying one), or None.
api.get_qualifying_submission(user, doctrine=doctrine)

# A ComplianceResult (.is_compliant, .submission, .verdict) for one user…
api.get_user_compliance(user, doctrine=doctrine)

# …or for many users in a single query (built for bulk audits).
for result in api.iter_user_compliance(users, doctrine=doctrine):
    ...
```

Target a `doctrine=` (compliant with *any* fit graded under it), a `fit=` (that specific
fitting standard), or both.

### Secure Groups

With the optional `securegroups` extra installed (see Installation), a **Smart Filter: Fit
Compliance** lets an admin require doctrine/fit compliance for membership of an Auth group —
e.g. a "Shield Supers" group that only holds pilots with a compliant Hel or Wyvern fit. The
filter is backed by the compliance API above and honours the same staleness / approval options.

---

## Roadmap

Versions follow [Semantic Versioning](https://semver.org/); the table is ordered by
priority, not pinned to specific version numbers (those are assigned at release).

| Milestone | Scope |
|-----------|-------|
| **Shipped** | Full substitution engine with bipartite matching; abyssal/mutated modules; per-doctrine policy snapshots; pre-built and custom named compliance policies (with disable/enable); ESI inventory validation; ESI saved-fittings intake (check fits saved in EVE's Fittings panel without an EFT paste); implant, booster, fuel bay, and Frigate Escape Bay verification (implants/boosters carried as cargo or fleet-hangar refit pass); category-driven visibility with group gating; proactive alliance/corp member checks; colcrunch `fittings` import and re-sync; pilot QoL tools (Save-to-EVE, Copy Buy All, Copy as EFT); submission-detail review with a per-section captured-loadout panel; review workflow with audit log and notifications; cross-plugin compliance Python API; optional Secure Groups smart filter |
| **Next** | Compliance reports + CSV export (`view_compliance_reports`); CI (GitHub Actions, tox py310–312); i18n pass |
| **Later** | Corporation-role internal audits (corp members with the right EVE roles audit ships in corp hangars against doctrines, no approval flow); [aa-srp](https://apps.allianceauth.org/apps/detail/aa-srp) integration (compare a loss's killmail fit to doctrines/fits/substitutions during SRP review); bulk "audit all my fits" for pilots; per-doctrine reviewer scoping; override-chip allow/forbid toggle |

See [CHANGELOG.md](CHANGELOG.md) for released and in-progress changes.

---

## Development

```bash
uv venv .venv --python 3.12
uv pip install -e .[tests] fakeredis
python manage.py test fitcheck
```

License: GPL-3.0-or-later.
