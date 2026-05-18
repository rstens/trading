import contextvars
from copy import deepcopy
from typing import Dict, Optional

import tradingagents.default_config as default_config

# Per-execution-context active config. Was a module-global until the web
# UI added multi-user concurrency; with multiple TradingAgentsGraph runs
# happening simultaneously (one per HTTP request), a process-wide global
# would race between users' providers / vendors / output languages. A
# ContextVar is per-thread (and per-asyncio-task within a thread), which
# matches exactly how runner.py spins up one thread per job.
#
# Caller contract is unchanged: `set_config(...)` is called once per
# TradingAgentsGraph construction; every tool function in `interface.py`
# reads via `get_config()`. Both still work as before, just isolated.
_active_config: contextvars.ContextVar[Optional[Dict]] = contextvars.ContextVar(
    "tradingagents_active_config", default=None
)


def initialize_config():
    """Compatibility no-op.

    The legacy module-global was lazily initialized on first access via
    this function. ContextVar uses the `default=None` parameter for the
    same job, so initialization happens implicitly inside `get_config()`.
    Kept as a public symbol so any external caller still importing it
    doesn't break.
    """
    return None


def set_config(config: Dict) -> None:
    """Push `config` as the active config for the current execution context.

    Dict-valued keys (e.g. ``data_vendors``) are merged one level deep so a
    partial update like ``{"data_vendors": {"core_stock_apis": "alpha_vantage"}}``
    keeps the other nested keys from the default; scalar keys are replaced.

    Each call replaces the contextvar's value (a deep copy, so subsequent
    mutations to the caller's dict don't leak through). The merge is
    layered on top of DEFAULT_CONFIG when nothing was set yet in this
    context, otherwise on top of the previous in-context value.
    """
    current = _active_config.get()
    base = deepcopy(current) if current is not None else deepcopy(default_config.DEFAULT_CONFIG)
    incoming = deepcopy(config)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key].update(value)
        else:
            base[key] = value
    _active_config.set(base)


def get_config() -> Dict:
    """Get the active config for the current execution context.

    Returns a deep copy so tool functions can freely mutate locally
    without leaking back into the contextvar.
    """
    current = _active_config.get()
    if current is None:
        return deepcopy(default_config.DEFAULT_CONFIG)
    return deepcopy(current)
