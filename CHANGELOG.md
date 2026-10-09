# Changelog

Completed backlog stories move here (see `backlog.md` and `CLAUDE.md` for the
workflow). Newest first.

## 2026-10-09
- **Fixed the admin UI's wizard feeling laggy while typing a prompt (was
  backlog #26)** - root cause: `base.html` was loading Tailwind's "Play
  CDN" script (`cdn.tailwindcss.com`), a ~400KB JIT compiler that recomputes
  styles in the browser and keeps a `MutationObserver` watching the whole
  page for as long as it's open - Tailwind's own docs explicitly call this
  unsuitable for production. Every admin page carried that overhead, but
  the wizard's free-text prompt box is the one place in this app where
  you're actually typing continuously, so it's where the cost was most
  noticeable. Replaced it with a plain stylesheet compiled once at Docker
  build time (new `cssbuild` stage using Tailwind's standalone CLI - no
  Node/npm needed in the image - see `tailwind.config.js`), served as a
  static file. No template here used a `dark:` variant, so nothing else
  about the (dark-only) look changes.
- **Vinted is now excluded by default from brand-new searches** - confirmed
  live that every Vinted request (`/catalog` search and item pages) now
  403s behind a genuine Cloudflare "managed challenge" (a JS computational
  challenge our plain HTTP fetches can never pass), so a search that
  includes it currently just logs repeated fetch failures for no benefit.
  Added a `Marketplace.enabled_by_default` flag (true for everything else)
  and a `default_marketplace_keys()` helper, used everywhere a brand-new
  search previously defaulted to "every registered marketplace" - the
  prompt wizard's new-search default and a watched item's linked "find
  used" search. Vinted is still fully registered and can be turned back on
  per-search via Advanced edit; existing searches that already have it
  enabled are untouched. Added backlog #29 with the planned real fix
  (FlareSolverr as a sidecar) for when that's worth doing.
- **Rebuilt the search builder's Claude-calling mechanism after the
  schema-narrowing fix below still didn't resolve "wizard still not
  working" live** - three straight rounds had gone into guessing what
  Anthropic's structured-output mode ("Schema is too complex") was actually
  counting (nesting, then field count), each guess only partially
  confirmed, and a separate change (wrapping the call in a background job
  for the 504 fix) had quietly started replacing every real error -
  including this one, if it was still happening - with a single generic
  "Something went wrong talking to Claude" message, so there was no way
  left to tell what was actually failing without reading container logs by
  hand. Rather than keep guessing: dropped Anthropic's structured-output
  schema (`output_config.format=json_schema`) entirely for this feature.
  Claude is now asked for a bare JSON object directly in the prompt and the
  response text is parsed the same way any other text response in this
  codebase is, with the same validate-and-retry-once behavior as before.
  There's no schema left for the API to reject, so "Schema is too complex"
  is now structurally impossible here, whatever was actually causing it.
  Also collapsed the "decide, then finalize" two-call split back into one
  call per turn - it existed only to work around the schema error, which is
  moot now - and the wizard's error screen shows the real exception message
  instead of a generic one, since this is a single-user admin tool with no
  one to hide an error from. Still not verified against the real Anthropic
  API from this environment (no key available here) - please try again and
  tell me exactly what you see this time, error message included if there
  is one.
- **Narrowed what the search builder asks Claude to generate, after the
  schema-flattening fix still 400'd with the same "Schema is too complex"
  error** - the previous fix removed nesting but kept all ~20 fields, and
  that alone wasn't enough; price_watch.py's near-identical mechanism (same
  model, same synchronous call) already works fine in production with a
  flat ~5-field schema, so field count - not nesting - was the actual
  limit. Cut the draft schema down to exactly what Lars said he actually
  wants AI help with: search phrases (including comparable-model
  suggestions, now encoded as a "[suggested] ..." prefix within the one
  phrase list rather than a separate field, to keep the count down),
  watched models, and hard/soft criteria - 4 fields total, in the same
  range as price_watch.py's proven one. Every other field (scope, location,
  price limits, which marketplaces, digest style) now gets a plain default
  (no restriction, every registered marketplace) instead of being asked of
  Claude, and stays adjustable via the existing Advanced/manual form -
  editing an existing search via its prompt now correctly preserves those
  fields as they currently stand rather than blanking them back to
  defaults, which the previous field-removal would otherwise have silently
  done. Still not verified against the real Anthropic API from this
  environment (no key available here) - please confirm generation actually
  completes this time.
