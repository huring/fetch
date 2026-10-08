# Secondhand marketplace watcher

Watches secondhand marketplaces for listings matching configurable "searches"
(hifi gear, a pickup truck, bookshelves - anything), scores candidates
against your criteria with Claude, and notifies you on Slack: instantly for
standout finds, once a day for everything else. Blocket, Vinted and Rehifi
are currently registered; more can be added without touching the pipeline,
admin routes, or templates (see "Marketplaces" below). Tradera support
existed early on and was removed rather than left half-wired - it'll come
back as a proper marketplace if/when it's needed.

## How it works

1. **Fetch** - each marketplace polls on its own schedule (see "Marketplaces"
   below), running every enabled search's phrases that's attached to that
   marketplace sequentially, with a short delay between requests.
2. **Dedupe** - seen listings and price history live in SQLite, keyed per
   `(search, source, listing id)`. A price drop on a previously-seen listing
   is treated as new again.
3. **Prefilter** - deterministic, no AI: price bounds (`max_price`/
   `min_price`), a search-phrase relevance check (the title/description must
   mention at least one word from what you actually searched for - catches a
   marketplace's own search being fuzzy, e.g. a plain "onkyo" search once
   returning a t-shirt), excluded model patterns (wildcards), excluded words,
   required keywords. Runs before anything is sent to Claude.
4. **Score** - listings that pass the prefilter are batched (up to
   `SCORING_BATCH_SIZE` per search, default 25) and submitted to Claude
   (`claude-haiku-4-5` by default) via the **Message Batches API** - 50%
   cheaper per token than a normal call, which is the right trade for an
   unattended background tool where nobody's waiting on a response (results
   usually land within minutes, but can take up to 24h - the scheduler picks
   them up on its own, nothing blocks on this). Each request carries that
   search's hard/soft criteria and watched models as a cached system prompt
   (free - and the one place caching meaningfully pays off here, since the
   poll interval is far longer than any cache TTL) and gets back a 1-10
   score, brief reasoning, a price assessment, and any spec it isn't sure
   about rather than guessing.
5. **Notify** - score >= `SCORE_INSTANT_THRESHOLD` (default 8) goes out on
   Slack immediately. Scores between `SCORE_DIGEST_MIN` (default 5) and the
   instant threshold are batched into one Slack message per day at
   `DIGEST_TIME` (default 08:00, local time), grouped by search.
6. **Health** - if a marketplace errors or returns nothing for
   `HEALTH_ALERT_AFTER_N_FAILURES` (default 3) runs in a row, a warning goes
   to Slack. Blocket has no uptime guarantee for this kind of access, so this
   is the early-warning signal that something broke silently.
