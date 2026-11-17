package com.semanticlayer.ontop;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.logging.Logger;

/**
 * Generates Ontop's native OBDA text mapping from the project's custom
 * ontology-mapping JSON (the shape returned by
 * {@code lambda/neptune-tools/index.py::tool_get_ontology_from_neptune}).
 *
 * <p>Output is a single Ontop {@code [MappingDeclaration] @collection [[ ... ]]}
 * block with one {@code mappingId}/{@code source}/{@code target} entry per mapped
 * class. The result is consumed by Task 4's {@code OntopTranslator}, which feeds
 * it to Ontop 5.5.0's native mapping parser / reformulator.
 *
 * <p>Two correctness rules drive this generator (both differ from naive
 * assumptions and are enforced here):
 * <ul>
 *   <li><b>Property→class linkage is by IRI nesting</b>: a property belongs to a
 *       class when {@code propIri.startsWith(classIri + "/")}. There is NO
 *       {@code domain} field in the data.</li>
 *   <li><b>Column mapping values are dotted {@code "table.column"}</b>: the bare
 *       column used in the OBDA target template is the last dotted segment.</li>
 * </ul>
 */
public class ObdaMappingGenerator {

    private static final Logger LOG = Logger.getLogger(ObdaMappingGenerator.class.getName());