- **Fixed the search builder's generation call failing outright with
  "Schema is too complex"** - reported live right after shipping the 504
  fix below: Claude's API rejected the request with a 400 the moment the
  draft schema (nested arrays-of-objects for phrase provenance and watched
  models) was combined with the "should I ask a question instead" schema in
  one request. Fixed by splitting what was one combined call into two
  smaller ones - a tiny "is there enough to finalize, or what should I ask"
  call, then (once ready) a separate call that produces the actual draft -
  so neither individual request has to represent both outcomes at once.
  Also flattened the draft schema itself: a search phrase's "suggested vs.
  from your prompt" tag is now two plain lists (explicit and suggested, with
  one shared note for why the suggestions are comparable) instead of a list
  of tagged objects, and a watched model is now one line of plain text
  ("pattern | note | good price | ideal") reusing the exact format the
  manual form's own textarea already used, instead of a list of objects -
  removing the nesting that was the actual source of the complexity, not
  just working around the specific combination that failed. Couldn't be
  verified against the real API from this environment (no key available
  here) - confirm live that generating now actually completes.
- **Fixed a 504 Gateway Timeout when generating a search from a prompt** -
  reported live right after shipping the prompt-based search builder: its
  Claude call ran directly inside the HTTP request/response cycle, and a
  slow generation (heavier than the single-field extraction price_watch.py
  does) could outlast whatever timeout the reverse proxy in front of this
  app allows - the proxy then hands the browser its own 504 while the
  Python request keeps running regardless, the response just never reaches
  anyone. Fixed by moving the Claude call onto the scheduler's own
  background thread (the same mechanism "Run now"/"Check now" already use)
  instead of running it inside the request: submitting a prompt (or an
  answer to a clarifying question) now gets an immediate redirect to a
  status page, which polls via a plain meta-refresh (no JS, consistent with
  the rest of this app) until the result is ready - showing a spinner and
  "Claude is working on this..." in the meantime, exactly the visual
  indicator also asked for alongside the fix.
- **Added a prompt-based way to create and edit searches** (was backlog #21)
  - until now, configuring a search (especially a "rated" one with hard/soft
  criteria and watched models) meant hand-filling a form with ~15 fields.
  "+ New search" now opens a prompt box instead: describe what you want in
  plain text (Swedish is fine), and Claude either asks a clarifying question
  (only when genuinely blocked, or to offer something non-trivial the
  prompt didn't address - most commonly, whether to also search for
  comparable models/brands when the prompt names one specific item) or
  proposes a finished search. The proposal shows a plain-language summary,
  and tags each search phrase as either straight from the prompt or a
  Claude-suggested comparable (with a reason) - suggested ones are
  pre-checked but can be unchecked before saving. A "Preview result counts"
  button shows roughly how many raw matches each marketplace currently has
  for this search (not an exact simulation, just a sense of whether the
  pool is tiny or huge, to judge whether to narrow or widen the prompt) -
  Blocket and Auctionet both expose a genuine total-match count in one
  cheap request, Rehifi's is free (a slug match against its own already-
  cached catalog), and Vinted's pagination only ever reveals an exact count
  up to 3 pages, so past that it's shown as a confirmed lower bound rather
  than a guess. A search built this way remembers its prompt (editable
  later via a new "Edit via prompt" option, which regenerates the whole
  search from scratch from the edited text - any manual field tweak made
  outside the prompt won't survive a later regeneration, which the existing
  manual/advanced form now warns about when applicable); the manual form
  itself is unchanged and still reachable as "or build it manually," for
  anyone who prefers it or needs to fix something Claude got wrong without
  fighting the prompt. (Lars also floated signing into Fetch with a
  claude.ai account, both as auth and to use Claude via that subscription
  instead of the existing API key - decided against for now: there's no
  general-purpose "sign in with Claude" flow Fetch could integrate with,
  and a claude.ai subscription doesn't include API access anyway, separate
  billing. Backlog #14, the actual "add admin UI auth" story, stays open
  but low-priority since Fetch only ever runs on Lars's own homelab behind
  his firewall.)
