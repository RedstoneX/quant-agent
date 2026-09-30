## item 188 — detail moved from the board 2026-09-30

The road half of this is FIXED in the same change: on 2026-09-29 all three of the portfolio manager's routes ran over one OpenRouter account, the balance hit HTTP 402, and the whole intraday decision run died while the desk's other endpoint was answering for free in the same process [measured, production log 19:46:45-19:47:46 against 19:46:08]. Route 3 for the three OpenRouter-primary seats now goes to Google AI Studio direct, so no seat has every route on one provider, and a CI test reads `config/settings.yaml` and fails if that ever regresses. What is OPEN is the quality half: `gemini-3.5-flash-lite` has never been benchmarked at the portfolio-manager, risk-manager or position-reviewer seat, so what the desk actually produces in a total OpenRouter outage is unknown rather than merely degraded. The routing-policy test does not catch it because it only governs models reached over OpenRouter.


