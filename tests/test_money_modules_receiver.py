"""Attribute calls on a PROVABLY foreign receiver resolve to no repo def.

``with ThreadPoolExecutor() as ex: ex.submit(f)`` used to land on the repo's
stop-invariant ``def submit`` by bare name, making every Yahoo download helper
look like an order writer (96 modules in scope for that reason alone, measured
2026-10-10). The refusal is narrow: the receiver must be bound in the same def
by ``with <Call> as x`` or ``x = <Call>`` where the Call is an import from
outside the repo. An untyped parameter stays fail-safe and still resolves.
"""

from __future__ import annotations

from scripts import money_modules as mm

STOP_WRITER = (
    "def place(client, qty):\n"
    "    return client.submit_order(qty)\n\n"
    "class StopOrder:\n"
    "    def submit(self, qty):\n"
    "        return place(self.client, qty)\n"
)


def _tree(tmp_path, files: dict[str, str]):
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return mm.derive(tmp_path, frozenset({"submit_order"}), "src")


def test_untyped_broker_parameter_still_counts_as_money(tmp_path):
    """Fail-safe kept: a bare ``broker`` argument is not provably foreign."""
    files = {
        "src/exec/stops.py": STOP_WRITER,
        "src/shim.py": "def sell(broker, qty):\n    return broker.submit(qty)\n",
        "src/news.py": "def headlines():\n    return []\n",
    }
    assert _tree(tmp_path, files) == ("src/exec/stops.py", "src/shim.py")


def test_with_threadpool_executor_submit_is_not_an_order_edge(tmp_path):
    files = {
        "src/exec/stops.py": STOP_WRITER,
        "src/data/market.py": (
            "from concurrent.futures import ThreadPoolExecutor\n\n"
            "def download(symbol):\n"
            "    def _download():\n"
            "        return symbol\n"
            "    with ThreadPoolExecutor(max_workers=1) as ex:\n"
            "        return ex.submit(_download).result(timeout=5)\n"
        ),
    }
    assert _tree(tmp_path, files) == ("src/exec/stops.py",)


def test_assigned_threadpool_executor_submit_is_not_an_order_edge(tmp_path):
    files = {
        "src/exec/stops.py": STOP_WRITER,
        "src/data/market.py": (
            "import concurrent.futures\n\n"
            "def download(f):\n"
            "    ex = concurrent.futures.ThreadPoolExecutor()\n"
            "    return ex.submit(f).result()\n"
        ),
    }
    assert _tree(tmp_path, files) == ("src/exec/stops.py",)


def test_receiver_constructed_from_a_repo_import_still_resolves(tmp_path):
    """Only imports from OUTSIDE the repo are foreign; a repo-built desk still carries money."""
    files = {
        "src/exec/stops.py": STOP_WRITER,
        "src/driver.py": (
            "from src.exec.stops import StopOrder\n\ndef go(qty):\n    so = StopOrder()\n    return so.submit(qty)\n"
        ),
    }
    assert _tree(tmp_path, files) == ("src/driver.py", "src/exec/stops.py")


def test_binding_in_another_def_does_not_leak(tmp_path):
    """``ex`` bound foreign in one def says nothing about ``ex`` as a parameter of another."""
    files = {
        "src/exec/stops.py": STOP_WRITER,
        "src/mixed.py": (
            "from concurrent.futures import ThreadPoolExecutor\n\n"
            "def pool(f):\n"
            "    with ThreadPoolExecutor() as ex:\n"
            "        return ex.submit(f)\n\n"
            "def sell(ex, qty):\n"
            "    return ex.submit(qty)\n"
        ),
    }
    assert _tree(tmp_path, files) == ("src/exec/stops.py", "src/mixed.py")
