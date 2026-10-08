# Fetch

A secondhand marketplace watcher: watches for listings matching configurable "searches"
(hifi gear, a pickup truck, bookshelves - anything), scores candidates
against your criteria with Claude, and notifies you on Slack: instantly for
standout finds, once a day for everything else. Blocket, Vinted, Rehifi and
Auctionet are currently registered; more can be added without touching the
pipeline, admin routes, or templates (see "Marketplaces" below). Tradera
support existed early on and was removed rather than left half-wired - it'll
come back as a proper marketplace if/when it's needed.

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
7. **Liveness sweep** (once daily, 03:30 local) - every listing ever
   surfaced to you (Claude-scored *or* a plain search's matches, not just
   rated ones) gets re-checked directly against its own page, not inferred
   from whether it still turns up in search results (unreliable: both
   marketplaces sort newest-first, so an old-but-still-unsold listing
   naturally falls off the fetched pages well before it's actually gone).
   A listing confirmed gone is recorded into `price_history` (title, final
   price, score, how long it was active) and removed from the active list.
   A rated listing still live after `STALE_AFTER_DAYS` (21, hardcoded in
   `watcher/liveness.py`) that was ever worth surfacing (score >=
   `SCORE_DIGEST_MIN`) gets a one-time "might be worth a lower offer" Slack
   notice - a plain listing has no score to compare, so it only ever gets
   the removal check, never this nudge.

Separately from all of the above, **watched items** track one exact product
URL (any site) for a price drop on their own daily/weekly/monthly schedule -
see "Watched items" below.

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

The admin UI is server-rendered Jinja2 (no frontend build step) styled with
Tailwind's CDN script (`<script src="...cdn.tailwindcss.com">`, compiled in
the browser rather than a build-time pipeline - deliberately, to keep this
project's "no Node toolchain" simplicity) and a fixed dark theme (no
light-theme toggle - there isn't one to maintain).

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

Independent of plain/rated, each search also has its own **digest style**:
**itemized** (the default - one Slack line per match, same as always) or
**summary link** (one line - "N new items in &lt;search name&gt;" - instead
of itemizing every match; new searches default to this one). Meant for a
high-volume search where itemizing every match would spam Slack (the
classic case: a broad vinyl/record search with lots of hits where you'd
rather just know *something* new showed up and go look). This only changes
the once-daily digest - an instant alert (`instant_alert_price` for a plain
search, or a high enough score for a rated one) is always itemized, since by
definition it's about one specific standout match. The summary link needs
`PUBLIC_BASE_URL` set (see `.env.example`) to actually be a clickable link
into that search's matches; left unset, it's just plain text (a count, no
link).

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
A plain search only ever populates **Found** - **Maybe**/**Yes!** are shown
as "-" since there's no Claude score to bucket by. These three are called
"found"/"daily_roundup"/"instant_alert" in code (named after *when* you're
notified, matching `SCORE_DIGEST_MIN`/`SCORE_INSTANT_THRESHOLD` directly) -
the admin UI just shows the terser "Found"/"Maybe"/"Yes!" instead:

- **Found** - listings that passed the deterministic prefilter (within the
  max-price/excluded-model rules), regardless of Claude's score.
- **Maybe** - listings scored in the digest range (`SCORE_DIGEST_MIN` to
  one below the instant threshold).
- **Yes!** - listings scored at or above `SCORE_INSTANT_THRESHOLD`.

The searches page also has an overview panel (this month's Claude cost,
total scanned/found/"Maybe" counts, and the current top-scoring ads across
every search) and a **Feed** page (`/feed`) that shows every search's
"Maybe" (or, toggled, "Yes!") listings together on one page instead of
clicking into each search individually - filterable to one specific search.

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

Each **search** (not marketplace) has a **Run now** button on `/searches`,
for testing without waiting for its next scheduled tick - it runs just that
one search, across whichever marketplaces it's attached to, not every search
sharing a marketplace. It runs on the scheduler's own background thread
(same as a normal scheduled cycle), so clicking it doesn't block the page,
and it doesn't touch that marketplace's own poll cadence/health tracking -
the regular scheduled tick keeps running on its own schedule regardless.
Results show up in `/health` and the search list shortly after. The
`/health` page has a matching **Clear listings & run history** action for
wiping accumulated data back to a clean slate (searches and marketplace
settings aren't touched) - handy after a change to what gets fetched or how
it's scored, to confirm the new behavior from scratch rather than mixed in
with old results.

### Auction marketplaces

A marketplace can also be flagged `is_auction=True` (Auctionet is the first
and currently only one) - a live, ascending-bid auction site rather than a
fixed-price classifieds site. Three things work differently for these,
without needing a separate adapter type:

- `price` means the current bid requirement (what a new bidder would need
  to bid right now to lead), not a seller's asking price - it's expected to
  rise before the auction ends.
- Removal is detected from the listing's own `auction_ends_at` deadline
  passing, not by re-fetching the page - cheaper than the normal liveness
  sweep, and it runs on every scheduler tick rather than waiting for the
  once-daily one.
- Claude is told (via the normal per-marketplace `scoring_note` mechanism)
  that a listing is a live auction and given its deadline, so it can factor
  in urgency; a plain-mode auction listing gets the same "ends in Xd/Xh"
  text appended to its Slack message algorithmically instead, since there's
  no Claude reasoning text to carry it there.

This deliberately doesn't re-score a listing as its price climbs toward (or
past) what looked like a good deal at discovery - it's scored once, near its
opening bid, and the live bid/deadline shown in Slack is what surfaces that
staleness to you rather than hiding it.

## Watched items

A **watched item** (`http://<host>:8000/watched-items`) is a different thing
from a search: instead of a phrase matched against a marketplace, it's one
exact product URL - any retailer, not just a registered marketplace - checked
on its own schedule for a price drop. There's no per-site parser: the fetched
page's text is handed to Claude with a structured-output schema to read off
the current price, title, a short description and stock status, so it keeps
working as a site's markup changes instead of a regex scraper quietly
breaking. When present, the page's own schema.org Product JSON-LD block is
quoted into the prompt ahead of the plain text and preferred for price/
availability - most e-commerce platforms emit this for Google's rich-snippet
eligibility regardless of how the visible page itself is rendered, so it's
often the only reliable source of a price on a JS-heavy page whose plain-text
content never shows one at all. This is one plain (non-batch) Claude call per
check, not the Batch API listing scoring uses - at the volume this is meant
for (a handful of items, checked at most daily) the Batch API's 50% discount
isn't worth its submit/collect bookkeeping.

The edit page shows a small "is this the right item?" card once a check has
succeeded at least once: the description and an image, both confirming the
check is reading the item you meant (the image is never downloaded or
stored - just linked to by its original URL, taken from the same JSON-LD
block, or the page's OpenGraph `og:image` tag if there's no JSON-LD).

An item's price is recorded and shown even while it's out of stock (still
useful to know what it's priced at) - only the instant-alert check itself
requires both a price at/below target *and* the item actually being
purchasable right now. A blank "-" specifically means no check has
succeeded yet at all, shown distinctly from "(out of stock)" next to a last
known price, or on its own if no price has ever been found. Stock status
comes from the page's own schema.org JSON-LD `availability` where present,
or Claude's own judgment of the rendered page otherwise.

Some retailers push back on automated requests outright rather than just
being slow or down - confirmed live (2026-10) on a Shopify storefront that
returns a persistent HTTP 429 on every product page. A watched item whose
last check failed with HTTP 403 or 429 shows an immediate "blocked (HTTP
429)"-style badge (distinct from the generic "unreachable" one, which only
appears after `HEALTH_ALERT_AFTER_N_FAILURES` in a row) - there's no clean
fix for an actively-blocking site beyond a paid proxy or a specialized
price-tracking API, but at least it's clear that's what's happening rather
than looking like a bug.

Fields: a name, the URL, an optional **target price** (alert on Slack the
moment the price is at or below it - once alerted, it won't repeat daily at
the same or a higher price, only on a further drop), and a **check
frequency** (daily/weekly/monthly - price drops aren't time-sensitive, so
daily is just the default, not a requirement). A **Check now** button on the
list page triggers an out-of-cycle check the same way a search's own **Run
now** does.

A watched item that fails `HEALTH_ALERT_AFTER_N_FAILURES` checks in a row
(fetch failed, or Claude couldn't extract a product from the page - the same
threshold and one-time-until-it-recovers pattern marketplace health alerts
already use) gets a one-time "this might be dead" Slack notice and an
"unreachable" badge on the list page, rather than silently sitting there
showing a stale price forever. A later successful check clears both.

**Find this item used**, if checked, also searches every registered
marketplace for a used copy once the product's title is known from the
first check - it does this by automatically creating a normal
"plain" search (see "Searches and the admin UI" above) with that title as its
one search phrase and the watched item's own target price as its instant
alert price, so a used (or new, elsewhere) copy at a good price alerts
exactly like the watched item itself does. From then on this is handled
entirely by the existing search/marketplace machinery - no separate code path
to find a used copy. Searching non-marketplace websites for a better price
is a possible future extension, not implemented yet.

Amazon (or any site) scraping this way is best-effort: unlike
Blocket/Vinted/Rehifi, there's no reverse-engineered stable structure to rely
on, and some retailers (Amazon in particular) actively push back on
automated traffic. At one check per item per day this is low-volume enough
to likely hold up, but there's no guarantee, and no clean fix beyond a paid
proxy or a specialized price-tracking API if a particular site starts
blocking it.

## Deploying via Portainer

**Build pipeline: GitHub Actions -> GHCR -> Portainer pulls.** Portainer CE
cannot build an image from a Dockerfile in a Git-based stack (confirmed CE
limitation, not configuration) and has no stack webhooks in the free tier
(Business Edition only). So:

1. `.github/workflows/build.yml` builds the image on every push to `main` and
   pushes it to `ghcr.io/<you>/fetch:latest` - the image name tracks the
   GitHub repo name automatically (the workflow tags it
   `ghcr.io/${{ github.repository }}:latest`), so renaming the repo alone is
   enough to change this, no workflow edit needed.
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
4. If `ghcr.io/<you>/fetch` is a private package, add it as a Custom
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
- **Auctionet** (auctionet.com) aggregates live auctions run by many
  independent Swedish auction houses. Unlike Blocket/Vinted/Rehifi, this is a
  genuine public, unauthenticated JSON API - `GET /api/v2/items` - the same
  endpoint the site's own search page calls (confirmed live, 2026-10);
  robots.txt only disallows `/admin/` and `/*/my`, nothing about search or
  this endpoint. It returns full title/description/condition text already,
  no separate detail-page fetch needed (unlike Blocket/Vinted). See "Auction
  marketplaces" above for how `is_auction=True` changes price/removal/
  scoring semantics for it.

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
