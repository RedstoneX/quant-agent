/* The analyst scorecard's committed example data.
 *
 * WHY THIS EXISTS. The conviction ledger only produces a row when a position
 * closes and is scored, so a freshly deployed desk has an empty scorecard for
 * weeks. A page that renders as a blank grid in that window cannot be reviewed,
 * cannot be tested, and teaches a reader nothing about how to read it. This
 * file is the worked example the page falls back to, and the page states in a
 * banner — every time it is showing — that these rows are invented.
 *
 * HOW IT WAS BUILT. Not by hand. A list of ideas (who backed each one, who
 * objected, what the trade did) was run through the SAME aggregation
 * `src/api/routes_scorecard.py` performs, so every total, running series, peak
 * and monthly step below is arithmetically consistent with the endpoint's own
 * output rather than a plausible-looking guess. The generator's idea list is
 * reproduced in the test that guards this file.
 *
 * WHAT IT DEMONSTRATES, deliberately:
 *   - `news` gets MORE accurate while LOSING money (50% -> 56%, -$126 -> -$558).
 *   - `technical` gets LESS accurate while MAKING money (67% -> 62%, +$108 -> +$744).
 *     Those two together are the contrast the desk overview exists to show.
 *   - `smart_money` and `earnings` have no record at the earlier date at all,
 *     which is a different thing from a record of zero and must render as such.
 *   - `smart_money` earns most of its money by OBJECTING to trades that lost.
 *
 * This is the fixture only. It is never merged with, defaulted into, or used
 * to fill gaps in a live response — see `chooseView` in scorecardModel.ts.
 */

import type { AnalystScorecardResponse } from "../api/client";

