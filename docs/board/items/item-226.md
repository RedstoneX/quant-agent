**226. A payment refusal was retried like an outage and reported as an unbounded-cost mystery -- filed 2026-10-01.** Measured on the production database 2026-10-01: the paid research account ran out of credit, the provider answered HTTP 402 with a falling affordable allowance (13290, 7311, 843, 811, 775), the desk spent 12 provider attempts on `portfolio_manager` and 9 on `tech_analyst` against an account no retry could revive, and then suspended paid analysis saying "the real cost is unknown and cannot be bounded safely" when the truth was that the account was empty.

DONE WHEN:
- [x] a payment refusal is classified on the STATUS CODE (402), never on the provider wording, and is terminal on the first occurrence: no retry and no further rung of the route ladder on the same account
- [x] a 429 saying credits could not be verified keeps every retry it has today, because that one is genuinely transient
- [x] the suspension records `provider_out_of_credit` and tells the owner the account is out of credit and needs topping up, while the call is still booked as unproven cost and no spending limit moves
- [ ] one production session observed where an out-of-credit refusal produces exactly one attempt per seat and the out-of-credit wording reaches Telegram -- fixed-but-unobserved until then
detail: docs/board_notes/item-226.md
