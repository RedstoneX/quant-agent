# item 226 -- a payment refusal is terminal, and the desk must name it

MEASURED 2026-10-01 against the production database, read-only.
`llm_circuit_events` records "portfolio_manager failed after 12 provider
attempt(s) with no provable-zero-cost telemetry" and "tech_analyst failed
after 9 provider attempt(s)". The provider answers behind those attempts were
HTTP 402 of the shape "This request requires more credits, or fewer
max_tokens. You requested up to 16000 tokens, but can only afford 843", and
the affordable figure fell across successive calls (13290, 7311, 843, 811,
775) -- an emptying balance, not a transient condition.

What was wrong -- two things, both honesty, neither one about spending.

- A 402 is permanent until a human tops the account up, so it cannot succeed
  on retry; but the decision seats' routes 2 and 3 sit on the SAME account as
  route 1, and were attempted anyway, out of the attempt budget the cost
  circuit then has to account for.
- The suspension reported "the real cost is unknown and cannot be bounded
  safely" for a failure whose cause the desk knew exactly. The owner read it
  and reasonably concluded the desk was broken. Standing doctrine: a
  misleading alert is itself a root-cause defect.

The fix. `is_payment_refusal` keys on the HTTP status code (402) anywhere in
the exception's cause chain, never on the provider's English -- a provider can
reword a message at any time, and this desk has already been burned once by
text matching. The first such refusal ends the route, and marks that PROVIDER
refused, so every remaining rung on the same account is skipped with a log
line saying why. A 429 that says credits could not be verified is deliberately
excluded: that one means the provider could not check the balance, not that
the balance is gone, so it keeps its retries.

The circuit is not weakened. The call is still booked as a failure of unproven
cost, no limit moved, and `provider_out_of_credit` was added to the
self-clearing trigger set so renaming the cause does not quietly turn a
self-clearing latch into a permanent one. Only the trigger code and the
wording changed.

Deliberately NOT changed. The one-shot "fewer max_tokens" shrink-retry landed
2026-09-30 stays: it is not a retry of the refused request but a smaller
request the provider itself said it would still serve, it is bounded at one
per route, and reverting another measured fix was not in scope. Zero retries
is therefore the rule for the refusal itself; no retry count or backoff number
was invented anywhere in this change.
