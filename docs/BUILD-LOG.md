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

## 2026-09-09 (later)

### Three files the specs asked for, and the checks that would have lied about them

llms.txt v2, Google's Open Knowledge Format and the AI-info-page convention all
describe artifacts this tool did not produce. Building them turned up two checks
that reported a pass on output they had not examined, which is the same failure
as the six above and was found the same way — by generating the thing and then
asking the rules what they thought of it.

Everything is built from stored rows. `Page.markdown` is a column and was
persisted the whole time; the sections were computed for llms.txt and discarded
afterwards. So the three artifacts cost **no crawl and no model call**, and are
available for a run that finished weeks ago.

#### `info_render` was written, tested, and imported by nothing

`app/core/info_render.py` is a complete XSS-safe HTML renderer carrying a
source-level test that asserts no f-string in it interpolates into markup.
Nothing imported it but its own test file. It is now what builds `ai-info.html`.

Third instance of this shape in two days: `copyrules.locale_conflicts` (written,
tested, never called), the deleted `repo` functions, and now this one. The
pattern is worth naming: a module with tests looks finished on every measure
this project has, including coverage.

#### Two checks that reported a pass on something they had not read

- **OKF-005** matched `\b\d{4}-\d{2}-\d{2}\b`. There is no word boundary between
  the `9` and the `T` of `2026-09-09T00:00:00+00:00` — the form `log.md` writes —
  so it matched a bare date, missed every ISO timestamp, and reported a correctly
  dated log as undated. Same shape as the `\bprogram` bug in `c378139`, found the
  same day.
- **HDR-001** scored **100** on a `_headers` advertising
  `rel="alternate"; type="text/markdown"` with no `md/` directory generated. Its
  path map cannot match a per-page alternate, and matching the path was never the
  point: what that rel asserts is that the directory exists. This is the rule
  whose own docstring says the generator refusing to lie "is one defence, and it
  does not survive a human editing the file afterwards."

#### Cloudflare cannot express the rel that v2 is mostly about

`_headers` matches path placeholders but performs no interpolation into header
**values**, so no rule maps `/about/` to `</about/index.md>`. The alternatives
were 419 literal blocks — past Cloudflare's file cap, which drops the tail
silently — or claiming the rel and not delivering it. So the homepage rule is
emitted because that one is expressible, and the general rule is written into the
file as an edge-config instruction.

Second time the honest answer for this component has been an instruction rather
than a file, which is what `templated=True` already meant.

#### A directory broke three things that measured a body

`Artifact` gained `files`, and two artifacts carry it instead of `body`:

- `client_report` sized a directory by `len(artifact.body)` and reported `md/` as
  **0 bytes**, then generated the step *"Upload it to your site so it answers at
  https://site/"* — for 419 files.
- `delivery` measured the same empty body.
- `_size` counted **characters** and labelled them **bytes**. Understates by a
  third on any page carrying an em-dash.

All three now read `Artifact.size`, which encodes UTF-8.

#### What the UI says that the score does not

A directory's LIVE chip is settled by fetching **one** URL —
`curl <site>/<some-page>.md` is the whole probe — so an emerald "Published" on
`md/` asserts every other file in it. The component card now says so under the
chip. The readiness score still counts that one-URL probe as a full pass: the UI
says what was measured and the number does not. Open.

**Measured:** 1,374 tests (was 1,337), 1 xfailed, ruff check and format clean.
Components 26 → 29. Rule sets 6 → 9 (MD-001..005, INF-001..007, OKF-001..005).

Deployed `063ba47` to web and worker, 2026-09-09 13:59 AEST.

---

## 2026-09-09

### Six ways the tool read data that was there, found by finishing one run

The redspot.com.au run had been `pending` since 25 August. `POST /runs/{id}/start`
existed and was deployed; nobody had pressed it. Starting it and following it to a
delivered file turned up six defects, none of which the test suite could have
found, because each is a case of an external source answering in a shape the
reader did not expect and the reader reporting a confident wrong answer instead of
no answer.

The run is the finding. Everything below was measured on it.

