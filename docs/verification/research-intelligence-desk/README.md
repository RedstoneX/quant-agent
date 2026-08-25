# Research Intelligence Desk visual acceptance

Captured: 2026-08-25

The frontend visual harness exercised the Research Desk with persisted-shape
fixtures at desktop and iPad sizes. It reported zero console/page errors and
no horizontal document overflow.

- `08-desktop-research-desk.png` — designed Dockview default with the daily
  read, signal stack, all-seat findings, and PM/Risk/execution delta.
- `09-ipad-portrait-research-brief.png` — dedicated portrait reading flow.
- `10-ipad-landscape-research-decision.png` — dedicated landscape decision
  view rather than a squeezed desktop workspace.
- `11-desktop-research-partial.png` — named partial/provider-error state.

These screenshots verify composition and responsive behavior. Production
verification separately uses the read-only `/research/daily/{date}` endpoint
over canonical QAMC records; fixtures are not production data.
