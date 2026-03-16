from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from unified_modernization.contracts.events import CanonicalDomainEvent
from unified_modernization.contracts.projection import ProjectionStatus
from unified_modernization.projection.builder import ProjectionBuilder


class SourceWatermark(BaseModel):
    source_name: str
    source_type: Literal[
        "cosmos_change_feed",
        "spanner_change_stream",
        "pubsub_snapshot",
        "debezium_offset",
        "kafka_offset",
    ]
    position: str
    captured_at_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CheckpointStore(Protocol):
    def load_position(self) -> int:
        raise NotImplementedError

    def save_position(self, position: int) -> None:
        raise NotImplementedError


class BackfillExecutionSummary(BaseModel):
    ingested_events: int = 0
    published_documents: int = 0
    deleted_documents: int = 0
    pending_documents: int = 0
    last_event_time_utc: datetime | None = None


class StreamHandoffPlan(BaseModel):
    captured_watermarks: list[SourceWatermark]
    pending_documents: int
    backlog_mode: str = "stream_delta_only"
    instructions: list[str] = Field(default_factory=list)


class BackfillExecutionResult(BaseModel):
    summary: BackfillExecutionSummary
    handoff_plan: StreamHandoffPlan


class BackfillCoordinator:
    """Bulk side-load coordinator that hands off to streaming after snapshot completion."""

    def __init__(self, projection_builder: ProjectionBuilder) -> None:
        self._projection_builder = projection_builder

    def side_load(
        self,
        events: Iterable[CanonicalDomainEvent],
        captured_watermarks: list[SourceWatermark],
        *,
        checkpoint_store: CheckpointStore | None = None,
        checkpoint_every: int = 50_000,
        now_utc: datetime | None = None,
    ) -> BackfillExecutionResult:
        now = (now_utc or datetime.now(UTC)).astimezone(UTC)
        summary = BackfillExecutionSummary()
        start_from = checkpoint_store.load_position() if checkpoint_store is not None else 0

        for index, event in enumerate(events):
            if index < start_from:
                continue
            summary.ingested_events += 1
            summary.last_event_time_utc = event.event_time_utc
            decision = self._projection_builder.upsert(event, now_utc=now)
            if decision.publish:
                if decision.state.status == ProjectionStatus.DELETED:
                    summary.deleted_documents += 1
                else:
                    summary.published_documents += 1
            if checkpoint_store is not None and (index + 1) % checkpoint_every == 0:
                checkpoint_store.save_position(index + 1)

        if checkpoint_store is not None:
            checkpoint_store.save_position(summary.ingested_events + start_from)

        summary.pending_documents = self._projection_builder.pending_count()
        instructions = [
            "Resume source CDC streams from the captured high watermarks, not from event zero.",
            "Send only delta changes through the streaming bus; historical records have already side-loaded into the projection store.",
            "Prioritize rehydration or repair events for entities still marked pending after bulk completion.",
        ]
        handoff_plan = StreamHandoffPlan(
            captured_watermarks=captured_watermarks,
            pending_documents=summary.pending_documents,
            instructions=instructions,
        )
        return BackfillExecutionResult(summary=summary, handoff_plan=handoff_plan)
