package com.semanticlayer.ontop;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.logging.Logger;

/**
 * Generates Ontop's offline <i>db-metadata</i> JSON from the project's custom
 * ontology-mapping JSON (the shape returned by
 * {@code lambda/neptune-tools/index.py::tool_get_ontology_from_neptune}).
 *
 * <p>Ontop's reformulator needs to know the relational schema (relations and
 * their columns) to rewrite SPARQL into SQL. Normally Ontop extracts that schema
 * by opening a JDBC connection at boot. This Lambda is <b>translate-only</b> — it
 * has NO Simba/Athena driver and no live connection — so we hand Ontop the schema
 * up front as a db-metadata JSON document derived from the SAME ontology mappings
 * that {@link ObdaMappingGenerator} uses. Task 4's {@code OntopTranslator} feeds
 * BOTH artifacts (this metadata + the OBDA mapping) into Ontop.
 *
 * <p>The output JSON shape (Ontop 5.5.0) is:
 * <pre>
 * {
 *   "metadata": {
 *     "dbmsProductName": "Amazon Athena",
 *     "driverName": "", "driverVersion": "",
 *     "quotationString": "\"", "extractionTime": ""
 *   },
 *   "relations": [
 *     { "name": ["db","table"],
 *       "columns": [ {"name":"col","datatype":"VARCHAR","isNullable":true}, ... ],
 *       "uniqueConstraints": [], "foreignKeys": [], "otherFunctionalDependencies": [] }
 *   ]
 * }
 * </pre>
 *
 * <p><b>Critical consistency with the OBDA mapping (Task 2):</b>
 * <ul>
 *   <li>The relation {@code name} is the dotted {@code "database.table"} split into a
 *       2-part {@code ["db","table"]} array (NO catalog) so it matches the OBDA
 *       {@code source} {@code SELECT * FROM db.table}.</li>
 *   <li>Columns are the BARE column name (last dotted segment of {@code "table.column"})
 *       — the same bare names the OBDA {@code target} references. The OBDA subject
 *       template's column (chosen by {@link OntologyMappings#subjectColumnFor}) is one
 *       of these mapped columns, so it is always present here too — no synthetic column
 *       is added.</li>
 * </ul>
 */
public class DbMetadataGenerator {

    private static final Logger LOG = Logger.getLogger(DbMetadataGenerator.class.getName());

    /** Shared Jackson serializer. ObjectMapper is thread-safe once configured. */
    private static final ObjectMapper MAPPER = new ObjectMapper();

    /** Athena exposes every column as a string at the JDBC layer; VARCHAR is the safe default. */
    private static final String DEFAULT_DATATYPE = "VARCHAR";

    /**
     * Convert an ontology-mapping JSON document into Ontop's offline db-metadata
     * JSON.
     *
     * @param ontologyJson the ontology document in the producer shape, with keys
     *                     {@code classes}, {@code properties} and {@code mappings}
     *                     (each a nested {@link Map}). A class is turned into a
     *                     relation only if {@code mappings[classIri].table} exists.
     * @return the db-metadata document serialized to a JSON {@link String}.
     * @throws IllegalArgumentException if {@code ontologyJson} is {@code null}.
     * @throws IllegalStateException    if Jackson fails to serialize the document.
     */
    public String generate(final Map<String, Object> ontologyJson) {
        if (ontologyJson == null) {
            throw new IllegalArgumentException("ontologyJson must not be null");
        }

        final List<Object> relations = new ArrayList<>();

        // Iterate in sorted IRI order (OntologyMappings sorts) so the emitted relation
        // order is stable regardless of the input map's iteration order.
        for (final String classIri : OntologyMappings.sortedClassIris(ontologyJson)) {
            // A class becomes a relation only if its mapping carries a dotted
            // "database.table". Skip (and log) anything unmapped.
            final String table = OntologyMappings.tableFor(ontologyJson, classIri);
            if (table == null || table.isBlank()) {
                LOG.warning("Skipping class with no table mapping: " + classIri);
                continue;
            }

            // Stay consistent with the OBDA generator: a class with no mapped property
            // columns has no real subject column, so the OBDA generator skips it. Skip
            // it here too so the relation set matches the mapping set exactly.
            final String subjectColumn = OntologyMappings.subjectColumnFor(ontologyJson, classIri);
            if (subjectColumn == null) {
                LOG.warning("Skipping class with no mapped property columns (no subject column): "
                    + classIri);
                continue;
            }

            relations.add(buildRelation(ontologyJson, classIri, table));
        }

        // foreignKeys/otherFunctionalDependencies stay empty: Ontop cannot infer them
        // offline. uniqueConstraints are now declared per-relation when the subject
        // column is key-like (id/*_id/pk) so Ontop drops the COUNT-undercounting
        // DISTINCT (todo item 3); a non-key subject still gets none.
        LOG.info("Emitting " + relations.size() + " relation(s); uniqueConstraints declared only "
            + "for key-like subject columns; foreignKeys/otherFunctionalDependencies empty "
            + "(Ontop cannot infer those offline).");

        return serialize(buildDocument(relations));
    }

