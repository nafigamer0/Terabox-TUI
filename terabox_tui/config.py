"""Configuration storage and curl-import for terabox-tui."""
from __future__ import annotations

import json
import re
import stat
from pathlib import Path

APP_NAME = "terabox-tui"
CONFIG_DIR = Path.home() / ".config" / "terabox-tui"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36"
)


def default_config() -> dict:
    return {
        "base_url": "https://dm.terabox.com",
        "upload_host": "https://dm1-cdata.terabox.com",
        "cookie": "",
        "js_token": "",
        "bdstoken": "",
        "user_agent": DEFAULT_USER_AGENT,
    }


def load_config() -> dict:
    cfg = default_config()
    if CONFIG_PATH.exists():
        try:
            stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cfg
        if isinstance(stored, dict):
            cfg.update({k: v for k, v in stored.items() if k in cfg})
    return cfg


def save_config(cfg: dict) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps({k: cfg.get(k, v) for k, v in default_config().items()}, indent=2),
        encoding="utf-8",
    )
    CONFIG_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return CONFIG_PATH


def config_ready(cfg: dict) -> bool:
    return bool(cfg.get("cookie") and cfg.get("js_token"))


_HEADER_RE = {
    "cookie": re.compile(
        r"""(?:-H|--header)\s+\$?(?P<q>['"])Cookie:\s*(?P<value>.*?)(?P=q)""",
        re.I | re.S,
    ),
    "user_agent": re.compile(
        r"""(?:-H|--header)\s+\$?(?P<q>['"])User-Agent:\s*(?P<value>.*?)(?P=q)""",
        re.I | re.S,
    ),
}
_JSTOKEN_RE = re.compile(r"[?&]jsToken=([0-9A-Fa-f]+)")
_BDSTOKEN_RE = re.compile(r"[?&]bdstoken=([0-9a-fA-F]+)")


def parse_curl(command: str) -> dict:
    """Extract cookie / jsToken / bdstoken / user-agent from a saved curl command."""
    out: dict = {}
    found = _JSTOKEN_RE.search(command)
    if found:
        out["js_token"] = found.group(1)
    found = _BDSTOKEN_RE.search(command)
    if found:
        out["bdstoken"] = found.group(1)
    for field, pattern in _HEADER_RE.items():
        found = pattern.search(command)
        if found:
            out[field] = found.group("value").strip()
    return out
