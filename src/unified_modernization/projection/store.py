from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Protocol

from unified_modernization.contracts.projection import (
    FragmentRecord,
    ProjectionKey,
    ProjectionStateRecord,
    ProjectionStatus,
)


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
        terminal = {ProjectionStatus.PUBLISHED, ProjectionStatus.DELETED}
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
        terminal = {ProjectionStatus.PUBLISHED, ProjectionStatus.DELETED}
        pending = 0
        for row in rows:
            state = ProjectionStateRecord.model_validate_json(row["payload_json"])
            if state.status not in terminal:
                pending += 1
        return pending
