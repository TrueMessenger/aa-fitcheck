# Fit Check — Frequently Asked Questions

Answers to the usage questions that come up most often once Fit Check is installed and
doctrines are live. This is a companion to [README.md](README.md) (features, install,
permissions, settings) — that file stays the canonical reference; this one answers "how
do I actually use this for my situation."

Don't see your question? Check the [open issues](https://github.com/TrueMessenger/aa-fitcheck/issues)
or file a new one. Maintainers: add new entries under the closest section as real questions
come up — keep answers short and link back to README for anything that's already documented
there in full.

## Contents

- [For Pilots](#for-pilots)
- [For Doctrine & Fit Managers](#for-doctrine--fit-managers)
- [For Reviewers](#for-reviewers)
- [Integrating with Secure Groups](#integrating-with-secure-groups)
- [For Leadership — Auditing & Reports](#for-leadership--auditing--reports)
- [ESI & Ship Data](#esi--ship-data)
- [Troubleshooting](#troubleshooting)

---

## For Pilots

### How do I check whether my ship matches a doctrine?

Two ways: paste the fit as EFT text on the submit page, or — if you've granted ESI
access — use **My Ships**, which reads your hangar directly and grades every ship you
own that matches a doctrine hull, no pasting required. My Ships also verifies implants
and abyssal/mutated rolls automatically since it reads your actual assets.

### What do the verdicts mean?

| Verdict | Meaning |
|---|---|
| `COMPLIANT` | Every slot matches the doctrine exactly. |
| `COMPLIANT_SUBS` | Passes, but one or more modules are substitutes rather than exact matches. |
| `NON_COMPLIANT` | A hard failure — wrong hull, a disallowed/missing module, short quantity, or a missing required implant. |
| `ERROR` | The submitted text couldn't be parsed as a fit. |

A reviewer can still **approve** a `NON_COMPLIANT` submission (that's a judgment call —
an FC waiver) or **reject** a passing one; the review status and the engine verdict are
tracked separately.

### Why does my submission show a "stale" badge?

The doctrine, fit, or the specific policy your submission was graded against has
changed since you submitted. Stale doesn't mean you failed — it means the goalposts
moved and your grade needs refreshing. A reviewer (or the periodic recheck) will
re-grade it; if the site has a **staleness grace period** configured, a previously
*passing* submission keeps counting as compliant for a few days after the change so
you're not locked out mid-fix.

### Can I load the doctrine fit straight into my client?

Yes — **Save to EVE** pushes any doctrine fit you can see into your in-game Fittings
panel via SSO (one-time consent). Implants aren't included in the push since EVE stores
those on the clone, not the fitting.

### How do I know what I'm missing to complete the fit?

**Copy Buy All** exports a grouped, summed multibuy list (hull + every module/charge/bay
item) ready to paste into EVE's Multibuy window. If a reviewer sent your submission back,
the review page also offers a **Missing Modules** button — a multibuy list of just the
gap between what you fitted and what's required.

---

## For Doctrine & Fit Managers

### How do I control who can see a doctrine or fitting?

Through **Categories** (`Fitting Standards → Categories`). A category admits a pilot if
they hold *any one* of its selected groups (OR) or *all* of its required groups (AND);
those two conditions combine with OR. A doctrine or fit with no category attached is
public to anyone with `fitcheck.basic_access`. Managers and reviewers bypass category
gates entirely so they can always administer everything.

### How do I let a faction/meta module count as a valid substitute?

Set the module's policy on the fit's item editor: **Standard** (variant-family, filtered
by allowed meta groups) or **Flexible** (meet-or-beat — any module, including mutated,
whose attributes meet or exceed the doctrine baseline). **Strict** allows only the exact
listed type. Explicit include/exclude overrides handle one-off exceptions to whatever the
general policy says.

### How are abyssal/mutated modules evaluated?

Per-attribute, against the doctrine baseline, honoring EVE's own "higher is better" or
"lower is better" direction per attribute. Rolls come from (in order of trust) a Pyfa EFT
export's mutation block, an ESI-verified read of the pilot's actual assets, or manual
entry (flagged **Self-reported** to reviewers, since it can't be independently verified).
Admins can require specific attributes meet a minimum via the abyssal-bounds modal.

### I imported doctrines from colcrunch `fittings` — how do updates stay in sync?

They don't auto-sync continuously — use the **Pull updates** button on an imported
doctrine or fit to re-fetch the latest BOM from the source plugin. Your compliance
policy, overrides, and targeting are preserved across a re-sync; only the fit's own
module list and (for doctrines) category membership are refreshed from the plugin.

### I changed a policy and now a pile of submissions show stale — did I break something?

No — that's the intended behavior. Any policy or BOM change bumps the fit's (or that
one doctrine assignment's) version, and every submission graded against the old version
is flagged stale so nobody's approval silently outlives the rule it was approved under.
Use **Recheck Stale** to re-grade them in bulk; pilots with `FITCHECK_NOTIFY_PILOTS_STALE`
enabled get notified automatically, with a diff of what changed when it's a BOM edit.

---

## For Reviewers

### What does the review queue actually ask me to do?

For each submission you either **approve** (comment optional — an approval is an FC
waiver and needs no justification) or **reject** (comment required — the pilot needs to
know exactly what to refit). The queue is filterable by pilot, doctrine, verdict, and
status so you can work through a backlog by whatever grouping makes sense.

### Is there an audit trail?

Yes — every decision is logged with the actor, action, and comment, visible on the
submission detail page. Reviewers also get notified of new submissions immediately or
via a periodic digest (`FITCHECK_REVIEWER_DIGEST`), and Discord delivery happens for
free if `aa-discordnotify` is installed.

---

## Integrating with Secure Groups

### How do I require fit compliance for membership in an existing Secure Group?

Install the optional extra, migrate, and a new smart filter type appears:

```bash
pip install aa-fitcheck[securegroups]
```

Add a **Smart Filter: Fit Compliance** to your Secure Group and set:

- **Doctrine and/or Fit** — the standard membership requires compliance with. Doctrine =
  any one of its fits qualifies; Fit = that specific standard only.
- **Require reviewer-approved** — off by default (a passing engine verdict is enough); turn
  it on if group membership should require an actual human sign-off, not just an
  automatic grade.
- **Require current** — on by default, meaning stale submissions don't count. Turn it off
  only if you deliberately want stale-but-passing submissions to keep qualifying.

The filter re-evaluates live every time Secure Groups runs its membership check — there's
nothing to keep in sync manually.

### Are grace periods defined by fitcheck or by the Secure Groups app?

Entirely by fitcheck. Secure Groups just asks "is this user compliant right now" each
time it runs; fitcheck's answer already accounts for the **staleness grace period**
(Settings → Enforcement Settings, default 0 = no grace). A positive grace period means a
submission that just went stale keeps counting as compliant for that many days — giving
pilots time to re-verify before they get bounced from the group. Secure Groups itself has
no separate grace concept to configure.

### What are the risks of wiring fitcheck into an existing Secure Group?

The main one is **flapping**: if a doctrine or policy changes and grace is set to 0, every
previously-compliant pilot can drop out of the group the instant Secure Groups
re-evaluates, even if their ship never changed. Set a grace period that gives pilots
realistic time to re-submit before enforcement bites, rather than an instant cutoff. The
other consideration is **`require_approved`** — with it off, a pilot who fixes their fit
regains group membership the moment the engine re-grades them, with no reviewer in the
loop; turn it on if your process requires a human to sign off before membership is
restored.

---

## For Leadership — Auditing & Reports

### As a director/CEO, can I see how many of my members pass a specific doctrine?

Yes, org-wide: the **Reports** tab (`fitcheck.view_compliance_reports`) shows every active
doctrine's target-audience size and its compliant / substitute / non-compliant /
never-submitted split, with a 14-day trend sparkline. Drilling into a doctrine gives a
member-by-member readiness list (filterable by state and searchable by name) plus a
trend chart and CSV export. **This view is not scoped to a single corporation** — it
reports against the doctrine's full audience (everyone the doctrine's categories admit),
filterable by category and member state, not by corp.

Trend history depends on the daily `take_compliance_snapshots` beat task actually being
scheduled (README installation step 5) — it can't be backfilled, so if reports look empty
on a fresh install, check that task is running.

### Can I scope an audit to just my own corporation?

Not as an aggregate report today. What exists is the **Member Inventory** proactive
scan: with `fitcheck.view_own_corp_inventory` a corp leader can browse their own corp's
members' hangars and see a live per-ship compliance verdict for each — useful for
"is everyone ready before we form up," but it's a manual scan you read ship-by-ship, not
a single "N of M pass" number scoped to your corp. `fitcheck.view_member_inventory` gives
the same capability alliance-wide.

Self-serve, corp-role-driven internal audits (a corp Director running an audit without
needing the alliance-wide reports/inventory permissions) is on the **Later** roadmap and
isn't built yet — see the [roadmap in README](README.md#roadmap) and file/upvote an issue
if this is blocking you.

### Do I need to grant every leadership permission to every FC?

No — the four leadership-facing permissions are independent, so you can hand out exactly
what a role needs: `view_compliance_reports` (Reports tab), `view_member_inventory` /
`view_own_corp_inventory` (proactive hangar scans), `review_submissions` or
`secure_group_management` (review queue only, no doctrine/policy editing), and
`manage_doctrines` / `manage_policies` for the people actually authoring standards. See
the full [Permissions table](README.md#permissions) for the complete list.

---

## ESI & Ship Data

### What do I need to authorize to use ESI features?

One click covers it — the **"Connect ESI access"** prompt on My Ships / Pilot Fittings
requests all pilot-facing scopes (assets, structures, implants, saved fittings, and
Save-to-EVE) in a single SSO grant, so you're not re-prompted feature by feature.

### Why does "My Ships" show nothing?

Almost always the local game-data mirror hasn't loaded yet — run
`python manage.py fitcheck_load_sde` (site admin action, not something a pilot can fix).
The page shows a warning banner when this is the cause. If ships still don't show up
after that, the [Diagnostics & Health](README.md) admin page includes an inventory
doctor that pinpoints which layer (token, corptools sync, SDE whitelist) is failing for
a specific character.

### Do I need aa-corptools?

No, but it's **strongly recommended at alliance scale**. Without it, every scanned pilot
in a Member Inventory scan costs one full live ESI asset-tree fetch, so scans are bounded
by a configurable budget (Settings → Scan & Result Limits). With corptools installed and
synced, Fit Check reads its local cache instead — no per-pilot ESI cost, and no fitcheck
token needed at all for that scan.

---

## Troubleshooting

### I upgraded and now some pages 500

Run `collectstatic` and restart the web workers after every upgrade. Alliance Auth's
manifest static storage returns a 500 on any page that references a bundled static asset
that hasn't been re-collected yet.

### A fresh install applies dozens of migrations at once — is that normal?

Yes. Like every Django app, a new install replays the full migration history in order;
the count just reflects the project's development history, not a problem. It runs in
seconds against empty tables.

### The Reports tab / trend chart is empty

Compliance history comes from a daily snapshot task (`take_compliance_snapshots`) that
can't be backfilled — if it wasn't scheduled from day one (README installation step 5),
history only starts accumulating from whenever it's turned on.
