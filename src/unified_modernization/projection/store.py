from __future__ import annotations
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

from unified_modernization.contracts.projection import (
    FragmentRecord,
    ProjectionKey,
    ProjectionStateRecord,
    ProjectionStatus,
)

try:
    from google.cloud.spanner_v1 import param_types as spanner_param_types
except ImportError:  # pragma: no cover - optional dependency
    spanner_param_types = None


SPANNER_PROJECTION_DDL: tuple[str, ...] = (
    """
    CREATE TABLE projection_fragments (
      tenant_id STRING(MAX) NOT NULL,
      domain_name STRING(MAX) NOT NULL,
      entity_type STRING(MAX) NOT NULL,
      logical_entity_id STRING(MAX) NOT NULL,
      fragment_owner STRING(MAX) NOT NULL,
      source_version INT64 NOT NULL,
      payload_json STRING(MAX) NOT NULL
    ) PRIMARY KEY (tenant_id, domain_name, entity_type, logical_entity_id, fragment_owner)
    """.strip(),
    """
    CREATE TABLE projection_states (
      tenant_id STRING(MAX) NOT NULL,
      domain_name STRING(MAX) NOT NULL,
      entity_type STRING(MAX) NOT NULL,
      logical_entity_id STRING(MAX) NOT NULL,
      status STRING(MAX) NOT NULL,
      payload_json STRING(MAX) NOT NULL
    ) PRIMARY KEY (tenant_id, domain_name, entity_type, logical_entity_id)
    """.strip(),
)


def _terminal_states() -> set[ProjectionStatus]:
    return {ProjectionStatus.PUBLISHED, ProjectionStatus.DELETED}


def _payload_json(value: FragmentRecord | ProjectionStateRecord) -> str:
    return value.model_dump_json()


class SpannerSnapshotProtocol(Protocol):
    def execute_sql(
        self,
        sql: str,
        params: dict[str, object] | None = None,
        param_types: dict[str, object] | None = None,
    ) -> Iterable[Mapping[str, object]]:
        raise NotImplementedError


class SpannerTransactionProtocol(Protocol):
    def execute_sql(
        self,
        sql: str,
        params: dict[str, object] | None = None,
        param_types: dict[str, object] | None = None,
    ) -> Iterable[Mapping[str, object]]:
        raise NotImplementedError

    def execute_update(
        self,
        sql: str,
        params: dict[str, object] | None = None,
        param_types: dict[str, object] | None = None,
    ) -> int:
        raise NotImplementedError


class SpannerDatabaseProtocol(Protocol):
    def snapshot(self) -> AbstractContextManager[SpannerSnapshotProtocol]:
        raise NotImplementedError

    def run_in_transaction(
        self,
        func: Callable[[SpannerTransactionProtocol], object],
    ) -> object:
        raise NotImplementedError


class ProjectionStateStore(Protocol):
    def get_fragments(self, key: ProjectionKey) -> dict[str, FragmentRecord]:
        raise NotImplementedError

    def upsert_fragment(self, key: ProjectionKey, fragment: FragmentRecord) -> None:
        raise NotImplementedError

    def get_state(self, key: ProjectionKey) -> ProjectionStateRecord | None:
        raise NotImplementedError

    def save_state(self, key: ProjectionKey, state: ProjectionStateRecord) -> None:
        raise NotImplementedError

    def pending_count(self) -> int:
        raise NotImplementedError


class InMemoryProjectionStateStore:
    def __init__(self) -> None:
        self._fragments: dict[tuple[str, str, str, str], dict[str, FragmentRecord]] = {}
        self._states: dict[tuple[str, str, str, str], ProjectionStateRecord] = {}

    @staticmethod
    def _tuple(key: ProjectionKey) -> tuple[str, str, str, str]:
        return (key.tenant_id, key.domain_name, key.entity_type, key.logical_entity_id)

    def get_fragments(self, key: ProjectionKey) -> dict[str, FragmentRecord]:
        return dict(self._fragments.get(self._tuple(key), {}))

    def upsert_fragment(self, key: ProjectionKey, fragment: FragmentRecord) -> None:
        fragments = self._fragments.setdefault(self._tuple(key), {})
        current = fragments.get(fragment.fragment_owner)
        if current is None or fragment.source_version >= current.source_version:
            fragments[fragment.fragment_owner] = fragment

    def get_state(self, key: ProjectionKey) -> ProjectionStateRecord | None:
        return self._states.get(self._tuple(key))

    def save_state(self, key: ProjectionKey, state: ProjectionStateRecord) -> None:
        self._states[self._tuple(key)] = state

    def pending_count(self) -> int:
        terminal = _terminal_states()
        return sum(1 for state in self._states.values() if state.status not in terminal)


