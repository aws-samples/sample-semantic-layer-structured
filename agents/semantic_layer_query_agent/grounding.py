"""Ontology-verified grounding gate for SQL-derived triples (Hybrid query agent).

This gate is the SQL analog of the VKG ``check_grounding`` gate. Where the VKG
gate parses SPARQL BGP triples and checks each predicate against its subject's
``rdfs:domain`` in the thin-VKG structural slice, this gate takes triples that
have ALREADY been extracted from generated SQL (by the SQL→ontology bridge,
Task 4) and applies the SAME grounding LOGIC against that slice.

The two lessons the grounding logic carries (copied, not imported):

1. **Grounding is _qualified_, not flat set-membership.** A predicate IRI valid
   on class A is NOT automatically valid when used against class B. We check each
   ``(subject_cls, predicate)`` pair against the predicate's ``rdfs:domain``
   edge(s) in the slice, walking ``rdfs:subClassOf`` ancestry so a predicate
   whose domain is a superclass of the subject's class still grounds. Where the
   subject class is unresolvable (``None``) or the predicate carries no domain
   edge in the slice, we fall back to slice-wide membership only (best-effort,
   ambiguous by construction) and do NOT flag it — degrade, don't loop.

2. **A predicate absent from the slice is a hard miss** (tagged
   ``property:<iri>``), while a predicate present-but-misused emits the
   ``property:<iri> on <class_iri>`` "valid on A, used on B" tag.

This module deliberately depends on the standard library and ``rdflib`` ONLY —
no shared or sibling-agent imports, and not even the bridge (it takes the
extracted triples as its input).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from rdflib import Graph, URIRef
from rdflib.namespace import RDFS

# Marker joining a misused predicate to the class it was (wrongly) applied to,
# in a missing tag: ``property:<iri> on <class_iri>``. Kept identical to the VKG
# reference gate so downstream classification can split on it the same way.
_ON = " on "


def _slice_lookups(
    graph: Graph,
) -> Tuple[Set[str], Dict[str, Set[str]], Dict[str, Set[str]]]:
    """Parse the slice graph into the grounding lookups.

    Copied verbatim (logic) from the VKG reference gate ``_slice_lookups`` so the
    two gates ground identically.

    Args:
        graph: The thin-VKG structural slice as an ``rdflib.Graph``.

    Returns:
        A ``(all_iris, domain_by_pred, subclass_parents)`` tuple:
          * ``all_iris`` — every IRI appearing anywhere in the slice (subject /
            predicate / object). Used for slice-wide membership (the fallback
            when a predicate's subject class is unresolvable).
          * ``domain_by_pred`` — ``{predicate_iri: {class_iri, ...}}`` from
            ``?p rdfs:domain ?c`` edges. Drives the qualified (triple-context)
            check. A predicate whose domain edge was dropped from the slice
            simply won't appear here and degrades to slice-wide membership.
          * ``subclass_parents`` — ``{class_iri: {parent_iri, ...}}`` from
            ``?c rdfs:subClassOf ?p`` so a predicate whose domain is a superclass
            of the subject's class still grounds.
    """
    all_iris: Set[str] = set()
    domain_by_pred: Dict[str, Set[str]] = {}
    subclass_parents: Dict[str, Set[str]] = {}
    # Iterate every triple once, harvesting membership + the two edge maps.
    for s, p, o in graph:
        if isinstance(s, URIRef):
            all_iris.add(str(s))
        if isinstance(p, URIRef):
            all_iris.add(str(p))
        if isinstance(o, URIRef):
            all_iris.add(str(o))
        if p == RDFS.domain and isinstance(s, URIRef) and isinstance(o, URIRef):
            domain_by_pred.setdefault(str(s), set()).add(str(o))
        if p == RDFS.subClassOf and isinstance(s, URIRef) and isinstance(o, URIRef):
            subclass_parents.setdefault(str(s), set()).add(str(o))
    return all_iris, domain_by_pred, subclass_parents


def _class_admitted(
    subject_cls: str,
    domains: Set[str],
    subclass_parents: Dict[str, Set[str]],
) -> bool:
    """Return True iff ``subject_cls`` or one of its ancestors is in ``domains``.

    Copied verbatim (logic) from the VKG reference gate ``_class_admitted``.

    Args:
        subject_cls: The IRI of the class the predicate was used against.
        domains: The predicate's ``rdfs:domain`` class IRIs from the slice.
        subclass_parents: ``{class_iri: {parent_iri, ...}}`` ancestry map.

    Returns:
        True when the subject class (or any transitive superclass) is one of the
        predicate's declared domains; False otherwise.
    """
    if subject_cls in domains:
        return True
    # Walk the subClassOf chain (bounded by the slice's edges; cycle-safe via
    # the ``seen`` guard so a subClassOf cycle can't loop forever).
    seen: Set[str] = set()
    frontier = list(subclass_parents.get(subject_cls, set()))
    while frontier:
        cls = frontier.pop()
        if cls in domains:
            return True
        if cls in seen:
            continue
        seen.add(cls)
        frontier.extend(subclass_parents.get(cls, set()))
    return False


def verify_sql_grounding(
    *,
    triples: List[Tuple[Optional[str], str]],
    slice_graph: Graph,
) -> List[str]:
    """Return the missing tags for SQL-derived triples not grounded in the slice.

    Applies the VKG grounding logic to triples extracted from generated SQL. An
    empty list means every triple is grounded.

    Grounding is **triple-context-aware**: a predicate is grounded only if its
    ``rdfs:domain`` (or, when no domain edge survives in the slice, slice-wide
    membership) admits the subject's class.

    Args:
        triples: The ``(subject_cls, predicate)`` pairs extracted from the SQL.
            ``subject_cls`` is the subject's class IRI, or ``None`` when the
            bridge could not resolve it.
        slice_graph: The thin-VKG structural slice as an ``rdflib.Graph``.

    Returns:
        A deduped (order-preserving) list of missing tags:
          * ``property:<iri>`` — predicate IRI absent from the slice.
          * ``property:<iri> on <class_iri>`` — predicate present in the slice
            but whose ``rdfs:domain`` does not admit the triple's subject class.
        An empty list means fully grounded.
    """
    all_iris, domain_by_pred, subclass_parents = _slice_lookups(slice_graph)

    missing: List[str] = []
    seen: Set[str] = set()

    for subject_cls, pred in triples:
        # Miss 1: the predicate itself is absent from the slice entirely.
        if pred not in all_iris:
            label = f"property:{pred}"
            if label not in seen:
                seen.add(label)
                missing.append(label)
            continue
        # Predicate exists in the slice. Only flag a qualified miss when we know
        # BOTH the subject's class AND the predicate's declared domain(s) — and
        # the domain (or an ancestor of the subject class) does not admit it.
        # When the subject class is None (unresolvable) or the predicate has no
        # domain edge in the slice, we degrade to slice-wide membership (already
        # satisfied above) and do NOT flag — degrade, don't loop.
        domains = domain_by_pred.get(pred)
        if subject_cls is not None and domains:
            if not _class_admitted(subject_cls, set(domains), subclass_parents):
                label = f"property:{pred}{_ON}{subject_cls}"
                if label not in seen:
                    seen.add(label)
                    missing.append(label)

    return missing
