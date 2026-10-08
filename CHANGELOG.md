# Changelog

Completed backlog stories move here (see `backlog.md` and `CLAUDE.md` for the
workflow). Newest first.

## 2026-10-08
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
