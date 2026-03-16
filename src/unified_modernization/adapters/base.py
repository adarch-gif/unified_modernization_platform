from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from unified_modernization.contracts.events import CanonicalDomainEvent


class SourceAdapter(Protocol):
    """Normalizes vendor-specific source records into canonical domain events."""

    def normalize(self, record: Mapping[str, object]) -> CanonicalDomainEvent:
        raise NotImplementedError
