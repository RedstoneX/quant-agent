"""Desk configuration package.

Every section lives in its own module (see the imports below); this file only
re-exports them so `src.config.X` keeps resolving for every import site and
every test patch target.
"""

# ---- RE-EXPORT MIRROR (the one and only block; keep every name here) ----
from src.config.notifications import NotificationsConfig  # noqa: F401
from src.config.macro import MacroConfig  # noqa: F401
from src.config.broker import (  # noqa: F401
    AlpacaConfig,
    ApiKeysConfig,
    LIVE_TRADING_AUTHORIZED,
    _ALPACA_PAPER_HOST,
)
from src.config.llm import (  # noqa: F401
    AGENT_NAMES,
    LLMConfig,
    _VALID_REASONING_EFFORTS,
)
from src.config.execution import (  # noqa: F401
    ExecutionConfig,
)
from src.config.risk import (  # noqa: F401
    CashReserveConfig,
    CashSweepConfig,
    EventRiskConfig,
    RiskConfig,
)
from src.config.research import (  # noqa: F401
    IntradayScanConfig,
    NewsConfig,
    NominationConfig,
    SmartMoneyConfig,
    UniverseScreenConfig,
)
from src.config.operations import (  # noqa: F401
    DeploymentGapConfig,
    EvolutionConfig,
    ReconciliationConfig,
    ScheduleConfig,
    StorageConfig,
    TradingConfig,
)
from src.config.llm_cost import (  # noqa: F401
    INTRA_CHECK_TICK_MINUTES,
    LLMCostCircuitConfig,
    _paid_run_count,
)
from src.config.app import (  # noqa: F401
    AppConfig,
    _substitute_env_vars,
    _walk_and_substitute,
    load_config,
)
# ---- END RE-EXPORT MIRROR ----
