# Backlog

## 1. Change how manual searches are triggered
Currently i can trigger a manual search per marketplace, that then performs all searches that are connected to that marketplace. I want to change how that works:

I want to be able to manually trigger a specific search (both plain and rated), that would then perform only that specific search on the selected marketplaces. I don't want to trigger the searches from the marketplace tabs

## 2. User interface and UX - DONE (2026-10-08)
We need to make improvements to the user interface, and use a more modern, nice looking style for both the start page, the listings and mainly for the search creation page. We should start by implementing Tailwind css and a theme that we can continue to work with in the future.

### Guidelines:
- Only show what is relevant: don't show form fields that aren't relevant for my current context, so if i'm creating a plain search, i don't need to see the claude/rating specific fields
- Responsive, i would like to be able to use this on my phone most of the time, so all pages needs to be responsive and look good both on my desktop and on my phone
- Use a dark theme, no need for a light theme
- Better use of labels and indicators to make the UI nicer to look at

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

## 16. Overview info/labels on the searches page - DONE (2026-10-08)
Add a summary panel to the searches page: monthly Claude token usage (cost only, not raw token counts - the cost is the interesting number), total scanned ads, total found ads, total summary ads, and top ads (the highest-scoring/most interesting ads across all searches, not just per search).

## 17. A combined feed of all "summary" ads across searches - DONE (2026-10-08)
One page showing every search's "summary"-bucket ads together instead of having to click into each search individually, so I can scan everything in one place. Needs to be filterable: by specific search(es), and a toggle to only show "above threshold" instead.

## 18. Rename the "summary" and "above threshold" buckets - DONE (2026-10-08)
The current bucket names (`found`/`summary`/`threshold`) aren't clear to me at a glance. Decided naming (2026-10-08):
- **Code/internal references**: "daily roundup" (was summary) / "instant alert" (was threshold) - names them by when you're notified, matching `SCORE_DIGEST_MIN`/`SCORE_INSTANT_THRESHOLD` directly so it won't go stale.
- **UI display labels**: "Maybe" (was summary) / "Yes!" (was threshold) - terser and more personality for the admin UI specifically.