"""Governed-metric record + DDB (de)serializer.

Lives under ``agents/shared/`` so it is importable from the REST API
Lambda (which bundles ``agents/shared/``) and from the agent runtime
container — no cross-package imports.

Storage layout: ``PK=NS#<namespace>``, ``SK=METRIC#<metric_id>`` on the
``semantic-layer-metrics`` table. The embedding lives on the row alongside
the metadata so the Tier 1 lookup path can warm-load metric blobs in a
single DDB scan if OpenSearch is unavailable.
"""
from __future__ import annotations
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

ALLOWED_DIALECTS = {"athena", "trino", "presto"}


class MetricLifecycle(str, Enum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    PUBLISHED = "PUBLISHED"


class Metric(BaseModel):
    """Governed metric definition. Authored via the REST API; consumed by
    the Tier 1 lookup path on the agent runtime."""

    metric_id: str
    namespace: str
    name: str
    description: str
    synonyms: List[str] = Field(default_factory=list)
    compiled_sql: str
    dialect: str
    supported_dimensions: List[str] = Field(default_factory=list)
    supported_filters: List[str] = Field(default_factory=list)
    linked_class: Optional[str] = None
    lifecycle: MetricLifecycle = MetricLifecycle.DRAFT
    version: int = 1

    @field_validator("dialect")
    @classmethod
    def _check_dialect(cls, v: str) -> str:
        """Restrict dialect to the SQL engines we know how to compile +
        execute against (Athena/Trino/Presto)."""
        if v not in ALLOWED_DIALECTS:
            raise ValueError(f"dialect must be one of {sorted(ALLOWED_DIALECTS)}")
        return v

    def to_ddb_item(self, *, embedding: Optional[List[float]] = None) -> Dict[str, Any]:
        """Serialize to a DDB Item dict. ``embedding`` is optional so DRAFT
        rows skip the vector cost — it's only attached on publish."""
        item: Dict[str, Any] = {
            "pk": f"NS#{self.namespace}",
            "sk": f"METRIC#{self.metric_id}",
            "metric_id": self.metric_id,
            "namespace": self.namespace,
            "name": self.name,
            "description": self.description,
            "synonyms": self.synonyms,
            "compiled_sql": self.compiled_sql,
            "dialect": self.dialect,
            "supported_dimensions": self.supported_dimensions,
            "supported_filters": self.supported_filters,
            "linked_class": self.linked_class,
            "lifecycle": self.lifecycle.value,
            "version": self.version,
        }
        if embedding is not None:
            # DDB rejects native float — convert to Decimal via str to avoid
            # binary float artifacts. Hydration on read coerces back to float.
            item["embedding"] = [Decimal(str(x)) for x in embedding]
        return item

    @classmethod
    def from_ddb_item(cls, item: Dict[str, Any]) -> "Metric":
        """Inverse of :meth:`to_ddb_item` — drops the pk/sk/embedding extras."""
        return cls(
            metric_id=item["metric_id"],
            namespace=item["namespace"],
            name=item["name"],
            description=item["description"],
            synonyms=item.get("synonyms", []),
            compiled_sql=item["compiled_sql"],
            dialect=item["dialect"],
            supported_dimensions=item.get("supported_dimensions", []),
            supported_filters=item.get("supported_filters", []),
            linked_class=item.get("linked_class"),
            lifecycle=MetricLifecycle(item.get("lifecycle", "DRAFT")),
            version=item.get("version", 1),
        )
