package com.semanticlayer.ontop;

import java.util.Map;
import java.util.SortedSet;
import java.util.TreeSet;

/**
 * Stateless, shared parsing/lookup helpers over the project's custom
 * ontology-mapping JSON (the shape returned by
 * {@code lambda/neptune-tools/index.py::tool_get_ontology_from_neptune}).
 *
 * <p>Both {@link ObdaMappingGenerator} (OBDA text) and {@link DbMetadataGenerator}
 * (db-metadata JSON) consume the SAME ontology shape and previously carried
 * byte-for-byte identical copies of these helpers. They are centralized here so the
 * parsing/lookup rules live in exactly one place; the generators keep only their own
 * OUTPUT formatting.
 *
 * <p>Two correctness rules baked into this class (both differ from naive assumptions
 * and must be preserved exactly):
 * <ul>
 *   <li><b>Property→class linkage is by IRI nesting</b>: a property belongs to a class
 *       when {@code propIri.startsWith(classIri + "/")}. There is NO {@code domain}
 *       field in the data.</li>
 *   <li><b>Mapping values are dotted</b>: a class's {@code table} is the dotted
 *       {@code "database.table"} and a property's {@code column} is the dotted
 *       {@code "table.column"}. The bare name is always the LAST dotted segment.</li>
 * </ul>
 *
 * <p>All read accessors keep the original null / {@code instanceof Map} defensiveness:
 * a missing key, a non-map entry, or a missing field yields {@code null} rather than
 * throwing. Iteration accessors return {@link SortedSet}s so callers get deterministic,
 * input-order-independent ordering (the producer maps are unordered {@code Map.of}).
 *
 * <p>This class is final and has a private constructor: it is a pure utility holder
 * and is never instantiated.
 */
final class OntologyMappings {

    /** Utility class — never instantiated. */
    private OntologyMappings() {
    }

    /**
     * The sorted set of class IRIs declared in the ontology's {@code classes} sub-map.
     *
     * <p>Sorting makes downstream emission order (OBDA entries / db-metadata relations)
     * stable regardless of the unordered input map's iteration order.
     *
     * @param ontologyJson the full ontology document. A missing {@code classes} key is
     *                     treated as an empty map.
     * @return a {@link SortedSet} of class IRIs in natural (ascending) order; never null.
     */
    static SortedSet<String> sortedClassIris(final Map<String, Object> ontologyJson) {
        return new TreeSet<>(classesOf(ontologyJson).keySet());
    }

    /**
     * The sorted set of property IRIs that both (a) nest under the given class IRI
     * ({@code propIri.startsWith(classIri + "/")}) AND (b) carry a {@code column}
     * mapping (a non-null, non-blank dotted {@code "table.column"} value).
     *
     * <p>Properties without a column mapping are excluded here so callers do not have to
     * re-check; this mirrors the original generators, which skipped (and logged) such
     * properties. Logging of the skip remains the caller's responsibility — this method
     * only filters.
     *
     * @param ontologyJson the full ontology document. Missing {@code properties} /
     *                     {@code mappings} keys are treated as empty maps.
     * @param classIri     the class IRI whose mapped properties are wanted.
     * @return a {@link SortedSet} of qualifying property IRIs in natural order; never null.
     */
    static SortedSet<String> sortedPropertyIrisForClass(final Map<String, Object> ontologyJson,
                                                        final String classIri) {
        final Map<String, Object> properties = propertiesOf(ontologyJson);
        final String classPrefix = classIri + "/";
        final SortedSet<String> result = new TreeSet<>();
        for (final String propIri : new TreeSet<>(properties.keySet())) {
            if (!propIri.startsWith(classPrefix)) {
                continue;  // not nested under this class — different class.
            }
            final String column = columnFor(ontologyJson, propIri);
            if (column == null || column.isBlank()) {
                continue;  // no column mapping — not a usable property.
            }
            result.add(propIri);
        }
        return result;
    }

