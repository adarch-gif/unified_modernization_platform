from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol, cast

from unified_modernization.contracts.projection import (
    FragmentRecord,
    ProjectionEntityRecord,
    ProjectionKey,
    ProjectionMutationResult,
    ProjectionStateRecord,
    ProjectionStatus,
    PublicationDecision,
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


def _initial_entity(key: ProjectionKey) -> ProjectionEntityRecord:
    return ProjectionEntityRecord(key=key.model_copy(deep=True))


def _finalize_mutation(
    *,
    key: ProjectionKey,
    result: ProjectionMutationResult,
    prior_revision: int,
) -> ProjectionMutationResult:
    if result.entity.key != key:
        raise ValueError("projection mutation returned an entity for the wrong key")
    next_revision = prior_revision + 1
    result.entity.revision = next_revision
    if result.entity.state is not None:
        result.entity.state.entity_revision = next_revision
    result.decision.state.entity_revision = next_revision
    return result


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

    def mutate_entity(
        self,
        key: ProjectionKey,
        mutator: Callable[[ProjectionEntityRecord], ProjectionMutationResult],
    ) -> ProjectionMutationResult:
        raise NotImplementedError

    def pending_count(self) -> int:
        raise NotImplementedError


class InMemoryProjectionStateStore:
    def __init__(self) -> None:
        self._fragments: dict[tuple[str, str, str, str], dict[str, FragmentRecord]] = {}
        self._states: dict[tuple[str, str, str, str], ProjectionStateRecord] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _tuple(key: ProjectionKey) -> tuple[str, str, str, str]:
        return (key.tenant_id, key.domain_name, key.entity_type, key.logical_entity_id)

    def get_fragments(self, key: ProjectionKey) -> dict[str, FragmentRecord]:
        with self._lock:
            return {
                owner: fragment.model_copy(deep=True)
                for owner, fragment in self._fragments.get(self._tuple(key), {}).items()
            }

    def upsert_fragment(self, key: ProjectionKey, fragment: FragmentRecord) -> None:
        with self._lock:
            fragments = self._fragments.setdefault(self._tuple(key), {})
            current = fragments.get(fragment.fragment_owner)
            if current is None or fragment.source_version >= current.source_version:
                fragments[fragment.fragment_owner] = fragment.model_copy(deep=True)

    def get_state(self, key: ProjectionKey) -> ProjectionStateRecord | None:
        with self._lock:
            state = self._states.get(self._tuple(key))
            return None if state is None else state.model_copy(deep=True)

    def save_state(self, key: ProjectionKey, state: ProjectionStateRecord) -> None:
        with self._lock:
            self._states[self._tuple(key)] = state.model_copy(deep=True)

    def mutate_entity(
        self,
        key: ProjectionKey,
        mutator: Callable[[ProjectionEntityRecord], ProjectionMutationResult],
    ) -> ProjectionMutationResult:
        with self._lock:
            key_tuple = self._tuple(key)
            current_state = self._states.get(key_tuple)
            current_entity = ProjectionEntityRecord(
                key=key.model_copy(deep=True),
                fragments={
                    owner: fragment.model_copy(deep=True)
                    for owner, fragment in self._fragments.get(key_tuple, {}).items()
                },
                state=None if current_state is None else current_state.model_copy(deep=True),
                revision=0 if current_state is None else current_state.entity_revision,
            )
            result = _finalize_mutation(
                key=key,
                result=mutator(current_entity),
                prior_revision=current_entity.revision,
            )
            self._fragments[key_tuple] = {
                owner: fragment.model_copy(deep=True)
                for owner, fragment in result.entity.fragments.items()
            }
            if result.entity.state is not None:
                self._states[key_tuple] = result.entity.state.model_copy(deep=True)
            elif key_tuple in self._states:
                del self._states[key_tuple]
            return ProjectionMutationResult(
                entity=result.entity.model_copy(deep=True),
                decision=result.decision.model_copy(deep=True),
            )

    def pending_count(self) -> int:
        with self._lock:
            terminal = _terminal_states()
            return sum(1 for state in self._states.values() if state.status not in terminal)


class SqliteProjectionStateStore:
    """Durable local control-plane store that mirrors the production store contract."""

    def __init__(self, database_path: str | Path = "projection_state.db") -> None:
        self._database_path = str(database_path)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._initialize()

    def _initialize(self) -> None:
        with sqlite3.connect(self._database_path) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
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
            connection.execute(
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

    def _conn(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self._database_path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            self._local.connection = connection
        return connection

    @staticmethod
    def _params(key: ProjectionKey) -> tuple[str, str, str, str]:
        return (key.tenant_id, key.domain_name, key.entity_type, key.logical_entity_id)

    def _load_fragments(self, key: ProjectionKey) -> dict[str, FragmentRecord]:
        rows = self._conn().execute(
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

    def _load_state(self, key: ProjectionKey) -> ProjectionStateRecord | None:
        row = self._conn().execute(
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

    def _persist_entity(
        self,
        key: ProjectionKey,
        entity: ProjectionEntityRecord,
        *,
        prior_fragment_owners: set[str],
    ) -> None:
        connection = self._conn()
        for removed_owner in sorted(prior_fragment_owners - set(entity.fragments)):
            connection.execute(
                """
                DELETE FROM projection_fragments
                WHERE tenant_id = ? AND domain_name = ? AND entity_type = ? AND logical_entity_id = ? AND fragment_owner = ?
                """,
                (*self._params(key), removed_owner),
            )
        for fragment in entity.fragments.values():
            connection.execute(
                """
                INSERT INTO projection_fragments (
                    tenant_id, domain_name, entity_type, logical_entity_id, fragment_owner, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, domain_name, entity_type, logical_entity_id, fragment_owner)
                DO UPDATE SET payload_json = excluded.payload_json
                """,
                (*self._params(key), fragment.fragment_owner, fragment.model_dump_json()),
            )

        if entity.state is None:
            connection.execute(
                """
                DELETE FROM projection_states
                WHERE tenant_id = ? AND domain_name = ? AND entity_type = ? AND logical_entity_id = ?
                """,
                self._params(key),
            )
            return

        connection.execute(
            """
            INSERT INTO projection_states (
                tenant_id, domain_name, entity_type, logical_entity_id, payload_json
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, domain_name, entity_type, logical_entity_id)
            DO UPDATE SET payload_json = excluded.payload_json
            """,
            (*self._params(key), entity.state.model_dump_json()),
        )

    def get_fragments(self, key: ProjectionKey) -> dict[str, FragmentRecord]:
        with self._lock:
            return {
                owner: fragment.model_copy(deep=True)
                for owner, fragment in self._load_fragments(key).items()
            }

    def upsert_fragment(self, key: ProjectionKey, fragment: FragmentRecord) -> None:
        def mutator(entity: ProjectionEntityRecord) -> ProjectionMutationResult:
            current = entity.fragments.get(fragment.fragment_owner)
            if current is None or fragment.source_version >= current.source_version:
                entity.fragments[fragment.fragment_owner] = fragment.model_copy(deep=True)
            state = entity.state or ProjectionStateRecord(
                tenant_id=key.tenant_id,
                domain_name=key.domain_name,
                entity_type=key.entity_type,
                logical_entity_id=key.logical_entity_id,
                status=ProjectionStatus.PENDING_REQUIRED_FRAGMENT,
                reason_code="fragment_only_mutation",
            )
            entity.state = state
            return ProjectionMutationResult(
                entity=entity,
                decision=PublicationDecision(publish=False, state=state),
            )

        self.mutate_entity(key, mutator)

    def get_state(self, key: ProjectionKey) -> ProjectionStateRecord | None:
        with self._lock:
            state = self._load_state(key)
            return None if state is None else state.model_copy(deep=True)

    def save_state(self, key: ProjectionKey, state: ProjectionStateRecord) -> None:
        def mutator(entity: ProjectionEntityRecord) -> ProjectionMutationResult:
            entity.state = state.model_copy(deep=True)
            return ProjectionMutationResult(
                entity=entity,
                decision=PublicationDecision(publish=False, state=entity.state),
            )

        self.mutate_entity(key, mutator)

    def mutate_entity(
        self,
        key: ProjectionKey,
        mutator: Callable[[ProjectionEntityRecord], ProjectionMutationResult],
    ) -> ProjectionMutationResult:
        with self._lock:
            connection = self._conn()
            with connection:
                fragments = self._load_fragments(key)
                state = self._load_state(key)
                entity = ProjectionEntityRecord(
                    key=key.model_copy(deep=True),
                    fragments={owner: fragment.model_copy(deep=True) for owner, fragment in fragments.items()},
                    state=None if state is None else state.model_copy(deep=True),
                    revision=0 if state is None else state.entity_revision,
                )
                result = _finalize_mutation(
                    key=key,
                    result=mutator(entity),
                    prior_revision=entity.revision,
                )
                self._persist_entity(key, result.entity, prior_fragment_owners=set(fragments))
                return ProjectionMutationResult(
                    entity=result.entity.model_copy(deep=True),
                    decision=result.decision.model_copy(deep=True),
                )

    def pending_count(self) -> int:
        with self._lock:
            rows = self._conn().execute("SELECT payload_json FROM projection_states").fetchall()
            terminal = _terminal_states()
            pending = 0
            for row in rows:
                state = ProjectionStateRecord.model_validate_json(row["payload_json"])
                if state.status not in terminal:
                    pending += 1
            return pending


class SpannerProjectionStateStore:
    """Spanner-backed control-plane store with entity-level mutation inside one transaction."""

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

    def _load_fragments_from_reader(
        self,
        reader: SpannerSnapshotProtocol | SpannerTransactionProtocol,
        key: ProjectionKey,
    ) -> dict[str, FragmentRecord]:
        sql = """
            SELECT fragment_owner, payload_json
            FROM projection_fragments
            WHERE tenant_id = @tenant_id
              AND domain_name = @domain_name
              AND entity_type = @entity_type
              AND logical_entity_id = @logical_entity_id
        """
        rows = reader.execute_sql(
            sql,
            params=self._params(key),
            param_types=self._string_params("tenant_id", "domain_name", "entity_type", "logical_entity_id"),
        )
        return {
            str(row["fragment_owner"]): FragmentRecord.model_validate_json(str(row["payload_json"]))
            for row in rows
        }

    def _load_state_from_reader(
        self,
        reader: SpannerSnapshotProtocol | SpannerTransactionProtocol,
        key: ProjectionKey,
    ) -> ProjectionStateRecord | None:
        sql = """
            SELECT payload_json
            FROM projection_states
            WHERE tenant_id = @tenant_id
              AND domain_name = @domain_name
              AND entity_type = @entity_type
              AND logical_entity_id = @logical_entity_id
        """
        rows = list(
            reader.execute_sql(
                sql,
                params=self._params(key),
                param_types=self._string_params("tenant_id", "domain_name", "entity_type", "logical_entity_id"),
            )
        )
        if not rows:
            return None
        return ProjectionStateRecord.model_validate_json(str(rows[0]["payload_json"]))

    def _delete_fragment(
        self,
        transaction: SpannerTransactionProtocol,
        *,
        key: ProjectionKey,
        fragment_owner: str,
    ) -> None:
        params = self._params(key) | {"fragment_owner": fragment_owner}
        param_types = self._string_params(
            "tenant_id",
            "domain_name",
            "entity_type",
            "logical_entity_id",
            "fragment_owner",
        )
        transaction.execute_update(
            """
            DELETE FROM projection_fragments
            WHERE tenant_id = @tenant_id
              AND domain_name = @domain_name
              AND entity_type = @entity_type
              AND logical_entity_id = @logical_entity_id
              AND fragment_owner = @fragment_owner
            """,
            params=params,
            param_types=param_types,
        )

    def _upsert_fragment_in_transaction(
        self,
        transaction: SpannerTransactionProtocol,
        *,
        key: ProjectionKey,
        fragment: FragmentRecord,
    ) -> None:
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

    def _upsert_state_in_transaction(
        self,
        transaction: SpannerTransactionProtocol,
        *,
        key: ProjectionKey,
        state: ProjectionStateRecord,
    ) -> None:
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

    def get_fragments(self, key: ProjectionKey) -> dict[str, FragmentRecord]:
        with self._database.snapshot() as snapshot:
            return self._load_fragments_from_reader(snapshot, key)

    def upsert_fragment(self, key: ProjectionKey, fragment: FragmentRecord) -> None:
        def mutator(entity: ProjectionEntityRecord) -> ProjectionMutationResult:
            current = entity.fragments.get(fragment.fragment_owner)
            if current is None or fragment.source_version >= current.source_version:
                entity.fragments[fragment.fragment_owner] = fragment.model_copy(deep=True)
            state = entity.state or ProjectionStateRecord(
                tenant_id=key.tenant_id,
                domain_name=key.domain_name,
                entity_type=key.entity_type,
                logical_entity_id=key.logical_entity_id,
                status=ProjectionStatus.PENDING_REQUIRED_FRAGMENT,
                reason_code="fragment_only_mutation",
            )
            entity.state = state
            return ProjectionMutationResult(
                entity=entity,
                decision=PublicationDecision(publish=False, state=state),
            )

        self.mutate_entity(key, mutator)

    def get_state(self, key: ProjectionKey) -> ProjectionStateRecord | None:
        with self._database.snapshot() as snapshot:
            return self._load_state_from_reader(snapshot, key)

    def save_state(self, key: ProjectionKey, state: ProjectionStateRecord) -> None:
        def mutator(entity: ProjectionEntityRecord) -> ProjectionMutationResult:
            entity.state = state.model_copy(deep=True)
            return ProjectionMutationResult(
                entity=entity,
                decision=PublicationDecision(publish=False, state=entity.state),
            )

        self.mutate_entity(key, mutator)

    def mutate_entity(
        self,
        key: ProjectionKey,
        mutator: Callable[[ProjectionEntityRecord], ProjectionMutationResult],
    ) -> ProjectionMutationResult:
        def transaction_body(transaction: SpannerTransactionProtocol) -> ProjectionMutationResult:
            fragments = self._load_fragments_from_reader(transaction, key)
            state = self._load_state_from_reader(transaction, key)
            entity = ProjectionEntityRecord(
                key=key.model_copy(deep=True),
                fragments={owner: fragment.model_copy(deep=True) for owner, fragment in fragments.items()},
                state=None if state is None else state.model_copy(deep=True),
                revision=0 if state is None else state.entity_revision,
            )
            result = _finalize_mutation(
                key=key,
                result=mutator(entity),
                prior_revision=entity.revision,
            )
            prior_owners = set(fragments)
            for removed_owner in sorted(prior_owners - set(result.entity.fragments)):
                self._delete_fragment(transaction, key=key, fragment_owner=removed_owner)
            for fragment in result.entity.fragments.values():
                self._upsert_fragment_in_transaction(transaction, key=key, fragment=fragment)
            if result.entity.state is None:
                raise ValueError("projection entity mutation must persist a state record")
            self._upsert_state_in_transaction(transaction, key=key, state=result.entity.state)
            return ProjectionMutationResult(
                entity=result.entity.model_copy(deep=True),
                decision=result.decision.model_copy(deep=True),
            )

        return cast(ProjectionMutationResult, self._database.run_in_transaction(transaction_body))

    def pending_count(self) -> int:
        sql = """
            SELECT status
            FROM projection_states
        """
        terminal = {status.value for status in _terminal_states()}
        with self._database.snapshot() as snapshot:
            rows = snapshot.execute_sql(sql)
            return sum(1 for row in rows if str(row["status"]) not in terminal)
