# aa-fitcheck v1.0 — Release Notes

> Initial public release.

## What is aa-fitcheck?

A doctrine ship-fit compliance plugin for [Alliance Auth](https://gitlab.com/allianceauth/allianceauth).
It checks member fits against published doctrine standards, with **module substitutions as a
first-class concept** — unlike existing Auth plugins that only publish fits or check skills,
aa-fitcheck actually grades whether a pilot's fit passes or fails, and exactly why.

---

## Major Features in v1.0

### Substitution Engine

The core of the plugin. Three substitution policies per module slot, assigned with
bipartite matching so overlapping candidate sets are always resolved correctly:

- **Exact** — only the listed module.
- **Variants** — any module in the SDE variant family, filtered by allowed meta groups
  (e.g. "Tech II or Faction only").
- **Meet-or-Beat** — any module whose EVE attributes meet or exceed the doctrine baseline,
  including mutated/abyssal modules when their rolled stats qualify.

Explicit include/exclude overrides let admins allow cross-family swaps or block specific
modules regardless of the general policy.

### Abyssal / Mutated Module Support

Handled across all intake paths: Pyfa EFT mutation blocks (parsed automatically), manual
guided entry (flagged *Self-reported*), and ESI-verified rolls pulled from the pilot's actual
in-game assets via the dynamic-items endpoint (matched per asset `item_id`). Per-attribute
pass/fail tables appear inline in the verdict; admins can set minimum-attribute windows per
module via an abyssal-bounds modal.

### Per-Doctrine Policy Snapshots

A fit can belong to several doctrines, each with its own independent policy rules.
Attaching a fit clones its policies into a snapshot; editing one doctrine's rules never
affects another's. A full override + abyssal-attribute editor is available per
(doctrine, fit) assignment directly from the fit page.

### ESI Integration Suite

- **Inventory validation** — pilots check their own ships from ESI assets without pasting text.
- **Saved-fittings intake** — grade fits the pilot has saved in EVE's in-game Fittings panel, no EFT paste.
- **Implant verification** — active implants read from the pilot's ESI clone.
- **Frigate Escape Bay** — FEB contents verified from ESI assets; REJECT / WARN / IGNORE enforcement.
- **Mutated rolls** — abyssal attributes verified via the public dynamic-items endpoint.
- **Private structure names** — Citadels the pilot's ships sit in are resolved by name plus system/region.
- **Save-to-EVE** — push any doctrine fit into the pilot's in-game Fittings panel.

### Proactive Member Fit Checks

Alliance and corp leadership can scan hangars before a fleet forms — see every member's ships
with per-ship compliance verdicts. Two permission levels: alliance-wide and corp-scoped.

### Category-Driven Visibility & Group Gating

`DoctrineCategory` controls access to both doctrines and fits:
- **Selected-OR** groups: pilot qualifies with any one matching group.
- **Required-AND** groups: pilot must hold all required groups simultaneously.
- Items with no categories are public; managers and reviewers always bypass gates.

### colcrunch `fittings` Integration

One-click import of existing doctrine/fit libraries, plus on-demand "Pull updates" that
refreshes BOMs while preserving every policy override. Soft dependency — everything no-ops
when `fittings` is not installed.

### Review Workflow

Filterable review queue; approve (comment optional) or reject (comment required, shown to
the pilot); stale badges when the policy changes after a submission; full audit log; AA
notifications with optional periodic digest; Discord delivery via `aa-discordnotify`.

---

## Additional Features in v1.0

**Pilot QoL:** Copy as EFT, Copy Buy All (multibuy list), Save to EVE, Missing Modules
deficit list on the review form. The submission page shows a collapsible **Submitted loadout**
panel — the full captured fit grouped by section, including drone/fuel/cargo bay contents — and
the findings comparison surfaces drone/fighter-bay items the pilot carries beyond the doctrine.

**Compliance Sections:** All slot sections (exact qty), Drone/Fighter bays, Cargo (refit
spares + implant requirements), Boosters (warn-only), Fuel Bay for capitals (warn-only),
Implants (ESI-verified; unverifiable submissions warn but never auto-fail), Frigate Escape Bay
(ESI-verified; configurable enforcement mode). Required implants and boosters the pilot
**carries in cargo or fleet hangar** (rather than plugged/active) pass as "Carried in cargo as
refit".

**Policy Editor:** Per-item policy mode, allowed meta groups, quantity leeway (Qty%), notes,
override chips, abyssal bounds modal. Named Compliance Policies apply a ruleset in bulk — including
four **pre-built** policies (Strict / Standard / Flexible / No Enforcement) that ship with the
plugin (red-bordered, editable by admins only) and can be applied to any fit.
Every change bumps the fit version and flags stale submissions.

**Enforcement Settings:** Site-wide REJECT / POLICY / WARN / IGNORE for Implants, Boosters,
Fuel Bay, and FEB — configured in-app with no `local.py` changes.

**Always-Current SDE:** ~9,400 types, ~130k attribute values, ~5,600 mutaplasmid mappings
from CCP's official JSONL bundle (including `dynamicItemAttributes`). Daily auto-refresh.

---

## Compatibility

| Component | Requirement |
|-----------|-------------|
| Alliance Auth | 4.x – 5.x |
| Django | 4.2 |
| Python | 3.10 / 3.11 / 3.12 |
| django-eveuniverse | >= 1.5 |

---

## After v1.0

These are the **v1.0.0** notes — a frozen snapshot of the first public release. Every release
since is recorded in **[CHANGELOG.md](CHANGELOG.md)**, which is the living changelog.

**v1.1.0** delivered the Secure Groups smart filter, the cross-plugin compliance Python API, the
optional corptools asset read-through, authoritative colcrunch category sync, the multi-select
Frigate Escape Bay picker, and GitHub Actions CI. Still on the roadmap: org-wide compliance
reports + CSV export, and an i18n pass — see the README roadmap.

---

## Installation

See the [README](README.md) for full installation instructions.

```bash
pip install aa-fitcheck
python manage.py migrate
python manage.py fitcheck_load_sde
```
