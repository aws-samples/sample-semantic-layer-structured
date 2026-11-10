"""CRUD + lifecycle for governed rules, stored as RDF in Neptune.

Unlike ``MetricService`` there is NO DynamoDB projection: Neptune is the single
source of truth and the agent runtime embeds rule text at KNN-hydration time.

This service is deliberately thin. Validation, compilation and the publish gate
live in ``agents/shared/rule_*.py`` as pure functions and are injected here, so
the REST Lambda and the agent runtime share one implementation of the rules
that matter.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from agents.shared.rule_graph import RuleReadError
from agents.shared.rule_models import Rule
from services.rule_import import mined_rules_to_drafts


class RuleService:
    """Neptune-backed rule store with a server-enforced lifecycle."""

    def __init__(
        self,
        *,
        read_rules: Callable[..., List[Rule]],
        read_rules_partitioned: Callable[
            ..., Tuple[List[Rule], List[Dict[str, str]]]
        ],
        write_rule: Callable[..., None],
        delete_rule: Callable[..., None],
        publish: Callable[..., Rule],
        validate_fn: Callable[..., Any],
        read_artifact: Optional[Callable[[str], Dict[str, Any]]] = None,
    ) -> None:
        """Wire the injected collaborators.

        :param read_rules: ``(namespace=...) -> list[Rule]`` SPARQL reader.
            Strict: raises if ANY rule in the namespace is unreadable.
        :param read_rules_partitioned: ``(namespace=...) -> (rules, failures)``
            reader that isolates unreadable rules, so one corrupt rule cannot
            hide every healthy one.
        :param write_rule: ``(rule=...) -> None`` INSERT DATA writer (upsert).
        :param delete_rule: ``(namespace=..., rule_id=...) -> None``.
        :param publish: The publish gate, ``(rule=...) -> Rule``.
        :param validate_fn: The four-level validator,
            ``(rule=...) -> (list[ValidationReport], list[str])``.
        :param read_artifact: ``(key) -> dict`` reader for mined-rule artefacts
            on the artifacts bucket. Optional so a service wired only for
            authoring stays constructible; :meth:`import_mined` raises rather
            than degrading when it is absent.
        """
        self.read_rules = read_rules
        self.read_rules_partitioned = read_rules_partitioned
        self.write_rule = write_rule
        self.delete_rule = delete_rule
        self.publish = publish
        self.validate_fn = validate_fn
        self.read_artifact = read_artifact

    def list(self, *, namespace: str) -> List[Rule]:
        """Return every rule in ``namespace``, or raise if any is unreadable.

        Strict variant, kept for internal callers that need a complete set.
        The HTTP list endpoint uses :meth:`list_partitioned` instead.

        :param namespace: Ontology id owning the rules graph.
        :returns: All rules, any lifecycle.
        :raises RuleReadError: If any rule in the namespace cannot be read.
        """
        return self.read_rules(namespace=namespace)

    def list_partitioned(
        self, *, namespace: str,
    ) -> Tuple[List[Rule], List[Dict[str, str]]]:
        """Return every readable rule, plus a report of the unreadable ones.

        One corrupt rule used to return 500 for the whole namespace, blanking the
        Governed Rules tab. The failures are returned rather than logged: a rule
        that vanished from the list without a word would be exactly the silent
        fallback this codebase forbids.

        :param namespace: Ontology id owning the rules graph.
        :returns: ``(rules, failures)`` where each failure is
            ``{"rule_id", "error"}``.
        """
        return self.read_rules_partitioned(namespace=namespace)

    def get(self, *, namespace: str, rule_id: str) -> Optional[Rule]:
        """Return one rule, or None if absent.

        Reads through the partitioning reader so an unrelated corrupt rule does
        not block reading a healthy one. If the REQUESTED rule is the corrupt
        one, this raises instead of returning None — None becomes a 404, which
        would claim the rule does not exist when it does.

        :param namespace: Ontology id.
        :param rule_id: Rule identifier.
        :returns: The rule, or None when no rule with that id exists.
        :raises RuleReadError: If the requested rule exists but is unreadable.
        """
        rules, failures = self.read_rules_partitioned(namespace=namespace)
        for rule in rules:
            if rule.rule_id == rule_id:
                return rule
        for failure in failures:
            if failure.get("rule_id") == rule_id:
                raise RuleReadError(
                    f"rule {rule_id} exists in namespace {namespace} but cannot "
                    f"be read: {failure.get('error')}",
                    rule_id=rule_id,
                )
        return None

    def _require(self, *, namespace: str, rule_id: str) -> Rule:
        """Fetch a rule or raise. Fail loudly rather than returning a default.

        :param namespace: Ontology id.
        :param rule_id: Rule identifier.
        :returns: The rule.
        :raises KeyError: If no such rule exists.
        :raises RuleReadError: If the rule exists but is unreadable — distinct
            from KeyError so the caller does not report a corrupt rule as absent.
        """
        rule = self.get(namespace=namespace, rule_id=rule_id)
        if rule is None:
            raise KeyError(f"rule {rule_id} not found in namespace {namespace}")
        return rule

    def _assert_thresholds_unowned(self, *, rule: Rule) -> None:
        """Refuse a ``threshold_id`` another rule in the namespace already owns.

        Threshold ids name the bounds a human approves. Two rules claiming one id
        makes an approval ambiguous, and it is the payload shape that triggered
        the shared-threshold-node corruption before threshold IRIs were scoped.

        Checked against READABLE rules only. If an unreadable sibling happens to
        own the id the collision is missed, but blocking all authoring in a
        namespace because one unrelated rule is corrupt would be worse — and that
        rule is already reported loudly by the list endpoint.

        :param rule: The rule being created or updated.
        :returns: None.
        :raises ValueError: If any of the rule's threshold ids is already owned
            by a different rule, or repeated within this rule.
        """
        seen: Dict[str, str] = {}
        for threshold in rule.thresholds:
            if threshold.threshold_id in seen:
                raise ValueError(
                    f"threshold_id {threshold.threshold_id} appears more than "
                    f"once on rule {rule.rule_id}"
                )
            seen[threshold.threshold_id] = rule.rule_id

        existing, _failures = self.read_rules_partitioned(
            namespace=rule.namespace
        )
        for other in existing:
            # Exclude the rule being updated, or every edit would collide with
            # its own stored thresholds.
            if other.rule_id == rule.rule_id:
                continue
            for threshold in other.thresholds:
                if threshold.threshold_id in seen:
                    raise ValueError(
                        f"threshold_id {threshold.threshold_id} is already used "
                        f"by rule {other.rule_id} in namespace {rule.namespace}"
                    )

    def create(self, *, rule: Rule) -> Rule:
        """Persist a new rule, forced to DRAFT with no compiled artefact.

        Lifecycle is server-controlled: a client cannot publish by POSTing
        ``lifecycle="PUBLISHED"``. Publication only happens through
        :meth:`publish_rule`, which runs the four-level gate.

        :param rule: The rule to create.
        :returns: The stored rule.
        :raises ValueError: If a threshold id is already owned by another rule.
        """
        self._assert_thresholds_unowned(rule=rule)
        draft = rule.model_copy(
            update={"lifecycle": "DRAFT", "compiled_sparql": None}
        )
        self.write_rule(rule=draft)
        return draft

    def import_mined(self, *, ontology_id: str, key: str) -> List[Rule]:
        """Import a mined-rules artefact as DRAFT rules.

        :param ontology_id: The layer to import into.
        :param key: S3 key of the artefact, as reported by the pipeline's
            ``minedRulesKey``.
        :returns: The rules created.
        :raises RuntimeError: If no artifact reader was injected. Loud rather
            than an empty list, which would read as "the document had no rules".
        :raises ValueError: If the artefact is structurally invalid.
        """
        if self.read_artifact is None:
            raise RuntimeError('import_mined requires an artifact reader')
        drafts = mined_rules_to_drafts(
            mined=self.read_artifact(key), ontology_id=ontology_id
        )
        # Routed through create() rather than write_rule so import cannot bypass
        # the lifecycle even if the artefact claims PUBLISHED.
        return [self.create(rule=draft) for draft in drafts]

    def update(self, *, rule: Rule) -> Rule:
        """Replace an existing rule's body, resetting it to DRAFT.

        Any edit invalidates the compiled SPARQL — keeping it would let an
        edited rule execute the previously-approved logic.

        :param rule: The new rule body.
        :returns: The stored rule.
        :raises KeyError: If the rule does not exist.
        :raises ValueError: If a threshold id is already owned by another rule.
        """
        self._require(namespace=rule.namespace, rule_id=rule.rule_id)
        self._assert_thresholds_unowned(rule=rule)
        reset = rule.model_copy(
            update={"lifecycle": "DRAFT", "compiled_sparql": None}
        )
        self.write_rule(rule=reset)
        return reset

    def approve_threshold(
        self, *, namespace: str, rule_id: str, threshold_id: str, approver: str
    ) -> Rule:
        """Record human approval of one threshold value.

        This is the single action the article insists a human owns — the
        compiler refuses any threshold without an approver.

        :param namespace: Ontology id.
        :param rule_id: Rule identifier.
        :param threshold_id: Threshold to approve.
        :param approver: Identity of the approving human. Must be non-blank.
        :returns: The updated rule.
        :raises ValueError: If ``approver`` is blank.
        :raises KeyError: If the rule or threshold does not exist.
        """
        if not approver.strip():
            raise ValueError("approver must be a non-blank identity")
        rule = self._require(namespace=namespace, rule_id=rule_id)
        if rule.threshold_by_id(threshold_id=threshold_id) is None:
            raise KeyError(f"threshold {threshold_id} not found on rule {rule_id}")
        thresholds = [
            t.model_copy(update={"approved_by": approver})
            if t.threshold_id == threshold_id
            else t
            for t in rule.thresholds
        ]
        updated = rule.model_copy(update={"thresholds": thresholds})
        self.write_rule(rule=updated)
        return updated

    def approve(self, *, namespace: str, rule_id: str, approver: str) -> Rule:
        """Move a DRAFT rule to APPROVED, recording the reviewer.

        :param namespace: Ontology id.
        :param rule_id: Rule identifier.
        :param approver: Reviewer identity. Must be non-blank.
        :returns: The updated rule.
        :raises ValueError: If ``approver`` is blank.
        :raises KeyError: If the rule does not exist.
        """
        if not approver.strip():
            raise ValueError("approver must be a non-blank identity")
        rule = self._require(namespace=namespace, rule_id=rule_id)
        updated = rule.model_copy(
            update={"lifecycle": "APPROVED", "reviewed_by": approver}
        )
        self.write_rule(rule=updated)
        return updated

    def validate(self, *, namespace: str, rule_id: str) -> Dict[str, Any]:
        """Run the four-level ladder and return a JSON-serialisable report set.

        Read-only: a reviewer inspects all four levels here BEFORE attempting a
        publish, so the gate's refusal is never the first thing they learn.

        :param namespace: Ontology id.
        :param rule_id: Rule identifier.
        :returns: ``{"reports": [...], "missing_inputs": [...]}``.
        :raises KeyError: If the rule does not exist.
        """
        rule = self._require(namespace=namespace, rule_id=rule_id)
        reports, missing = self.validate_fn(rule=rule)
        return {
            "reports": [
                {"level": r.level, "passed": r.passed, "violations": r.violations}
                for r in reports
            ],
            "missing_inputs": missing,
        }

    def publish_rule(self, *, namespace: str, rule_id: str) -> Rule:
        """Run the publish gate and persist the result.

        Named ``publish_rule`` (not ``publish``) because ``self.publish`` is the
        injected gate callable.

        :param namespace: Ontology id.
        :param rule_id: Rule identifier.
        :returns: The PUBLISHED rule.
        :raises KeyError: If the rule does not exist.
        """
        rule = self._require(namespace=namespace, rule_id=rule_id)
        published = self.publish(rule=rule)
        self.write_rule(rule=published)
        return published

    def delete(self, *, namespace: str, rule_id: str) -> None:
        """Remove a rule and all its child nodes from the rules graph.

        :param namespace: Ontology id.
        :param rule_id: Rule identifier.
        """
        self.delete_rule(namespace=namespace, rule_id=rule_id)
