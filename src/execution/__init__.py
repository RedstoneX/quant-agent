"""Execution package. The owner-flag gate is installed on the broker door here."""

from src.execution.broker import AlpacaBroker as _AlpacaBroker
from src.execution.owner_flags_gate import install as _install_owner_flags

_install_owner_flags(_AlpacaBroker)
