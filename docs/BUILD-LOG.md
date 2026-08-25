# Build log

One entry per working session, newest first. Each records what changed, what was
**measured**, and what was deployed.

The measured numbers are the point. A readiness score, a component count or a
"verified against X" goes stale silently, and there is no way to tell a number
that is still true from one that was true in June. A dated entry lets a claim be
checked against when it was made — the same reason every probe result in this
tool carries its evidence rather than just its verdict.

Written at commit time or not at all. A build log nobody updates is worse than
no build log, because it reads as current. The deploy line is the one exception
— it is filled in after the deploy succeeds, since it records something that had
not happened yet when the code was committed.

---

## 2026-08-25 (later)

### llms-full.txt now passes its own rules: 12/100 -> 96/100

Wiring the FULL-* rules to the artifact revealed the generator producing a file
that failed seven of its own nine checks. `render_llms_full` concatenated each
page's markdown **unchanged**, and a page's markdown was written to stand alone:
its own H1, H2 for its own sections, and whatever the site puts on every page.

**Measured before, on prosperitymedia.com.au:** 12/100. 83 H1s. 608 H2s against
74 `Source:` lines and 74 page blocks. 616 body headings at H1 or H2. One block
repeated 46 times, ~3,240 wasted tokens. 834 emphasis-wrapped headings. 249,977
tokens against a 200,000 budget.

New `app/core/full_text.py` normalises each page before concatenation. It
imports its thresholds from `full_rules` rather than restating them, so the
generator and the check cannot drift apart.

- Headings demote by a **shift**, not to a fixed level, so a page running
  H1/H2/H3 becomes H3/H4/H5 and relative hierarchy survives. Never promoted.
- Emphasis wrapping stripped; a heading that merely *starts* with an asterisk
  (`*args`) is escaped instead, because stripping would silently edit text.
- Boilerplate **hoisted, not deleted** — once, into a shared section. That
  section is H3: H2 means "page boundary with a Source:", and FULL-002 counts.
- Whitespace collapsed, except two blank lines survive inside a fence where a
  gap between functions is formatting. FULL-006 only objects at three.
- `DEFAULT_FULL_MAX_CHARS` **derived** from FULL-009's budget. It was 1,000,000
  against the rule's 800,000, so the generator's ceiling permitted a file that
  failed the generator's rule — and did, by almost exactly that margin.

**Three defects only real content surfaced**, which the fixture would never have
produced:

1. A body line reading `Source: https://...` — a post citing a study — is this
   format's page-boundary marker and inflates the count FULL-002 checks. The
   colon is escaped: renders identically, stops counting.
2. An empty heading (`<h3>` wrapping only an image) emitted `#### ` with a
   trailing space. That one line was the whole of FULL-006 on an otherwise clean
   file.
3. Code must never be hoisted or split — blocks are reassembled after hoisting,
   so a naive blank-line split could drop half a fence and leave it unclosed.

**Measured after, against live pages fetched through the same extractor the
crawler uses:** 29/100 → 96/100 on that sample. H1s 5 → 1. H2/Source/page
25/7/7 → 7/7/7. The remaining failure is FULL-008 — five thin pages — and that
is the sample, not the generator: `/blog/` and `/case-studies/` return 265
characters each. FULL-008 already passed on the real 74-page crawl. **No content
is dropped to satisfy any rule.**

Two existing fixtures had to change, both wrong the same way: page bodies that
were byte-identical across pages, which the hoist correctly reads as boilerplate,
leaving every page block empty. Real crawled pages differ. Both now say so, or
the next person tidies them back into constants.

**Measured:** 904 tests, ruff clean.

**Deployed:** web + worker, 2026-08-25.

---

## 2026-08-25

### llms-full.txt: asked for, and no longer hidden

`GenerateOptions.generate_full` defaulted to `True` and **no caller ever passed
it**, so every run built a full-text file. `bundle` then only offered it when the
scenario was `read_and_cite` — so six goals out of seven paid to generate a file
they were never shown. Generated always, delivered rarely, asked for never.

