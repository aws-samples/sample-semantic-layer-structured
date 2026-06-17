"""CRUD + lifecycle for governed metrics.

``compiled_sql`` is parsed with sqlglot at create/update time so an
authoring mistake is caught before publish. PUBLISHED metrics persist
their embedding alongside the row in DDB; the agent runtime hydrates
its in-memory KNN index from DDB on first use, so this service does
not need a cross-process KNN mirror.
"""
from __future__ import annotations
from typing import Any, Callable, List, Optional

import sqlglot
from boto3.dynamodb.conditions import Key

# Imported from agents.shared (eng-review F1 fix): the model is shared
# between the REST API Lambda and the agent runtime; no cross-deployment
# imports allowed.
from agents.shared.metric_models import Metric, MetricLifecycle


class MetricService:
    """DDB-backed CRUD with sqlglot validation. Embeddings are computed
    on publish and stored on the row; the agent runtime's in-memory KNN
    index is hydrated lazily from DDB and is not maintained from here."""

    def __init__(
        self,
        *,
        ddb_table: Any,
        embed_fn: Callable[[str], List[float]],
    ) -> None:
        self.t = ddb_table
        self.embed = embed_fn

    def _validate_sql(self, sql: str, dialect: str) -> None:
        """Parse + enforce SELECT-only at the root. Anything else (DROP /
        DELETE / INSERT / UPDATE / TRUNCATE) parses fine in sqlglot, so
        we have to inspect the AST root."""
        try:
            tree = sqlglot.parse_one(sql, read=dialect)
        except sqlglot.errors.ParseError as e:
            raise ValueError(f"invalid SQL: {e}") from e
        from sqlglot import exp as _exp
        if not isinstance(tree, (_exp.Select, _exp.Union, _exp.Subquery, _exp.With)):
            raise ValueError(
                f"invalid SQL: only SELECT statements allowed (got {type(tree).__name__})"
            )

    def _embed_text(self, m: Metric) -> str:
        """Concatenate the fields a steward would search by — name +
        description + synonyms — into a single embedding input."""
        parts = [m.name, m.description] + list(m.synonyms)
        return "\n".join(p for p in parts if p)

    def create(self, m: Metric) -> Metric:
        """Validate SQL, embed if published, write to DDB."""
        self._validate_sql(m.compiled_sql, m.dialect)
        vec: Optional[List[float]] = None
        if m.lifecycle == MetricLifecycle.PUBLISHED:
            vec = self.embed(self._embed_text(m))
        self.t.put_item(Item=m.to_ddb_item(embedding=vec))
        return m

    def get(self, *, namespace: str, metric_id: str) -> Optional[Metric]:
        """Return the metric or None if missing."""
        resp = self.t.get_item(
            Key={"pk": f"NS#{namespace}", "sk": f"METRIC#{metric_id}"}
        )
        item = resp.get("Item")
        return Metric.from_ddb_item(item) if item else None

    def list(self, *, namespace: str) -> List[Metric]:
        """List every metric in a namespace via a single Query."""
        resp = self.t.query(
            KeyConditionExpression=Key("pk").eq(f"NS#{namespace}")
            & Key("sk").begins_with("METRIC#"),
        )
        return [Metric.from_ddb_item(i) for i in resp.get("Items", [])]

    def update(self, m: Metric) -> Metric:
        """Replace the row, bumping ``version``. Re-embeds if published."""
        self._validate_sql(m.compiled_sql, m.dialect)
        existing = self.get(namespace=m.namespace, metric_id=m.metric_id)
        if existing is None:
            raise KeyError(f"metric not found: {m.namespace}/{m.metric_id}")
        m = m.model_copy(update={"version": existing.version + 1})
        vec = self.embed(self._embed_text(m)) if m.lifecycle == MetricLifecycle.PUBLISHED else None
        self.t.put_item(Item=m.to_ddb_item(embedding=vec))
        return m

    def publish(self, *, namespace: str, metric_id: str) -> Metric:
        """Flip lifecycle to PUBLISHED, bump version, embed, persist."""
        m = self.get(namespace=namespace, metric_id=metric_id)
        if m is None:
            raise KeyError("not found")
        m = m.model_copy(
            update={"lifecycle": MetricLifecycle.PUBLISHED, "version": m.version + 1}
        )
        vec = self.embed(self._embed_text(m))
        self.t.put_item(Item=m.to_ddb_item(embedding=vec))
        return m

    def delete(self, *, namespace: str, metric_id: str) -> None:
        """Idempotent delete from DDB."""
        self.t.delete_item(
            Key={"pk": f"NS#{namespace}", "sk": f"METRIC#{metric_id}"}
        )