    /**
     * Build a single relation entry for one mapped class.
     *
     * @param ontologyJson the full ontology document (passed through to the shared
     *                     {@link OntologyMappings} lookups for this class's properties).
     * @param classIri     the class IRI; properties nested under it
     *                     ({@code propIri.startsWith(classIri + "/")}) become columns.
     * @param table        the dotted {@code "database.table"} mapping value; split into a
     *                     2-part {@code ["db","table"]} name (NO catalog) to match the OBDA source.
     * @return an ordered {@link Map} representing the relation, ready for Jackson.
     */
    private Map<String, Object> buildRelation(final Map<String, Object> ontologyJson,
                                              final String classIri, final String table) {
        final List<Object> columns = new ArrayList<>();
        // Track emitted bare column names to skip duplicates. Multiple property IRIs can
        // map to the same underlying column (e.g. two properties both mapped to PARTY_ID).
        // Ontop loads db-metadata columns into an ImmutableMap keyed by name and throws
        // "Multiple entries with same key" if duplicates are present.
        final java.util.LinkedHashSet<String> seen = new java.util.LinkedHashSet<>();

        // Collect a column for every property nested under this class IRI that also has a
        // column mapping. sortedPropertyIrisForClass already filters to mapped properties
        // and returns them sorted, keeping the column order stable.
        for (final String propIri : OntologyMappings.sortedPropertyIrisForClass(ontologyJson, classIri)) {
            final String column = OntologyMappings.columnFor(ontologyJson, propIri);
            final String bare = OntologyMappings.bareColumn(column);
            if (seen.add(bare)) {
                // Declare the column with its REAL SQL datatype derived from the
                // property's xsd:range, NOT a blanket VARCHAR. A boolean column
                // (is_deleted etc.) declared VARCHAR made Ontop emit lower(STR(col))
                // over a physically-boolean Athena column → FUNCTION_NOT_FOUND /
                // TYPE_MISMATCH (gt-08). Telling Ontop the column is BOOLEAN lets it
                // translate a direct boolean comparison correctly. Object-property FK
                // columns keep VARCHAR (their value is templated into an IRI).
                columns.add(buildColumn(bare, sqlDatatypeFor(ontologyJson, propIri)));
            } else {
                LOG.warning("Skipping duplicate column '" + bare + "' for class " + classIri
                    + " (property " + propIri + " maps to the same column as a prior property).");
            }
        }

        // No synthetic column is added: the OBDA subject template's column is one of the
        // mapped property columns above, so it is already declared on this relation.

        // Relation name is the 2-part [db, table] split of the dotted value, with
        // NO catalog — matching the OBDA "SELECT * FROM db.table" source exactly.
        final Map<String, Object> relation = new LinkedHashMap<>();
        relation.put("name", List.of(OntologyMappings.splitDottedTable(table)));
        relation.put("columns", columns);
        // Declare a uniqueConstraint on the subject column when it is key-like
        // (id / *_id / pk). This lets Ontop treat the column as unique and DROP
        // the defensive DISTINCT + IS-NOT-NULL it otherwise wraps a class-level
        // COUNT in — which undercounts on a non-null unique key. A non-key
        // (preference-3) subject stays unconstrained (correctly conservative,
        // since duplicate/NULL values would genuinely collapse). (todo item 3)
        final String subjectColumn = OntologyMappings.subjectColumnFor(ontologyJson, classIri);
        if (OntologyMappings.isKeyLikeSubject(subjectColumn)) {
            final Map<String, Object> uniqueConstraint = new LinkedHashMap<>();
            uniqueConstraint.put("name", subjectColumn + "_pk");
            uniqueConstraint.put("determinants", List.of(subjectColumn));
            relation.put("uniqueConstraints", List.of(uniqueConstraint));
        } else {
            relation.put("uniqueConstraints", List.of());
        }
        relation.put("foreignKeys", List.of());
        relation.put("otherFunctionalDependencies", List.of());
        return relation;
    }