    /**
     * Convert an ontology-mapping JSON document into an Ontop native OBDA mapping
     * block.
     *
     * @param ontologyJson the ontology document in the producer shape, with keys
     *                     {@code classes}, {@code properties}, {@code mappings} and
     *                     {@code databases} (each a nested {@link Map} / {@link List}).
     * @return the OBDA mapping text: a {@code [MappingDeclaration] @collection [[ ... ]]}
     *         block containing one {@code mappingId}/{@code source}/{@code target} entry
     *         per mapped class. Classes (and properties) without a usable mapping are
     *         logged and omitted.
     */
    public String generate(final Map<String, Object> ontologyJson) {
        if (ontologyJson == null) {
            throw new IllegalArgumentException("ontologyJson must not be null");
        }

        final List<String> entries = new ArrayList<>();

        // Iterate in sorted IRI order (OntologyMappings sorts) so the emitted OBDA
        // entry order is stable regardless of the input map's iteration order.
        for (final String classIri : OntologyMappings.sortedClassIris(ontologyJson)) {
            // A class is mappable only if its mapping entry carries a dotted
            // "database.table". Skip (and log) anything unmapped — fail loud about
            // the omission rather than emitting a broken mapping.
            final String table = OntologyMappings.tableFor(ontologyJson, classIri);
            if (table == null || table.isBlank()) {
                LOG.warning("Skipping class with no table mapping: " + classIri);
                continue;
            }

            // The subject template uses REAL mapped columns (NOT the old synthetic __pk):
            // the ontology's DECLARED primary key when it has one, else a single
            // name-heuristic column. Ontop treats the subject as ROW IDENTITY, so this
            // choice decides whether every property access joins on the true key or
            // self-joins on a value column — the single largest measured drag on BIRD EX
            // (0.0500 vs 0.1313). A class with no mapped property columns cannot form a
            // subject IRI at all — it is instance-less and unqueryable — so skip and log.
            final List<String> subjectColumns =
                OntologyMappings.subjectColumnsFor(ontologyJson, classIri);
            if (subjectColumns.isEmpty()) {
                LOG.warning("Skipping class with no mapped property columns (no subject column): "
                    + classIri);
                continue;
            }
            if (!OntologyMappings.hasDeclaredKey(ontologyJson, classIri)) {
                // Loud about the guess: a wrong identity yields plausible SQL with wrong
                // join semantics, which is invisible downstream without this line.
                LOG.warning("No primaryKey declared for class " + classIri
                    + " — falling back to the NAME HEURISTIC subject column '"
                    + subjectColumns.get(0) + "'. Row identity is a GUESS; if it is not a "
                    + "real key, property access self-joins on a value column and results "
                    + "are silently wrong.");
            }

            // Collect property triples for every property nested under this class IRI
            // that also has a column mapping. sortedPropertyIrisForClass already filters
            // to mapped properties and returns them in sorted order, keeping the
            // property-triple order within an entry stable.
            final List<String> propertyTriples = new ArrayList<>();
            // Computed source columns for prefix-transform FKs (see below): name → SQL expr.
            final Map<String, String> computedColumns = new java.util.LinkedHashMap<>();
            for (final String propIri : OntologyMappings.sortedPropertyIrisForClass(ontologyJson, classIri)) {
                final String column = OntologyMappings.columnFor(ontologyJson, propIri);
                final String bareColumn = OntologyMappings.bareColumn(column);
                // An owl:ObjectProperty is an FK relationship: its object must be an
                // IRI that matches the TARGET class's subject template
                // (<TargetClassIri/{fkColumn}>), NOT a bare literal {fkColumn}. With a
                // literal object, a SPARQL join like `?cov hasHolding ?h . ?h a Holding`
                // cannot unify ?h (a string) with Holding's IRI subjects, so Ontop
                // reformulates to EMPTY (no NativeNode) — the gt-03 failure. Emitting
                // the FK as the target's subject-IRI template makes the join resolve:
                // the FK column value templates into the same IRI shape the target
                // class's subject uses, so Ontop generates the underlying FK SQL join.
                final String rangeClassIri =
                    OntologyMappings.isObjectProperty(ontologyJson, propIri)
                        ? OntologyMappings.rangeFor(ontologyJson, propIri)
                        : null;
                if (rangeClassIri != null && !rangeClassIri.isBlank()) {
                    // Ontop unifies an FK template with the target's SUBJECT template
                    // STRUCTURALLY. A single FK column therefore cannot match a target
                    // whose declared key is COMPOSITE (<Target/{a};{b}>), and the join
                    // reformulates to EMPTY — zero rows, no error. Emitting the FK as a
                    // plain literal instead at least keeps the value queryable and makes
                    // the un-joinable edge visible in the log rather than silently
                    // returning nothing.
                    final List<String> targetKey =
                        OntologyMappings.subjectColumnsFor(ontologyJson, rangeClassIri);
                    if (targetKey.size() > 1) {
                        LOG.warning("FK property " + propIri + " references " + rangeClassIri
                            + ", whose declared key is COMPOSITE " + targetKey
                            + ", but the FK maps a single column '" + bareColumn
                            + "'. A single-column IRI template cannot unify with a composite "
                            + "subject template, so this join would reformulate to EMPTY. "
                            + "Emitting the FK as a literal; declare the full composite FK to "
                            + "make the join resolvable.");
                        propertyTriples.add("<" + propIri + "> {" + bareColumn + "}");
                        continue;
                    }
                    // FK key-prefix transform: when the bridge/child FK stores the id
                    // UNPREFIXED but the target PK is PREFIXED (coverage.party_id
                    // 'PARTY000042' vs party.party_id 'PARTY#PARTY000042'), the comment
                    // documents CONCAT('PARTY#', fk). Ontop matches FK templates to the
                    // target SUBJECT template STRUCTURALLY (same column expression), so
                    // baking the prefix into the template literal alone is NOT enough —
                    // it must be a COMPUTED SOURCE COLUMN whose values already equal the
                    // target PK, referenced by a template structurally identical to the
                    // target's subject. We add `CONCAT('PARTY#', fk) AS fk__ref` to the
                    // source SQL and template the FK as <Target/{fk__ref}>. Prefix VALUE
                    // is read from the comment (layer-agnostic). No prefix → plain column.
                    final String prefix = OntologyMappings.concatPrefixFor(ontologyJson, propIri);
                    if (!prefix.isEmpty()) {
                        final String ref = bareColumn + "__ref";
                        computedColumns.put(ref,
                            "CONCAT('" + prefix + "', " + bareColumn + ") AS " + ref);
                        propertyTriples.add(
                            "<" + propIri + "> <" + rangeClassIri + "/{" + ref + "}>");
                    } else {
                        propertyTriples.add(
                            "<" + propIri + "> <" + rangeClassIri + "/{" + bareColumn + "}>");
                    }
                } else {
                    propertyTriples.add("<" + propIri + "> {" + bareColumn + "}");
                }
            }

            entries.add(buildEntry(classIri, table, subjectColumns, propertyTriples,
                                   computedColumns));
        }

        return wrapCollection(entries);
    }