The run form now asks, the answer rides in `run.plan`, and the pipeline reads it
back while the run is attached. **The default is now `False`**: an unset caller
no longer commits the run to the expensive path.

The bundle half is deliberately not symmetrical. If the file exists it is
offered whatever the scenario, because at that point it has already been paid
for and hiding it wastes the spend; where the goal does not call for one the
artifact carries a note saying so. The scenario still decides what a goal
*requires* — it no longer decides what the operator may see.

One test changed rather than being added to.
`test_llms_full_respects_its_character_budget` built its options without the
flag and relied on the old default, so under the new default it stopped
exercising the budget it was named for. It now asks for the file it measures.

**Measured:** 872 tests, ruff clean.

### The sidebar becomes a gunmetal panel

Brought across from GEO Tracker at your request: the panel surface,
`--sidebar-accent` (`#2e3a41`) as the active fill, group labels, and an icon per
item. Not brought across: shadcn's components — this is Jinja with hand-written
CSS — and collapse-to-icons, which needs client state we deliberately do not
have.

The nav was a light menu floating beside an already-dark top bar, so the chrome
read as two unrelated pieces. Gunmetal with white text is an approved
core-colour pairing at **14.05:1** and makes the dark chrome one shape.

**It fixed a pill nobody could see.** `.gap` is a 12%-white veil with white
text. Over the old near-white sidebar that was white on white, so every gap
count — the number the sidebar exists to surface — was invisible unless its item
happened to be active. Found by asking what each colour was sitting on, not by
looking at the pill.

Icons are inline SVG, not a sprite or a font: they inherit `currentColor`, so
one set serves the dark panel and the light ground below the breakpoint, and
markup already in the document cannot half-arrive the way a failed asset can.
Hand-authored at 1.6 stroke, the weight that holds at 18px. All `aria-hidden` —
the label beside each is already the accessible name.

**Contrast computed, not eyeballed.** New `--pm-on-dark-muted` is `#8b9599`,
**4.59:1** on gunmetal — AA for normal text, which a disabled item still is. It
never lands on the active fill, where it would fail at **3.81:1**, because a
disabled item is by definition not the current one.

Three tests, because this breaks silently: an item with no icon renders the
fallback dot and looks merely wrong, never broken. Verified the macro test
catches a typo, and that all 20 shapes are distinct.

**Measured:** 875 tests, ruff clean. 20 nav items, all with icons.

**Deployed:** web + worker, 2026-08-25.

### The rail now runs the full page height

Reported from the deployed build: the panel stopped after the last nav item and
left a band of page ground beneath it, so the dark chrome ended mid-screen on
any short page.

`body` becomes a flex column with `min-height: 100vh`; `.shell` aligns `stretch`
rather than `flex-start`; and sticky moves off the rail onto a new
`.side-inner`.

That third change is why the first two work. **A single element cannot both
stretch and stick** — the two want different heights, and `position: sticky` was
silently winning. Switching `align-items` alone would have changed nothing
visible, which is the sort of fix that gets called "not working" when it was
never reaching the element it targeted.

The rail is flush now rather than a floating rounded card, so the header and the
rail read as one continuous L. `main` already carries 2rem of padding, so the
shell needs no gap to hold the content off the dark edge.

**Measured:** 875 tests, ruff clean. Rail 0→224px, full body height, verified
in the browser at 1920×1080.

### And the width

The rail fix left the other half of the same complaint. `main` caps at 1180px
and centres — right when a page was a centred document, wrong beside a 224px
rail. **Measured at 1920: 258px of dead ground on each side.**

Inside the shell the cap is now 1560px. Pages outside the shell — sign-in,
sign-up — keep the narrow measure.

Widening alone would have traded one whitespace complaint for a worse one: the
paragraph under each panel heading already runs ~140 characters at 1180px and
would reach ~185 at 1560. `.panel > p` takes a 75ch measure — scoped to a direct
child of a panel because that is descriptive prose by construction, where
capping `.muted` would also have caught table cells doing a different job.

**Measured:** 875 tests, ruff clean.

---

## 2026-08-24

