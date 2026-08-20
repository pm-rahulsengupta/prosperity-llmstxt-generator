# Conventions

Short list. Each entry exists because breaking it cost something.

## Human-authored state never nests inside a machine-replaced blob

`save_site_config` assigns `config.plan = plan.to_dict()` — a whole-column
replacement — and `CrawlPlan.from_dict` keeps only the keys it models. Anything
else nested in `plan` is therefore one plan approval away from being gone, with
nothing logged and no error raised.

This is not hypothetical. The onboarding brief was going to live under
`plan["brief"]` for convenience, which would have deleted a client's answers the
first time anyone approved a plan on that domain.

So:

- A decision a person made gets **its own column or its own table**. The brief is
  `site_configs.brief`; uploaded metrics are `site_metrics`.
- If it must share a JSONB column, the writer merges rather than replaces —
  `run.stats = {**(run.stats or {}), ...}` is the pattern to copy.
- A field that round-trips through a `from_dict` that drops unknown keys is not
  storage. It is a cache of whatever that class models.

**Next candidate, not yet built:** group verdicts carrying
`verdict_source: "human"`. When the review gate starts recording which verdicts a
person overrode, those rows belong in their own table. They must not be nested
under `run.plan` or `site_configs.plan`, however convenient it looks — a verdict
a person set by hand is the most expensive state in the system to lose and the
hardest to notice missing.

## Absent data is not zero

`PageMetrics` fields are `None` when unknown. A page with no clicks and a page we
have no click data for lead to opposite verdicts, and flattening the two is how a
tool quietly deletes a site's newest work.

Same rule one level up: `head_click_share` is `None` when there are too few
earners to measure a distribution. `0.0` reads as "measured, and flat".

## A skipped rule is not a passed rule

The validator excludes rules that did not apply from the denominator and lists
them as unrun. Scoring a rule that never executed as a pass inflates every score,
and an offline 100 and a networked 100 are different claims.

## Enforce in code what you would otherwise ask a model for

The summarise prompt asked for descriptions "starting with a verb where that
reads naturally" and got 106 banned openers. A prompt is not an enforcement
mechanism. If a rule matters, it gets a deterministic check and a test; the
prompt may also ask, but the check is what makes it true.

The onboarding brief follows this: its free-text answers go to the model, its URL
patterns do not — they are enforced at the verdict layer where a test can prove
they were honoured.

## After any string-replace edit, check `git diff --stat`

An edit that reports success while replacing nothing is invisible. It happened
here: a search string with mangled escapes matched nothing, the prompt gained an
unused import, ruff removed the import as dead, and all 235 tests still passed
because they tested behaviour rather than artifacts.

Where the artifact is the deliverable — prompts, templates, rendered output —
snapshot it. `UPDATE_GOLDEN=1` regenerates the snapshots, and a deliberate change
then shows up as a reviewable diff instead of silence.

## An override predicates on its condition, never on a verdict name

The same collision has now appeared three times, each surviving only until the
verdict taxonomy shifted under it:

1. the low-coverage `EXCLUDE` branch structurally preempted the concentration
   check, so `PROMOTE_EXEMPLARS` could never fire on the case it existed for;
2. the facet override was then guarded with `verdict is not PROMOTE_EXEMPLARS`,
   which held only while promotion was the sole verdict that identified winners;
3. when an unmeasurable-but-material group started arriving as
   `REVIEW`-with-exemplars, that guard stopped matching and the override ate the
   hub again.

A verdict name is an *output* of the rules an override exists to constrain, so
predicating on one couples the override to a taxonomy that will keep moving.
Predicate on the underlying condition instead — the presence of exemplars, a
measured concentration, a declared pattern — which is what the rule actually
means and does not change when a verdict is renamed or added.

The facet override now reads `not group.exemplars`, and that phrasing survives
any number of new verdicts.

## Fold-change for two magnitudes, percentage for a share of a whole

Percentage change is asymmetric: a quantity can grow without limit but can only
ever fall by 100%. Any threshold at or above 1.0 therefore catches doubling and
can never catch halving — drift shipped with exactly that, unable to see a
sitemap group gutted from 4,000 URLs to 200.

Use `fold_change(before, after)` for any before-and-after comparison: click
trends, CTR movement, coverage across runs, group sizes. It is defined in
`app.core.onboarding` and re-exported from `app.core.metrics`, which is where
callers should import it from; it lives a layer down only because `metrics`
imports `onboarding` and the dependency cannot run both ways.

Do **not** use it for a share of a whole — coverage, orphan share, CTR itself.
Those are bounded fractions, not two magnitudes, and a percentage is the right
unit for them.

Audited at the time of writing: drift was the only place in the codebase
comparing two magnitudes. The helper exists so the next one is written correctly
rather than discovered later.

## Embargo means never crawled, never stored, and not left in derived artifacts

Four surfaces hold an embargoed URL, and the first implementation cleared one:

- `pages` — the crawled body;
- `site_metrics` — rows keyed by the URL;
- `runs.llmstxt` and each `document_revisions.llmstxt` — link lines, removed
  individually, which is safe because the format is one line per page;
- `runs.llms_full` and its revisions — **blanked entirely, never edited**. That
  file is concatenated page bodies with no reliable per-page boundary to cut on,
  and a partial removal nobody can verify is worse than an empty field.

Purge is logged with the URLs, not a count: "three pages were removed" cannot
confirm the right three went.

A mistyped pattern is recoverable by removing it from the brief and re-running.
Nothing purged is unrecoverable *from the source*, because the source is the
client's own live website — which is precisely why deleting our copy can be
eager rather than cautious.
