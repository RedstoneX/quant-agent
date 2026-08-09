---
paths:
  - "src/api/static/**/*"
  - "docs/verification/**/*"
---

# Frontend verification rules

`src/api/static/` is the Mission Control cockpit — a browser/iPad surface an
operator relies on to understand trading, not a backend contract a test
suite alone can validate. Automated tests proving the API returns correct
JSON do not prove the UI renders it correctly.

- Any UI/frontend change intended for acceptance (an internal or external
  checkpoint) must be **browser/runtime verified by Claude before that
  review** — actually loaded in a real browser against seeded/representative
  data and actually looked at, not just asserted not to throw.
- Verification must cover, wherever materially relevant to the change:
  - a desktop viewport and an iPad-sized viewport;
  - populated, empty, degraded and error states;
  - any new drill-down/detail view introduced by the change.
- Save a small **representative** acceptance screenshot set to Git under
  `docs/verification/<stage-or-checkpoint>/` (e.g.
  `docs/verification/stage-4-5/`) — enough images to demonstrate the states
  above were actually seen, not an exhaustive capture of every click.
- Every screenshot set must ship with a manifest/README in the same
  directory recording, per screenshot: the commit SHA it was captured
  against, the viewport and scenario it depicts, and the verification
  date/time.
- Do **not** commit routine or transient browser-test screenshots (ad hoc
  debugging captures, every intermediate iteration) — only the curated
  representative set that documents acceptance evidence. Generate the rest
  in a scratch/temp location and discard them.
- This verification requirement is permanent and applies to every future
  frontend acceptance pass, not just the checkpoint that introduced this
  rule.
