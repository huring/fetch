# Backlog

## 1. Change how manual searches are triggered
Currently i can trigger a manual search per marketplace, that then performs all searches that are connected to that marketplace. I want to change how that works:

I want to be able to manually trigger a specific search (both plain and rated), that would then perform only that specific search on the selected marketplaces. I don't want to trigger the searches from the marketplace tabs

## 3. Setting per search if it should be included in summary, and in what way
I want a setting per search how i should be notified. For some searches it's enough to provide a link to our listing. This should be the default setting for all searches that are not rated, to avoid spamming the slack channel with lots of items 

### Example
- Looking for vinyls that will result in lots of hits, in the summary i only want a link that says "N new items in your <search name>", not every new item in the slack message.
- Looking for a very specific type of amplifier below a certain price, i want to be notified right away when that amp is found with the specified accessories at my price. 

## 4. Known bug: price-watch alerts hardcode SEK
`price_watch.py` extracts a `currency` field from the watched page but never uses it - the Slack alert always says "SEK" regardless of what currency Claude actually read off the page. Fine today since every watched item happens to be in SEK, but wrong the moment a non-SEK item (e.g. a USD Amazon listing) is watched. Small, self-contained fix whenever it comes up.

## 5. Snooze a search or watched item
Pause something for N days/weeks instead of just enable/disable - e.g. "I just bought this, stop looking for 3 months" without losing its config or history. Needs a resume-at date/duration and the scheduler to respect it.

## 6. Liveness/sold-tracking parity for plain searches and watched items
The daily liveness sweep only re-checks rated (Claude-scored) listings today - a plain search's matches and a watched item's URL never get the same "confirmed gone"/stale handling.

## 7. Distinguish "blocked" from "down" in source health alerts
A 403/429 (possible ToS pushback/rate-limiting) looks identical to a timeout right now. Worth a different Slack message - one means "fix the code," the other means "you may have been blocked."

## 8. Export/import searches and watched items as JSON
A backup/restore button in the admin UI, independent of the Docker volume backup - useful before a risky change or a host migration.

## 9. Per-search/per-item digest timing
Right now there's one global DIGEST_TIME. Letting a specific search opt into its own time (or "only weekdays") would pair with the per-search notification-style setting above.

## 10. Richer Slack messages (Block Kit)
A listing's actual photo plus inline buttons ("mute this search", "not interested, don't show again") instead of a plain-text link - act straight from Slack without opening the admin UI.

## 11. Cross-marketplace duplicate detection
A reseller cross-posting the same item to both Blocket and Vinted currently shows up twice, independently scored. Worth a fuzzy title+price match to merge/flag duplicates.

## 12. Price-history dashboard
There's already a `price_history` table recording what sold for how much and how fast, but no UI surfaces it. Would help calibrate `good_price` ranges with real data instead of guesswork.

## 13. "Find this item used" on arbitrary (non-marketplace) sites
The explicitly-deferred half of the watched-items "find used" feature - a general web-search step before falling back to Blocket/Vinted/Rehifi, for items not well covered by the existing marketplaces.

## 14. Admin UI access control
There's currently zero auth on the admin UI. Low risk while it's only reachable inside the network via Portainer, but worth a basic password gate before it's ever exposed more broadly.

## 15. Rename the project to "Fetch"
Decided on "Fetch" as the official name (nginx proxy is now at fetch.home). Rename everywhere: the GitHub repo itself (currently `hifi-agent`), the README title/intro, any code comments or strings that say "hifi-agent" or reference the old name, the Docker/GHCR image name, and the Portainer stack name. GitHub repo renames keep the old URL as a redirect, but the GHCR image name and Portainer stack config need to be updated by hand to match - plan for a brief redeploy, not just a code change.

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

## 21. Add prompt/NLP when creating new searches
I want to be able to type into a prompt what i'm looking for, what my requirements are, and by using NLP infer what should go where in the search parameters, to create a new search.

## 22. Known test-suite flakiness: source-adapter tests can occasionally fail from cross-test interference
Found while adding the Auctionet adapter (2026-10-08): the admin app's background scheduler (test_admin_routes.py) fires real jobs on their own threads, and the production shutdown path is deliberately non-blocking (a slow job shouldn't hold up a container stop) - so a thread can still be mid-fetch when its test function returns. If that straggler reaches the network while an unrelated, later test's `@responses.activate` happens to be active, it can get matched against mocks meant for that other test and break it in a confusing, hard-to-reproduce way (full-suite run shows this roughly 1-in-8 times; every affected test passes reliably in isolation).

Already mitigated (not fully fixed): `no_real_marketplace_fetches` in test_admin_routes.py is module-scoped rather than per-test (closes most of the race), `tests/conftest.py` blocks real socket connections session-wide as a general safety net, and a couple of the most commonly affected tests were hardened to filter `responses.calls` by their own query string instead of assuming call-count/index. A `scheduler.shutdown(wait=True)` was tried to close this properly but caused the suite to hang (looked like a deadlock against APScheduler's own worker threads) and was reverted - worth investigating properly rather than retrying blindly.

The real root cause for the remaining cases: `responses.add()` as used throughout the blocket/vinted/rehifi/auctionet source-adapter tests matches by URL only, not query string, so a stray call with a *different* search phrase to the *same* endpoint can silently consume a mock meant for the test's own call. Properly fixing this means adding strict query-string matching (`responses.matchers.query_param_matcher` or equivalent) across all of those tests - a real but separate, bounded piece of work, not something to do as a side effect of another task.
