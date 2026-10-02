"""L2 Ports — the interfaces an adapter must satisfy.

One module per port. A port imports only the kernel (L0) and the standard
library: never ``src.storage``, ``src.execution``, ``src.agents``, ``src.data``,
and never a service or the pipeline. A port that names SQLite, Alpaca, Telegram
or Anthropic is not a port. See docs/architecture for the layer rules.
"""