    /**
     * Pick the deterministic <i>subject column</i> for a class — the REAL bare column
     * used as the row-identity in the OBDA subject template ({@code <classIri/{subjectColumn}>})
     * and which therefore MUST also be a declared column of the relation.
     *
     * <p>This replaces the earlier synthetic {@code __pk} placeholder, which did not
     * exist in the real Athena table and caused Ontop to emit {@code R1.__PK}, which
     * Athena rejected with {@code COLUMN_NOT_FOUND}. Selecting a real mapped column
     * keeps the translated SQL resolvable against the actual table.
     *
     * <p>The chosen column is one of the class's OWN mapped property columns (so it is
     * guaranteed to also appear in the OBDA target triples and the db-metadata relation
     * columns). Selection is deterministic and preference-ordered:
     * <ol>
     *   <li>a bare column named exactly {@code id}, else one ending in {@code _id}
     *       (a natural key, e.g. {@code admin_code_id}); ties broken by sorted order;</li>
     *   <li>else a bare column named exactly {@code pk};</li>
     *   <li>else the first bare column in sorted property-IRI order.</li>
     * </ol>
     * Preferring {@code *_id}/{@code pk} keeps the subject a stable, ideally-unique
     * identifier.
     *
     * <p><b>KNOWN LIMITATION (class-level COUNT undercount):</b> when no key-like
     * column exists and selection falls through to preference 3 (the first sorted
     * column), a class-level COUNT may <i>undercount</i>. Ontop translates such a
     * count to roughly
     * {@code COUNT(*) FROM (SELECT DISTINCT <subjectCol> ... WHERE <subjectCol> IS NOT NULL)},
     * so any rows where the chosen subject column is NULL, or that share a duplicate
     * value (a non-unique column), collapse and are missed. The offline db-metadata we
     * generate declares no {@code uniqueConstraints}, so Ontop cannot know the column is
     * a key and conservatively applies the DISTINCT + IS-NOT-NULL. The {@code *_id}/{@code pk}
     * preference mitigates this for typical schemas (those columns are usually unique and
     * non-null), but accurate counts for non-key-mapped classes would require Ontop lenses
     * or explicit {@code uniqueConstraints} in the db-metadata (out of scope here).
     *
     * @param ontologyJson the full ontology document.
     * @param classIri     the class IRI whose subject column is wanted.
     * @return the chosen REAL bare column name, or {@code null} if the class has NO
     *         mapped property columns at all (an instance-less class that cannot form
     *         a subject and so must be skipped by callers).
     */
    static String subjectColumnFor(final Map<String, Object> ontologyJson, final String classIri) {
        // Collect the REAL bare columns of this class's mapped properties, in sorted
        // (property-IRI) order so ties resolve deterministically.
        final SortedSet<String> bareColumns = new TreeSet<>();
        for (final String propIri : sortedPropertyIrisForClass(ontologyJson, classIri)) {
            final String column = columnFor(ontologyJson, propIri);
            // sortedPropertyIrisForClass already filtered to non-blank column mappings.
            bareColumns.add(bareColumn(column));
        }
        if (bareColumns.isEmpty()) {
            return null;  // no mapped columns → no real subject → caller must skip the class.
        }

        // Preference 1: a natural key — exactly "id", else ending in "_id".
        String exactId = null;
        String suffixId = null;
        for (final String bare : bareColumns) {
            if (bare.equals("id")) {
                exactId = bare;  // strongest match.
                break;
            }
            if (suffixId == null && bare.endsWith("_id")) {
                suffixId = bare;  // first (sorted) "_id" column wins.
            }
        }
        if (exactId != null) {
            return exactId;
        }
        if (suffixId != null) {
            return suffixId;
        }

        // Preference 2: a column named exactly "pk".
        if (bareColumns.contains("pk")) {
            return "pk";
        }

        // Preference 3: the first bare column in sorted order.
        return bareColumns.first();
    }

    /**
     * Whether a bare subject column is "key-like" — exactly {@code id}, ending in
     * {@code _id}, or exactly {@code pk}. These are precisely the columns
     * {@link #subjectColumnFor} preferences 1 &amp; 2 select.
     *
     * <p>A key-like subject is treated as unique so {@link DbMetadataGenerator} can
     * declare a {@code uniqueConstraint} on it. That lets Ontop know the column is a
     * key and DROP the defensive {@code DISTINCT <col> ... WHERE <col> IS NOT NULL}
     * it otherwise wraps a class-level {@code COUNT} in — which undercounts when the
     * subject is a non-null unique key (the COUNT-undercount follow-up). A non-key
     * (preference-3) subject is NOT key-like, so it stays unconstrained and the
     * conservative DISTINCT is preserved.
     *
     * @param bareColumn the bare (un-dotted) column name; {@code null} yields {@code false}.
     * @return {@code true} when the column is key-like.
     */
    static boolean isKeyLikeSubject(final String bareColumn) {
        if (bareColumn == null) {
            return false;
        }
        return bareColumn.equals("id") || bareColumn.endsWith("_id") || bareColumn.equals("pk");
    }

    /**
     * The dotted {@code "database.table"} mapping value for a class, or {@code null}.
     *
     * @param ontologyJson the full ontology document.
     * @param classIri     the class IRI to look up in the {@code mappings} sub-map.
     * @return the dotted table value (e.g. {@code "normalized.admin_codes"}), or
     *         {@code null} if the class is unmapped or has no {@code table} field.
     */
    static String tableFor(final Map<String, Object> ontologyJson, final String classIri) {
        return mappingValue(mappingsOf(ontologyJson), classIri, "table");
    }

