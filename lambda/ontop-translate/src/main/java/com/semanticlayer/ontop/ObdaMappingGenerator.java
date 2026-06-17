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

            // The subject template uses a REAL mapped column (NOT the old synthetic
            // __pk). A class with no mapped property columns cannot form a subject IRI
            // — it is instance-less and unqueryable — so skip (and log) it.
            final String subjectColumn = OntologyMappings.subjectColumnFor(ontologyJson, classIri);
            if (subjectColumn == null) {
                LOG.warning("Skipping class with no mapped property columns (no subject column): "
                    + classIri);
                continue;
            }

            // Collect property triples for every property nested under this class IRI
            // that also has a column mapping. sortedPropertyIrisForClass already filters
            // to mapped properties and returns them in sorted order, keeping the
            // property-triple order within an entry stable.
            final List<String> propertyTriples = new ArrayList<>();
            for (final String propIri : OntologyMappings.sortedPropertyIrisForClass(ontologyJson, classIri)) {
                final String column = OntologyMappings.columnFor(ontologyJson, propIri);
                final String bareColumn = OntologyMappings.bareColumn(column);
                propertyTriples.add("<" + propIri + "> {" + bareColumn + "}");
            }

            entries.add(buildEntry(classIri, table, subjectColumn, propertyTriples));
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
     * @param subjectColumn   the REAL bare column used as the row-identity in the subject
     *                        template; chosen by {@link OntologyMappings#subjectColumnFor}
     *                        and guaranteed to be one of this class's mapped columns.
     * @param propertyTriples already-rendered property triples of the form
     *                        {@code <propIri> {bareColumn}}.
     * @return the textual OBDA entry: a {@code mappingId} line, a {@code source} line
     *         and a {@code target} line.
     */
    private String buildEntry(final String classIri, final String table,
                              final String subjectColumn, final List<String> propertyTriples) {
        // Subject template references a REAL mapped column of the relation, so Ontop
        // emits SQL that resolves against the actual Athena table (no synthetic __pk).
        final String subject = "<" + classIri + "/{" + subjectColumn + "}>";

        // target is a Turtle-like template: subject, rdf:type triple, then one
        // property triple per mapped column, all separated by " ; " and ended
        // with " .".
        final StringBuilder target = new StringBuilder();
        target.append(subject).append(" a <").append(classIri).append(">");
        for (final String triple : propertyTriples) {
            target.append(" ; ").append(triple);
        }
        target.append(" .");

        // mappingId must be unique within the collection; the class IRI is unique.
        final StringBuilder entry = new StringBuilder();
        entry.append("mappingId\t").append(classIri).append("\n");
        // 2-part source, no catalog prefix.
        entry.append("source\t\tSELECT * FROM ").append(table).append("\n");
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
