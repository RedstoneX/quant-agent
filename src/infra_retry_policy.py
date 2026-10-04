"""The desk's transient-fault retry policy, in a leaf module anything may import.

Single home of the numbers: `LLMCostCircuitConfig` takes its defaults from here
and the owner-alert sender reads them from here, so neither pulls the config
package (and the agents package behind it) into the notifier.
"""

MAX_RETRIES = 2
BACKOFF_BASE_S = 2.0
BACKOFF_MAX_S = 8.0
