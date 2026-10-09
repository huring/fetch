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

## 21. Add prompt/NLP when creating new searches
I want to be able to type into a prompt what i'm looking for, what my requirements are, and by using NLP infer what should go where in the search parameters, to create a new search.

## 24. Show status badge on searches page
When "Run now" is clicked, update the status-badge to a yellow "running" badge while the search is running.

## 25. Delete items when searches are deleted
When i remove a search, delete all the items associated whith that search as well. Keep the pricing info if there is any, incase i add the item later.