- **Fixed a plain Blocket search dropping most of its real matches, and
  sorted plain-search results by distance** - reported live: a national
  "Förstärkare" search only surfaced 22 ads, far fewer than the thousands
  Blocket itself reports for that term. Root cause: Blocket's own search
  matches a phrase anywhere in an ad (title or body), but its search-results
  endpoint only ever returns the title - no body text - so a genuine match
  identified only by model name (e.g. "Hegel H190", nowhere near the word
  "förstärkare") was being rejected by Fetch's own local relevance check
  before ever getting the enrichment step that fetches the ad's real
  description, which only used to run for listings that already passed.
  Fixed by retrying that one check with the full description fetched first,
  specifically (and only) when the sole reason a listing failed was a
  missing phrase match - a hard reject (price, excluded word/model) still
  skips straight to rejection, so this doesn't add cost there. Separately,
  a plain search's results (which have no AI ranking of their own) are now
  sorted the same way Blocket's own "Closest" filter would, assuming Lars's
  own location (Boden, Norrbotten, configurable via new HOME_LAT/HOME_LON
  settings) - both the fetch itself and the admin UI's display order.
- **Fixed a "local" scope search silently finding nothing** - reported live:
  a Norrbotten-scoped "Ski-doo"/"Ski-doo Summit" search returned zero ads,
  despite the same search on blocket.se itself returning 9. Root cause: the
  client-side location filter compared a search's typed location ("Norrbotten",
  a county) against Blocket's own location field on each listing ("Luleå",
  "Boden", etc - a municipality), with a plain substring match that can never
  succeed between the two - every listing with a known location was silently
  dropped the moment a search's location named a county rather than a city
  (invisible until now since every existing search happened to use a
  location that matched its own capital city by coincidence, e.g.
  "Stockholm"). Fixed two ways: the location filter now also checks Blocket's
  own county/municipality geography (confirmed live, 2026-10 - 21 counties,
  290 municipalities, read straight from Blocket's own search-facet data), so
  a county match works for every marketplace; and Blocket's search endpoint
  turns out to genuinely filter server-side when given its own county facet
  code (e.g. "0.300025" for Norrbotten) - a real fix to backlog #26's
  conclusion that location filtering was entirely broken there, which had
  only tried passing a plain county name (confirmed live, 2026-10, to still
  400 as before - the facet code was the missing piece). Separately
  confirmed working as designed, not a bug: a search's multiple phrase lines
  (one per line, e.g. "Ski-doo" and "Ski-doo Summit") already each run as
  their own independent marketplace search and get merged into one result
  set - no change needed there.
- **Fixed "Top ads" (and the feed's "Maybe"/"Yes!" view) showing a stale
  search's old scores instead of current standouts** - reported live: a
  search that used to be AI-rated and was later switched to plain kept
  surfacing its old high-scored listings in "Top ads", crowding out two
  genuinely current "Yes!" results from searches that are actually rated
  today. Root cause: `update_search` only ever touches a search's own row -
  switching scoring_mode never resets or re-evaluates listings it already
  scored, and `get_top_listings`/`list_feed_listings`'s rated-only buckets
  were filtering purely on `listings.score`, with no check that the
  listing's *search* is still rated. Both now also require
  `searches.scoring_mode = 'rated'`; the "found" bucket is deliberately
  unaffected, since it's meant to include plain-surfaced matches too.
- **Closed the remaining test-suite flakiness gap** (was backlog #28) -
  `responses.add()` as used throughout the blocket/vinted/auctionet
  source-adapter tests matched by URL only, so a stray real request from an
  unrelated test's leftover background thread (now a much rarer event after
  today's other scheduler fix, but not impossible - a handful of tests still
  intentionally trigger one via "Run now"/"Check now") could silently
  consume a mock meant for the test's own call, with a different search
  phrase, and throw off pagination or call-count assertions in a confusing
  way. Every search-endpoint mock in those three files now also matches on
  the exact query string (`responses.matchers.query_param_matcher`) it was
  written for. Rehifi's own tests didn't need this - its adapter crawls a
  sitemap and fetches per-product URLs, never a single endpoint with a
  varying search-phrase query string, so the underlying ambiguity doesn't
  exist there. Verified with 10 consecutive full-suite runs in a fresh
  Python 3.12 container.
- **Fixed the actual cause of test_admin_routes.py's intermittent CI
  failures** (was backlog #22; CHANGELOG also already covers a segfault
  fixed the same day from the same underlying mechanism) - confirmed live
  when a CI run failed an assertion (`test_feed_shows_daily_roundup_by_default`)
  that passed reliably in 10 straight local full-suite reruns, meaning it
  really was the long-documented, timing-dependent cross-test interference,
  not a code regression. Root cause: every one of this file's ~30 `client`
  fixtures started a real BackgroundScheduler whose immediate startup tick
  fires a real (if mocked) marketplace-fetch cycle on its own thread, well
  beyond what a test exercising only HTTP routes has any use for - and
  that thread outlives its own test by design (the production shutdown path
  is deliberately non-blocking, so a slow job never holds up a real
  container stop). `create_app()` now takes a `start_background_jobs` flag;
  the shared `client` fixture passes `False`, so none of those ~30 tests
  spin up a scheduler job at all unless they explicitly click "Run now" or
  "Check now" (a handful still do, intentionally, to test that exact
  behavior). Verified with 15 consecutive full-suite runs in a fresh Python
  3.12 container (matching CI) - zero failures, where a run would
  previously fail roughly 1 in 8. The separate, narrower `responses.add()`
  query-string-matching gap in the source-adapter test files is still open,
  split out as its own backlog #28.
- **Renamed the project to "Fetch", end to end** (was backlog #15) - the
  code-side rename landed earlier; Lars completed the remaining manual
  pieces: the GitHub repo itself is renamed (old `hifi-agent` URL still
  redirects), the local clone's remote points at the new URL directly, and
  CI is confirmed publishing to `ghcr.io/huring/fetch:latest`. The Portainer
  stack's own Git URL is deliberately left pointing at the old (redirecting)
  URL rather than migrated - a conscious call, not an oversight, since the
  redirect works fine for git operations too and re-pointing it would mean
  re-entering every secret as a fresh stack.
- **Reworked "Top ads" into a richer card grid, scoped to the "Yes!" bucket**
  - previously it was a bare title/score/price text list that could include
  "Maybe"-tier listings too if too few "Yes!" ones existed (it just took the
  overall top 5 by score). Now it's strictly the instant_alert bucket, shown
  as cards: a photo (every registered marketplace's own API/page data
  already carries one - Blocket, Vinted, Auctionet and Rehifi all now
  extract it, never downloaded/stored, just linked to), a description
  excerpt, the score, price, location and marketplace name - a standout find
  is recognizable at a glance instead of needing a click-through to see
  anything but a title.
- **Watched items: keep the price even when out of stock, and show a
  distinct "blocked" badge for sites that actively reject scraping** - found
  while debugging two live reports. First: a watched item showed its new
  confirmation card (title/image/description) but no current price, because
  the page's own data said it was genuinely out of stock
  (`availability: OutOfStock` in its schema.org JSON-LD) - the code was
  clearing the price in that case, which isn't actually wanted (the price is
  still useful to know), and looked identical in the UI to an item that had
  never been checked at all either way. Now the price is always recorded
  regardless of stock status - only the instant-alert check itself requires
  both a good price *and* the item being purchasable - and a new `in_stock`
  field (always the latest check's own finding) lets the list and
  confirmation card show "(out of stock)" explicitly. Second: another item
  showed nothing at all, because that retailer (a Shopify storefront)
  returns a persistent HTTP 429 on every product page - confirmed live, not
  a transient rate limit. A watched item's last HTTP failure status is now
  tracked separately, surfacing an immediate "blocked (HTTP 429)" badge
  distinct from the generic "unreachable" one (which only appears after
  `HEALTH_ALERT_AFTER_N_FAILURES` in a row) - there's no fix for a site that
  deliberately blocks automated requests, but it's now clear that's what's
  happening instead of looking like a bug.
- **Fixed test_admin_routes.py's full suite silently taking minutes longer
  than it reports** (was backlog #27) - confirmed live in CI: a run that
  prints "263 passed in ~18s" was actually taking ~8 minutes to finish.
  Every one of this file's ~30 `client` fixtures fires its own immediate
  fetch cycle on a background thread that outlives its own test (by
  design - a slow job shouldn't hold up a real container's shutdown), and
  with a real 2-second sleep between phrases, Python won't let the process
  fully exit until every one of those threads finishes naturally - none of
  which shows up in pytest's own printed duration at all. Fixed by making
  that sleep instant in tests, after first making the one test whose
  assertion depended on the old slow timing (`test_healthz_starting_when_no_runs`)
  immune to it instead, by directly preventing a run from being recorded in
  its own isolated client rather than racing to check before one
  appears. Confirmed fixed under both Python 3.9 (local) and 3.12 (CI's
  version) - real wall-clock time now matches pytest's reported duration.
- **Fixed a CI segfault introduced by the "Fetch" repo rename** (related to
  backlog #15/#22) - re-running the build workflow after renaming the GitHub
  repo crashed the `test` job outright with "Fatal Python error: Segmentation
  fault", not a normal test failure. Root-caused (reproduced reliably in a
  clean Python 3.12 container, matching CI, vs. never seen locally on Python
  3.9) to a real concurrency bug in the admin app's scheduler shutdown: every
  background job (the scheduled fetch tick, digest, liveness sweep, a manual
  "run now") shares one SQLite connection, serialized against each other by
  a lock - but the shutdown path was closing that same connection without
  taking that lock first. If a job was still mid-query at that exact moment,
  two threads touched the same native SQLite connection at once - a C-level
  race, not something Python's GIL alone prevents. Fixed by having shutdown
  try to take the same lock (non-blocking, so a slow job still can't hold up
  a container stop - that's the whole point of the non-blocking shutdown
  this follows) and skip the close if something's still using it; an
  unclosed connection is reclaimed by the OS at process exit regardless, and
  every write already commits through its own `conn.commit()` elsewhere, so
  nothing is lost. Verified with a tight 40-iteration stress script (fetch
  and the inter-phrase sleep both mocked instant - the worst-case timing for
  triggering the race) and three full suite runs in a fresh Python 3.12
  container, all clean. A first attempt also mocked out the real
  inter-phrase sleep in tests to stop background threads from piling up,
  but that surfaced a different, unrelated pre-existing timing assumption in
  `test_healthz_starting_when_no_runs` - reverted that part; left as its own
  backlog item (#27) rather than fixed as a side effect of this one.

## 2026-10-08
- **Code-side rename to "Fetch"** (partial progress on backlog #15) - README
  title/intro, the two example GHCR paths in the Portainer deploy section,
  `docker-compose.yml`'s image line, and the admin app's internal FastAPI
  title now all say "Fetch" instead of "hifi-agent"/"Watcher admin" (the
  admin UI's own pages already did, from earlier work). The actual GitHub
  repo rename, GHCR package visibility, and the Portainer stack's own config
  are manual steps outside what's possible from this environment (no GitHub
  API access here) - left in backlog #15 with the exact remaining steps,
  rather than closed out, since the story isn't fully done yet.
- **Enriched watched items + likely fix for "current price" staying empty**
  (was backlog #23) - the edit page now shows a small "is this the right
  item?" card (a short description and the product's own image, linked to
  its original URL - never downloaded or stored) once a check has succeeded
  at least once, so it's easy to confirm a watched item is tracking the
  product you actually meant. Along the way, found the likely cause of
  "Current price" staying empty for some items: a watched item's page text
  is handed to Claude for extraction, but on a JS-heavy retailer site the
  price is often rendered client-side and never appears in the plain HTML
  response at all. Most e-commerce platforms also emit a schema.org Product
  JSON-LD block for Google's rich-snippet eligibility regardless of how the
  visible page renders - that block (price/availability/image/description)
  is now extracted and quoted into Claude's prompt ahead of the plain text,
  and preferred when the two disagree, which should make price extraction
  far more reliable on sites like this. Confirmed live against a real
  product page (rehifi.se) end to end.
- **Fixed every Blocket search for a "local" scope failing outright** (was
  backlog #26) - confirmed live that Blocket's own search API returns a 400
  for *any* request carrying a `location` parameter (tried as a plain county
  name, a numeric code, and lowercased - all rejected, even paired with an
  otherwise-working query). Since a local-scope search's location match was
  already being re-applied client-side after fetching anyway, the fix is to
  just stop sending that parameter to Blocket at all, rather than try to
  reverse-engineer whatever shape it actually wants.
- **Per-search manual trigger** (was backlog #1) - "Run now" moved from each
  marketplace (which ran every search attached to it) to each search
  instead, covering just that search across whichever marketplaces it's
  attached to. Along the way, found and fixed a real latent bug this
  surfaced: a search attached to more than one marketplace could have
  enrichment run with the *wrong* marketplace's function against a row from
  a different one, since the prefilter pass didn't look up each row's own
  source - now it does, for both the per-search trigger and the regular
  scheduled cycle.
- **Per-search digest style** (was backlog #3) - a search can now be
  "itemized" (the default - one Slack line per match, unchanged) or
  "summary link" (one "N new items in &lt;search name&gt;" line instead,
  linking into the admin UI if `PUBLIC_BASE_URL` is set - new searches
  default to this one). Only changes the once-daily digest; an instant
  alert is always itemized.
- **Fixed watched-item price-watch alerts hardcoding "SEK"** (was backlog
  #4) - now shows whatever currency Claude actually read off the page.
- **Liveness/sold-tracking parity for plain searches and watched items**
  (was backlog #6) - the daily liveness sweep now also re-checks a plain
  search's surfaced matches, not just Claude-scored ones (it just skips the
  score-based "still listed, might be worth a lower offer" nudge, which
  only makes sense for a rated search). A watched item that fails
  `HEALTH_ALERT_AFTER_N_FAILURES` checks in a row now gets a one-time "this
  might be dead" Slack alert and an "unreachable" badge in the admin UI,
  mirroring the existing marketplace source-health pattern, instead of
  silently showing a stale price forever.
- **Auction marketplace support + Auctionet adapter** (was backlog #19) -
  added a new `Marketplace.is_auction` flag/concept (rather than a separate
  adapter type) so a marketplace can be a live ascending-bid auction instead
  of a fixed-price classifieds site: `price` means "current bid
  requirement" and a new `auction_ends_at` field carries the deadline.
  Auction listings are removed the moment their deadline passes - no network
  re-check needed at all (cheaper than the existing Blocket/Vinted/Rehifi
  liveness sweep, which has to re-fetch each page), running on every
  scheduler tick rather than waiting for the once-daily sweep. Claude's
  scoring prompt is told a listing is a live auction (via the existing
  per-marketplace `scoring_note` mechanism) and given each listing's
  deadline so it can factor in urgency; a plain-mode auction listing (no
  Claude involved) gets the same "ends in Xd/Xh" context appended
  algorithmically to its Slack text instead, since there's no reasoning text
  to carry it. Auctionet itself (auctionet.com) uses a confirmed public,
  unauthenticated JSON API - `GET /api/v2/items` - that's the same endpoint
  its own search page calls.
- **UI/UX rework with Tailwind, dark theme** (was backlog #2) - every admin
  page restyled with Tailwind's CDN script, fixed dark theme, responsive
  layout. The plain/rated contextual-field toggle on the search form carries
  over unchanged, just reskinned.
- **Overview panel on the searches page** (was backlog #16) - this month's
  Claude cost, total scanned/found/"Maybe" counts, and the current
  top-scored ads across every search.
- **Combined feed page** (was backlog #17) - `/feed` shows every search's
  "Maybe" (or, toggled, "Yes!") listings together on one page, filterable to
  one specific search.
- **Renamed listing buckets** (was backlog #18) - "summary"/"above
  threshold" are now "daily_roundup"/"instant_alert" in code (named after
  *when* you're notified, matching `SCORE_DIGEST_MIN`/`SCORE_INSTANT_THRESHOLD`
  directly) and "Maybe"/"Yes!" in the UI.