    /**
     * Build one OBDA mapping entry (mappingId / source / target) for a single class.
     *
     * @param classIri        the class IRI; used for the mappingId, the subject
     *                        template and the {@code rdf:type} (the {@code a ...}) triple.
     * @param table           the dotted {@code "database.table"} mapping value for the
     *                        class; emitted verbatim (2-part) into the source SQL with
     *                        NO catalog prefix (catalog is applied at execution time via
     *                        the Athena QueryExecutionContext).
     * @param subjectColumns  the REAL bare columns forming the row-identity in the subject
     *                        template, from {@link OntologyMappings#subjectColumnsFor};
     *                        each is guaranteed to be one of this class's mapped columns.
     *                        More than one means a COMPOSITE declared key.
     * @param propertyTriples already-rendered property triples of the form
     *                        {@code <propIri> {bareColumn}}.
     * @return the textual OBDA entry: a {@code mappingId} line, a {@code source} line
     *         and a {@code target} line.
     */
    private String buildEntry(final String classIri, final String table,
                              final List<String> subjectColumns,
                              final List<String> propertyTriples,
                              final Map<String, String> computedColumns) {
        // Subject template references REAL mapped columns of the relation, so Ontop emits
        // SQL that resolves against the actual Athena table (no synthetic __pk). A
        // composite key renders as <Class/{a};{b}>: the ';' is a literal separator inside
        // the IRI path, so distinct column-value pairs cannot collide into one IRI (which
        // would merge unrelated rows into a single subject). Junction tables — precisely
        // the ones a single-column guess gets most wrong — need this.
        final StringBuilder subjectTemplate = new StringBuilder();
        for (final String column : subjectColumns) {
            if (subjectTemplate.length() > 0) {
                subjectTemplate.append(';');
            }
            subjectTemplate.append('{').append(column).append('}');
        }
        final String subject = "<" + classIri + "/" + subjectTemplate + ">";

        // target is a Turtle-like template: subject, rdf:type triple, then one
        // property triple per mapped column, all separated by " ; " and ended
        // with " .".
        final StringBuilder target = new StringBuilder();
        target.append(subject).append(" a <").append(classIri).append(">");
        for (final String triple : propertyTriples) {
            target.append(" ; ").append(triple);
        }
        target.append(" .");

        // Source SQL: `SELECT *` plus any computed prefix-transform columns (e.g.
        // `CONCAT('PARTY#', party_id) AS party_id__ref`) so a prefix-transform FK
        // template can reference a column whose values already equal the target PK —
        // the structural shape Ontop needs to reformulate the join. With none, it's a
        // plain `SELECT * FROM table` (unchanged for the common case).
        final String selectList = computedColumns.isEmpty()
            ? "*"
            : "*, " + String.join(", ", computedColumns.values());

        // mappingId must be unique within the collection; the class IRI is unique.
        final StringBuilder entry = new StringBuilder();
        entry.append("mappingId\t").append(classIri).append("\n");
        // 2-part source, no catalog prefix.
        // Quote the identifier: an unquoted reserved word (e.g. `sets`, `Match`) makes
        // JSqlParser fail, which sends Ontop down its live-DB "black box view" path and
        // breaks reformulator construction entirely. See
        // OntologyMappings.quoteDottedTable for the full failure mode.
        entry.append("source\t\tSELECT ").append(selectList).append(" FROM ")
             .append(OntologyMappings.quoteDottedTable(table)).append("\n");
        entry.append("target\t\t").append(target);
        return entry.toString();
    }

    /**
     * Wrap the per-class mapping entries in Ontop's native
     * {@code [MappingDeclaration] @collection [[ ... ]]} envelope.
     *
     * @param entries the rendered per-class entries (each a mappingId/source/target
     *                block).
     * @return the complete OBDA mapping text. When {@code entries} is empty the
     *         envelope is still emitted (an empty but structurally valid collection).
     */
    private String wrapCollection(final List<String> entries) {
        final StringBuilder obda = new StringBuilder();
        obda.append("[MappingDeclaration] @collection [[\n");
        // Ontop separates entries within the collection by a blank line.
        obda.append(String.join("\n\n", entries));
        if (!entries.isEmpty()) {
            obda.append("\n");
        }
        obda.append("]]\n");
        return obda.toString();
    }
}