    /**
     * The dotted {@code "table.column"} mapping value for a property, or {@code null}.
     *
     * @param ontologyJson the full ontology document.
     * @param propIri      the property IRI to look up in the {@code mappings} sub-map.
     * @return the dotted column value (e.g. {@code "admin_codes.code_value"}), or
     *         {@code null} if the property is unmapped or has no {@code column} field.
     */
    static String columnFor(final Map<String, Object> ontologyJson, final String propIri) {
        return mappingValue(mappingsOf(ontologyJson), propIri, "column");
    }

    /**
     * Extract the bare (last-segment) name from a dotted mapping value.
     *
     * <p>Used for both a property's dotted {@code "table.column"} (yielding the bare
     * column) and, where relevant, a dotted {@code "db.table"}.
     *
     * @param dottedColumn the dotted mapping value, e.g. {@code "admin_codes.code_value"}.
     * @return the last dotted segment, e.g. {@code "code_value"}. If there is no dot the
     *         input is returned unchanged.
     */
    static String bareColumn(final String dottedColumn) {
        return dottedColumn.substring(dottedColumn.lastIndexOf('.') + 1);
    }

    /**
     * Split a dotted {@code "database.table"} value into a 2-part {@code [db, table]} array.
     *
     * <p>Only the LAST dot is treated as the {@code db}/{@code table} separator, so a
     * single-segment value (no dot) yields a 1-element array rather than throwing.
     *
     * @param dottedTable the dotted mapping value, e.g. {@code "normalized.admin_codes"}.
     * @return a String array like {@code ["normalized", "admin_codes"]}; or {@code ["x"]}
     *         when {@code dottedTable} has no dot.
     */
    static String[] splitDottedTable(final String dottedTable) {
        final int lastDot = dottedTable.lastIndexOf('.');
        if (lastDot < 0) {
            return new String[] {dottedTable};
        }
        return new String[] {dottedTable.substring(0, lastDot), dottedTable.substring(lastDot + 1)};
    }

    // ---- internal: sub-map access + single-field lookup ---------------------------

    /**
     * Read a single field from a class/property's mapping entry, with the original
     * null / {@code instanceof Map} defensiveness.
     *
     * @param mappings the {@code mappings} sub-map (iri → {table?, column?}).
     * @param iri      the class or property IRI to look up.
     * @param field    the field name, e.g. {@code "table"} or {@code "column"}.
     * @return the field value's {@code toString()}, or {@code null} if the IRI is
     *         unmapped, the entry is not a map, or the field is absent.
     */
    private static String mappingValue(final Map<String, Object> mappings, final String iri,
                                       final String field) {
        final Object entry = mappings.get(iri);
        if (!(entry instanceof Map)) {
            return null;
        }
        @SuppressWarnings("unchecked")
        final Object value = ((Map<String, Object>) entry).get(field);
        return value == null ? null : value.toString();
    }

    /**
     * Fetch the {@code classes} sub-map, defaulting to empty when absent.
     *
     * @param ontologyJson the full ontology document.
     * @return the {@code classes} sub-map (classIri → {label?, ...}); never null.
     */
    private static Map<String, Object> classesOf(final Map<String, Object> ontologyJson) {
        return subMap(ontologyJson, "classes");
    }

    /**
     * Fetch the {@code properties} sub-map, defaulting to empty when absent.
     *
     * @param ontologyJson the full ontology document.
     * @return the {@code properties} sub-map (propIri → {type, ...}); never null.
     */
    private static Map<String, Object> propertiesOf(final Map<String, Object> ontologyJson) {
        return subMap(ontologyJson, "properties");
    }

    /**
     * Fetch the {@code mappings} sub-map, defaulting to empty when absent.
     *
     * @param ontologyJson the full ontology document.
     * @return the {@code mappings} sub-map (iri → {table?, column?}); never null.
     */
    private static Map<String, Object> mappingsOf(final Map<String, Object> ontologyJson) {
        return subMap(ontologyJson, "mappings");
    }

    /**
     * Read a string-keyed nested sub-map, defaulting to an empty map when the key is
     * absent. The nested maps come straight from Jackson / {@code Map.of} and are always
     * string-keyed, so the unchecked cast is safe.
     *
     * @param ontologyJson the full ontology document.
     * @param key          the sub-map key, e.g. {@code "classes"}.
     * @return the requested sub-map, or an empty map; never null.
     */
    @SuppressWarnings("unchecked")
    private static Map<String, Object> subMap(final Map<String, Object> ontologyJson,
                                              final String key) {
        return (Map<String, Object>) ontologyJson.getOrDefault(key, Map.of());
    }
}
