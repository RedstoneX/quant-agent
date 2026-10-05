"""The method inventory of the pipeline modules, read from the AST.

Board item 210. Read from the abstract syntax tree and never from
`import`/`dir()`, so a move that breaks an import still reports the truth about
what each file CONTAINS. Nothing is recorded: `scripts/pipeline_method_guard.py`
compares this measurement of the working tree with the same measurement of
`origin/main` at check time.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The modules the split touches. A later step ADDS its new module here in the
#: same change that moves the code, which is the point of the guard.
TRACKED_MODULES: tuple[str, ...] = (
    "src/pipeline.py",
    "src/pipeline_admission.py",
    "src/pipeline_admission_shell.py",
    "src/pipeline_delever.py",
    "src/pipeline_exits.py",
    "src/pipeline_intraday.py",
    "src/intraday/safety.py",
    "src/intraday/session.py",
    "src/intraday/gating.py",
    "src/intraday/candidates.py",
    "src/pipeline_prompt_facts.py",
    "src/pipeline_prompt_facts_pure.py",
    "src/pipeline_prompt_facts_review.py",
    "src/pipeline_protection.py",
    "src/protection/protected_sell.py",
    "src/protection/reprotect_records.py",
    "src/pipeline_risk_gate.py",
    "src/pipeline_research_continuity.py",
    "src/pipeline_sizing.py",
    "src/pipeline_earnings_quality.py",
    "src/pipeline_stages.py",
    # Board item 210 step 10: the four stage classes moved out of
    # `src/pipeline_stages.py` verbatim into one file each.
    "src/stage_morning_research.py",
    "src/stage_decision.py",
    "src/stage_risk.py",
    "src/stage_execution.py",
)

_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


def text_inventory(name: str, text: str) -> dict[str, object]:
    """Every module-level function name and every method name per class, from the AST."""
    tree = ast.parse(text, filename=name)
    functions = sorted(n.name for n in tree.body if isinstance(n, _FUNC))
    classes: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes[node.name] = sorted(
                child.name for child in node.body if isinstance(child, _FUNC)
            )
    return {"module_functions": functions, "classes": dict(sorted(classes.items()))}


def module_inventory(path: Path) -> dict[str, object]:
    return text_inventory(str(path), path.read_text(encoding="utf-8"))
