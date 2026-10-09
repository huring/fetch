# Backlog

## 5. Snooze a search or watched item
Pause something for N days/weeks instead of just enable/disable - e.g. "I just bought this, stop looking for 3 months" without losing its config or history. Needs a resume-at date/duration and the scheduler to respect it.

## 7. Distinguish "blocked" from "down" in source health alerts
A 403/429 (possible ToS pushback/rate-limiting) looks identical to a timeout right now. Worth a different Slack message - one means "fix the code," the other means "you may have been blocked."

## 8. Export/import searches and watched items as JSON
A backup/restore button in the admin UI, independent of the Docker volume backup - useful before a risky change or a host migration.

## 9. Per-search/per-item digest timing
Right now there's one global DIGEST_TIME. Letting a specific search opt into its own time (or "only weekdays") would pair with the per-search notification-style setting above.

## 10. Richer Slack messages (Block Kit)
A listing's actual photo plus inline buttons ("mute this search", "not interested, don't show again") instead of a plain-text link - act straight from Slack without opening the admin UI.

## 11. Cross-marketplace duplicate detection - low
A reseller cross-posting the same item to both Blocket and Vinted currently shows up twice, independently scored. Worth a fuzzy title+price match to merge/flag duplicates.

## 12. Price-history dashboard
There's already a `price_history` table recording what sold for how much and how fast, but no UI surfaces it. Would help calibrate `good_price` ranges with real data instead of guesswork.

## 13. "Find this item used" on arbitrary (non-marketplace) sites
The explicitly-deferred half of the watched-items "find used" feature - a general web-search step before falling back to Blocket/Vinted/Rehifi, for items not well covered by the existing marketplaces.

## 14. Admin UI access control
There's currently zero auth on the admin UI. Low risk while it's only reachable inside the network via Portainer, but worth a basic password gate before it's ever exposed more broadly.

## 20. New marketplace: Luleå Auktionsverk
https://www.luleaauktionsverk.se/

Research done (2026-10-08, alongside the Auctionet adapter - see CHANGELOG):
robots.txt is permissive (only `/action-handler` disallowed, two sitemaps
given). It's a standalone Next.js site - object detail pages embed full
JSON-LD (title/price/images/description) via the same `self.__next_f.push`
flight-data mechanism `vinted.py` already parses, reusable as-is. But there's
no keyword-searchable endpoint: the public "ongoing items" listing page
fetches results client-side, and the bundle hints at a signed Algolia search
key minted by their own backend (`/api/admin/search-key`) - chasing that
felt like real reverse-engineering, a step beyond "read what's already
public." Better alternative found: `/objekt/sitemap.xml` lists every item
with a `lastmod` date (confirmed ~70-300 touched/day against a ~10k-item
catalog) - an incremental crawl (fetch only items with lastmod newer than
the last check, run those through the same title-relevance prefilter every
other marketplace uses) avoids the API-key problem entirely.

**Also register this as `is_auction=True`** (see marketplaces.py/models.py -
the auction-marketplace infra built for Auctionet applies here too).

**Important wrinkle Lars flagged**: this auction house only runs auctions
monthly, so there often won't be any ongoing items at all between auctions -
the adapter/polling needs to handle "nothing live right now" as a normal,
expected state, not a failure (e.g. not tripping the source-health
"N failures in a row" alert just because a month's auction hasn't started).

## 24. Show status badge on searches page
When "Run now" is clicked, update the status-badge to a yellow "running" badge while the search is running.

## 25. Delete items when searches are deleted
When i remove a search, delete all the items associated whith that search as well. Keep the pricing info if there is any, incase i add the item later.

## 26. UI when using wizard is really laggy
The form for typing the prompt to create a search is really laggy and unresponsive.

## 29. Fix Vinted via FlareSolverr
Vinted now serves a real Cloudflare "managed challenge" (JS computational
challenge) for every `/catalog` request - confirmed live (2026-10) via a
direct curl: `403`, `cf-mitigated: challenge` header, and a "Please wait...
enable JavaScript and cookies" interstitial instead of the server-rendered
page `sources/vinted.py` parses. A plain `requests.get` can never pass this
(no JS execution, no way to produce the `cf_clearance` cookie), so every
Vinted fetch/count currently 403s regardless of search phrase. Dropped to
`enabled_by_default=False` in `marketplaces.py` in the meantime - still
fully wired up and selectable per-search via Advanced edit, just excluded
from what a brand-new search starts with.

Planned fix: add [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr)
(a small, widely-used proxy purpose-built for solving Cloudflare challenges
with a real headless browser under the hood, returning the solved page +
cookies over a simple JSON API) as a second container, rather than bundling
a headless browser into this app's own image.

Steps:
1. Add a `flaresolverr/flaresolverr` service to `docker-compose.yml` (no
   persistent volume needed; exposes its API on e.g. `:8191` internally,
   not published externally).
2. In `sources/vinted.py`, replace the direct `get_text(SEARCH_URL, ...)`
   calls (in `fetch`, `count`, `fetch_item_description`, `check_active`)
   with a POST to FlareSolverr's `/v1` endpoint
   (`{"cmd": "request.get", "url": ..., "maxTimeout": 60000}`), parsing the
   target page's HTML out of the JSON response's `solution.response`.
   FlareSolverr's own `cmd: "sessions.create"`/`sessions.destroy` lets a
   solved challenge's cookies be reused across the many calls one fetch
   cycle makes, instead of re-solving per request - worth doing given how
   many search phrases/pages run per cycle.
3. Add a `FLARESOLVERR_URL` setting (e.g.
   `http://flaresolverr:8191/v1`, matching the compose service name) to
   `settings.py`, threaded down to `sources/vinted.py` the same way other
   per-deployment config already is.
4. Handle FlareSolverr itself being unreachable/slow the same way any other
   source failure is handled today (`SourceError`, logged, cycle continues)
   - it's an extra moving part that can itself go down.
5. Once confirmed working live, flip `enabled_by_default=True` back on for
   Vinted in `marketplaces.py` and remove this note's caveat from its
   registration comment.