#### `site:` no longer returns an index estimate, and the tool acted on one

Measured 2026-09-09 against DataForSEO, `serp/google/organic/live/advanced`:

| query | `se_results_count` | organic items |
|---|---|---|
| `site:amazon.com` | 25 | 10 |
| `site:nytimes.com` | 25 | 10 |
| `site:reddit.com` | 26 | 10 |
| `site:github.com` | 10 | 10 |
| `site:wikipedia.org` | 10 | 10 |
| `site:redspot.com.au` | 10 | 10 |

Raising `depth` from 10 to 100 moved none of them. Those sites do not share an
index size; the field tracks page one of the SERP. Google has stopped publishing
an "About N results" line for `site:` queries and DataForSEO passes through what
is there.

Read as a count it was worse than useless because it is *small*. `assess`
compares it against the sitemap and warns that "most of what is published is not
being kept ... **cap the crawl and lean on the exclude rules**" for any site over
fifty pages. redspot carried that instruction to its review gate on a figure of
10, against a 442-URL sitemap that is fine. An operator acting on it would have
excluded real pages on the strength of a number that describes nothing.

`_describes_only_page_one` refuses a count at or below the organic results in the
same response. The test is the SERP's own shape rather than a tuned floor: an
estimate of a whole index cannot be smaller than the results on the page
reporting it. `SMALLEST_CREDIBLE_ESTIMATE = 30` covers the gap above that and
below 100, where the first tier starts, so no credible estimate is discarded.
Refusing is the conservative direction — `best_count` takes the larger of the two
counts, so a missing one cannot shrink a crawl.

The nrma.com.au runs of 23–24 August recorded 924 and 943. Whatever changed,
changed after that.

#### A gateway can accept `response_format` and drop it

OpenAI's key was out of credit (`insufficient_quota`), so the run went through the
Prosperity OmniRoute gateway. `strict` json_schema is an OpenAI feature and an
OpenAI-*compatible* endpoint is free to accept the parameter and ignore it. That
is not a refusal anyone can see: the request succeeds, the usage is billed, and
the reply is whatever the model felt like returning.

Three shapes observed from the same model on the same day:

