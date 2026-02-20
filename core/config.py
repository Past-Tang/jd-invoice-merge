"""Configuration management.

Loads user config from config.json, with defaults for all fields.
"""
import json
import os

DEFAULT_CONFIG = {
    "invoice": {
        "ivc_title": "",
        "ivc_title_type": 4,
        "ivc_type": 3,
        "ivc_content": 1,
        "change_reason": "抬头有误",
    },
    "merge": {
        "target_amount": 100.0,
        "max_orders_per_invoice": 10,
    },
    "execution": {
        "delay_min": 4,
        "delay_max": 9,
        "retry_limit": 2,
        "cdp_port": 9444,
    },
    "paths": {
        "data_dir": "data",
        "all_orders_file": "data/all_orders.json",
        "all_tab_orders_file": "data/all_tab_orders.json",
        "merge_plan_file": "data/merge_plan.json",
        "merge_progress_file": "data/merge_progress.json",
        "log_file": "data/batch_merge.log",
    },
}

_config = None


def load_config(path: str = "config.json") -> dict:
    """Load config from JSON file, merged with defaults.

    Args:
        path: Path to config.json.

    Returns:
        Merged config dict.
    """
    global _config
    if _config is not None:
        return _config

    config = _deep_copy(DEFAULT_CONFIG)

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user_config = json.load(f)
        _deep_merge(config, user_config)

    _config = config
    return config


def get_config() -> dict:
    """Get the loaded config (loads default if not yet loaded)."""
    if _config is None:
        return load_config()
    return _config


def _deep_copy(d: dict) -> dict:
    return json.loads(json.dumps(d))


def _deep_merge(base: dict, override: dict):
    """Recursively merge override into base."""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
