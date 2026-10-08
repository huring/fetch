# Changelog

Completed backlog stories move here (see `backlog.md` and `CLAUDE.md` for the
workflow). Newest first.

## 2026-10-08
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
