"""Source and sink adapter contracts."""

from unified_modernization.adapters.cosmos_change_feed import CosmosChangeFeedAdapter, CosmosChangeFeedAdapterConfig
from unified_modernization.adapters.debezium_cdc import DebeziumChangeEventAdapter, DebeziumChangeEventAdapterConfig
from unified_modernization.adapters.firestore_outbox import FirestoreOutboxRecord, normalize_outbox_record
from unified_modernization.adapters.spanner_change_stream import SpannerChangeStreamAdapter, SpannerChangeStreamAdapterConfig

__all__ = [
    "CosmosChangeFeedAdapter",
    "CosmosChangeFeedAdapterConfig",
    "DebeziumChangeEventAdapter",
    "DebeziumChangeEventAdapterConfig",
    "FirestoreOutboxRecord",
    "SpannerChangeStreamAdapter",
    "SpannerChangeStreamAdapterConfig",
    "normalize_outbox_record",
]