class SqliteProjectionStateStore:
    """Durable local control-plane store that mirrors the production store contract."""

    def __init__(self, database_path: str | Path = "projection_state.db") -> None:
        self._database_path = str(database_path)
        self._connection = sqlite3.connect(self._database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS projection_fragments (
                    tenant_id TEXT NOT NULL,
                    domain_name TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    logical_entity_id TEXT NOT NULL,
                    fragment_owner TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, domain_name, entity_type, logical_entity_id, fragment_owner)
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS projection_states (
                    tenant_id TEXT NOT NULL,
                    domain_name TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    logical_entity_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, domain_name, entity_type, logical_entity_id)
                )
                """
            )

    @staticmethod
    def _params(key: ProjectionKey) -> tuple[str, str, str, str]:
        return (key.tenant_id, key.domain_name, key.entity_type, key.logical_entity_id)

    def get_fragments(self, key: ProjectionKey) -> dict[str, FragmentRecord]:
        rows = self._connection.execute(
            """
            SELECT fragment_owner, payload_json
            FROM projection_fragments
            WHERE tenant_id = ? AND domain_name = ? AND entity_type = ? AND logical_entity_id = ?
            """,
            self._params(key),
        ).fetchall()
        return {
            row["fragment_owner"]: FragmentRecord.model_validate_json(row["payload_json"])
            for row in rows
        }

    def upsert_fragment(self, key: ProjectionKey, fragment: FragmentRecord) -> None:
        current = self.get_fragments(key).get(fragment.fragment_owner)
        if current is not None and current.source_version > fragment.source_version:
            return
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO projection_fragments (
                    tenant_id, domain_name, entity_type, logical_entity_id, fragment_owner, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, domain_name, entity_type, logical_entity_id, fragment_owner)
                DO UPDATE SET payload_json = excluded.payload_json
                """,
                (*self._params(key), fragment.fragment_owner, fragment.model_dump_json()),
            )

    def get_state(self, key: ProjectionKey) -> ProjectionStateRecord | None:
        row = self._connection.execute(
            """
            SELECT payload_json
            FROM projection_states
            WHERE tenant_id = ? AND domain_name = ? AND entity_type = ? AND logical_entity_id = ?
            """,
            self._params(key),
        ).fetchone()
        if row is None:
            return None
        return ProjectionStateRecord.model_validate_json(row["payload_json"])

    def save_state(self, key: ProjectionKey, state: ProjectionStateRecord) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO projection_states (
                    tenant_id, domain_name, entity_type, logical_entity_id, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, domain_name, entity_type, logical_entity_id)
                DO UPDATE SET payload_json = excluded.payload_json
                """,
                (*self._params(key), state.model_dump_json()),
            )

    def pending_count(self) -> int:
        rows = self._connection.execute("SELECT payload_json FROM projection_states").fetchall()
        terminal = _terminal_states()
        pending = 0
        for row in rows:
            state = ProjectionStateRecord.model_validate_json(row["payload_json"])
            if state.status not in terminal:
                pending += 1
        return pending


class SpannerProjectionStateStore:
    """Spanner-backed control-plane store with explicit table design and optimistic upsert rules."""

    def __init__(self, database: SpannerDatabaseProtocol) -> None:
        self._database = database

    @staticmethod
    def projection_schema_ddl() -> tuple[str, ...]:
        return SPANNER_PROJECTION_DDL

    @staticmethod
    def _params(key: ProjectionKey) -> dict[str, object]:
        return {
            "tenant_id": key.tenant_id,
            "domain_name": key.domain_name,
            "entity_type": key.entity_type,
            "logical_entity_id": key.logical_entity_id,
        }

    @staticmethod
    def _string_params(*names: str) -> dict[str, object] | None:
        if spanner_param_types is None:
            return None
        return {name: spanner_param_types.STRING for name in names}

    def get_fragments(self, key: ProjectionKey) -> dict[str, FragmentRecord]:
        sql = """
            SELECT fragment_owner, payload_json
            FROM projection_fragments
            WHERE tenant_id = @tenant_id
              AND domain_name = @domain_name
              AND entity_type = @entity_type
              AND logical_entity_id = @logical_entity_id
        """
        with self._database.snapshot() as snapshot:
            rows = snapshot.execute_sql(
                sql,
                params=self._params(key),
                param_types=self._string_params("tenant_id", "domain_name", "entity_type", "logical_entity_id"),
            )
            return {
                str(row["fragment_owner"]): FragmentRecord.model_validate_json(str(row["payload_json"]))
                for row in rows
            }

    def upsert_fragment(self, key: ProjectionKey, fragment: FragmentRecord) -> None:
        params = self._params(key) | {
            "fragment_owner": fragment.fragment_owner,
            "source_version": fragment.source_version,
            "payload_json": _payload_json(fragment),
        }
        param_types = self._string_params(
            "tenant_id",
            "domain_name",
            "entity_type",
            "logical_entity_id",
            "fragment_owner",
            "payload_json",
        )
        if param_types is not None:
            param_types["source_version"] = spanner_param_types.INT64

        def transaction_body(transaction: SpannerTransactionProtocol) -> None:
            current_rows = list(
                transaction.execute_sql(
                    """
                    SELECT source_version
                    FROM projection_fragments
                    WHERE tenant_id = @tenant_id
                      AND domain_name = @domain_name
                      AND entity_type = @entity_type
                      AND logical_entity_id = @logical_entity_id
                      AND fragment_owner = @fragment_owner
                    """,
                    params=params,
                    param_types=param_types,
                )
            )
            if current_rows and int(current_rows[0]["source_version"]) > fragment.source_version:
                return
            transaction.execute_update(
                """
                INSERT OR UPDATE INTO projection_fragments (
                  tenant_id, domain_name, entity_type, logical_entity_id,
                  fragment_owner, source_version, payload_json
                ) VALUES (
                  @tenant_id, @domain_name, @entity_type, @logical_entity_id,
                  @fragment_owner, @source_version, @payload_json
                )
                """,
                params=params,
                param_types=param_types,
            )

        self._database.run_in_transaction(transaction_body)

    def get_state(self, key: ProjectionKey) -> ProjectionStateRecord | None:
        sql = """
            SELECT payload_json
            FROM projection_states
            WHERE tenant_id = @tenant_id
              AND domain_name = @domain_name
              AND entity_type = @entity_type
              AND logical_entity_id = @logical_entity_id
        """
        with self._database.snapshot() as snapshot:
            rows = list(
                snapshot.execute_sql(
                    sql,
                    params=self._params(key),
                    param_types=self._string_params("tenant_id", "domain_name", "entity_type", "logical_entity_id"),
                )
            )
            if not rows:
                return None
            return ProjectionStateRecord.model_validate_json(str(rows[0]["payload_json"]))

    def save_state(self, key: ProjectionKey, state: ProjectionStateRecord) -> None:
        params = self._params(key) | {
            "status": state.status.value,
            "payload_json": _payload_json(state),
        }
        param_types = self._string_params(
            "tenant_id",
            "domain_name",
            "entity_type",
            "logical_entity_id",
            "status",
            "payload_json",
        )

        def transaction_body(transaction: SpannerTransactionProtocol) -> None:
            transaction.execute_update(
                """
                INSERT OR UPDATE INTO projection_states (
                  tenant_id, domain_name, entity_type, logical_entity_id,
                  status, payload_json
                ) VALUES (
                  @tenant_id, @domain_name, @entity_type, @logical_entity_id,
                  @status, @payload_json
                )
                """,
                params=params,
                param_types=param_types,
            )

        self._database.run_in_transaction(transaction_body)

    def pending_count(self) -> int:
        sql = """
            SELECT status
            FROM projection_states
        """
        terminal = {status.value for status in _terminal_states()}
        with self._database.snapshot() as snapshot:
            rows = snapshot.execute_sql(sql)
            return sum(1 for row in rows if str(row["status"]) not in terminal)
