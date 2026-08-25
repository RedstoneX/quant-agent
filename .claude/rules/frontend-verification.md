---
paths:
  - "src/api/static/**/*"
  - "frontend/**/*"
  - "docs/verification/**/*"
---

# Frontend verification rules

Mission Control is a browser/iPad surface the operator relies on to understand trading. Automated tests proving correct JSON do not prove the UI renders it correctly.

- Any UI/frontend change intended for acceptance must be **browser/runtime verified by the engineering agent** — actually loaded against seeded/representative data and visually inspected, not merely asserted not to throw.
- Verification should cover, wherever materially relevant:
  - a desktop viewport and an iPad-sized viewport;
  - populated, empty, degraded and error states;
  - any new drill-down/detail view introduced by the change.
- Visual verification may run in parallel with other independent acceptance work when that saves time.
- Save a small **representative** acceptance screenshot set to Git under `docs/verification/<stage-or-checkpoint>/` — enough to prove the relevant states were seen, not every click.
- The screenshot set should record the commit SHA, viewport/scenario and verification date/time.
- Do **not** commit routine/transient debugging screenshots; keep only curated acceptance evidence.
- This is an acceptance requirement, **not an external review gate**. Under Paper-beta autonomy the lead agent may use the evidence to merge/deploy once the relevant acceptance criteria pass.
