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

## 15. Rename the project to "Fetch" - remaining manual steps
Code-side rename is done (2026-10-09 - see CHANGELOG): README title/intro,
`docker-compose.yml`'s image line, and the admin app's internal title all say
"Fetch" now. What's left is outside what Claude can do from this environment
(no `gh`/GitHub API access here) - on you:

1. Rename the GitHub repo itself: Settings -> General -> Repository name,
   `hifi-agent` -> `fetch`. GitHub keeps the old URL working as a redirect.
2. Update your local clone's remote so it points at the new URL directly
   rather than relying on the redirect: `git remote set-url origin
   https://github.com/huring/fetch.git`.
3. Push (or re-run the Actions workflow) once renamed - it'll build and push
   to `ghcr.io/huring/fetch:latest` automatically (the tag is derived from
   `github.repository`, no workflow edit needed). The old
   `ghcr.io/huring/hifi-agent` package is **not** renamed or redirected - it's
   a separate, now-orphaned package that keeps existing until you delete it
   by hand (GitHub -> your profile -> Packages).
4. The new `fetch` package likely starts **private** by default regardless of
   what visibility the old one had - if you want to skip configuring a
   registry PAT in Portainer (see README step 4), set it to public: the new
   package's own page -> Settings -> Danger Zone -> Change visibility.
5. In Portainer, update the stack's Git repository URL to the new `fetch`
   URL (works via the redirect either way, but cleaner not to depend on it
   long-term), and rename the stack itself if you want its display name to
   match - plan for a brief redeploy once the new image is pullable.

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

## 24. Show status badge on searches page
When "Run now" is clicked, update the status-badge to a yellow "running" badge while the search is running.

## 25. Delete items when searches are deleted
When i remove a search, delete all the items associated whith that search as well. Keep the pricing info if there is any, incase i add the item later.