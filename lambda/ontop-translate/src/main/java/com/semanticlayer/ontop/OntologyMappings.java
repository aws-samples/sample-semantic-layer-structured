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
     * The DECLARED primary-key columns of a class, in declaration order, or an empty
     * list when the ontology declares none.
     *
     * <p>Read from the class's {@code mappings} entry field {@code primaryKey}, a
     * comma-separated list of dotted {@code "table.column"} (or bare) names produced
     * from the source schema's real key — {@code PRAGMA table_info(...).pk} for the
     * SQLite-sourced benchmark layers, the authored key for curated layers. Declaring
     * the key is the ONLY reliable way to know row identity: Glue/S3-Tables expose no
     * primary-key metadata, so nothing downstream can recover it by inspection.
     *
     * <p>Columns not actually mapped by one of this class's properties are DROPPED: the
     * subject template's columns must also be declared columns of the relation, or Ontop
     * emits SQL referencing a column the db-metadata does not contain
     * ({@code COLUMN_NOT_FOUND}). A dropped column is logged by the caller.
     *
     * @param ontologyJson the full ontology document.
     * @param classIri     the class IRI whose declared key is wanted.
     * @return the declared key's bare column names, order-preserving and de-duplicated,
     *         restricted to this class's mapped columns; never null, empty when the
     *         ontology declares no key.
     */
    static java.util.List<String> declaredKeyColumnsFor(final Map<String, Object> ontologyJson,
                                                       final String classIri) {
        final String declared = mappingValue(mappingsOf(ontologyJson), classIri, "primaryKey");
        if (declared == null || declared.isBlank()) {
            return java.util.List.of();
        }
        // Only columns this class actually maps can appear in the subject template.
        final SortedSet<String> mapped = mappedBareColumns(ontologyJson, classIri);
        final java.util.LinkedHashSet<String> keyColumns = new java.util.LinkedHashSet<>();
        for (final String raw : declared.split(",")) {
            final String bare = bareColumn(raw.trim());
            if (bare != null && !bare.isBlank() && mapped.contains(bare)) {
                keyColumns.add(bare);
            }
        }
        return java.util.List.copyOf(keyColumns);
    }

    /**
     * The bare column names mapped by a class's properties, sorted.
     *
     * @param ontologyJson the full ontology document.
     * @param classIri     the class IRI whose mapped columns are wanted.
     * @return the sorted bare column names; never null.
     */
    static SortedSet<String> mappedBareColumns(final Map<String, Object> ontologyJson,
                                               final String classIri) {
        final SortedSet<String> bareColumns = new TreeSet<>();
        for (final String propIri : sortedPropertyIrisForClass(ontologyJson, classIri)) {
            bareColumns.add(bareColumn(columnFor(ontologyJson, propIri)));
        }
        return bareColumns;
    }

    /**
     * The row-identity columns for a class's OBDA subject template — the DECLARED key
     * when the ontology has one, else a single heuristically-chosen column.
     *
     * <p>This is the fix for the measured mapping defect behind BIRD's low EX. Ontop
     * treats the subject template as row identity, so every property access becomes a
     * self-join on those columns. When identity is guessed by NAME
     * ({@link #subjectColumnFor}) the guess lands on a VALUE column often enough to
     * dominate the score: over the 2026-08-09 BIRD run, 23/75 tables fell through to the
     * alphabetical fallback and 113/270 executed rows self-joined on a non-key column
     * ({@code aboutme}, a free-text bio; {@code amount}; {@code avgscrmath}). Rows whose
     * SQL was clean scored EX 0.1313 against 0.0500 for the self-joining rows.
     *
     * <p>A COMPOSITE declared key yields a multi-column template
     * ({@code <Class/{a};{b}>}), which is the correct identity for a junction table —
     * exactly the tables whose single-column guess is most wrong.
     *
     * @param ontologyJson the full ontology document.
     * @param classIri     the class IRI whose subject columns are wanted.
     * @return the declared key columns, else a single-element list holding the heuristic
     *         column, else an EMPTY list when the class maps no columns at all (an
     *         instance-less class that callers must skip).
     */
    static java.util.List<String> subjectColumnsFor(final Map<String, Object> ontologyJson,
                                                    final String classIri) {
        final java.util.List<String> declared = declaredKeyColumnsFor(ontologyJson, classIri);
        if (!declared.isEmpty()) {
            return declared;
        }
        final String heuristic = subjectColumnFor(ontologyJson, classIri);
        return heuristic == null ? java.util.List.of() : java.util.List.of(heuristic);
    }

    /**
     * Whether a class's row identity is a DECLARED key rather than a name guess.
     *
     * <p>Gates {@code uniqueConstraints} in the generated db-metadata. A uniqueness
     * claim licenses Ontop to drop de-duplication, so asserting it from a NAME PATTERN
     * is unsound: the previous rule declared {@code *_id} unique and was measurably
     * false on {@code trans.account_id} / {@code disp.account_id} / {@code order.account_id},
     * where the column is a many-side FK. Only a declared key earns the constraint.
     *
     * @param ontologyJson the full ontology document.
     * @param classIri     the class IRI to test.
     * @return {@code true} when the ontology declares a usable key for the class.
     */
    static boolean hasDeclaredKey(final Map<String, Object> ontologyJson, final String classIri) {
        return !declaredKeyColumnsFor(ontologyJson, classIri).isEmpty();
    }

    /**
     * Pick the deterministic <i>subject column</i> for a class — the REAL bare column
     * used as the row-identity in the OBDA subject template ({@code <classIri/{subjectColumn}>})
     * and which therefore MUST also be a declared column of the relation.
     *
     * <p><b>Prefer {@link #subjectColumnsFor}</b>, which consults the ontology's DECLARED
     * key first. This method is the NAME-HEURISTIC fallback used only when no key is
     * declared, and its guess is wrong often enough to dominate execution accuracy (see
     * {@link #subjectColumnsFor}).
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
     * <p><b>KNOWN LIMITATION — this heuristic corrupts joins, not just counts.</b> The
     * chosen column becomes ROW IDENTITY, so when the guess lands on a value column
     * every property access self-joins on it and the query silently returns wrong rows.
     * Measured over the 2026-08-09 BIRD run: 42% of executed rows self-joined on a
     * non-key column, scoring EX 0.0500 against 0.1313 for the rows with clean SQL.
     * A class-level COUNT additionally <i>undercounts</i>: Ontop translates it to roughly
     * {@code COUNT(*) FROM (SELECT DISTINCT <subjectCol> ... WHERE <subjectCol> IS NOT NULL)},
     * so NULL or duplicate values collapse. {@link DbMetadataGenerator} only declares a
     * {@code uniqueConstraint} (which lets Ontop drop that defensive DISTINCT) for a
     * DECLARED key — never for a name guess, since the previous name-pattern rule was
     * measurably false on many-side FK columns like {@code trans.account_id}.
     *
     * <p>The real fix is to DECLARE the key: see {@link #subjectColumnsFor} and
     * {@link #declaredKeyColumnsFor}. This heuristic remains only as the fallback for
     * layers whose ontology carries no {@code primaryKey}.
     *
     * @param ontologyJson the full ontology document.
     * @param classIri     the class IRI whose subject column is wanted.
     * @return the chosen REAL bare column name, or {@code null} if the class has NO
     *         mapped property columns at all (an instance-less class that cannot form
     *         a subject and so must be skipped by callers).
     */
    static String subjectColumnFor(final Map<String, Object> ontologyJson, final String classIri) {
        // The REAL bare columns of this class's mapped properties, sorted so ties
        // resolve deterministically.
        final SortedSet<String> bareColumns = mappedBareColumns(ontologyJson, classIri);
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
     * The {@code rdfs:comment} text of a class or property, or {@code ""} when absent.
     *
     * <p>Comments are authored by the metadata/ontology agent and carry the
     * layer-specific join/transform knowledge (e.g. a key-prefix transform) the OBDA
     * generator reads at translate time — never hard-coded here.
     *
     * @param ontologyJson the full ontology document.
     * @param iri          the class or property IRI (lives on {@code classes} or
     *                     {@code properties}).
     * @return the comment string, or {@code ""}.
     */
    static String commentFor(final Map<String, Object> ontologyJson, final String iri) {
        for (final String sub : new String[] {"properties", "classes"}) {
            final Map<String, Object> m = subMap(ontologyJson, sub);
            final Object entry = m.get(iri);
            if (entry instanceof Map) {
                @SuppressWarnings("unchecked")
                final Object c = ((Map<String, Object>) entry).get("comment");
                if (c != null && !c.toString().isBlank()) {
                    return c.toString();
                }
            }
        }
        return "";
    }

    // Matches a key-prefix transform documented in an FK property/PK comment, e.g.
    // "join via CONCAT('PARTY#', child.party_id) = party.party_id". The first
    // CONCAT string-literal argument is the prefix the UNPREFIXED FK value must gain
    // to match the PREFIXED target subject. Single OR double quotes; the literal may
    // contain any non-quote chars (e.g. 'PARTY#', 'POLICY#'). LAYER-AGNOSTIC: the
    // prefix VALUE is read from the comment data, never hard-coded.
    private static final java.util.regex.Pattern _CONCAT_PREFIX = java.util.regex.Pattern.compile(
        "CONCAT\\s*\\(\\s*['\"]([^'\"]+)['\"]", java.util.regex.Pattern.CASE_INSENSITIVE);

    /**
     * Extract the FK key-prefix transform for an object property, or {@code ""}.
     *
     * <p>When a bridge/child FK stores an id UNPREFIXED but the target class's PK is
     * PREFIXED (e.g. {@code coverage.party_id = 'PARTY000042'} vs
     * {@code party.party_id = 'PARTY#PARTY000042'}), the ontology comment documents
     * the join as {@code CONCAT('PARTY#', child.party_id) = party.party_id}. Without
     * applying that prefix, the FK IRI template {@code <Party/{party_id}>} →
     * {@code <Party/PARTY000042>} can never unify with the target subject
     * {@code <Party/PARTY#PARTY000042>}, so Ontop reformulates the join to EMPTY (the
     * gt-03/gt-04 failure). We parse the prefix literal from the property's comment
     * (or the target PK's comment as a fallback) and return it so the OBDA template
     * can bake it in: {@code <Party/PARTY#{party_id}>}.
     *
     * @param ontologyJson the full ontology document.
     * @param propIri      the FK object-property IRI.
     * @return the prefix literal (e.g. {@code "PARTY#"}), or {@code ""} when the
     *         comment declares no such transform.
     */
    static String concatPrefixFor(final Map<String, Object> ontologyJson, final String propIri) {
        // 1) The FK object property's own comment (some layers annotate it here).
        String prefix = _firstConcatPrefix(commentFor(ontologyJson, propIri));
        if (!prefix.isEmpty()) {
            return prefix;
        }
        // 2) The SIBLING DATATYPE property mapping to the SAME column. The transform is
        //    typically authored on the FK *column* property (e.g. `Coverage/party_id`
        //    "JOIN ... CONCAT('PARTY#', coverage.party_id) = party.party_id"), NOT on
        //    the object property (`Coverage/hasParty`, "Links Coverage to Party"). Find
        //    a property mapped to the same dotted column and read its comment.
        final String fkColumn = columnFor(ontologyJson, propIri);
        if (fkColumn != null && !fkColumn.isBlank()) {
            final Map<String, Object> mappings = mappingsOf(ontologyJson);
            for (final String otherIri : mappings.keySet()) {
                if (otherIri.equals(propIri)) {
                    continue;
                }
                if (fkColumn.equals(columnFor(ontologyJson, otherIri))) {
                    prefix = _firstConcatPrefix(commentFor(ontologyJson, otherIri));
                    if (!prefix.isEmpty()) {
                        return prefix;
                    }
                }
            }
        }
        return "";
    }

    /** First {@code CONCAT('PREFIX', …)} literal in {@code comment}, or {@code ""}. */
    private static String _firstConcatPrefix(final String comment) {
        final java.util.regex.Matcher m = _CONCAT_PREFIX.matcher(comment == null ? "" : comment);
        return m.find() ? m.group(1) : "";
    }

    /** OWL type IRI of an object/datatype property (the {@code owl:…Property}), or {@code null}. */
    private static final String OWL_OBJECT_PROPERTY =
        "http://www.w3.org/2002/07/owl#ObjectProperty";

    /**
     * True when {@code propIri} is an {@code owl:ObjectProperty} (an FK relationship
     * between two classes) rather than an {@code owl:DatatypeProperty} (a column value).
     *
     * <p>Object properties must be emitted in the OBDA mapping with an IRI-template
     * object that matches the referenced class's subject template, so Ontop can
     * reformulate the join. Datatype properties are emitted as bare literals. The type
     * lives on the {@code properties} sub-map's {@code type} field (set from the
     * {@code rdf:type owl:…Property} triple by {@code tool_get_ontology_from_neptune}).
     *
     * @param ontologyJson the full ontology document.
     * @param propIri      the property IRI.
     * @return {@code true} iff the property is declared an {@code owl:ObjectProperty}.
     */
    static boolean isObjectProperty(final Map<String, Object> ontologyJson, final String propIri) {
        final Map<String, Object> properties = propertiesOf(ontologyJson);
        final Object entry = properties.get(propIri);
        if (!(entry instanceof Map)) {
            return false;
        }
        @SuppressWarnings("unchecked")
        final Object type = ((Map<String, Object>) entry).get("type");
        return OWL_OBJECT_PROPERTY.equals(type == null ? null : type.toString());
    }

    /**
     * The {@code rdfs:range} target-class IRI of an object property, or {@code null}.
     *
     * <p>For an FK {@code owl:ObjectProperty} (e.g. {@code …/Coverage/hasHolding}) the
     * range is the referenced class IRI (e.g. {@code …/Holding}). Captured on the
     * {@code properties} sub-map's {@code range} field by
     * {@code tool_get_ontology_from_neptune}.
     *
     * @param ontologyJson the full ontology document.
     * @param propIri      the property IRI.
     * @return the range target-class IRI, or {@code null} if absent.
     */
    static String rangeFor(final Map<String, Object> ontologyJson, final String propIri) {
        final Map<String, Object> properties = propertiesOf(ontologyJson);
        final Object entry = properties.get(propIri);
        if (!(entry instanceof Map)) {
            return null;
        }
        @SuppressWarnings("unchecked")
        final Object range = ((Map<String, Object>) entry).get("range");
        return range == null ? null : range.toString();
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

    /**
     * Render a dotted {@code "database.table"} value as a double-quoted SQL identifier
     * pair for the OBDA {@code source} query, e.g. {@code "db"."sets"}.
     *
     * <p><b>Why quoting is mandatory.</b> Ontop parses every mapping's {@code source}
     * SQL with JSqlParser. When a segment is a SQL reserved word the parse FAILS, and
     * Ontop then falls back to treating the source as an opaque "black box view" whose
     * metadata it must fetch from a live database — but the offline configuration has
     * no parent metadata provider, so it throws
     * {@code UnsupportedOperationException: Has no parent provider. Should not have
     * been called} from {@code JsonSerializedMetadataProvider.getBlackBoxView}. That
     * surfaced as "Failed to build Ontop reformulator", failing EVERY SPARQL query for
     * the affected layer regardless of query shape — measured on
     * {@code bird_card_games} (table {@code sets}) and {@code bird_european_football_2}
     * (table {@code Match}). Quoting makes the identifier a plain quoted name that
     * JSqlParser accepts, so the normal (offline) metadata path is used.
     *
     * <p>Quoting also PRESERVES CASE: unquoted {@code postHistory} is folded to
     * {@code POSTHISTORY} by the parser, whereas {@code "postHistory"} stays as written.
     * Athena resolves both (its identifiers are case-insensitive), so quoting is safe
     * for the mixed-case tables that already worked.
     *
     * @param dottedTable the dotted mapping value, e.g. {@code "bird_card_games.sets"}.
     * @return the quoted identifier pair, e.g. {@code "bird_card_games"."sets"}; a
     *         single-segment value yields one quoted identifier.
     */
    static String quoteDottedTable(final String dottedTable) {
        final StringBuilder quoted = new StringBuilder();
        for (final String segment : splitDottedTable(dottedTable)) {
            if (quoted.length() > 0) {
                quoted.append('.');
            }
            // Escape any embedded double quote by doubling it, per SQL identifier rules.
            quoted.append('"').append(segment.replace("\"", "\"\"")).append('"');
        }
        return quoted.toString();
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
