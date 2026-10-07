# Secondhand marketplace watcher

Watches Blocket and Tradera for listings matching configurable "searches"
(hifi gear, a pickup truck, bookshelves - anything), scores candidates
against your criteria with Claude, and notifies you on Slack: instantly for
standout finds, once a day for everything else.

## How it works

1. **Fetch** - every `POLL_INTERVAL_MINUTES` (default 20), runs every enabled
   search's Blocket/Tradera queries sequentially, with a short delay between
   requests.
2. **Dedupe** - seen listings and price history live in SQLite, keyed per
   `(search, source, listing id)`. A price drop on a previously-seen listing
   is treated as new again.
3. **Prefilter** - deterministic, no AI: max price, excluded model patterns
   (wildcards), excluded words, required keywords. Runs before anything is
   sent to Claude.
4. **Score** - listings that pass the prefilter are batched to Claude
   (`claude-haiku-4-5` by default) along with that search's hard/soft
   criteria and watched models. Claude returns a 1-10 score, reasoning, a
   price assessment, and flags any spec it isn't sure about rather than
   guessing.
5. **Notify** - score >= `SCORE_INSTANT_THRESHOLD` (default 8) goes out on
   Slack immediately. Scores between `SCORE_DIGEST_MIN` (default 5) and the
   instant threshold are batched into one Slack message per day at
   `DIGEST_TIME` (default 08:00, local time), grouped by search.
6. **Health** - if a source errors or returns nothing for
   `HEALTH_ALERT_AFTER_N_FAILURES` (default 3) runs in a row, a warning goes
   to Slack. Blocket and Tradera have no uptime guarantees for this kind of
   access, so this is the early-warning signal that something broke silently.

## Searches and the admin UI

Everything you watch for is a **search**: a name, a scope (local/national + a
location), whether shipping should be required, a deterministic prefilter
(max price / excluded models / excluded words / required keywords), free-text
hard and soft criteria for Claude's judgment, watched models (wildcard pattern
+ note + rough good price), and the actual Blocket/Tradera search queries to
run.

Searches live in the same SQLite database as everything else and are managed
entirely through the admin UI at `http://<host>:8000/searches` - there are no
`searches.yaml`/`criteria.yaml` files to edit on the host. Add a search,
toggle one off (e.g. once you've found what you needed in "Stugan hifi" and
don't want to see it anymore), or edit its criteria, all from the browser;
changes take effect on the next scheduled run with no restart.

On first boot (empty database), the app seeds the hifi searches this project
was originally built around (`watcher/seed.py`) - review their scope/location
in the UI, since they default to national/no-location.

List fields in the form (excluded models, watched models, queries, ...) are
edited as plain text, one entry per line - the format for multi-part fields
(watched models, queries) is shown as a hint under each field.

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
3. Set the secrets (`ANTHROPIC_API_KEY`, `TRADERA_APP_ID`, `TRADERA_APP_KEY`,
   `SLACK_WEBHOOK_URL`) as environment variables on the Portainer stack, not
   in the repo. `.env.example` documents every variable; `.env` itself is
   gitignored.
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
- **Tradera**: implemented against the officially documented REST v4 API
  (`api.tradera.com`, `X-App-Id`/`X-App-Key` headers), which Tradera
  themselves describe as built for AI-agent integrations. The exact endpoint
  path and query parameter names in `watcher/sources/tradera.py` follow
  community client conventions, not a directly confirmed OpenAPI spec (the
  developer portal is a JS app that couldn't be fully inspected). Register
  your own app at api.tradera.com, then run once with `--dry-run` and check
  the logs before trusting results - `_parse_item`/`_SEARCH_PATH` are the one
  place to adjust field/endpoint names if they don't match.
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

See `.env.example` for the full list with defaults. Secrets
(`ANTHROPIC_API_KEY`, `TRADERA_APP_ID`, `TRADERA_APP_KEY`,
`SLACK_WEBHOOK_URL`) have no defaults and must be set for real runs; sources
without Tradera credentials configured are skipped with a log warning rather
than failing the whole run.
