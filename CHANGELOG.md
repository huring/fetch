# Changelog

Completed backlog stories move here (see `backlog.md` and `CLAUDE.md` for the
workflow). Newest first.

## 2026-10-09
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
