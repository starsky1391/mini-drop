"""数据库引擎与会话管理。

通过 DATABASE_URL 环境变量切换后端：
  PostgreSQL: DATABASE_URL=postgresql+psycopg://user:pass@host:5432/db
  SQLite:     DATABASE_URL=sqlite:///mini_drop.db（默认，测试/演示适用）

引擎和 Session factory 通过 _get_engine() / _get_sessionmaker() 延迟创建，
测试代码可以在 import 本模块之前设置 DATABASE_URL 环境变量。
"""

from __future__ import annotations

import os
import threading

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from server.app.models import Base

_engine: Engine | None = None
_sessionmaker: sessionmaker | None = None
# _get_sessionmaker() may initialize the engine while holding this lock, so it
# must be re-entrant in a fresh process where neither singleton exists yet.
_lock = threading.RLock()


def _build_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    if url:
        return url
    db_file = os.getenv("SQLITE_PATH", "mini_drop.db")
    return f"sqlite:///{db_file}"


def _get_engine() -> Engine:
    global _engine
    if _engine is not None:
        return _engine
    with _lock:
        if _engine is not None:
            return _engine
        url = _build_url()
        connect_args: dict = {}
        engine_kwargs: dict = {}
        if "sqlite" in url:
            connect_args["check_same_thread"] = False
            if url in {"sqlite:///:memory:", "sqlite://"}:
                engine_kwargs["poolclass"] = StaticPool
        _engine = create_engine(
            url,
            echo=False,
            pool_pre_ping=True,
            connect_args=connect_args,
            **engine_kwargs,
        )
        return _engine


def _get_sessionmaker() -> sessionmaker:
    global _sessionmaker
    if _sessionmaker is not None:
        return _sessionmaker
    with _lock:
        if _sessionmaker is not None:
            return _sessionmaker
        _sessionmaker = sessionmaker(
            bind=_get_engine(), autoflush=False, autocommit=False,
            expire_on_commit=False,
        )
        return _sessionmaker


def init_db() -> None:
    """创建所有表（幂等）。应用启动时调用一次。"""
    engine = _get_engine()
    Base.metadata.create_all(bind=engine)
    _ensure_probe_evidence_columns(engine)
    _ensure_watch_episode_columns(engine)


def _ensure_probe_evidence_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    table = "diagnosis_probe_executions"
    if table not in inspector.get_table_names():
        return
    columns = {item["name"] for item in inspector.get_columns(table)}
    statements = []
    if "evidence_status" not in columns:
        statements.append(f"ALTER TABLE {table} ADD COLUMN evidence_status VARCHAR(32)")
    if "evidence_reason" not in columns:
        statements.append(f"ALTER TABLE {table} ADD COLUMN evidence_reason TEXT")
    if not statements:
        return
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def _ensure_watch_episode_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    statements: list[str] = []
    if "watch_subscriptions" in tables:
        columns = {item["name"] for item in inspector.get_columns("watch_subscriptions")}
        if "auto_diagnosis_enabled" not in columns:
            statements.append(
                "ALTER TABLE watch_subscriptions ADD COLUMN auto_diagnosis_enabled BOOLEAN NOT NULL DEFAULT TRUE"
            )
    if "watch_incidents" in tables:
        columns = {item["name"] for item in inspector.get_columns("watch_incidents")}
        timestamp_type = "TIMESTAMP WITH TIME ZONE" if engine.dialect.name == "postgresql" else "TIMESTAMP"
        additions = {
            "episode_id": "VARCHAR(128)",
            "episode_status": "VARCHAR(32) NOT NULL DEFAULT 'AGGREGATING'",
            "aggregation_deadline": timestamp_type,
            "first_seen_at": timestamp_type,
            "last_seen_at": timestamp_type,
            "recovery_observations": "INTEGER NOT NULL DEFAULT 0",
            "occurrence_count": "INTEGER NOT NULL DEFAULT 1",
            "diagnosis_eligible": "BOOLEAN NOT NULL DEFAULT FALSE",
            "impact_status": "VARCHAR(32) NOT NULL DEFAULT 'impact_unconfirmed'",
            "anomaly_points_json": "JSON",
            "conclusion_revision_count": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, declaration in additions.items():
            if name not in columns:
                statements.append(f"ALTER TABLE watch_incidents ADD COLUMN {name} {declaration}")
    if not statements:
        return
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def new_session() -> Session:
    """返回一个新的数据库会话。调用方负责 close。"""
    return _get_sessionmaker()()


def reset_engine() -> None:
    """重置引擎和 session factory（测试用，强制下次调用时重建）。"""
    global _engine, _sessionmaker
    with _lock:
        _engine = None
        _sessionmaker = None
