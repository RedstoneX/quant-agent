#!/usr/bin/env python3
"""Benchmark OpenRouter models on QAMC's own agent tasks.

    python ops/model_policy/benchmark_models.py --from-onecli --repeats 2
    python ops/model_policy/benchmark_models.py --models qwen/qwen3.7-flash --scenario tech_batch
    python ops/model_policy/benchmark_models.py --report ops/model_policy/results/latest.json

Every candidate is driven through the REAL agent classes with the REAL
prompts (see `scenarios.py` for why), and graded by deterministic Python
assertions. The output is decision-quality-per-dollar, not token price:

    quality      = weighted mean of the scenario's graded checks, 0..1
    cost         = measured tokens x OpenRouter's own published rate
    quality/$    = quality / cost, the number the policy is chosen on

Cost uses OpenRouter's `/api/v1/models` pricing rather than LiteLLM: we pay
OpenRouter's rate for OpenRouter-routed traffic, and their catalog is the
only source that is definitionally correct about that.

Credentials: this never holds a real key. It sends the same
`placeholder-managed-by-onecli` stand-in the commissioning preflight uses
and lets the OneCLI gateway substitute the real value server-side, so a
benchmark log can never contain a credential. `--from-onecli` resolves the
gateway wiring the same way `ops/commissioning/verify_commissioning.py`
does; without it, the process environment must already carry it (the
runtime account's normal state).

Runs cost real money — a full sweep is roughly 5 scenarios x N models x
repeats LLM calls. Nothing here places an order or touches the broker.
"""
from __future__ import annotations

import argparse
import atexit
import contextlib
import hashlib
import importlib
import json
import os
import signal
import statistics
import sys
import tempfile
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ops.model_policy.scenarios import (  # noqa: E402
    Check, DEFAULT_SCENARIOS, Scenario, SCENARIOS, SCENARIOS_BY_KEY,
)

ONECLI_DASHBOARD = "http://127.0.0.1:10254/api/container-config"
OPENROUTER_CATALOG = "https://openrouter.ai/api/v1/models"
PLACEHOLDER_KEY = "placeholder-managed-by-onecli"

BASELINE_MODEL = "openai/gpt-5.5"

# The candidate slate. Chosen from a full sweep of OpenRouter's catalog
# (410 models on 2026-08-12) by: text-capable, >= 128K context (QAMC's
# tech_analyst batches are large), priced, non-`:free` (free tiers are
# rate-limited and can be withdrawn without notice, which is not an
# acceptable dependency for a trading session), and either a current
# flagship or a current cost-efficient workhorse. The Qwen and DeepSeek
# entries the work contract names explicitly are all here.
DEFAULT_CANDIDATES = [
    BASELINE_MODEL,
    # --- frontier / near-frontier, for the decision seats ---
    "deepseek/deepseek-v4-pro-0813",
    "qwen/qwen3.7-max",
    "qwen/qwen3.7-plus",
    "z-ai/glm-5.2",
    "openai/gpt-5.6-luna",
    "minimax/minimax-m3",
    # --- cost-efficient workhorses, for the specialist seats ---
    "deepseek/deepseek-v4-flash-0731",
    "qwen/qwen3.7-flash",
    "qwen/qwen3-235b-a22b-2507",
    "openai/gpt-5-nano",
    "google/gemini-2.5-flash-lite",
]


# --------------------------------------------------------------------------
# Gateway wiring + pricing
# --------------------------------------------------------------------------


