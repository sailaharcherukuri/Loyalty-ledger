"""MySQL connection settings are read from environment variables."""

import os
from pathlib import Path

import mysql.connector


def _load_local_env():
    """Load simple KEY=VALUE settings without overriding shell variables."""
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if name:
            os.environ.setdefault(name, value)


_load_local_env()


def get_connection():
    """Open a MySQL connection using LOYALTY_DB_* environment variables."""
    required = ("LOYALTY_DB_USER", "LOYALTY_DB_PASSWORD")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "Missing database settings: " + ", ".join(missing)
            + ". See .env.example and the README."
        )

    return mysql.connector.connect(
        host=os.getenv("LOYALTY_DB_HOST", "localhost"),
        port=int(os.getenv("LOYALTY_DB_PORT", "3306")),
        user=os.environ["LOYALTY_DB_USER"],
        password=os.environ["LOYALTY_DB_PASSWORD"],
        database=os.getenv("LOYALTY_DB_NAME", "loyalty_ledger"),
    )