7. **Liveness sweep** (once daily, 03:30 local) - every listing Claude has
   actually scored gets re-checked directly against its own page, not
   inferred from whether it still turns up in search results (unreliable:
   both marketplaces sort newest-first, so an old-but-still-unsold listing
   naturally falls off the fetched pages well before it's actually gone).
   A listing confirmed gone is recorded into `price_history` (title, final
   price, score, how long it was active) and removed from the active list.
   A listing still live after `STALE_AFTER_DAYS` (21, hardcoded in
   `watcher/liveness.py`) that was ever worth surfacing (score >=
   `SCORE_DIGEST_MIN`) gets a one-time "might be worth a lower offer" Slack
   notice.

## Keeping Claude spend low

A few things work together to keep this cheap enough to run indefinitely
without thinking about it:

- **Narrower input**: the prefilter's search-phrase relevance check and
  `min_price` (see above) mean fewer irrelevant listings ever reach Claude in
  the first place - the single biggest lever, since it cuts both input and
  output tokens together.
- **Shorter output**: the prompt asks for one short sentence of reasoning and
  a few words of price assessment, not an essay - output tokens cost 5x input
  tokens on Haiku, so this matters more than it looks.
- **Fewer, bigger calls**: `SCORING_BATCH_SIZE` (default 25) amortizes the
  fixed per-call overhead (criteria, instructions) over more listings.
- **The Message Batches API** (see "Score" above): 50% off every token - a
  good fit here because nothing in this workload needs a synchronous
  response.
- **Prompt caching** on the stable per-search part of each request - a
  smaller win than usual here specifically, since the poll interval is far
  longer than any cache TTL, so it only pays off within a single scoring
  sweep (a search with more pending listings than one batch holds).

Actual spend (not an estimate) is in the admin UI at `/health` - monthly
input/output tokens and dollar cost, logged per completed scoring batch.

## Searches and the admin UI

Everything you watch for is a **search**: a name, a scope (local/national + a
location), whether shipping should be required, a deterministic prefilter
(min/max price / excluded models / excluded words / required keywords), and a
shared list of search phrases - all stored once on the search itself, not
duplicated per marketplace. A search also picks which registered
marketplace(s) it runs on; each marketplace decides how to use the search's
phrases and other settings (e.g. Blocket uses the phrase as its "q" param and
the location for local-scope searches) rather than storing its own copy of
them.

Every search is either **plain** or **AI-rated** (the "AI-rated" toggle on the
search form; new searches default to plain):

- **Plain** - no Claude call at all. A listing that passes the deterministic
  prefilter is just surfaced: browsable in the admin UI and included in the
  daily digest with its title/price/link, no score or reasoning. Set
  `instant_alert_price` to also get pinged on Slack the moment a match turns
  up at or below that price, instead of waiting for the digest. Good for "I
  know exactly what I want and roughly what it should cost" searches (e.g. a
  specific record you're hunting for).
- **AI-rated** - today's full pipeline: free-text hard and soft criteria,
  watched models (wildcard pattern + note + rough good price, optionally
  flagged as a buy-it-now/ideal target - see below), and Claude scoring each
  match 1-10 with reasoning, via the Batch API (see "Keeping Claude spend low"
  below). Good for "I'm not sure exactly what I want, judge it for me"
  searches.

Searches live in the same SQLite database as everything else and are managed
entirely through the admin UI at `http://<host>:8000/searches` - there are no
`searches.yaml`/`criteria.yaml` files to edit on the host. Add a search,
toggle one off (e.g. once you've found what you needed in "Stugan hifi" and
don't want to see it anymore), or edit its criteria, all from the browser;
changes take effect on the next scheduled run with no restart.

On first boot (empty database), the app seeds the hifi searches this project
was originally built around (`watcher/seed.py`) - review their scope/location
in the UI, since they default to national/no-location.

List fields in the form (excluded models, watched models, search phrases, ...)
are edited as plain text, one entry per line - the format for multi-part
fields (watched models) is shown as a hint under each field.

A watched model can be flagged as a **buy-it-now / ideal target** by writing
`ideal` in its 4th field (`pattern | note | good price | ideal`) - e.g. the
one GPU model you actually want, or the exact AV receiver that'd be a
no-brainer at the right price. If a listing is genuinely that model (or a
clear equivalent) in working condition at or below its good price, Claude
scores it 10/10 (triggering the usual instant Slack alert, no separate
mechanism needed) and judges every other candidate in that search relative
to it. This is still Claude's judgment, not a deterministic price/title
match, so a "wanted" post or a broken unit that happens to mention the model
name won't blindly score 10. Note that the deterministic `max_price` filter
still runs first - set it generously (or leave it blank) if your ideal
target's good-price range sits above what you'd otherwise cap a search at,
or it'll get silently filtered out before Claude ever sees it.

The search list shows three counts per search, each a link to the actual
listings behind it (so you can check what's there without going via Slack).
A plain search only ever populates **Found** - **Summary**/**Above
threshold** are shown as "-" since there's no Claude score to bucket by:

- **Found** - listings that passed the deterministic prefilter (within the
  max-price/excluded-model rules), regardless of Claude's score.
- **Summary** - listings scored in the digest range (`SCORE_DIGEST_MIN` to
  one below the instant threshold).
- **Above threshold** - listings scored at or above `SCORE_INSTANT_THRESHOLD`.

All three only count currently-active listings - one confirmed sold/removed
by the daily liveness sweep (see "How it works" above) disappears from every
bucket and from the database, not just archived quietly.

## Marketplaces

A marketplace (`watcher/marketplaces.py`) is a code-level registration: its
fetch logic (how it turns one of a search's phrases into listings) and,
optionally, auth fields it needs. Adding a new one means writing a source
adapter plus one `register(...)` call - nothing in the pipeline, admin routes,
or templates needs to change.

What's *not* code is per-deployment and lives in the admin UI at
`http://<host>:8000/marketplaces`, one row per registered marketplace:

- **Poll interval** - how often that marketplace's searches are fetched.
  Each marketplace runs on its own independent schedule; a lightweight
  scheduler tick (every 5 minutes) checks each one's config fresh from the
  database and only runs a cycle once its own interval has elapsed. Changing
  the interval here takes effect on the next tick - no restart needed.
- **Request delay** - pause between individual requests to that marketplace.
- **Auth** (if the marketplace needs any - Blocket doesn't) - e.g. an API
  key. Secret fields are never echoed back in the form; leaving one blank on
  save keeps the current value rather than clearing it.

This replaced a single global `POLL_INTERVAL_MINUTES` env var - if you set
that previously, it no longer has any effect; set the interval per
marketplace at `/marketplaces` instead.

Each marketplace also has a **Run now** button there, for testing without
waiting for its next scheduled tick - it runs on the scheduler's own
background thread (same as a normal scheduled cycle), so clicking it doesn't
block the page. Results show up in `/health` and the search list shortly
after. The `/health` page has a matching **Clear listings & run history**
action for wiping accumulated data back to a clean slate (searches and
marketplace settings aren't touched) - handy after a change to what gets
fetched or how it's scored, to confirm the new behavior from scratch rather
than mixed in with old results.

## Deploying via Portainer

**Build pipeline: GitHub Actions -> GHCR -> Portainer pulls.** Portainer CE
cannot build an image from a Dockerfile in a Git-based stack (confirmed CE
limitation, not configuration) and has no stack webhooks in the free tier
(Business Edition only). So:

1. `.github/workflows/build.yml` builds the image on every push to `main` and
   pushes it to `ghcr.io/<you>/hifi-agent:latest`.
2. In Portainer, create a **Git-based stack** pointing at this repo's
   `docker-compose.yml`, with **Polling** auto-update enabled at whatever
   interval you're comfortable with (e.g. every few minutes). Portainer pulls
   the compose file from Git and the image from GHCR - it never builds
   anything itself.
3. Set the secrets (`ANTHROPIC_API_KEY`, `SLACK_WEBHOOK_URL`) as environment
   variables on the Portainer stack, not in the repo. `.env.example`
   documents every variable; `.env` itself is gitignored. (Marketplace-level
   secrets, if a marketplace ever needs one, are set separately in the admin
   UI at `/marketplaces` - see above - not here.)
4. If `ghcr.io/<you>/hifi-agent` is a private package, add it as a Custom
   Registry in Portainer (Registries -> Add registry) with a GitHub PAT that
   has `read:packages`, so the stack can pull it. Making the package public
   avoids this step entirely and is reasonable here since the image contains
   no secrets (those are all runtime env vars).

Edit `docker-compose.yml`'s image line to match your own GHCR path before
first deploy.

### Updating the image

Just push to `main` - the Actions workflow rebuilds and pushes `:latest`, and
Portainer's polling picks it up on its own schedule. No manual redeploy step.

### Backing up `/data`

`/data/watcher.db` (SQLite, in the `watcher_data` named volume) holds
everything: searches, listing history, run/cost logs. Back it up like any
other named volume, e.g.:

```sh
docker run --rm -v watcher_data:/data -v "$PWD":/backup alpine \
  tar czf /backup/watcher-data-backup.tar.gz -C /data .
```

## Known uncertainties (read before relying on this)

- **Blocket**: hits the same unauthenticated JSON endpoint the Blocket
  website itself uses (confirmed working via live test during development).
  Blocket's `robots.txt` explicitly prohibits automated access without
  written permission - this project uses it anyway for personal, low-frequency
  (one request per search every ~20 min) monitoring. That's a real ToS
  conflict, not just a gray area; it's a deliberate choice made when this
  project was scoped, not something this code decides for you.
- **Blocket response field names**: confirmed live during development
  (2026-10) - search results use a top-level `docs` key, `price.amount`, and
  an epoch-millisecond `timestamp` field, all handled in `_parse_ad`. If
  Blocket changes its response shape again, that function is the one place
  to fix it; `--dry-run` and check the logs if anything looks wrong.
- **Blocket has no description text in search results at all** - only title,
  price, location and url. For listings that pass the deterministic
  prefilter on title/price alone, `blocket.fetch_ad_description` fetches the
  ad's own detail page and extracts a short description from its embedded
  schema.org JSON-LD block (confirmed working live, e.g. "Marantz SR5010
  AV-surroundreceiver i svart..."). This is a **truncated snippet
  (~150 chars), not the full ad body** - Blocket renders the complete
  description client-side via an API this project couldn't locate without a
  browser. Prefilter re-runs against the enriched text, so an excluded word
  that only appears in the full body (not the title) can still reject a
  listing before it reaches Claude.
- **"Wanted" posts mix into Blocket search results** (e.g. a title prefixed
  "**Sökes**" - someone looking to buy, not sell). Blocket's `trade_type`
  field doesn't distinguish these reliably (it reports "Säljes" even on a
  wanted post), so this is handled with a standing instruction in Claude's
  scoring prompt rather than a prefilter rule, since reading "Sökes" in
  context is something Claude does reliably and a keyword filter would not.
- **Vinted**: its own JSON API now sits behind a bootstrapped bearer token and
  Cloudflare - but the plain catalog search *page* (the one a browser loads)
  embeds the same item data as server-rendered JSON in a
  `self.__next_f.push(...)` script tag, with no token or cookie needed at all
  (confirmed live, 2026-10 - `watcher/sources/vinted.py` parses that embedded
  JSON rather than calling the token-gated API). Same ToS situation as
  Blocket: personal, low-frequency use of a public page, not a sanctioned API.
  If Vinted changes its page structure, `_extract_catalog_items` is the one
  place to fix - `--dry-run` and check the logs if results stop coming back.
- **Vinted has no location/city filter and is shipping-only** - unlike
  Blocket, a search's `scope`/`location` has no effect on Vinted results, and
  every Vinted listing is marked `ships=True` rather than left uncertain.
- **Vinted's search results carry no description text**, same as Blocket -
  `vinted.fetch_item_description` enriches from the item's detail page via
  the same embedded JSON-LD mechanism Blocket uses, but unlike Blocket's
  truncated ~150-char snippet, Vinted's is the **full, untruncated
  description** (confirmed live). That page's JSON-LD also contains a price,
  but in the seller's own listing currency (not SEK) - the enrichment only
  ever reads the description field, never the price, to avoid silently
  mixing currencies.
- **Rehifi is a single online store, not a classifieds marketplace** - used/
  refurbished hifi gear, one seller, ships nationally. It has no search
  concept of "location". Its own search endpoint is explicitly disallowed by
  `robots.txt` (confirmed live, 2026-10), so `watcher/sources/rehifi.py`
  doesn't use it - instead it follows the sitemap robots.txt itself points
  crawlers at (`/product/*` and the sitemap are both allowed), matching a
  search phrase against product URL slugs (the product name is in the URL)
  before fetching only the matching product pages for price/stock/
  description, read from the page's own schema.org JSON-LD and description
  markup. The ~28k-URL product sitemap (current stock and years of
  sold/archived history alike) is cached in-process for 24h rather than
  re-crawled every poll cycle.
- **Every Rehifi listing gets a standing note in Claude's scoring prompt**
  that it includes 3 months warranty, 30-day exchange and 10-day right of
  return (confirmed live in the site's own product-page copy), so a good
  Rehifi deal should score higher / read as a better deal than an
  equivalent-price private listing with no such protection. This uses a
  generic per-marketplace `scoring_note` mechanism (`marketplaces.py`) rather
  than a Rehifi-specific code path - Blocket's "wanted post" instruction
  (above) now uses the same mechanism instead of being hardcoded into every
  prompt regardless of marketplace.
- **A sold Rehifi item's page stays up** (moved to an internal "archive"
  category) rather than 404ing like a removed Blocket/Vinted listing - so its
  liveness is read from the JSON-LD offer's `availability` field
  (`InStock`/`OutOfStock`), not from the page merely existing.

## Local development

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

Run a single cycle locally:

```sh
export ANTHROPIC_API_KEY=sk-...
export DB_PATH=./watcher-dev.db
python -m watcher.main --once --dry-run
```

Run the full service (admin UI + scheduler) locally:

```sh
python -m watcher.main
# admin UI at http://localhost:8000/searches
```

## Environment variables

See `.env.example` for the full list with defaults. `ANTHROPIC_API_KEY` and
`SLACK_WEBHOOK_URL` have no defaults and must be set for real runs.
Per-marketplace settings (poll interval, request delay, auth) are not env
vars - see "Marketplaces" above.
