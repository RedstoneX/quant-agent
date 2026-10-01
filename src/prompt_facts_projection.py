"""Prompt-fact family: what the book would look like after the proposed trades: the projected portfolio against the constructor's and risk engine's limits, and the correlation matrix that projection needs.

Step 10 (second half) of `docs/ARCHITECTURE.md` §4, board item 210: one of
the six fact families split out of `PromptFacts`. Method bodies are the
former `PromptFacts` bodies byte for byte; this class takes ONLY the
5 collaborators those bodies read. Nothing here may import
`src.pipeline` or `src.pipeline_prompt_facts`.
"""

from src.models import TechAnalysisResult
from src.pipeline_context import PMFacts
from src.prompt_facts_ports import _ABSENT, bind_ports, logger


class ProjectionFacts:
    """What the book would look like after the proposed trades: the projected portfolio against the constructor's and risk engine's limits, and the correlation matrix that projection needs."""

    def __init__(
        self,
        *,
        market=_ABSENT,
        config=_ABSENT,
        last_symbol_sectors=_ABSENT,
        portfolio_constructor=None,
        risk_engine=None,
    ) -> None:
        bind_ports(self, {
            "market": market,
            "config": config,
            "_last_symbol_sectors": last_symbol_sectors,
        })
        self.portfolio_constructor = portfolio_constructor
        self.risk_engine = risk_engine

    def _build_projected_portfolio(
        self,
        positions,
        analyses: list[TechAnalysisResult],
        total_value: float,
    ) -> str:
        """Preview of the book if PM rubber-stamped every BUY-rated TA candidate.

        Surfaces sector concentration BEFORE PM writes decisions, so it can
        self-correct instead of waiting for RM or the hard sector cap to flag
        it. Kept simple on purpose: no correlation math here (that's RM's
        correlation_cluster advisory). Just current vs projected sector mix.

        BOARD ITEM 221 — every candidate used to be previewed at a FLAT
        `default_buy_pct=5.0`, a per-candidate constant nothing else in the
        desk used, and the projected sector mix was built by summing it.
        The constructor sizes each name from its OWN stop distance, so the
        mix shown here was a book no candidate would ever be given.

        THIS PREVIEW IS STRUCTURALLY INCAPABLE OF PROJECTING REALISED
        SECTOR WEIGHTS, AND NO LONGER CLAIMS ONE. The constructor ships
        the SMALLEST of several limits: the PM's target delta, the name's
        stop-implied ceiling, and the further limits on how much of one
        name and one sector the desk may hold, whose interaction is itself
        unsettled (board item 222). The PM
        target delta is the PM's own decision, and this preview is an INPUT
        to that decision — it is built BEFORE the PM writes a target. So no
        candidate's eventual weight is knowable here, and every projected
        mix this function could print would be a guess. Sizing each
        candidate at the largest size its stop permits is NOT the fix: with
        the desk's ordinary stop widths that reaches the single-name
        ceiling and projects sector weights in the hundreds of percent — a
        more confident fiction than the flat slice, correcting the PM for a
        concentration that cannot occur.

        What it reports instead is parameter-free and all of it is known at
        this moment: the held book's measured sector weights, the SECTOR
        COMPOSITION of the candidate set (which names fall in which sector,
        and how many), and per candidate its own stop distance and the
        ceiling that stop implies through `risk_budget_allocation_pct` —
        the SAME single definition `PortfolioConstructor._build_buy` caps
        with — CLAMPED to the single-name notional ceiling so the figure on
        the page is one the desk could actually reach. A ceiling on one
        name is a property of that trade; it is not a weight and is never
        summed into one. Each sector's share OF THE CANDIDATE SET is stated
        as the forward fact, measured rather than projected, with no
        threshold and no warning level attached to it.

        If you are here to "fix" the preview so it projects a mix again:
        read the paragraph above first. The number you would need is a
        decision nobody has made yet.
        """
        from src.execution.broker import _get_sector
        from src.portfolio_constructor import ConstructorConfig
        from src.risk.constants import risk_budget_allocation_pct
        from src.risk.rules import book_exposure, sector_side_gross
        if total_value <= 0:
            return ""
        buy_candidates = [
            a for a in analyses
            if a.rating in ("buy", "strong_buy") and a.entry_price
        ]
        if not positions and not buy_candidates:
            return ""

        cached_sectors = dict(getattr(self, "_last_symbol_sectors", {}))

        def _resolve_sector(symbol: str, fallback: str | None = None) -> str:
            sector = (fallback or "").strip() if fallback else ""
            if sector and sector != "Unknown":
                cached_sectors[symbol] = sector
                return sector

            sector = cached_sectors.get(symbol, "")
            if sector and sector != "Unknown":
                return sector

            sector = _get_sector(symbol) or "Unknown"
            if sector != "Unknown":
                cached_sectors[symbol] = sector
            return sector

        # Same `book_exposure` the PM's Account Status, the PMFacts Book
        # State block and the pre-trade advisory read. This preview used to
        # carry its own `abs(sum(mv * signed_mult))` — a fourth number for
        # the one quantity, in the same prompt as the other three, and the
        # `abs()` made a net-SHORT book render as positively invested.
        current_book = book_exposure(positions, total_value)
        current_invested_pct = current_book.deployed_pct
        current_net = current_book.net_usd
        # Spec §12.2 — GROSS (unsigned) and split by side, keyed
        # `(sector, side)`. Before §12.2 this summed SIGNED `market_value`
        # exactly as the gate did, so a held short shrank its sector in the
        # very preview whose job is to surface concentration.
        sector_gross: dict[tuple[str, str], float] = sector_side_gross(
            positions,
            resolve_sector=lambda p: _resolve_sector(p.symbol, p.sector),
            include_unknown=True,
        )

        unresolved_symbols: list[str] = []
        unsized_symbols: list[str] = []
        # The constructor's own sizing dials, read off the constructor the
        # pipeline actually builds orders with, so the preview cannot drift
        # from it. The fallbacks are `ConstructorConfig`'s own ratified
        # defaults, not numbers chosen here, and they are reached only when
        # no constructor is attached (a bare pipeline in a test) or when a
        # MagicMock config auto-creates a non-numeric attribute.
        cstr_cfg = getattr(
            getattr(self, "portfolio_constructor", None), "cfg", None,
        )

        def _dial(name: str) -> float:
            """One of the constructor's own sizing dials, read off the
            constructor the pipeline really builds orders with.

            The fallback is `ConstructorConfig`'s OWN declared default for
            that same field — never a literal written here. A literal at
            this call site is a flat number the number-source scanner
            cannot see (it reads definition sites, not positional call
            arguments), so it would silently desync the text the PM reads
            from what the constructor does the next time the default moves.
            Reached only when no constructor is attached (a bare pipeline
            in a test) or when a MagicMock config auto-creates a
            non-numeric attribute.
            """
            default = getattr(ConstructorConfig(), name)
            raw = getattr(cstr_cfg, name, None)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                return float(default)
            return float(raw) if raw > 0 else float(default)

        risk_budget_pct = _dial("risk_budget_pct")
        max_position_pct = _dial("max_position_pct")
        by_sector: dict[str, list[str]] = {}
        per_candidate: list[str] = []
        for a in buy_candidates:
            entry = float(a.entry_price)
            # The candidate's OWN stop-implied ceiling, through the one
            # shared definition the constructor caps a long with. It is a
            # CEILING on this one name, not a weight: nothing here knows
            # what the PM will ask for, so nothing here may claim a size.
            ceiling_pct = risk_budget_allocation_pct(
                entry_price=entry,
                stop_price=a.stop_loss if a.stop_loss is not None else 0.0,
                total_value=total_value,
                risk_budget_pct=risk_budget_pct,
            )
            sec = _resolve_sector(a.symbol)
            if sec == "Unknown":
                unresolved_symbols.append(a.symbol)
            by_sector.setdefault(sec, []).append(a.symbol)
            if ceiling_pct is None or ceiling_pct <= 0 or entry <= 0:
                # No usable stop geometry: NAMED, never back-filled with an
                # assumed size. Inventing one is the defect this preview had.
                unsized_symbols.append(a.symbol)
                continue
            stop_distance_pct = abs(entry - float(a.stop_loss)) / entry * 100
            # CLAMPED to the single-name notional ceiling. An UNREACHABLE
            # number printed beside a sector label is an invitation to add
            # up, whatever the sentence beside it says — unclamped, the
            # desk's ordinary stop widths print things like "≤247%", which
            # is the rejected 225% arithmetic in another costume.
            reachable_pct = min(ceiling_pct, max_position_pct)
            per_candidate.append(
                f"{a.symbol} stop -{stop_distance_pct:.1f}% "
                f"→ ≤{reachable_pct:.0f}%"
            )
        self._last_symbol_sectors = cached_sectors

        def _sector_line(sector_dict: dict[tuple[str, str], float]) -> str:
            if not sector_dict:
                return "(empty)"
            sorted_secs = sorted(sector_dict.items(), key=lambda kv: -kv[1])[:5]
            return ", ".join(
                f"{sec} {side} {v / total_value * 100:.0f}%"
                for (sec, side), v in sorted_secs
            )

        lines = [
            f"- Current: {current_invested_pct:.0f}% invested (capital at work) · "
            f"net direction {current_book.net_pct:+.0f}% · sectors: {_sector_line(sector_gross)}",
        ]
        # Spec §12.2/§12.3 — the concentration target comes from the SAME
        # `max_sector_pct` the constructor sizes against and the gate
        # measures against, so the preview cannot warn about a line the rest
        # of the system does not draw. It is applied to the HELD book, which
        # is measured; it is no longer applied to a projected book, which
        # cannot be computed here (see below).
        target_pct = getattr(
            getattr(self, "risk_engine", None), "config", None,
        )
        target_pct = getattr(target_pct, "max_sector_pct", None) or 75.0
        overweight = [
            f"{sec} ({side})" for (sec, side), v in sector_gross.items()
            if v / total_value * 100 > target_pct and sec != "Unknown"
        ]
        if overweight:
            lines.append(
                f"    ⚠ Held sector sides already over the {target_pct:.0f}% "
                f"concentration target (each further trade there is scaled "
                f"down, not refused): {', '.join(sorted(overweight))}"
            )
        if buy_candidates:
            # Each sector's share OF THE CANDIDATE SET. This is a MEASURED
            # forward fact — "6 of 9 candidates are Technology" is true of
            # what is on offer right now and needs no projection, no
            # assumed size and no invented threshold. It is deliberately
            # stated WITHOUT a warning level: the seat judges it. Without
            # it, six candidates in one sector against nothing held read as
            # a bare count and the PM's own ordering and dropping decision
            # — the thing this preview exists to inform — is unguided.
            total_candidates = len(buy_candidates)
            composition = ", ".join(
                f"{sec} {len(syms)} of {total_candidates} "
                f"({len(syms) / total_candidates * 100:.0f}% of the candidate "
                f"set: {', '.join(syms[:6])}"
                + (f" +{len(syms) - 6} more" if len(syms) > 6 else "")
                + ")"
                for sec, syms in sorted(
                    by_sector.items(), key=lambda kv: (-len(kv[1]), kv[0]),
                )
            )
            lines.append(
                f"- {total_candidates} BUY-rated candidate(s) on offer, "
                f"by sector: {composition}"
            )
            if per_candidate:
                n = len(per_candidate)
                shown = per_candidate[:8]
                tail = f" +{n - 8} more" if n > 8 else ""
                lines.append(
                    "- Per candidate, its OWN stop distance and the ceiling "
                    f"that stop implies at the {risk_budget_pct:.1f}% risk "
                    f"budget — a cap on ONE name, NOT a weight: "
                    f"{'; '.join(shown)}{tail}"
                )
                lines.append(
                    f"    (already clamped to the {max_position_pct:.0f}% "
                    "single-name notional ceiling, which is ONE of several "
                    "limits on how much of one name the desk may hold; "
                    "which limit actually binds is unsettled — board item "
                    "222 — so treat each figure as an upper bound that may "
                    "be cut further, never as an entitlement)"
                )
            if unsized_symbols:
                lines.append(
                    "    ⚠ No usable stop geometry, so no ceiling can be "
                    "stated and none is assumed: "
                    f"{', '.join(dict.fromkeys(unsized_symbols))}"
                )
            if unresolved_symbols:
                unique = list(dict.fromkeys(unresolved_symbols))
                lines.append(
                    "    ⚠ Sector unresolved for: "
                    f"{', '.join(unique)} — the composition above may "
                    "understate how crowded one sector is."
                )
            lines.append(
                "- This preview CANNOT tell you what these candidates would "
                "weigh as a share of the book, and does not try. The "
                "constructor sizes each name at the SMALLEST of several "
                "limits — the target weight YOU write, that name's "
                "stop-implied ceiling, and further limits on how much of "
                "one name and one sector the desk may hold (board item 222: "
                "which of them binds is unsettled) — and your targets do "
                "not exist yet, because this preview is an INPUT to the "
                "decision you are about to make. Judge crowding from the "
                "held weights and the candidate composition above."
            )
        return "\n".join(lines)

    def _ensure_correlation_matrix(self, ctx, positions) -> dict:
        """Build the run's correlation matrix once, memoized on `ctx`.

        It used to be built inside `RiskStage`, which runs AFTER the Portfolio
        Manager has already chosen — so the PM's prompt could tell it to "avoid
        stacking highly correlated positions" while the only correlation data
        in the system was computed too late to inform that choice (audit §1.2).
        Building it here, from the DecisionStage side, lets PM see the clusters
        BEFORE it decides, and RiskStage reuses the same matrix rather than
        paying for a second one — the deterministic cluster check must judge
        PM against the numbers PM was actually shown.
        """
        cached = getattr(ctx, "correlation_matrix", None)
        if cached:
            return cached
        try:
            from src.data.correlation import build_correlation_matrix
            # THE CORRELATION WINDOW (board item 148), recorded honestly.
            # The bars that feed this matrix span `trading.lookback_days`
            # (deployed 1800 ≈ 5 trading years, config/settings.yaml) — the
            # SAME history fetched for MA200 and every other indicator, reused
            # here rather than chosen for correlation. It has NO correlation-
            # specific derivation: settings.yaml records the 320→1800 raise as
            # "purely for structure" (deterministic support/resistance), and
            # `build_correlation_matrix` needs only 20 overlapping daily returns
            # (`df.corr(min_periods=20)`) over pairwise-complete observations, so
            # the extra ~1,780 bars add older returns that may straddle regime
            # changes rather than sharpen a cluster estimate. The window moved
            # 120d → 5y silently on the switch to `trading.lookback_days`; this
            # comment is the reason that was never recorded — it is INHERITED
            # from the structural-level fetch, not justified for clustering.
            # It is not a ledgered number: `trading.lookback_days` carries no
            # numeric default (`Field(ge=1)` in src/config.py), so it is not a
            # definition site the number-ledger scanner can attach an entry to;
            # the 0.7 cutoff that used to sit beside it is GONE (item 186,
            # 2026-09-30): clusters are now read from the correlation
            # geometry itself, so the window is the only unjustified input
            # left on this path.
            pool_bars = dict(ctx.symbols_bars)
            for p in positions:
                if p.symbol not in pool_bars:
                    pool_bars[p.symbol] = self.market.get_ohlcv(
                        p.symbol, self.config.trading.lookback_days,
                    ) or []
            matrix = build_correlation_matrix(pool_bars) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to build correlation matrix: %s (continuing without)", e)
            matrix = {}
        ctx.correlation_matrix = matrix
        return matrix