export const ANALYST_SCORECARD_EXAMPLE: AnalystScorecardResponse =
{
  "as_of": "2026-08-26T20:00:00+00:00",
  "state": "populated",
  "read_error": null,
  "risk_dollars_per_call": 100.0,
  "resolved_calls_total": 47,
  "months": [
    "2026-05",
    "2026-06",
    "2026-07",
    "2026-08"
  ],
  "analysts": [
    {
      "analyst": "technical",
      "resolved_calls": 13,
      "calls_right": 8,
      "hit_rate_pct": 61.54,
      "avg_win": 1.3762,
      "avg_loss": -0.714,
      "cumulative_credit": 7.44,
      "peak": 7.44,
      "below_best": 0.0,
      "below_best_since": null,
      "calls_since_peak": 0,
      "cumulative": [
        {
          "resolved_at": "2026-05-06 20:00:00",
          "cumulative": 0.9,
          "peak": 0.9,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-05-19 20:00:00",
          "cumulative": 1.98,
          "peak": 1.98,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-05-21 20:00:00",
          "cumulative": 1.08,
          "peak": 1.98,
          "below_best": 0.9
        },
        {
          "resolved_at": "2026-06-03 20:00:00",
          "cumulative": 3.48,
          "peak": 3.48,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-06-17 20:00:00",
          "cumulative": 2.48,
          "peak": 3.48,
          "below_best": 1.0
        },
        {
          "resolved_at": "2026-06-25 20:00:00",
          "cumulative": 3.11,
          "peak": 3.48,
          "below_best": 0.37
        },
        {
          "resolved_at": "2026-07-02 20:00:00",
          "cumulative": 4.71,
          "peak": 4.71,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-07-15 20:00:00",
          "cumulative": 4.56,
          "peak": 4.71,
          "below_best": 0.15
        },
        {
          "resolved_at": "2026-07-19 20:00:00",
          "cumulative": 3.84,
          "peak": 4.71,
          "below_best": 0.87
        },
        {
          "resolved_at": "2026-07-29 20:00:00",
          "cumulative": 4.74,
          "peak": 4.74,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-08-13 20:00:00",
          "cumulative": 6.84,
          "peak": 6.84,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-08-20 20:00:00",
          "cumulative": 6.04,
          "peak": 6.84,
          "below_best": 0.8
        },
        {
          "resolved_at": "2026-08-26 20:00:00",
          "cumulative": 7.44,
          "peak": 7.44,
          "below_best": 0.0
        }
      ],
      "monthly": [
        {
          "month": "2026-05",
          "credit": 1.08,
          "cumulative": 1.08,
          "resolved_calls": 3,
          "calls_right": 2,
          "hit_rate_pct": 66.67
        },
        {
          "month": "2026-06",
          "credit": 2.03,
          "cumulative": 3.11,
          "resolved_calls": 3,
          "calls_right": 2,
          "hit_rate_pct": 66.67
        },
        {
          "month": "2026-07",
          "credit": 1.63,
          "cumulative": 4.74,
          "resolved_calls": 4,
          "calls_right": 2,
          "hit_rate_pct": 60.0
        },
        {
          "month": "2026-08",
          "credit": 2.7,
          "cumulative": 7.44,
          "resolved_calls": 3,
          "calls_right": 2,
          "hit_rate_pct": 61.54
        }
      ]
    },
    {
      "analyst": "smart_money",
      "resolved_calls": 6,
      "calls_right": 6,
      "hit_rate_pct": 100.0,
      "avg_win": 1.105,
      "avg_loss": null,
      "cumulative_credit": 6.63,
      "peak": 6.63,
      "below_best": 0.0,
      "below_best_since": null,
      "calls_since_peak": 0,
      "cumulative": [
        {
          "resolved_at": "2026-06-12 20:00:00",
          "cumulative": 1.5,
          "peak": 1.5,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-07-02 20:00:00",
          "cumulative": 2.46,
          "peak": 2.46,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-07-19 20:00:00",
          "cumulative": 3.66,
          "peak": 3.66,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-07-24 20:00:00",
          "cumulative": 4.02,
          "peak": 4.02,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-08-05 20:00:00",
          "cumulative": 5.19,
          "peak": 5.19,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-08-24 20:00:00",
          "cumulative": 6.63,
          "peak": 6.63,
          "below_best": 0.0
        }
      ],
      "monthly": [
        {
          "month": "2026-06",
          "credit": 1.5,
          "cumulative": 1.5,
          "resolved_calls": 1,
          "calls_right": 1,
          "hit_rate_pct": 100.0
        },
        {
          "month": "2026-07",
          "credit": 2.52,
          "cumulative": 4.02,
          "resolved_calls": 3,
          "calls_right": 3,
          "hit_rate_pct": 100.0
        },
        {
          "month": "2026-08",
          "credit": 2.61,
          "cumulative": 6.63,
          "resolved_calls": 2,
          "calls_right": 2,
          "hit_rate_pct": 100.0
        }
      ]
    },
    {
      "analyst": "earnings",
      "resolved_calls": 4,
      "calls_right": 4,
      "hit_rate_pct": 100.0,
      "avg_win": 1.0175,
      "avg_loss": null,
      "cumulative_credit": 4.07,
      "peak": 4.07,
      "below_best": 0.0,
      "below_best_since": null,
      "calls_since_peak": 0,
      "cumulative": [
        {
          "resolved_at": "2026-06-03 20:00:00",
          "cumulative": 1.44,
          "peak": 1.44,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-07-08 20:00:00",
          "cumulative": 2.54,
          "peak": 2.54,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-07-29 20:00:00",
          "cumulative": 2.81,
          "peak": 2.81,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-08-13 20:00:00",
          "cumulative": 4.07,
          "peak": 4.07,
          "below_best": 0.0
        }
      ],
      "monthly": [
        {
          "month": "2026-06",
          "credit": 1.44,
          "cumulative": 1.44,
          "resolved_calls": 1,
          "calls_right": 1,
          "hit_rate_pct": 100.0
        },
        {
          "month": "2026-07",
          "credit": 1.37,
          "cumulative": 2.81,
          "resolved_calls": 2,
          "calls_right": 2,
          "hit_rate_pct": 100.0
        },
        {
          "month": "2026-08",
          "credit": 1.26,
          "cumulative": 4.07,
          "resolved_calls": 1,
          "calls_right": 1,
          "hit_rate_pct": 100.0
        }
      ]
    },
    {
      "analyst": "macro",
      "resolved_calls": 8,
      "calls_right": 4,
      "hit_rate_pct": 50.0,
      "avg_win": 0.5062,
      "avg_loss": -0.5363,
      "cumulative_credit": -0.12,
      "peak": 1.44,
      "below_best": 1.56,
      "below_best_since": "2026-06-12 20:00:00",
      "calls_since_peak": 5,
      "cumulative": [
        {
          "resolved_at": "2026-05-06 20:00:00",
          "cumulative": 0.27,
          "peak": 0.27,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-05-21 20:00:00",
          "cumulative": 0.54,
          "peak": 0.54,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-06-12 20:00:00",
          "cumulative": 1.44,
          "peak": 1.44,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-07-02 20:00:00",
          "cumulative": 0.48,
          "peak": 1.44,
          "below_best": 0.96
        },
        {
          "resolved_at": "2026-08-05 20:00:00",
          "cumulative": 1.065,
          "peak": 1.44,
          "below_best": 0.375
        },
        {
          "resolved_at": "2026-08-11 20:00:00",
          "cumulative": 0.93,
          "peak": 1.44,
          "below_best": 0.51
        },
        {
          "resolved_at": "2026-08-13 20:00:00",
          "cumulative": 0.3,
          "peak": 1.44,
          "below_best": 1.14
        },
        {
          "resolved_at": "2026-08-26 20:00:00",
          "cumulative": -0.12,
          "peak": 1.44,
          "below_best": 1.56
        }
      ],
      "monthly": [
        {
          "month": "2026-05",
          "credit": 0.54,
          "cumulative": 0.54,
          "resolved_calls": 2,
          "calls_right": 2,
          "hit_rate_pct": 100.0
        },
        {
          "month": "2026-06",
          "credit": 0.9,
          "cumulative": 1.44,
          "resolved_calls": 1,
          "calls_right": 1,
          "hit_rate_pct": 100.0
        },
        {
          "month": "2026-07",
          "credit": -0.96,
          "cumulative": 0.48,
          "resolved_calls": 1,
          "calls_right": 0,
          "hit_rate_pct": 75.0
        },
        {
          "month": "2026-08",
          "credit": -0.6,
          "cumulative": -0.12,
          "resolved_calls": 4,
          "calls_right": 1,
          "hit_rate_pct": 50.0
        }
      ]
    },
    {
      "analyst": "news",
      "resolved_calls": 16,
      "calls_right": 9,
      "hit_rate_pct": 56.25,
      "avg_win": 0.4833,
      "avg_loss": -1.4186,
      "cumulative_credit": -5.58,
      "peak": 0.54,
      "below_best": 6.12,
      "below_best_since": "2026-05-06 20:00:00",
      "calls_since_peak": 15,
      "cumulative": [
        {
          "resolved_at": "2026-05-06 20:00:00",
          "cumulative": 0.54,
          "peak": 0.54,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-05-19 20:00:00",
          "cumulative": -1.26,
          "peak": 0.54,
          "below_best": 1.8
        },
        {
          "resolved_at": "2026-06-12 20:00:00",
          "cumulative": -2.16,
          "peak": 0.54,
          "below_best": 2.7
        },
        {
          "resolved_at": "2026-06-17 20:00:00",
          "cumulative": -2.46,
          "peak": 0.54,
          "below_best": 3.0
        },
        {
          "resolved_at": "2026-06-25 20:00:00",
          "cumulative": -4.56,
          "peak": 0.54,
          "below_best": 5.1
        },
        {
          "resolved_at": "2026-07-08 20:00:00",
          "cumulative": -3.9,
          "peak": 0.54,
          "below_best": 4.44
        },
        {
          "resolved_at": "2026-07-15 20:00:00",
          "cumulative": -3.75,
          "peak": 0.54,
          "below_best": 4.29
        },
        {
          "resolved_at": "2026-07-19 20:00:00",
          "cumulative": -3.39,
          "peak": 0.54,
          "below_best": 3.93
        },
        {
          "resolved_at": "2026-07-24 20:00:00",
          "cumulative": -3.03,
          "peak": 0.54,
          "below_best": 3.57
        },
        {
          "resolved_at": "2026-07-29 20:00:00",
          "cumulative": -2.49,
          "peak": 0.54,
          "below_best": 3.03
        },
        {
          "resolved_at": "2026-08-05 20:00:00",
          "cumulative": -4.44,
          "peak": 0.54,
          "below_best": 4.98
        },
        {
          "resolved_at": "2026-08-11 20:00:00",
          "cumulative": -4.17,
          "peak": 0.54,
          "below_best": 4.71
        },
        {
          "resolved_at": "2026-08-13 20:00:00",
          "cumulative": -3.54,
          "peak": 0.54,
          "below_best": 4.08
        },
        {
          "resolved_at": "2026-08-20 20:00:00",
          "cumulative": -4.02,
          "peak": 0.54,
          "below_best": 4.56
        },
        {
          "resolved_at": "2026-08-24 20:00:00",
          "cumulative": -6.42,
          "peak": 0.54,
          "below_best": 6.96
        },
        {
          "resolved_at": "2026-08-26 20:00:00",
          "cumulative": -5.58,
          "peak": 0.54,
          "below_best": 6.12
        }
      ],
      "monthly": [
        {
          "month": "2026-05",
          "credit": -1.26,
          "cumulative": -1.26,
          "resolved_calls": 2,
          "calls_right": 1,
          "hit_rate_pct": 50.0
        },
        {
          "month": "2026-06",
          "credit": -3.3,
          "cumulative": -4.56,
          "resolved_calls": 3,
          "calls_right": 0,
          "hit_rate_pct": 20.0
        },
        {
          "month": "2026-07",
          "credit": 2.07,
          "cumulative": -2.49,
          "resolved_calls": 5,
          "calls_right": 5,
          "hit_rate_pct": 60.0
        },
        {
          "month": "2026-08",
          "credit": -3.09,
          "cumulative": -5.58,
          "resolved_calls": 6,
          "calls_right": 3,
          "hit_rate_pct": 56.25
        }
      ]
    }
  ],
  "ideas": [
    {
      "symbol": "UBER",
      "direction": "long",
      "position_id": "position-uber",
      "decision_id": "decision-uber",
      "resolved_at": "2026-08-26 20:00:00",
      "r_multiple": 1.4,
      "supported": [
        {
          "analyst": "technical",
          "side": "supported",
          "stance": "buy",
          "conviction": "high",
          "weight": 1.0,
          "credit": 1.4,
          "nominated": true,
          "reason": "New high on the weekly chart with no overhead supply."
        },
        {
          "analyst": "news",
          "side": "supported",
          "stance": "buy",
          "conviction": "medium",
          "weight": 0.6,
          "credit": 0.84,
          "nominated": false,
          "reason": "Take-rate commentary improved for the third quarter running."
        }
      ],
      "opposed": [
        {
          "analyst": "macro",
          "side": "opposed",
          "stance": "bearish",
          "conviction": "low",
          "weight": 0.3,
          "credit": -0.42,
          "nominated": false,
          "reason": "Discretionary spending surveys are softening."
        }
      ]
    },
    {
      "symbol": "PTON",
      "direction": "long",
      "position_id": "position-pton",
      "decision_id": "decision-pton",
      "resolved_at": "2026-08-24 20:00:00",
      "r_multiple": -2.4,
      "supported": [
        {
          "analyst": "news",
          "side": "supported",
          "stance": "buy",
          "conviction": "high",
          "weight": 1.0,
          "credit": -2.4,
          "nominated": true,
          "reason": "Turnaround coverage has gone from sceptical to positive."
        }
      ],
      "opposed": [
        {
          "analyst": "smart_money",
          "side": "opposed",
          "stance": "bearish",
          "conviction": "medium",
          "weight": 0.6,
          "credit": 1.44,
          "nominated": false,
          "reason": "Every insider trade this year has been a sale."
        }
      ]
    },
    {
      "symbol": "DIS",
      "direction": "long",
      "position_id": "position-dis",
      "decision_id": "decision-dis",
      "resolved_at": "2026-08-20 20:00:00",
      "r_multiple": -0.8,
      "supported": [
        {
          "analyst": "technical",
          "side": "supported",
          "stance": "buy",
          "conviction": "high",
          "weight": 1.0,
          "credit": -0.8,
          "nominated": true,
          "reason": "Held the 200-day on the retest."
        },
        {
          "analyst": "news",
          "side": "supported",
          "stance": "buy",
          "conviction": "medium",
          "weight": 0.6,
          "credit": -0.48,
          "nominated": false,
          "reason": "Subscriber additions beat the low bar set in May."
        }
      ],
      "opposed": []
    },
    {
      "symbol": "AMD",
      "direction": "long",
      "position_id": "position-amd",
      "decision_id": "decision-amd",
      "resolved_at": "2026-08-13 20:00:00",
      "r_multiple": 2.1,
      "supported": [
        {
          "analyst": "technical",
          "side": "supported",
          "stance": "buy",
          "conviction": "high",
          "weight": 1.0,
          "credit": 2.1,
          "nominated": true,
          "reason": "Cup-and-handle completed with a wide-range day."
        },
        {
          "analyst": "earnings",
          "side": "supported",
          "stance": "buy",
          "conviction": "medium",
          "weight": 0.6,
          "credit": 1.26,
          "nominated": false,
          "reason": "Data-centre segment margin inflected."
        },
        {
          "analyst": "news",
          "side": "supported",
          "stance": "buy",
          "conviction": "low",
          "weight": 0.3,
          "credit": 0.63,
          "nominated": false,
          "reason": "Two supply-chain notes turned positive."
        }
      ],
      "opposed": [
        {
          "analyst": "macro",
          "side": "opposed",
          "stance": "bearish",
          "conviction": "low",
          "weight": 0.3,
          "credit": -0.63,
          "nominated": false,
          "reason": "Semiconductor cycles turn faster than this rally assumes."
        }
      ]
    },
    {
      "symbol": "BYND",
      "direction": "long",
      "position_id": "position-bynd",
      "decision_id": "decision-bynd",
      "resolved_at": "2026-08-11 20:00:00",
      "r_multiple": 0.45,
      "supported": [
        {
          "analyst": "news",
          "side": "supported",
          "stance": "buy",
          "conviction": "medium",
          "weight": 0.6,
          "credit": 0.27,
          "nominated": true,
          "reason": "Distribution win with a national grocer."
        }
      ],
      "opposed": [
        {
          "analyst": "macro",
          "side": "opposed",
          "stance": "bearish",
          "conviction": "low",
          "weight": 0.3,
          "credit": -0.135,
          "nominated": false,
          "reason": "This is a discretionary purchase in a tightening consumer."
        }
      ]
    },
    {
      "symbol": "TGT",
      "direction": "long",
      "position_id": "position-tgt",
      "decision_id": "decision-tgt",
      "resolved_at": "2026-08-05 20:00:00",
      "r_multiple": -1.95,
      "supported": [
        {
          "analyst": "news",
          "side": "supported",
          "stance": "buy",
          "conviction": "high",
          "weight": 1.0,
          "credit": -1.95,
          "nominated": true,
          "reason": "Back-to-school promotion coverage read strongly."
        }
      ],
      "opposed": [
        {
          "analyst": "macro",
          "side": "opposed",
          "stance": "bearish",
          "conviction": "low",
          "weight": 0.3,
          "credit": 0.585,
          "nominated": false,
          "reason": "Real income growth for this customer is flat."
        },
        {
          "analyst": "smart_money",
          "side": "opposed",
          "stance": "bearish",
          "conviction": "medium",
          "weight": 0.6,
          "credit": 1.17,
          "nominated": false,
          "reason": "No insider has bought at this level."
        }
      ]
    },
    {
      "symbol": "MSFT",
      "direction": "long",
      "position_id": "position-msft",
      "decision_id": "decision-msft",
      "resolved_at": "2026-07-29 20:00:00",
      "r_multiple": 0.9,
      "supported": [
        {
          "analyst": "technical",
          "side": "supported",
          "stance": "buy",
          "conviction": "high",
          "weight": 1.0,
          "credit": 0.9,
          "nominated": true,
          "reason": "Trend intact; every dip has been bought at the 20-day."
        },
        {
          "analyst": "news",
          "side": "supported",
          "stance": "buy",
          "conviction": "medium",
          "weight": 0.6,
          "credit": 0.54,
          "nominated": false,
          "reason": "Cloud backlog commentary was unusually specific."
        },
        {
          "analyst": "earnings",
          "side": "supported",
          "stance": "buy",
          "conviction": "low",
          "weight": 0.3,
          "credit": 0.27,
          "nominated": false,
          "reason": "Consensus estimates have drifted up for two quarters."
        }
      ],
      "opposed": []
    },
    {
      "symbol": "CHWY",
      "direction": "long",
      "position_id": "position-chwy",
      "decision_id": "decision-chwy",
      "resolved_at": "2026-07-24 20:00:00",
      "r_multiple": 0.6,
      "supported": [
        {
          "analyst": "news",
          "side": "supported",
          "stance": "buy",
          "conviction": "medium",
          "weight": 0.6,
          "credit": 0.36,
          "nominated": true,
          "reason": "Autoship penetration was called out twice on the call."
        },
        {
          "analyst": "smart_money",
          "side": "supported",
          "stance": "buy",
          "conviction": "medium",
          "weight": 0.6,
          "credit": 0.36,
          "nominated": false,
          "reason": "The CFO bought at the open the next morning."
        }
      ],
      "opposed": []
    },
    {
      "symbol": "RIVN",
      "direction": "long",
      "position_id": "position-rivn",
      "decision_id": "decision-rivn",
      "resolved_at": "2026-07-19 20:00:00",
      "r_multiple": -1.2,
      "supported": [
        {
          "analyst": "technical",
          "side": "supported",
          "stance": "buy",
          "conviction": "medium",
          "weight": 0.6,
          "credit": -0.72,
          "nominated": true,
          "reason": "Volume dried up on the pullback, which usually precedes a bounce."
        }
      ],
      "opposed": [
        {
          "analyst": "smart_money",
          "side": "opposed",
          "stance": "bearish",
          "conviction": "high",
          "weight": 1.0,
          "credit": 1.2,
          "nominated": false,
          "reason": "Insider selling has continued every week this quarter."
        },
        {
          "analyst": "news",
          "side": "opposed",
          "stance": "bearish",
          "conviction": "low",
          "weight": 0.3,
          "credit": 0.36,
          "nominated": false,
          "reason": "Delivery numbers missed the whisper figure."
        }
      ]
    },
    {
      "symbol": "ETSY",
      "direction": "long",
      "position_id": "position-etsy",
      "decision_id": "decision-etsy",
      "resolved_at": "2026-07-15 20:00:00",
      "r_multiple": 0.5,
      "supported": [
        {
          "analyst": "news",
          "side": "supported",
          "stance": "buy",
          "conviction": "low",
          "weight": 0.3,
          "credit": 0.15,
          "nominated": true,
          "reason": "Seller-count decline finally stopped."
        }
      ],
      "opposed": [
        {
          "analyst": "technical",
          "side": "opposed",
          "stance": "bearish",
          "conviction": "low",
          "weight": 0.3,
          "credit": -0.15,
          "nominated": false,
          "reason": "Still below every moving average that matters."
        }
      ]
    },
    {
      "symbol": "LULU",
      "direction": "long",
      "position_id": "position-lulu",
      "decision_id": "decision-lulu",
      "resolved_at": "2026-07-08 20:00:00",
      "r_multiple": 1.1,
      "supported": [
        {
          "analyst": "earnings",
          "side": "supported",
          "stance": "buy",
          "conviction": "high",
          "weight": 1.0,
          "credit": 1.1,
          "nominated": true,
          "reason": "Margin recovery is running a quarter ahead of guidance."
        },
        {
          "analyst": "news",
          "side": "supported",
          "stance": "buy",
          "conviction": "medium",
          "weight": 0.6,
          "credit": 0.66,
          "nominated": false,
          "reason": "Store traffic data turned positive in June."
        }
      ],
      "opposed": []
    },
    {
      "symbol": "NVDA",
      "direction": "long",
      "position_id": "position-nvda",
      "decision_id": "decision-nvda",
      "resolved_at": "2026-07-02 20:00:00",
      "r_multiple": 1.6,
      "supported": [
        {
          "analyst": "technical",
          "side": "supported",
          "stance": "buy",
          "conviction": "high",
          "weight": 1.0,
          "credit": 1.6,
          "nominated": true,
          "reason": "Consolidation resolved upward exactly at the prior high."
        },
        {
          "analyst": "smart_money",
          "side": "supported",
          "stance": "buy",
          "conviction": "medium",
          "weight": 0.6,
          "credit": 0.96,
          "nominated": false,
          "reason": "Two directors bought in the open market."
        }
      ],
      "opposed": [
        {
          "analyst": "macro",
          "side": "opposed",
          "stance": "bearish",
          "conviction": "medium",
          "weight": 0.6,
          "credit": -0.96,
          "nominated": false,
          "reason": "This much of the index in one name is a crowding risk."
        }
      ]
    },
    {
      "symbol": "KO",
      "direction": "long",
      "position_id": "position-ko",
      "decision_id": "decision-ko",
      "resolved_at": "2026-06-25 20:00:00",
      "r_multiple": -2.1,
      "supported": [
        {
          "analyst": "news",
          "side": "supported",
          "stance": "buy",
          "conviction": "high",
          "weight": 1.0,
          "credit": -2.1,
          "nominated": true,
          "reason": "Reformulation launch got unusually warm coverage."
        }
      ],
      "opposed": [
        {
          "analyst": "technical",
          "side": "opposed",
          "stance": "bearish",
          "conviction": "low",
          "weight": 0.3,
          "credit": 0.63,
          "nominated": false,
          "reason": "Momentum has been fading for two months."
        }
      ]
    },
    {
      "symbol": "COIN",
      "direction": "long",
      "position_id": "position-coin",
      "decision_id": "decision-coin",
      "resolved_at": "2026-06-17 20:00:00",
      "r_multiple": -1.0,
      "supported": [
        {
          "analyst": "technical",
          "side": "supported",
          "stance": "buy",
          "conviction": "high",
          "weight": 1.0,
          "credit": -1.0,
          "nominated": true,
          "reason": "Higher low held at 210."
        },
        {
          "analyst": "news",
          "side": "supported",
          "stance": "buy",
          "conviction": "low",
          "weight": 0.3,
          "credit": -0.3,
          "nominated": false,
          "reason": "Exchange volumes ticked up week on week."
        }
      ],
      "opposed": []
    },
    {
      "symbol": "F",
      "direction": "long",
      "position_id": "position-f",
      "decision_id": "decision-f",
      "resolved_at": "2026-06-12 20:00:00",
      "r_multiple": -1.5,
      "supported": [
        {
          "analyst": "news",
          "side": "supported",
          "stance": "buy",
          "conviction": "medium",
          "weight": 0.6,
          "credit": -0.9,
          "nominated": true,
          "reason": "Union deal removes a known overhang."
        }
      ],
      "opposed": [
        {
          "analyst": "smart_money",
          "side": "opposed",
          "stance": "bearish",
          "conviction": "high",
          "weight": 1.0,
          "credit": 1.5,
          "nominated": false,
          "reason": "Two insiders sold into the announcement."
        },
        {
          "analyst": "macro",
          "side": "opposed",
          "stance": "bearish",
          "conviction": "medium",
          "weight": 0.6,
          "credit": 0.9,
          "nominated": false,
          "reason": "Auto credit delinquencies are still climbing."
        }
      ]
    },
    {
      "symbol": "AVGO",
      "direction": "long",
      "position_id": "position-avgo",
      "decision_id": "decision-avgo",
      "resolved_at": "2026-06-03 20:00:00",
      "r_multiple": 2.4,
      "supported": [
        {
          "analyst": "technical",
          "side": "supported",
          "stance": "buy",
          "conviction": "high",
          "weight": 1.0,
          "credit": 2.4,
          "nominated": true,
          "reason": "Broke a six-week base on the heaviest volume of the year."
        },
        {
          "analyst": "earnings",
          "side": "supported",
          "stance": "buy",
          "conviction": "medium",
          "weight": 0.6,
          "credit": 1.44,
          "nominated": false,
          "reason": "Backlog grew faster than revenue for a third quarter."
        }
      ],
      "opposed": []
    },
    {
      "symbol": "XOM",
      "direction": "long",
      "position_id": "position-xom",
      "decision_id": "decision-xom",
      "resolved_at": "2026-05-21 20:00:00",
      "r_multiple": -0.9,
      "supported": [
        {
          "analyst": "technical",
          "side": "supported",
          "stance": "buy",
          "conviction": "high",
          "weight": 1.0,
          "credit": -0.9,
          "nominated": true,
          "reason": "Clean base breakout above 118."
        }
      ],
      "opposed": [
        {
          "analyst": "macro",
          "side": "opposed",
          "stance": "bearish",
          "conviction": "low",
          "weight": 0.3,
          "credit": 0.27,
          "nominated": false,
          "reason": "Crude inventories are building, not drawing."
        }
      ]
    },
    {
      "symbol": "SNAP",
      "direction": "long",
      "position_id": "position-snap",
      "decision_id": "decision-snap",
      "resolved_at": "2026-05-19 20:00:00",
      "r_multiple": -1.8,
      "supported": [
        {
          "analyst": "news",
          "side": "supported",
          "stance": "buy",
          "conviction": "high",
          "weight": 1.0,
          "credit": -1.8,
          "nominated": true,
          "reason": "Analyst day guidance sounded confident."
        }
      ],
      "opposed": [
        {
          "analyst": "technical",
          "side": "opposed",
          "stance": "bearish",
          "conviction": "medium",
          "weight": 0.6,
          "credit": 1.08,
          "nominated": false,
          "reason": "Lower highs since March; the trend is still down."
        }
      ]
    },
    {
      "symbol": "PLTR",
      "direction": "long",
      "position_id": "position-pltr",
      "decision_id": "decision-pltr",
      "resolved_at": "2026-05-06 20:00:00",
      "r_multiple": 0.9,
      "supported": [
        {
          "analyst": "technical",
          "side": "supported",
          "stance": "buy",
          "conviction": "high",
          "weight": 1.0,
          "credit": 0.9,
          "nominated": true,
          "reason": "Reclaimed the 50-day average on rising volume."
        },
        {
          "analyst": "news",
          "side": "supported",
          "stance": "buy",
          "conviction": "medium",
          "weight": 0.6,
          "credit": 0.54,
          "nominated": false,
          "reason": "Two new government contracts announced this week."
        },
        {
          "analyst": "macro",
          "side": "supported",
          "stance": "buy",
          "conviction": "low",
          "weight": 0.3,
          "credit": 0.27,
          "nominated": false,
          "reason": "Rate expectations easing; risk appetite improving."
        }
      ],
      "opposed": []
    }
  ]
};
