"""The desk's transient-fault retry policy, in a leaf module anything may import.

Single home of the fields: `LLMCostCircuitConfig` takes its defaults from here
and the owner-alert sender reads them from here, so neither pulls the config
package (and the agents package behind it) into the notifier.
"""

from pydantic import BaseModel, Field


class InfraRetryPolicy(BaseModel):
    max_retries: int = Field(default=2, ge=0, le=5)
    backoff_base_s: float = Field(default=2.0, gt=0, le=30.0)
    backoff_max_s: float = Field(default=8.0, gt=0, le=60.0)


_F = InfraRetryPolicy.model_fields
MAX_RETRIES = _F["max_retries"].default
BACKOFF_BASE_S = _F["backoff_base_s"].default
BACKOFF_MAX_S = _F["backoff_max_s"].default
