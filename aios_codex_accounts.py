"""Safe multi-account slots for Codex authentication.

Each slot uses its own CODEX_HOME, so switching never copies, prints, or
rewrites OAuth tokens. The registry stores only labels and paths.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import uuid


DEFAULT_HOME = Path.home() / ".codex"
ACCOUNTS_ROOT = Path.home() / ".aios" / "codex-accounts"
REGISTRY_PATH = ACCOUNTS_ROOT / "accounts.json"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def _registry() -> dict:
    data = _read_json(REGISTRY_PATH)
    accounts = data.get("accounts") if isinstance(data.get("accounts"), list) else []
    if not any(item.get("id") == "default" for item in accounts if isinstance(item, dict)):
        accounts.insert(0, {"id": "default", "label": "Default Codex account", "home": str(DEFAULT_HOME)})
    data["accounts"] = accounts
    return data


def active_home(config_path: Path | None = None) -> Path:
    explicit = str(os.environ.get("AIOS_ACTIVE_CODEX_HOME") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if config_path:
        configured = str(_read_json(config_path).get("codex_account_home") or "").strip()
        if configured:
            return Path(configured).expanduser()
    return Path(os.environ.get("CODEX_HOME") or DEFAULT_HOME).expanduser()


def list_accounts(config_path: Path | None = None) -> list[dict]:
    import codex_usage

    active = active_home(config_path)
    result = []
    for item in _registry()["accounts"]:
        if not isinstance(item, dict):
            continue
        home = Path(str(item.get("home") or "")).expanduser()
        if not str(home):
            continue
        logged_in, auth_label = codex_usage.codex_auth_info(home)
        result.append({
            "id": str(item.get("id") or ""),
            "label": auth_label if logged_in else str(item.get("label") or "Codex account"),
            "logged_in": bool(logged_in),
            "active": home.resolve() == active.resolve(),
        })
    return result


def switch_account(account_id: str, config_path: Path) -> dict:
    account_id = str(account_id or "").strip()
    item = next((entry for entry in _registry()["accounts"] if entry.get("id") == account_id), None)
    if not item:
        raise ValueError("Unknown Codex account")
    home = Path(str(item["home"])).expanduser()
    if not (home / "auth.json").is_file():
        raise ValueError("That Codex account is not signed in yet")
    config = _read_json(config_path)
    config["codex_account_home"] = str(home)
    _write_json(config_path, config)
    os.environ["AIOS_ACTIVE_CODEX_HOME"] = str(home)
    return {"ok": True, "account_id": account_id, "label": item.get("label") or account_id}


def create_login_slot(config_path: Path, label: str = "") -> dict:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    clean = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:28]
    account_id = f"{clean or 'account'}-{uuid.uuid4().hex[:8]}"
    home = ACCOUNTS_ROOT / account_id
    home.mkdir(parents=True, exist_ok=True)
    default_config = DEFAULT_HOME / "config.toml"
    if default_config.is_file() and not (home / "config.toml").exists():
        shutil.copy2(default_config, home / "config.toml")
    data = _registry()
    data["accounts"].append({
        "id": account_id,
        "label": label.strip() or f"Codex account added {stamp}",
        "home": str(home),
    })
    _write_json(REGISTRY_PATH, data)
    config = _read_json(config_path)
    config["codex_account_home"] = str(home)
    _write_json(config_path, config)
    os.environ["AIOS_ACTIVE_CODEX_HOME"] = str(home)
    return {"id": account_id, "home": str(home), "label": data["accounts"][-1]["label"]}
