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
