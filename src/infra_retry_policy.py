"""The desk's transient-fault retry policy, in a leaf module anything may import.

Single home of the three numbers: `LLMCostCircuitConfig` takes its defaults from
here and the owner-alert sender reads them from here, so neither pulls the
config package (and the agents package behind it) into the notifier. Plain
constants, not a pydantic model: the bounds already live on the consuming
`Field(...)`s, and this module must stay import-free.

They are not invented: each equals the macro data provider's transient-fault
default (`MacroConfig.max_retries`, `MacroConfig.retry_backoff_base_s`,
`MacroConfig.retry_backoff_max_s`, used by `MacroDataProvider._next_backoff`).
Each has its own row in config/number_ledger.yaml.
"""

MAX_RETRIES = 2
BACKOFF_BASE_S = 2.0
BACKOFF_MAX_S = 8.0