    /**
     * Build one column descriptor.
     *
     * @param name the BARE column name (no dotted prefix).
     * @return an ordered {@link Map} with {@code name}/{@code datatype}/{@code isNullable}.
     */
    private Map<String, Object> buildColumn(final String name, final String datatype) {
        final Map<String, Object> column = new LinkedHashMap<>();
        column.put("name", name);
        column.put("datatype", datatype == null || datatype.isBlank() ? DEFAULT_DATATYPE : datatype);
        column.put("isNullable", true);
        return column;
    }

    /**
     * Map a property's {@code xsd:range} to the SQL datatype Ontop should believe the
     * column has. Only BOOLEAN is promoted away from the {@code VARCHAR} default: a
     * physically-boolean Athena column declared VARCHAR makes Ontop generate string
     * functions (lower/STR) over a boolean, which Athena rejects (gt-08). Numeric/date
     * casts are handled explicitly in the SPARQL (xsd:decimal etc.), so VARCHAR stays
     * the safe default for everything else (Athena exposes most columns as strings at
     * the JDBC layer). An {@code owl:ObjectProperty} keeps VARCHAR — its column value is
     * templated into an IRI, never compared as a typed literal.
     *
     * @param ontologyJson the full ontology document.
     * @param propIri      the property whose backing column type we are declaring.
     * @return {@code "BOOLEAN"} for an {@code xsd:boolean}-ranged datatype property,
     *         otherwise {@code "VARCHAR"}.
     */
    private String sqlDatatypeFor(final Map<String, Object> ontologyJson, final String propIri) {
        if (OntologyMappings.isObjectProperty(ontologyJson, propIri)) {
            return DEFAULT_DATATYPE;
        }
        final String range = OntologyMappings.rangeFor(ontologyJson, propIri);
        if (range != null && range.endsWith("#boolean")) {
            return "BOOLEAN";
        }
        return DEFAULT_DATATYPE;
    }

    /**
     * Build the top-level db-metadata document (metadata header + relations).
     *
     * @param relations the rendered relation entries.
     * @return an ordered {@link Map} mirroring Ontop's db-metadata JSON shape.
     */
    private Map<String, Object> buildDocument(final List<Object> relations) {
        final Map<String, Object> metadata = new LinkedHashMap<>();
        metadata.put("dbmsProductName", "Amazon Athena");
        metadata.put("driverName", "");
        metadata.put("driverVersion", "");
        // quotationString is the identifier-quoting character Ontop emits in SQL.
        metadata.put("quotationString", "\"");
        metadata.put("extractionTime", "");

        final Map<String, Object> document = new LinkedHashMap<>();
        document.put("metadata", metadata);
        document.put("relations", relations);
        return document;
    }

    /**
     * Serialize a document map to a JSON string via Jackson.
     *
     * @param document the db-metadata document map.
     * @return the JSON string.
     * @throws IllegalStateException if Jackson serialization fails (fail loud — a
     *                               serialization error means a programming bug, not a
     *                               recoverable condition).
     */
    private String serialize(final Map<String, Object> document) {
        try {
            return MAPPER.writeValueAsString(document);
        } catch (final JsonProcessingException e) {
            throw new IllegalStateException("Failed to serialize db-metadata JSON", e);
        }
    }

}
