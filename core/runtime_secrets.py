"""Single source for deployment secrets and the database connection URL.

Environment variables take precedence.  File-based deployments read the
mounted Render secret directly; local development may use
``.secrets/secrets.toml``.  The historical Streamlit path remains read-only for
one compatibility window and can be removed after every deployment is moved.
"""

from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import tomllib
from urllib.parse import quote_plus


BASE_DIR = Path(__file__).resolve().parents[1]


def _candidate_paths() -> tuple[Path, ...]:
    configured = str(os.getenv("SECRETS_FILE", "")).strip()
    paths = [
        Path(configured).expanduser() if configured else None,
        Path("/etc/secrets/secrets.toml"),
        BASE_DIR / ".secrets" / "secrets.toml",
        BASE_DIR / ".streamlit" / "secrets.toml",
    ]
    return tuple(dict.fromkeys(path for path in paths if path is not None))


@lru_cache(maxsize=1)
def file_secrets() -> dict:
    """Return the first configured TOML secret file, failing closed."""

    for path in _candidate_paths():
        if not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                loaded = tomllib.load(handle)
            return loaded if isinstance(loaded, dict) else {}
        except (OSError, tomllib.TOMLDecodeError):
            return {}
    return {}


def get_secret(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        value = file_secrets().get(name, default)
    return str(value if value is not None else default).strip()


def get_database_url() -> str | None:
    env_url = str(os.getenv("DATABASE_URL", "")).strip()
    if env_url:
        return env_url

    database = file_secrets().get("connections", {}).get("postgresql", {})
    if not isinstance(database, dict) or not database:
        return None
    dialect = str(database.get("dialect", "postgresql"))
    username = quote_plus(str(database.get("username", "")))
    password = quote_plus(str(database.get("password", "")))
    host = str(database.get("host", "localhost"))
    port = str(database.get("port", "5432"))
    name = str(database.get("database", ""))
    if not username or not host or not name:
        return None
    return f"{dialect}://{username}:{password}@{host}:{port}/{name}"