1. Schema-correct JSON inside a ```json fence — `kr/claude-sonnet-4.5`,
   `kr/glm-5`, `kr/deepseek-3.2`, `kr/qwen3-coder-next`, all four.
2. An empty string.
3. Four paragraphs of English opening `**Classification: hub**`, for the
   group-intent prompt.

One preflight recorded a plan built from a real model answer beside an intent
stage that had fallen back, from two calls a second apart.

`_unfence` handles (1) and `_system_for` handles (2) and (3) by sending the schema
in the system prompt whenever `OPENAI_BASE_URL` is set. This module's rule is that
the request carries the schema and the prompt carries context; where nothing
carries it, the prompt is the better of the two remaining options. The OpenAI path
is byte-identical to before.

Two error messages were also lying. An empty body reported as "invalid JSON:
Expecting value: line 1 column 1 (char 0)", which describes a parse failure that
did not happen and is why this took a reproduction to find. A prose body reported
as "invalid JSON" and hid the diagnosis; it now quotes its first eighty
characters, which *is* the diagnosis.

#### An exclude rule excluded nothing

`select_urls` asked each *template* whether it was included and collected the URLs
of the ones that were. A URL matches by shape, so it belongs to every template it
fits — `/customer-service/feedback/` is a member of both `/{slug}/{slug}` and
`/{slug}/feedback` — and an exclude rule therefore only ever declined to add its
own list. It never removed what a broader include had already added.

The planner wrote twelve exclude rules for redspot: careers sub-pages, a damage
report form, a feedback form, a sponsorship page. Each named a real cluster, with
a reason, at the review gate. **All twelve URLs were selected for the crawl.**

This is the control the gate is built around. `core/metrics.py` opens "curation is
happening by truncation" and `render_planning_table` says "one line here can
exclude four thousand URLs before anything is fetched". Neither was true. On
CarsGuide's 11,909 URLs it is also the bill.

`_excluded` now decides per URL. Precedence is specificity — literal segments,
then length — not rule order or priority: the planner emits general and specific
rules together and neither position says which was meant to win, while the rule
naming a URL most precisely is self-evidently the one written about it. So a
specific exclude beats a broad include *and* a specific include survives a broad
exclude. A tie resolves to exclude, the recoverable direction: a page wrongly left
out shows as a gap at the review gate; a page wrongly fetched has been paid for
and may already be in the client's file.

**Measured:** 442 sitemap URLs, 430 fetched. Twelve fewer, exactly as written.

#### `must_appear` was absolute everywhere except the crawl

The onboarding form calls it *"Absolute. Joins the identity set, which no traffic
rule can exclude."* That was true of the group verdict in `_apply_overrides` and
of nothing else. `select_urls` never saw the brief, so a named URL could be
excluded by a template rule or fall past `ordered[:page_cap]` — and a page that is
not fetched cannot appear in any file assembled after it.

redspot named 173 URLs. 171 are in the sitemap, and at the size-derived cap of 400
over 442, `/vehicles/van-hire/tradies/` fell off the end of the truncation.
Nothing reported it, because from the crawl's point of view nothing had gone
wrong.

Named URLs are promoted rather than appended. Appending would change which page is
dropped for each one added, trading an operator's explicit choice against the
planner's ordering at the boundary without saying so. Matched against the recon
inventory, so the two named URLs no sitemap lists add nothing — `must_appear` is a
claim about priority, not a licence to fetch a URL discovery never found.

#### A rate limit is a request to wait

Four summarise batches ended at `[kiro/claude-sonnet-4.5] [429]: Too many
requests, please wait before trying again. (reset after 5s)`. There was no retry,
so **a hundred of the file's 419 link lines took URL-slug descriptions while the
provider was saying how long to wait**. `319 of 419 link lines written by model`
is the number that recorded it.

Three attempts, six then twelve seconds. Bounded rather than exponential because
the worker runs one job at a time and a stage that never gives up blocks the queue
behind it. `insufficient_quota` and `credit balance` are excluded deliberately: a
spent account also answers 429, and retrying that spends the same failure three
times before the fallback the run actually needs.

**Measured after:** 419 of 419.

#### Two headings, one list; and one blockquote too many

`agents.md` printed `read_only_urls` under both "Search and listings" and
"Read-only browsing", because both headings render the same list and it is the
only one of its kind on the document. Thirteen links, byte-identical, twice.
3,876 bytes → 2,632. The second heading now declines through
`_section_available`, so it lands in `omitted` with a reason rather than
vanishing.

`llms.txt` shipped `> > Australian car rental company`. The spec allows one
blockquote and the renderer supplies it, so a model returning a pre-quoted blurb
produces two. The run's own QA stage reported it — the check caught what the
renderer had no reason to expect.

#### The delivered run

`68dc7418`, redspot.com.au, complete 2026-09-09.

- 442 sitemap URLs, 430 selected after the twelve excludes, **419 fetched, every
  one on the cheap HTTP tier — zero browser launches.** 11 failed.
- 419 pages in 5 sections. `llms.txt` 77,896 chars; `llms-full.txt` 799,802.
- 29 LLM calls, 430,428 prompt + 46,714 completion tokens, all
  `kr/claude-sonnet-4.5`. **One fallback** — a triage batch whose JSON broke at
  char 5,312, leaving 40 of 419 pages placed heuristically.
- 168 of 173 named URLs are in the file. Of the five absent, two are in no
  sitemap (`/lidcombe-faqs/`, `/locations/lidcombe/`) and three are the pages the
  crawl flagged as JavaScript-only.
- 7 QA issues, and all seven are findings about redspot's own site — duplicate
  vehicle pages under two URLs, a missing rental-policy page — rather than defects
  in the file.

Two things worth carrying forward. `llms-full.txt` stops at 82 of 419 page blocks
because it reaches FULL-009's 800,000-character budget; that is the rule working,
but it means the full-text file covers a fifth of the site and nothing says so on
the run page. And the three JavaScript-only pages are detected, counted and then
dropped, so a `must_appear` URL can be named, found, flagged and still absent —
which is a design question about what the file should contain, not a bug to fix
quietly.

#### A pre-send check, and the four defects it caught in my own handover

The files above were about to be sent. Running the tool's own rule engine over
them first — which nothing did — scored them **79/100 with four failures**, and
three of the four were ours rather than redspot's:

| | | |
|---|---|---|
| XF-001 | `llms-full.txt` had no blockquote | `render_llms_full` was never passed `site_summary`. The rule has failed on every full file this tool has produced. |
| AGT-013 | `agents.md` did not mention `llms.txt` | The pointer only renders when a probe found a *live* file. A site we are about to publish one to never gets it, so the bundle arrives internally inconsistent. |
| IDX-015 | `licence` x22 / `license` x12 | `copyrules.locale_conflicts` was written, correct, tested, and **called by nothing**. `enforce_copy_rules` — whose entire job is "check every link line, regenerate what fails once" — never consulted it, so IDX-015 caught the mixing at audit time, after assembly, when the only move left is to regenerate the run. |
| IDX-013 | "Top End" flagged as a superlative | The northern third of the Northern Territory. redspot has a location page for it. The file was refused over a place name. |

The last two share a cause worth naming on its own: **`copyrules` and
`index_rules` each implemented the same two checks, three files apart, with their
own `LOCALE_PAIRS`.** Both copies were wrong, in different directions. The audit's
spelling check matched `\bprogram` with no closing boundary, so every "programme"
counted as both spellings and a consistently British document reported a conflict
with itself; the generator's superlative check learned about proper nouns and the
audit's did not. Both now call the shared function and the duplicate list is gone.

`app/core/delivery.py` is the check itself, and its value is the sort rather than
the list. Three piles, because the response to each is opposite:

* **Defect** — ours. The file is wrong, not the site. Blocks the send.
* **Finding** — the client's. Duplicate titles, thin pages, three pages that need
  JavaScript. This *is* the deliverable; blocking on it would mean refusing to
  deliver an audit because the audit found something.
* **Limit** — nobody's. A stage that fell back, 11 of 430 pages that did not
  fetch, an unavailable index count. Only has to be said out loud, because a gap a
  client is not told about gets read as one of the first two.

XF-002 turns on disclosure rather than arithmetic. It wants every indexed URL in
the full file; FULL-009 caps that file at 800,000 characters; past a few hundred
pages both cannot hold and the cap should win. `render_llms_full` already writes
the shortfall into its own footer, so a stated truncation is a limit and a silent
one is a defect — then the file quietly claims to be the whole site.

**Measured, after the fixes, on the same bundle:** 0 defects, 4 findings, 6
limits. `llms.txt` 79/100 — the remaining points are redspot's seven duplicate
titles and the size warning. `agents.md` **100/100**. Both files now carry the
same blockquote, and 419 link lines across 5 sections.

The handover page renders the three lists at `/sites/{domain}/handover`.

#### The stranded runs

Both recovered and parked at the review gate, where a person decides:

- **rentalcover.com** — 15,226 sitemap URLs, tier `huge`. The run warns that it is
  too large to crawl exhaustively and that an unbounded crawl here is real money.
  Not approved.
- **westpac.com.au** — 4,568 URLs. Not approved. Note there are now *two* westpac
  runs at the gate, `417eb0ca` from 28 August and `784504a9`; one should be
  cancelled before either is approved.

Neither was blocked, refused or broken. Both had simply never been queued.

**Measured:** 1336 passed, 1 xfailed. ruff clean.

**Deployed:** web and worker, 2026-09-09. `OPENAI_BASE_URL` pointed at OmniRoute
on both, all five `LLM_MODEL_*` on `kr/claude-sonnet-4.5`.

---

## 2026-08-26

### Client share links, an export list, and PDF export

A client can now be sent a private link to one section of their audit, or a PDF
of it. This is the **first surface in the tool that returns client data without a
session**, so most of the work is the reasons it is safe to.

**The constraint that decided the architecture:** there is no ownership model.
No `owner` or `user_id` column exists on any client-scoped table — `require_user`
is the entire authorisation layer, and every signed-in staff member sees every
client. A client is not staff, so the token has to *be* the authority. Neither
the domain nor the section appears in the URL; both come off the row, which makes
"the handler ignores what the request claims" a property of the shape rather than
something a reader has to verify.

**A verified leak decided the rendering.** `site_state.derive` writes
`f"marked done by {marks[key]}"` into `ComponentStatus.detail`, where the value
is `user.email`, and `partials/component.html` renders `detail` unconditionally.
Reusing the staff partial for a client — even behind guards — would have put a
Prosperity Media address in a client's PDF. So the boundary is a set of dataclass
fields (`app/core/client_report.py`), not a set of `{% if %}`: a guard is one
edit from being deleted and fails silently as valid HTML; a field that does not
exist cannot leak.

One predicate closes three leaks at once — `evidence` is carried only where
`probe_decided`. That covers the operator email (a mark is never probe-decided),
a MANUAL check whose `detail` is the *verify command* rather than evidence, and
`derive`'s internal fallback strings.

#### Decisions worth keeping

- **DB-backed opaque token, not `itsdangerous`.** A signed token cannot be
  revoked without a denylist table — so you pay for the table anyway and lose the
  listing, `created_by`, view counts and the delete cascade.
- **SHA-256, not argon2**, though argon2 is already here. Argon2 is salted, so it
  cannot be looked up by index: one page view would become O(live links) × 50ms
  of KDF. Its cost parameter defends low-entropy human secrets, and there is
  nothing to brute-force in 256 bits. The hash defends against *disclosure* — a
  dump, a replica, a `SELECT *` in Slack.
- **`ShareScope` strips the Cookie header** rather than clearing the session. The
  obvious `request.session.clear()` is a bug: Starlette emits a delete-cookie
  when a session was non-empty in and empty out, so a staff member checking their
  own link would be signed out.
- **One 404 for every failure** — unknown, expired, revoked, deleted, never
  probed. Not 410: "Gone" confirms it was real.
- **`Disallow: /`, never `/share/`** — a disallow line publishes the surface it
  names.
- **The throttle buys no guessing resistance and says so.** It bounds render CPU.
  Keyed on the token, never the IP: `forwarded_allow_ips="*"` makes
  `X-Forwarded-For` client-controlled, so an IP limiter is evadable *and*
  weaponisable to lock a client out.
- **No IP, User-Agent or Referer recorded.** Personal information about someone
  with no relationship to us, and wrong anyway — mail scanners fetch every URL in
  an email, so the log would read "the client opened it" when it was a datacentre
  in Virginia.

#### Four artifacts were being generated and thrown away

Reported by looking for `agents.md` and not finding it. `build_bundle` gated
`agents.md`, `ai-catalog.json`, `robots.txt` and `_headers` on the operator's
chosen *goal* — so each was generated, stored, and dropped from the bundle for
six of the seven goals. The rule that fixes it had been written that morning on
`llms-full.txt`, with the reasoning spelled out — *a file we produced and hid is
worse than one we never made* — and applied to exactly one file. It now applies
to all of them.

One correction fell out of it: `_headers` advertised the catalog on
`bool(ai_catalog) and "ai-catalog.json" in wanted`, so a run that built a catalog
whose goal did not name it would have emitted headers announcing a file the
bundle withheld — the exact contradiction CAT-001 and HDR-001 exist to catch.

#### The export list, and guides

Every generated file in one table with its size, where to serve it, a download
link, and a tick. **The ticks are derived, not kept**: they read the same
`SiteStatus` the family tabs read, so a ticked row is one where the probe fetched
that URL and the site answered.

A test caught a bug that would have made every row wrong: I matched the published
set on `ComponentStatus.artifact_name`, and `derive` only sets that on a READY
component — the LIVE branch never does, because a file already published is not
one we are handing over. The set would have been permanently empty and every row
would have read "not published yet".

Step-by-step guides sit in a disclosure under each finding, assembled from the
artifact's path, the client's own URL, and **`bundle.tasks` — generated on every
run since it was written and rendered by no page at all**, the third piece of
built-but-unreachable work this tool has turned up. It is where the platform
reasoning lives: a Shopify client is told their store cannot serve response
headers, a WordPress one to use `Header add Link` rather than a plugin.

#### PDF

`page.pdf()` on the Chromium already in the image. In the web process, because
the worker is `concurrency=1` across both queues and there is nowhere to put the
bytes. `page.goto()` over loopback rather than `set_content()`, which leaves an
opaque origin so fonts fail CORS and the PDF renders in a fallback face **with no
error anywhere**.

Three defects the tests caught, two of which a fake render never could:
`page.pdf()` takes no `timeout=` in Playwright 1.62; `sync_playwright` cannot
start its driver inside pytest-asyncio's loop, so a module-level browser check
skipped the only test that exercises Chromium; and fixing *that* by setting
`WindowsProactorEventLoopPolicy` left it set — process-global, so thirteen
unrelated tests failed and the suite went from 30 seconds to 3 minutes.

**Measured:** real render — 9 pages, 207KB. Checked under print-media emulation
rather than assumed: the gunmetal cover keeps its background,
`print-color-adjust` resolves to `exact`, pills and export ticks keep their
colour, guides open.

Two pre-existing CSS defects found by sweeping my own additions: `button:hover`
referenced an undeclared `--pm-gunmetal-lift`, so every gunmetal button lost its
whole background on hover; and a global `th` rule uppercased row headers, so
filenames read `LLMS.TXT` and family names `CRAWL RULES`.

**Measured:** 1017 tests, ruff clean.

**Deployed:** web, 2026-08-26. `SHARE_LINKS_ENABLED=true`.

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

### The switch never reached the worker

Found by running a real crawl end to end rather than by any test. Ticking "Also
build llms-full.txt" produced a run whose artifact came back **`missing`**.

`create_run` wrote the flag as a key inside `run.plan`. Three separate code paths
then do `run.plan = plan.to_dict()`, and `CrawlPlan` has no such field — so
`from_dict` dropped it and `to_dict` never wrote it back. **Approving the crawl
plan, which every crawl does, cleared the operator's choice on the way past.**

It is a column now (`c8f2a91b4d17`). A run option is not part of the crawl plan,
and nothing rewrites a column. The migration recovers the intent of any run whose
flag is still sitting in the blob, so the deploy does not repeat the bug one last
time on its way out.

Two tests aimed at the mechanism, not the symptom. One asserts `CrawlPlan` still
drops the key, so moving the flag back into the plan dict fails immediately. The
other asserts the column is non-null and defaults to false.

This is the same class of defect as the readiness and chat-spend bugs: state put
somewhere another code path owns. The test that existed asserted the pipeline
*read* the flag — it could not see that nothing still *wrote* it.

### End-to-end, on the deployed build

Two real capped crawls of prosperitymedia.com.au through the UI and the app's own
endpoints:

| | before | after |
|---|---|---|
| llms-full.txt | **12/100**, 7 failures | **96/100**, 1 |
| llms.txt | 100/100 | 100/100 |

The remaining failure is FULL-008: two pages under 3,000 characters, the
homepage among them at 2,160. **That is a fact about the site's content, not a
generator defect** — the only ways to clear it are to drop the homepage from the
file or to invent content. Neither is on. It did not fire at all on the full
74-page run; it appears here because the run was capped at 10 pages.

### Also

The run page polled `/progress` every 3s while `awaiting_review`. Nothing moves
during that status — the run is waiting on a person — so a review tab left open
overnight made roughly 28,000 requests returning the same panel. Now 30s in that
state, 3s elsewhere. Not dropped, because a teammate may approve in another tab.

The "Also build" checkbox label sat in two grid cells: a bare text node and a
`<code>` element are two anonymous grid items, so the filename wrapped onto its
own line. One span holds them together.

**Measured:** 906 tests, ruff clean.

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
