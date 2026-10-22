"""Where sampan keeps its config and state.

SAMPAN_CONFIG / SAMPAN_STATE_DIR override the defaults.
"""
import os
from pathlib import Path

ENV_KEYS = ("SAMPAN_CONFIG", "SAMPAN_STATE_DIR")


def env_value(env, key):
    """The environment variable's value, or None when unset or empty."""
    return env.get(key) or None


def effective_env(env=os.environ):
    """{SAMPAN_CONFIG/SAMPAN_STATE_DIR: value} for the ones in effect."""
    return {k: v for k in ENV_KEYS if (v := env_value(env, k))}


def _home(home):
    return Path(home) if home else Path.home()


def config_path(env=os.environ, home=None):
    value = env_value(env, "SAMPAN_CONFIG")
    return Path(value) if value else _home(home) / ".config" / "sampan" / "config.toml"


def state_dir(env=os.environ, home=None):
    value = env_value(env, "SAMPAN_STATE_DIR")
    return Path(value) if value else _home(home) / ".local" / "state" / "sampan"