Deployed `bb22a6b` — web and worker. Three commits: the rename and the rule
engine (`13a7f0a`), hosted Lighthouse and CrUX (`14b0bbb`), the AI refine layer
(`bb22a6b`).

**Measured**

- 809 tests, ruff clean
- prosperitymedia.com.au: readiness **53/100**, 6 of 20 components live
- agents.md spec score **98/100** — the one failure is AGT-013 (INFO, no
  llms.txt pointer), which is correct: the site does not publish one yet
- CLS **0.01** from real users, origin-wide, steady over 25 periods
- tap targets **FAIL on 2 of 3** sampled pages
- probe ~40s with Lighthouse and CrUX; page render from the snapshot ~0.03s

**Changed**

- Renamed to the AI SEO Technical Discovery Support Tool. Two of the strings are
  client-facing — the `agents.md.liquid` credit and the `ai-catalog.json` `"by"`
  field — so the golden fixture diff was reviewed rather than regenerated blind.
- The sidebar's "Generate" group is gone. It offered "llms.txt" and "agents.md"
  and neither opened a file. Families now carry an outstanding count, with
  `None` rendering nothing because nothing was measured and `0` rendering a tick.
- **Wired the rule engine in.** It had been dead code: nothing outside `tests/`
  imported `app/core/rules/`, so AGT-004 — *"Every URL in the file must be one
  the probe confirmed"* — had never once run.
- Lighthouse via PageSpeed and CrUX now settle `cls` and `tap-targets`.
- The AI refine layer, as operations on `AgentsDoc` rather than on file text.

**Learned**

- **Wiring the rules in immediately found a disagreement.** AGT-004 failed our
  own agents.md on twelve URLs, all twelve crawled pages. `_assemble` had it
  right and `evidence.py` had it wrong: a crawled page is evidence for a link an
  agent *reads*, even though it is not evidence for an endpoint an agent
  *calls*. I had applied endpoint reasoning to links.
- **AGT-006 then caught a defect in the refine layer**, before it shipped:
  attribution was putting the operator's email into a file the rule correctly
  describes as *"fetched by anyone, forever"*.
- **Verifying vendor APIs beat trusting our own docs, twice.** `tap-targets` does
  not exist in Lighthouse 13 (it is `target-size`), and PageSpeed's embedded
  field data was empty for a site that does have some — CrUX queried directly at
  origin granularity answers where the page-level query 404s.
- **Two latent bugs surfaced that the Lighthouse work would have activated**: a
  manual tick could override a *failing* probe, and the UI kept offering the
  tick for checks a probe had just decided.

**Correction to `bb22a6b`'s commit message**

That message says *"Chat spend is now recorded"*. **It is not.** `refine_turn`
creates an `LLMUsage` and passes it to `LLMClient`, and then never reads it —
all three exit paths ignore it and it is garbage-collected with the request.
Passing the argument was necessary and not sufficient.

Three sites leak, not one: `refine_turn` (`app/main.py:1428`), `chat_edit`
(`:2121`, no usage object at all) and `suggest_brief` (`:526`, same shape as
refine). `cost_of` reads `Run.stats` and nothing else, so `/admin` does not
report these as unpriced — it reports them as **not having happened**, while
they run on `gpt-4o`. There is still no spend ceiling anywhere.

Fixing it needs a decision, not just a line: `ArtifactEdit` has no numeric
column, so domain-scoped spend needs a migration rather than an attribution to
some arbitrary `Run`. Written up as Phase 1 of
`tools/plans/02-llmstxt-completion.md`.

---

## 2026-08-21 — finish the registry consolidation

Deployed `bd3a70d` — web and worker, migrations clean (none in this commit),
both `SUCCESS`, `/healthz` 200.

**Measured**

- 691 tests, ruff clean
- prosperitymedia.com.au: **53/100**, sampled from 3 pages, WordPress,
  5 components live of 20 applicable — and the wizard and the overview now
  return the same 53 from the same three URLs
- Registry: **26 components**, 8 templated
  - content site: 20 applicable — 6 client, 14 developer
  - ecommerce site: 24 applicable — 7 client, 17 developer