def wire_from_onecli() -> str:
    """Point this process at the OneCLI credential gateway.

    Same shape as `ops/commissioning/verify_commissioning.py:resolve_wiring`
    and `ops/onecli/README.md` step 4b: read the container-config, rewrite
    `host.docker.internal` to loopback (bare processes are not in a
    container), write the CA to a 0600 temp file removed on exit.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(ONECLI_DASHBOARD, timeout=15) as resp:
        cfg = json.loads(resp.read().decode())
    proxy = (cfg.get("env", {}).get("HTTPS_PROXY") or "").replace(
        "host.docker.internal", "127.0.0.1"
    )
    cert = cfg.get("caCertificate") or ""
    if not proxy or not cert:
        raise SystemExit("onecli container-config lacked a proxy URL or CA certificate")
    fd, path = tempfile.mkstemp(prefix="qamc-bench-ca-", suffix=".pem")
    with os.fdopen(fd, "w") as fh:
        fh.write(cert)
    os.chmod(path, 0o600)
    atexit.register(lambda: os.path.exists(path) and os.unlink(path))
    os.environ["HTTPS_PROXY"] = proxy
    os.environ["SSL_CERT_FILE"] = path
    os.environ["REQUESTS_CA_BUNDLE"] = path
    return "onecli container-config"


def openrouter_pricing() -> dict[str, dict[str, float]]:
    """{model_id: {'input': $/M, 'output': $/M}} straight from the catalog."""
    opener = urllib.request.build_opener()
    with opener.open(OPENROUTER_CATALOG, timeout=30) as resp:
        data = json.loads(resp.read().decode())["data"]
    out: dict[str, dict[str, float]] = {}
    for m in data:
        p = m.get("pricing") or {}
        try:
            inp = float(p.get("prompt") or 0) * 1e6
            outp = float(p.get("completion") or 0) * 1e6
        except (TypeError, ValueError):
            continue
        if inp > 0 and outp > 0:
            out[m["id"]] = {"input": inp, "output": outp}
    return out


# --------------------------------------------------------------------------
# Running one (model, scenario) pair
# --------------------------------------------------------------------------


@dataclass
class Trial:
    model: str
    scenario: str
    role: str
    ok: bool
    quality: float
    checks: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    latency_s: float = 0.0
    error: str = ""
    # First LLM response, truncated. A score is only auditable if a reviewer
    # can see what the model actually said — "qwen scored 0.4 on risk" is an
    # assertion; the verdict text next to it is evidence. Bounded so the
    # results file stays reviewable, and it holds market opinions about
    # synthetic tickers, never a credential.
    sample_output: str = ""
    # What ACTUALLY answered, per AgentResult. If this ever differs from
    # `model`, the trial silently benchmarked something else and the row
    # must not be read as evidence about `model`.
    actual_model: str = ""
    used_fallback: bool = False
    # WHICH symbols this run proposed, as "ACTION:SYMBOL", sorted.
    # quality says whether a run was VALID; this says whether two runs
    # AGREED. A seat that scores 1.00 twice while naming disjoint books is
    # not a stable seat, and the score alone cannot show that.
    picks: list[str] = field(default_factory=list)


def _load_agent_cls(path: str):
    module_name, cls_name = path.split(":")
    return getattr(importlib.import_module(module_name), cls_name)


class TrialTimeout(BaseException):
    """Raised when a trial exceeds the session budget.

    Deliberately a BaseException, not an Exception: `BaseAgent._execute`
    catches `Exception` broadly and would classify this as a transient
    failure and retry it, which is precisely the runaway the deadline
    exists to stop.
    """


# A trading session is not open-ended: `scripts/run_if_et_window.sh` wraps
# each run in a hard kill, and an agent that has not answered inside that
# window is unusable at that seat no matter how well it scores.
#
# This deadline matters more than it looks. `_call_openai` STREAMS (see the
# docstring there — it is how QAMC survives a Cloudflare 524 relay), which
# makes `_LLM_HTTP_TIMEOUT` a per-CHUNK read timeout rather than a total
# one. A slow reasoning model that keeps trickling tokens therefore runs
# unbounded inside a single attempt, and `_DEFAULT_RETRY_DEADLINE_S` never
# sees it because that only applies BETWEEN attempts. Without this, one
# pathological candidate stalls the whole sweep — observed on the first
# run, where a single tech_batch call passed 15 minutes with no timeout.
TRIAL_DEADLINE_S = 420.0


@contextlib.contextmanager
def trial_deadline(seconds: float):
    """SIGALRM-based wall-clock cap around one trial.

    A watchdog thread cannot help here: the call is blocked in a socket read
    inside C, and Python threads have no way to interrupt that. A signal
    delivered to the main thread does.
    """
    def _fire(_signum, _frame):
        raise TrialTimeout(f"exceeded {seconds:.0f}s trial deadline")

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class _Meter:
    """Captures token usage from the agent's own AgentResult.

    BaseAgent already records exact input/output tokens and hands them back
    on every AgentResult, so the benchmark reads the same numbers the
    production telemetry would log — no parallel accounting, no estimate.
    """

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.sample_output = ""
        self.actual_model = ""
        self.used_fallback = False

    def observe(self, result) -> None:
        if result is None:
            return
        self.input_tokens += getattr(result, "input_tokens", 0) or 0
        self.output_tokens += getattr(result, "output_tokens", 0) or 0
        if not self.sample_output:
            self.sample_output = (getattr(result, "raw_text", "") or "")[:1500]
        self.actual_model = getattr(result, "model", "") or self.actual_model
        self.used_fallback = self.used_fallback or bool(
            getattr(result, "used_fallback", False)
        )


def _extract_picks(output) -> list[str]:
    """The book a trial proposed, flattened to sorted "ACTION:SYMBOL" strings.

    Deliberately duck-typed over the parsed agent output rather than keyed to
    one agent class: `decisions` (constructed orders) is preferred over
    `targets` (pre-construction intent) because it is what would reach the
    broker. Any shape it does not recognise yields [], never an exception —
    a benchmark must not fail because it could not summarise a result.
    """
    if output is None:
        return []
    try:
        decisions = getattr(output, "decisions", None)
        if decisions:
            return sorted(
                f"{getattr(d, 'action', '?')}:{str(getattr(d, 'symbol', '?')).upper()}"
                for d in decisions
            )
        targets = getattr(output, "targets", None)
        if targets:
            return sorted(
                f"TARGET:{str(getattr(x, 'symbol', '?')).upper()}" for x in targets
            )
    except Exception:
        return []
    return []


def run_trial(scenario: Scenario, model: str, pricing: dict, cost_circuit=None) -> Trial:
    agent_cls = _load_agent_cls(scenario.agent_path)
    agent = agent_cls(
        api_key=PLACEHOLDER_KEY,
        model=model,
        max_tokens=scenario.max_tokens,
        provider="openrouter",
    )
    if cost_circuit is not None:
        agent.set_cost_circuit(cost_circuit)

    meter = _Meter()
    # Wrap _execute so every underlying call is metered even for agents that
    # make several (tech_analyst chunks). Wrapping the lowest common call
    # rather than each entry point keeps this honest as agents change.
    original_execute = agent._execute

    def metered(user_message: str, **kwargs):
        result = original_execute(user_message, **kwargs)
        meter.observe(result)
        return result

    agent._execute = metered  # type: ignore[method-assign]

    started = time.monotonic()
    error = ""
    output = None
    try:
        with trial_deadline(TRIAL_DEADLINE_S):
            output = scenario.invoke(agent)
    except TrialTimeout as exc:
        # Scores 0, and that is the right answer rather than a gap in the
        # table: "could not answer inside the session budget" is a
        # disqualifying result for a seat, not missing data.
        error = f"TrialTimeout: {exc}"
    except Exception as exc:  # a model that cannot complete the call scores 0
        error = f"{type(exc).__name__}: {str(exc)[:240]}"
    latency = time.monotonic() - started

    try:
        checks = scenario.grade(output)
    except Exception as exc:
        checks = [Check("grading_crashed", 1.0, False, f"{type(exc).__name__}: {exc}")]

    total_weight = sum(c.weight for c in checks) or 1.0
    quality = sum(c.weight for c in checks if c.passed) / total_weight

    rates = pricing.get(model)
    cost = None
    if rates and (meter.input_tokens or meter.output_tokens):
        cost = (
            meter.input_tokens * rates["input"] + meter.output_tokens * rates["output"]
        ) / 1e6
    # A trial that timed out or raised never returned an AgentResult, so no
    # usage was metered — but tokens WERE spent. Reporting $0.0000 there
    # would understate the true cost of a model that fails slowly, which is
    # exactly the model this table needs to warn about. `None` renders "$?"
    # and keeps it out of the averages.

    return Trial(
        model=model,
        scenario=scenario.key,
        role=scenario.role,
        ok=not error,
        quality=round(quality, 4),
        checks=[asdict(c) for c in checks],
        input_tokens=meter.input_tokens,
        output_tokens=meter.output_tokens,
        cost_usd=cost,
        latency_s=round(latency, 2),
        error=error,
        sample_output=meter.sample_output,
        actual_model=meter.actual_model,
        used_fallback=meter.used_fallback,
        picks=_extract_picks(output),
    )


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _prompt_conflicts(sources: list[dict]) -> list[str]:
    """Roles whose prompt sha differs between the files being merged."""
    by_role: dict[str, dict[str, list[str]]] = {}
    for src in sources:
        for role, fp in (src.get("prompts") or {}).items():
            sha = fp.get("sha256")
            if sha:
                by_role.setdefault(role, {}).setdefault(sha, []).append(src["path"])
    out = []
    for role, shas in sorted(by_role.items()):
        if len(shas) > 1:
            detail = "; ".join(
                f"{sha[:12]} <- {', '.join(paths)}" for sha, paths in shas.items()
            )
            out.append(f"{role}: {detail}")
    return out


def prompt_fingerprints(scenarios) -> dict:
    """Record WHICH PROMPT produced these scores.

    The harness drives the real agent classes, which load
    `config/prompts/<role>.md` from disk. The prompt is therefore an INPUT
    to every grade, not a constant — and until 2026-09-01 no result file
    recorded which version it was. That is how a full set of PM
    model-selection numbers stayed on disk for a week after the prompt they
    were measured against had been rewritten, with nothing to reveal it.
    Nobody was careless; the file simply could not be asked the question.

    A missing or unreadable prompt is recorded as an explicit error rather
    than omitted, because a silently absent fingerprint reproduces the exact
    failure this exists to prevent.
    """
    out: dict[str, dict] = {}
    for role in sorted({s.role for s in scenarios}):
        rel = f"config/prompts/{role}.md"
        path = PROJECT_ROOT / rel
        try:
            raw = path.read_bytes()
        except OSError as exc:
            out[role] = {"path": rel, "error": str(exc)}
            continue
        out[role] = {
            "path": rel,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "lines": raw.count(b"\n") + (0 if raw.endswith(b"\n") else 1),
        }
    return out


def aggregate(trials: list[Trial]) -> dict:
    """Per (model, scenario) means, then per-model rollups."""
    by_pair: dict[tuple[str, str], list[Trial]] = {}
    for t in trials:
        by_pair.setdefault((t.model, t.scenario), []).append(t)

    pairs = {}
    for (model, scenario), group in by_pair.items():
        costs = [t.cost_usd for t in group if t.cost_usd is not None]
        pairs[f"{model}|{scenario}"] = {
            "model": model,
            "scenario": scenario,
            "role": group[0].role,
            "runs": len(group),
            "quality_mean": round(statistics.fmean(t.quality for t in group), 4),
            "quality_min": round(min(t.quality for t in group), 4),
            "cost_mean": round(statistics.fmean(costs), 6) if costs else None,
            "latency_mean": round(statistics.fmean(t.latency_s for t in group), 2),
            "errors": [t.error for t in group if t.error],
            # A row where something else answered is not evidence about
            # `model`. Surfaced rather than silently averaged in.
            "misattributed": [
                t.actual_model for t in group
                if t.actual_model and t.actual_model != t.model
            ],
        }

    models = {}
    for entry in pairs.values():
        m = models.setdefault(entry["model"], {"scenarios": {}, "quality": [], "cost": []})
        m["scenarios"][entry["scenario"]] = entry
        m["quality"].append(entry["quality_mean"])
        if entry["cost_mean"] is not None:
            m["cost"].append(entry["cost_mean"])
    # Worst SINGLE run, not the worst scenario average. Averaging hides the
    # failure mode that actually matters for a trading session: a model that
    # alternates between excellent and unparseable looks fine on the mean and
    # silences a session every other day.
    worst_trial: dict[str, float] = {}
    for t in trials:
        worst_trial[t.model] = min(worst_trial.get(t.model, 1.0), t.quality)
    for model, m in models.items():
        m["quality_mean"] = round(statistics.fmean(m["quality"]), 4) if m["quality"] else 0.0
        m["quality_worst"] = round(min(m["quality"]), 4) if m["quality"] else 0.0
        m["quality_worst_run"] = round(worst_trial.get(model, 0.0), 4)
        # `m["cost"]` holds one PER-RUN MEAN per scenario, not per trial, so
        # summing it never produced a total. It was published as
        # `cost_total` anyway, and `quality_per_dollar` was derived from it
        # — which read correctly at one scenario x one repeat and was wrong
        # by a factor of `repeats` everywhere else. Found 2026-09-01 when a
        # 10-repeat run reported a $0.06 "total" for $0.69 of real spend.
        #
        # Both numbers are now published under names that say what they are.
        # quality_per_dollar stays keyed to the PER-RUN cost: the decision
        # it informs is "what does one session cost at this seat", which a
        # sweep total does not answer.
        m["cost_per_run_mean"] = (
            round(statistics.fmean(m["cost"]), 6) if m["cost"] else None
        )
        m["cost_total_usd"] = round(
            sum(t.cost_usd for t in trials
                if t.model == model and t.cost_usd is not None), 6
        ) or None
        if m["cost_per_run_mean"]:
            m["quality_per_dollar"] = round(
                m["quality_mean"] / m["cost_per_run_mean"], 2
            )
        else:
            m["quality_per_dollar"] = None
        del m["quality"], m["cost"]
    return {"pairs": pairs, "models": models}


def render_markdown(report: dict) -> str:
    models = report["models"]
    scen_keys = [s.key for s in SCENARIOS]
    lines = [
        "| model | " + " | ".join(scen_keys)
        + " | mean | worst run | $/sweep | quality/$ |",
        "|" + "---|" * (len(scen_keys) + 5),
    ]
    # Ordered by QUALITY first, cost second — deliberately not by
    # quality/$. Cost here spans three orders of magnitude while quality
    # spans one, so ranking on the ratio puts the cheapest model on top
    # almost regardless of how it scored, which is the exact reasoning error
    # this table exists to prevent. The policy is chosen by clearing a
    # quality bar and THEN taking the cheapest that clears it; quality/$ is
    # reported as a tiebreaker, not as the ranking.
    ordered = sorted(
        models.items(),
        key=lambda kv: (-kv[1]["quality_mean"], -kv[1]["quality_worst"],
                        kv[1]["cost_per_run_mean"]
                        if kv[1]["cost_per_run_mean"] is not None else 1e9),
    )
    for model, m in ordered:
        cells = []
        for key in scen_keys:
            e = m["scenarios"].get(key)
            cells.append("—" if e is None else f"{e['quality_mean']:.2f}")
        cost = m["cost_per_run_mean"]
        lines.append(
            f"| `{model}` | " + " | ".join(cells)
            + f" | {m['quality_mean']:.2f} | {m['quality_worst_run']:.2f} | "
            + (f"${cost:.4f}" if cost is not None else "?")
            + " | "
            + (f"{m['quality_per_dollar']:,.0f}" if m["quality_per_dollar"] else "?")
            + " |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--models", nargs="*", default=None,
                    help="model ids to test (default: the curated candidate slate)")
    ap.add_argument("--scenario", action="append", choices=sorted(SCENARIOS_BY_KEY),
                    help="restrict to this scenario (repeatable)")
    ap.add_argument("--repeats", type=int, default=1,
                    help="runs per (model, scenario) — 2+ exposes variance")
    ap.add_argument("--from-onecli", action="store_true",
                    help="resolve gateway wiring from the live OneCLI instance")
    ap.add_argument("--out", default="ops/model_policy/results/latest.json")
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument(
        "--report", nargs="+",
        help="render markdown from existing results file(s) and exit; several "
             "files are merged at the trial level, so a scenario added after "
             "a sweep can be run on its own and folded in rather than paying "
             "for the whole sweep again",
    )
    ap.add_argument("--merge-out",
                    help="with --report: also write the merged report here")
    args = ap.parse_args(argv)

    if args.report:
        # Later files SUPERSEDE earlier ones for any (model, scenario) pair
        # they cover. That is what makes a targeted re-run reproducible: when
        # a harness bug is found that distorted only some pairs — as happened
        # with the benchmark-local `max_tokens` cap that scored two deepseek
        # rows 0.00 for being cut off mid-JSON — the fix is to re-run those
        # pairs and list the correction file last, not to silently average a
        # known-bad trial in with its replacement.
        by_pair: dict[tuple[str, str], list[Trial]] = {}
        order: list[tuple[str, str]] = []
        sources = []
        superseded: list[str] = []
        for path in args.report:
            doc = json.loads(Path(path).read_text())
            sources.append({"path": path,
                            "generated_at": doc.get("generated_at"),
                            "prompts": doc.get("prompts")})
            seen_here: set[tuple[str, str]] = set()
            for raw in doc["trials"]:
                trial = Trial(**raw)
                key = (trial.model, trial.scenario)
                if key not in seen_here and key in by_pair:
                    superseded.append(f"{trial.model}|{trial.scenario} <- {path}")
                    by_pair[key] = []
                seen_here.add(key)
                if key not in by_pair:
                    by_pair[key] = []
                    order.append(key)
                by_pair[key].append(trial)
        merged: list[Trial] = [t for key in order for t in by_pair[key]]
        if superseded:
            print(f"superseded {len(superseded)} pair(s) by a later file:",
                  file=sys.stderr)
            for s in superseded:
                print(f"  {s}", file=sys.stderr)
        # Merging trials graded against DIFFERENT prompts is the silent
        # staleness this fingerprint exists to catch. Say so loudly rather
        # than averaging across a prompt rewrite.
        prompt_conflicts = _prompt_conflicts(sources)
        if prompt_conflicts:
            print("PROMPT MISMATCH — these files were graded against "
                  "different prompts. Their scores are NOT comparable:",
                  file=sys.stderr)
            for c in prompt_conflicts:
                print(f"  {c}", file=sys.stderr)
        missing = [s_["path"] for s_ in sources if not s_.get("prompts")]
        if missing:
            print("NO PROMPT FINGERPRINT recorded in "
                  f"{len(missing)} file(s) — predates this field, so prompt "
                  "drift CANNOT be ruled out:", file=sys.stderr)
            for m_ in missing:
                print(f"  {m_}", file=sys.stderr)

        report = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "merged_from": sources,
            "superseded_pairs": superseded,
            "prompt_conflicts": prompt_conflicts,
            "sources_without_prompt_fingerprint": missing,
            "baseline": BASELINE_MODEL,
            "scenarios": {s.key: {"role": s.role, "description": s.description}
                          for s in SCENARIOS},
            "trials": [asdict(t) for t in merged],
            "aggregate": aggregate(merged),
        }
        if args.merge_out:
            out = Path(args.merge_out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, indent=2))
            print(f"wrote {out}\n", file=sys.stderr)
        print(render_markdown(report["aggregate"]))
        return 0

    if args.from_onecli:
        source = wire_from_onecli()
    elif os.environ.get("HTTPS_PROXY"):
        source = "environment"
    else:
        print("No gateway wiring: pass --from-onecli or run with HTTPS_PROXY set.",
              file=sys.stderr)
        return 2

    pricing = openrouter_pricing()
    # The breaker uses the same in-process pinned table. OpenRouter's live
    # catalog is authoritative for benchmark-only candidates and was fetched
    # before any completion request.
    from src.cost_table import PRICING
    PRICING.update(pricing)
    from src.config import load_config
    from src.cost_circuit import activate_paid_call_session
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    app_config = load_config(config_path)
    cost_circuit = activate_paid_call_session(
        app_config,
        run_id=f"benchmark-{int(time.time())}", mode="benchmark",
    )
    cost_circuit.require_paid_analysis("benchmark_start")
    models = args.models or DEFAULT_CANDIDATES
    unpriced = [m for m in models if m not in pricing]
    if unpriced:
        print(f"WARNING: no catalog price for {unpriced} — their cost will be null.",
              file=sys.stderr)

    # DEFAULT_SCENARIOS, not SCENARIOS: the production-scale tech batch is
    # opt-in, because running it against every candidate costs far more
    # than it informs. Name it explicitly for the finalists.
    scenarios = (
        [SCENARIOS_BY_KEY[k] for k in args.scenario]
        if args.scenario else DEFAULT_SCENARIOS
    )

    trials: list[Trial] = []
    total = len(models) * len(scenarios) * args.repeats
    n = 0
    for model in models:
        for scenario in scenarios:
            for _ in range(args.repeats):
                n += 1
                print(f"[{n}/{total}] {model} :: {scenario.key} ... ",
                      end="", flush=True, file=sys.stderr)
                trial = run_trial(scenario, model, pricing, cost_circuit=cost_circuit)
                trials.append(trial)
                print(
                    f"q={trial.quality:.2f} "
                    + (f"${trial.cost_usd:.4f} " if trial.cost_usd is not None else "$? ")
                    + f"{trial.latency_s:.0f}s"
                    + (f" ERR {trial.error[:90]}" if trial.error else ""),
                    file=sys.stderr,
                )

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "wiring_source": source,
        "baseline": BASELINE_MODEL,
        "repeats": args.repeats,
        "scenarios": {s.key: {"role": s.role, "description": s.description}
                      for s in scenarios},
        "prompts": prompt_fingerprints(scenarios),
        "pricing_used": {m: pricing.get(m) for m in models},
        "trials": [asdict(t) for t in trials],
        "aggregate": aggregate(trials),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"\nwrote {out}\n", file=sys.stderr)
    print(render_markdown(report["aggregate"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
