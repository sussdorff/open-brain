"""Configuration loader for open-brain plugin."""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("open-brain-plugin")

DEFAULT_CONFIG = {
    "server_url": "",
    "api_key": "",
    "project": "auto",
    "skip_tools": [
        "Read", "Glob", "Grep", "Skill", "ToolSearch", "AskUserQuestion",
        "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput", "TaskStop",
        "CronCreate", "CronDelete", "CronList", "SendMessage",
        "ListMcpResourcesTool", "ReadMcpResourceTool",
        "EnterPlanMode", "ExitPlanMode", "EnterWorktree",
        "NotebookEdit",
    ],
    "context_limit": 50,
    "bash_output_max_kb": 10,
    "log_level": "INFO",
}

CONFIG_DIR = Path.home() / ".open-brain"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = CONFIG_DIR / "hook-log.jsonl"


def load_config() -> dict | None:
    """Load config from ~/.open-brain/config.json.

    Returns None if config doesn't exist or server_url is not set (graceful no-op).
    """
    if not CONFIG_FILE.exists():
        return None

    try:
        with open(CONFIG_FILE) as f:
            user_config = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read config: %s", e)
        return None

    config = {**DEFAULT_CONFIG, **user_config}

    if not config.get("server_url"):
        return None

    return config


def ensure_log_dir():
    """Ensure ~/.open-brain/ directory exists for logging."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


_project_cache: dict[str, str] = {}


def detect_project(cwd: str | None = None) -> str:
    """Detect project name from git remote or cwd basename. Result is cached per cwd."""
    import subprocess

    work_dir = cwd or os.getcwd()
    if work_dir in _project_cache:
        return _project_cache[work_dir]

    name = "unknown"

    # Try git remote
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=3,
            cwd=work_dir,
        )
        if result.returncode == 0 and result.stdout.strip():
            url = result.stdout.strip()
            # Extract repo name from SSH or HTTPS URL
            name = url.rstrip("/").rsplit("/", 1)[-1]
            if name.endswith(".git"):
                name = name[:-4]
    except Exception:
        pass

    if name == "unknown":
        name = Path(work_dir).name or "unknown"

    _project_cache[work_dir] = name
    return name
