"""Environment-driven configuration. Nothing here is hardcoded to any host.

Values are read from the process environment (optionally seeded from a local
`.env` file via python-dotenv). CLI flags may override individual fields.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is a core dep but keep import soft
    def load_dotenv(*_args, **_kwargs):  # type: ignore
        return False


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass
class Neo4jSettings:
    uri: str = "bolt://127.0.0.1:7687"
    user: str = "neo4j"
    password: str = ""
    database: str = "neo4j"
    batch_size: int = 500
    include_lines: bool = False

    @classmethod
    def from_env(cls) -> Neo4jSettings:
        return cls(
            uri=os.getenv("NEO4J_URI", cls.uri),
            user=os.getenv("NEO4J_USER", cls.user),
            password=os.getenv("NEO4J_PASSWORD", cls.password),
            database=os.getenv("NEO4J_DATABASE", cls.database),
            batch_size=_int(os.getenv("GF_BATCH_SIZE"), cls.batch_size),
            include_lines=_bool(os.getenv("GF_INCLUDE_LINES"), cls.include_lines),
        )

    def check_connectable(self) -> None:
        """Fail with advice *before* connecting, rather than as a driver traceback.

        Called only from the two places that actually open a driver, so
        ``--emit`` and ``--dry-run`` still need no configuration at all.
        """
        if self.password:
            return
        if _bool(os.getenv("GF_ALLOW_EMPTY_PASSWORD"), False):
            return  # a server started with NEO4J_AUTH=none
        raise ValueError(
            "NEO4J_PASSWORD is not set.\n"
            "  copy .env.example to .env and fill it in, or pass --neo4j-password.\n"
            "  if your server runs with auth disabled, set GF_ALLOW_EMPTY_PASSWORD=true.\n"
            "  `graphforge doctor` will tell you which of these applies."
        )


@dataclass
class GitSettings:
    repo_dir: str = "./repos"
    username: str = ""
    password: str = ""
    token: str = ""
    gitlab_server: str = ""
    gitlab_group_id: str = ""
    gitlab_token: str = ""
    default_branch: str = "main"
    history_limit: int = 0

    @classmethod
    def from_env(cls) -> GitSettings:
        return cls(
            repo_dir=os.getenv("GF_REPO_DIR", cls.repo_dir),
            username=os.getenv("GIT_USERNAME", cls.username),
            password=os.getenv("GIT_PASSWORD", cls.password),
            token=os.getenv("GIT_TOKEN", cls.token),
            gitlab_server=os.getenv("GITLAB_SERVER", cls.gitlab_server),
            gitlab_group_id=os.getenv("GITLAB_GROUP_ID", cls.gitlab_group_id),
            gitlab_token=os.getenv("GITLAB_TOKEN", cls.gitlab_token),
            default_branch=os.getenv("GIT_DEFAULT_BRANCH", cls.default_branch),
            history_limit=_int(os.getenv("GF_HISTORY_LIMIT"), cls.history_limit),
        )


@dataclass
class DbSettings:
    engine: str = "mysql"
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = ""
    password: str = ""
    names: list[str] = field(default_factory=list)
    mssql_driver: str = "ODBC Driver 18 for SQL Server"
    pg_schemas: list[str] = field(default_factory=lambda: ["public"])

    @classmethod
    def from_env(cls) -> DbSettings:
        return cls(
            engine=os.getenv("DB_ENGINE", cls.engine).lower(),
            host=os.getenv("DB_HOST", cls.host),
            port=_int(os.getenv("DB_PORT"), cls.port),
            user=os.getenv("DB_USER", cls.user),
            password=os.getenv("DB_PASSWORD", cls.password),
            names=_csv(os.getenv("DB_NAMES")),
            mssql_driver=os.getenv("MSSQL_DRIVER", cls.mssql_driver),
            pg_schemas=_csv(os.getenv("PG_SCHEMAS")) or ["public"],
        )


@dataclass
class Settings:
    neo4j: Neo4jSettings = field(default_factory=Neo4jSettings)
    git: GitSettings = field(default_factory=GitSettings)
    db: DbSettings = field(default_factory=DbSettings)


def load_settings(env_file: str | None = None) -> Settings:
    """Load settings from the environment, seeding from a .env file if present."""
    load_dotenv(dotenv_path=env_file, override=False)
    return Settings(
        neo4j=Neo4jSettings.from_env(),
        git=GitSettings.from_env(),
        db=DbSettings.from_env(),
    )
