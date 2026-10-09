# Project instructions for Claude

## Backlog and changelog workflow

`backlog.md` is the list of not-yet-done work; `CHANGELOG.md` is the list of
completed work. Keep them in sync:

- Every story in `backlog.md` is numbered (`## N. Title`). Numbers are
  permanent references - once assigned, a number is never reused or changed,
  even after its story is removed. New stories always get the next unused
  number. Don't renumber existing stories to close gaps.
- When a backlog story is completed (implemented, tested, and committed):
  1. Remove its entire entry from `backlog.md`.
  2. Add an entry to `CHANGELOG.md` under today's date (newest date first,
     new entries at the top of that date's list) describing what was done,
     written for the project owner (not a commit-message-style diff
     summary) - what changed and why it matters, in plain language. Note
     which backlog number it was, e.g. "(was backlog #16)", for traceability
     back to the original ask.
  3. Don't be wordy in the changelog or the commit messages, be really brief
     but clearly explain what has been fixed.
- A backlog story can be partially done (e.g. bundled with others, or scoped
  down) - only remove it from the backlog once it's actually fully done;
  otherwise leave it and describe the partial progress in the changelog
  entry, or ask before closing it out.