- `app.core.bundle.Effort is app.core.components.Effort` → **True** (was `False`)
- `templates/agents.html`: 273 → 108 lines

**Changed**

- **Removed the second `Effort` enum.** `bundle.py` defined its own; `site_state`
  returned `components.Effort` keys and `handover.html` looked them up in
  `bundle`'s `EFFORT_LABELS`. That page worked only because both were `StrEnum`
  and a `StrEnum` hashes as its string. Change either to a plain `Enum` and it
  is a `KeyError` on a live page — verified by doing exactly that in a scratch
  script. The test that guards it asserts **identity**, not equality, because
  equality is what masked the bug.
- `SCENARIO_FILES` → `SCENARIO_COMPONENTS`, keyed on component keys rather than
  filenames. This immediately caught a scenario naming `llms-full.txt` with no
  component behind it; `llms-full` is now in the registry (25 → 26).
- `HEADER_HINTS` moved to `components.py`, beside what it describes.
- `_deployment_tasks` is now a projection of `for_developer(site_type)` rather
  than a hand-written list that had to be remembered alongside the registry.
- **Two prose-only components became templates.** `link-header` and
  `markdown-negotiation` were the last two handing a developer a paragraph.
  Both are platform-aware: Shopify, Wix and Squarespace get "not achievable
  here" instead of a snippet they would spend an afternoon failing to apply.
- **`/agents` is now the site overview** — probe surfaces, readiness score,
  live-of-applicable, per-family counts, links into the tabs. The family tabs
  own everything it shed. Counts come from the same `SiteStatus` the tabs
  render, so the overview cannot contradict a tab.

**The score disagreed with itself**

Found while verifying the overview end to end. The onboarding wizard sampled one
page per sitemap group; `/agents` read the homepage alone. Same site, same
moment, two different numbers, and neither page said how many pages it had read —
so the two looked like a contradiction rather than two different measurements.

Three changes, in the order they matter:

1. `ReadinessReport.sampled` records the pages the page-level checks actually
   read, and `summary()` says so — including *"read the homepage only, so
   template-level faults may be missed"* when that is what happened. A score
   that does not carry its sample cannot be compared with another score.
2. `sample_from_sources()` is now the single definition of how this tool samples
   a site. The bug was two call sites deciding independently; one function is
   what stops a third from being added.
3. `/agents` re-reads the sitemaps to sample by the same rule. The first attempt
   sampled by URL template from the existing crawl instead — cheaper, no extra
   requests — and it was **wrong**: prosperitymedia is flat, every page is
   `/{slug}`, so clustering by path yields one template and the audit sees a
   blog post and nothing else. Sitemap membership still separates a post from a
   service page where the URL does not. The cheaper sample was worse in exactly
   the case that motivated sampling.

**Learned**

- The template test asserted every template contains `REPLACE_ME`. The two new
  server-config snippets legitimately have no placeholder — they are real config
  to paste, and inventing placeholders would make them useless. There are two
  kinds of template: a **service** template asserts something exists and answers,
  so every claim in it is a placeholder; a **config** snippet asserts nothing.
  Both must still say they are not files to publish. The test now splits on that,
  plus a third test asserting every templated component falls into one kind or
  the other — so a new template belonging to neither is caught rather than
  silently untested.

## 2026-08-21 (earlier) — WCAG checks, and two checks that were lying

Deployed `1a638a7` · 670 tests

- Added WCAG 4.1.2 (deprecated/abstract/unknown ARIA roles) and 4.1.1
  (duplicate IDs, dangerous ones first).
- **`roles` was passing a page with two real violations.** Found by fetching a
  rendered DOM through Scrapling and comparing it against the static parse: the
  check only matched inline `onclick`, so anything bound by a framework was
  invisible to it. `semantic-html` would likewise have failed any site using
  ARIA landmarks instead of HTML5 tags.
- prosperitymedia.com.au readiness **59 → 42**. Nothing about the site changed;
  a check stopped lying. Every fix in this tool so far has made the number worse
  and truer.
- Readiness now samples one page per sitemap group rather than the homepage
  alone, worst answer wins — a homepage is the least representative page a site
  has.
