"""Persisted (non-secret) settings: excluded domains, last-used connection fields, db path."""
import json
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "email_extract"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS = {
    "excluded_domains": [],
    "last_host": "",
    "last_port": 993,
    "last_username": "",
    "last_use_ssl": True,
    "db_path": str(Path.cwd() / "contacts.db"),
}


def load() -> dict:
    """Load persisted settings, falling back to defaults for missing/invalid data.

    Returns:
        Config dict merged over DEFAULTS. Never raises on a missing or
        corrupt config file.
    """
    if not CONFIG_FILE.exists():
        return dict(DEFAULTS)
    try:
        data = json.loads(CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)
    merged = dict(DEFAULTS)
    merged.update(data)
    return merged


def save(cfg: dict) -> None:
    """Persist settings to CONFIG_FILE, dropping any unknown/secret keys.

    Only keys present in DEFAULTS are written, so passing a dict that also
    contains a password or other secret is safe: that field is silently
    discarded rather than written to disk.

    Args:
        cfg: Settings to persist. Extra keys (e.g. a password) are ignored.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    safe = {k: v for k, v in cfg.items() if k in DEFAULTS}
    CONFIG_FILE.write_text(json.dumps(safe, indent=2))
