package com.semanticlayer.ontop;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertIterableEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

/**
 * Edge-case tests for the shared {@link OntologyMappings} parsing/lookup helper.
 *
 * <p>These guard the small-but-load-bearing string-splitting and IRI-nesting rules
 * that both {@link ObdaMappingGenerator} and {@link DbMetadataGenerator} delegate to.
 * The generators' own suites cover the end-to-end output; this suite pins down the
 * helper's contract directly (bare-column extraction, dotted-table split, the
 * mapped-property nesting filter, and the null defensiveness).
 */
class OntologyMappingsTest {

    @Test
    void bareColumnTakesLastDottedSegment() {
        assertEquals("code_value", OntologyMappings.bareColumn("admin_codes.code_value"));
    }

    @Test
    void bareColumnReturnsInputWhenNoDot() {
        // No dot → input returned unchanged (preserved from the original helpers).
        assertEquals("code_value", OntologyMappings.bareColumn("code_value"));
    }

    @Test
    void quoteDottedTableQuotesHyphenatedConnectorTable() {
        // A DynamoDB-connector table name carries HYPHENS. Unquoted,
        // default.semantic-layer-dev-riders parses as arithmetic subtraction and Ontop
        // mis-resolves the relation. Unconditional per-segment quoting emits the
        // hyphenated tail as one literal identifier.
        assertEquals("\"default\".\"semantic-layer-dev-riders\"",
            OntologyMappings.quoteDottedTable("default.semantic-layer-dev-riders"));
        // A single-segment (no-dot) value is handled without throwing.
        assertEquals("\"weird-name\"", OntologyMappings.quoteDottedTable("weird-name"));
    }

    @Test
    void splitDottedTableSplitsOnLastDot() {
        assertArrayEquals(
            new String[] {"normalized", "admin_codes"},
            OntologyMappings.splitDottedTable("normalized.admin_codes"));
    }

    @Test
    void splitDottedTableReturnsSingletonWhenNoDot() {
        // Single-segment value yields a 1-element array rather than throwing.
        assertArrayEquals(
            new String[] {"admin_codes"},
            OntologyMappings.splitDottedTable("admin_codes"));
    }

    @Test
    void sortedPropertyIrisForClassFiltersNestingAndRequiresColumnMapping() {
        final Map<String, Object> ont = Map.of(
            "classes", Map.of("http://x/AdminCode", Map.of()),
            "properties", Map.of(
                "http://x/AdminCode/code", Map.of("type", "DatatypeProperty"),
                "http://x/AdminCode/nomap", Map.of("type", "DatatypeProperty"),  // nested, no mapping
                "http://x/Other/code", Map.of("type", "DatatypeProperty")),       // not nested
            "mappings", Map.of(
                "http://x/AdminCode/code", Map.of("column", "admin_codes.code_value"),
                "http://x/Other/code", Map.of("column", "other.code")));

        // Only the nested property WITH a column mapping qualifies; sorted order.
        assertIterableEquals(
            List.of("http://x/AdminCode/code"),
            OntologyMappings.sortedPropertyIrisForClass(ont, "http://x/AdminCode"));
    }

    @Test
    void lookupsReturnNullForUnmappedIris() {
        final Map<String, Object> ont = Map.of("mappings", Map.of());
        assertNull(OntologyMappings.tableFor(ont, "http://x/Missing"));
        assertNull(OntologyMappings.columnFor(ont, "http://x/Missing/prop"));
    }

    @Test
    void subjectColumnPrefersExactIdOverIdSuffix() {
        final Map<String, Object> ont = Map.of(
            "classes", Map.of("http://x/C", Map.of()),
            "properties", Map.of(
                "http://x/C/foo", Map.of("type", "DatatypeProperty"),
                "http://x/C/theId", Map.of("type", "DatatypeProperty"),
                "http://x/C/otherId", Map.of("type", "DatatypeProperty")),
            "mappings", Map.of(
                "http://x/C/foo", Map.of("column", "t.foo"),
                "http://x/C/theId", Map.of("column", "t.id"),         // exact "id" wins
                "http://x/C/otherId", Map.of("column", "t.other_id")));  // *_id loses to exact
        assertEquals("id", OntologyMappings.subjectColumnFor(ont, "http://x/C"));
    }

    @Test
    void subjectColumnPrefersIdSuffixOverPkAndFirst() {
        final Map<String, Object> ont = Map.of(
            "classes", Map.of("http://x/C", Map.of()),
            "properties", Map.of(
                "http://x/C/aaa", Map.of("type", "DatatypeProperty"),
                "http://x/C/pk", Map.of("type", "DatatypeProperty"),
                "http://x/C/key", Map.of("type", "DatatypeProperty")),
            "mappings", Map.of(
                "http://x/C/aaa", Map.of("column", "t.aaa"),       // sorts first but not a key
                "http://x/C/pk", Map.of("column", "t.pk"),         // pk loses to *_id
                "http://x/C/key", Map.of("column", "t.admin_code_id")));  // *_id wins
        assertEquals("admin_code_id", OntologyMappings.subjectColumnFor(ont, "http://x/C"));
    }

    @Test
    void subjectColumnPrefersPkWhenNoIdColumn() {
        final Map<String, Object> ont = Map.of(
            "classes", Map.of("http://x/C", Map.of()),
            "properties", Map.of(
                "http://x/C/aaa", Map.of("type", "DatatypeProperty"),
                "http://x/C/thePk", Map.of("type", "DatatypeProperty")),
            "mappings", Map.of(
                "http://x/C/aaa", Map.of("column", "t.aaa"),  // sorts first
                "http://x/C/thePk", Map.of("column", "t.pk")));  // pk wins (no id/_id present)
        assertEquals("pk", OntologyMappings.subjectColumnFor(ont, "http://x/C"));
    }

    @Test
    void subjectColumnFallsBackToFirstSortedBareColumn() {
        final Map<String, Object> ont = Map.of(
            "classes", Map.of("http://x/C", Map.of()),
            "properties", Map.of(
                "http://x/C/zzz", Map.of("type", "DatatypeProperty"),
                "http://x/C/aaa", Map.of("type", "DatatypeProperty")),
            "mappings", Map.of(
                "http://x/C/zzz", Map.of("column", "t.zeta"),
                "http://x/C/aaa", Map.of("column", "t.alpha")));  // no key → first sorted bare wins
        assertEquals("alpha", OntologyMappings.subjectColumnFor(ont, "http://x/C"));
    }

    @Test
    void subjectColumnIsNullWhenClassHasNoMappedColumns() {
        final Map<String, Object> ont = Map.of(
            "classes", Map.of("http://x/C", Map.of()),
            "properties", Map.of(
                "http://x/C/nomap", Map.of("type", "DatatypeProperty")),  // nested but no column
            "mappings", Map.of());
        assertNull(OntologyMappings.subjectColumnFor(ont, "http://x/C"));
    }

    @Test
    void subjectColumnForAdminCodesFixturePrefersAdminCodeId() {
        // The shared fixture has code_value + admin_code_id; the *_id key must win.
        assertEquals("admin_code_id",
            OntologyMappings.subjectColumnFor(TestFixtures.adminCodesOntology(), "http://x/AdminCode"));
    }

    // ── Declared primary key (preferred over every name heuristic) ────────────────
    //
    // Ontop treats the subject template as ROW IDENTITY, so a guessed identity does not
    // merely mislabel rows — it makes every property access self-join on that column.
    // Over the 2026-08-09 BIRD run the name heuristic put identity on value columns
    // (`aboutme`, a free-text bio; `amount`; `avgscrmath`) in 42% of executed rows, which
    // scored EX 0.0500 against 0.1313 for rows with clean SQL. These tests pin the
    // declared key winning, since that is the whole mechanism of the fix.

    /** Build a one-class ontology over columns {@code aboutme, id, userid}. */
    private static Map<String, Object> classWith(final Object mappingEntry) {
        return Map.of(
            "classes", Map.of("http://x/C", Map.of()),
            "properties", Map.of(
                "http://x/C/aboutme", Map.of("type", "DatatypeProperty"),
                "http://x/C/theId", Map.of("type", "DatatypeProperty"),
                "http://x/C/userId", Map.of("type", "DatatypeProperty")),
            "mappings", Map.of(
                "http://x/C", mappingEntry,
                "http://x/C/aboutme", Map.of("column", "t.aboutme"),
                "http://x/C/theId", Map.of("column", "t.id"),
                "http://x/C/userId", Map.of("column", "t.userid")));
    }

    @Test
    void declaredKeyBeatsNameHeuristic() {
        // `id` would win on name; the DECLARED key is `userid` and must be used instead.
        final Map<String, Object> ont =
            classWith(Map.of("table", "db.t", "primaryKey", "t.userid"));
        assertEquals(List.of("userid"), OntologyMappings.subjectColumnsFor(ont, "http://x/C"));
        assertTrue(OntologyMappings.hasDeclaredKey(ont, "http://x/C"));
    }

    @Test
    void declaredCompositeKeyKeepsBothColumnsInOrder() {
        // A junction table's identity is the COMBINATION. Declaration order is preserved
        // so the subject template is stable across builds.
        final Map<String, Object> ont =
            classWith(Map.of("table", "db.t", "primaryKey", "t.userid, t.id"));
        assertEquals(List.of("userid", "id"),
            OntologyMappings.subjectColumnsFor(ont, "http://x/C"));
    }

    @Test
    void declaredKeyDropsColumnsTheClassDoesNotMap() {
        // The subject template's columns must also be declared columns of the relation,
        // or Ontop emits SQL referencing a column the db-metadata lacks (COLUMN_NOT_FOUND).
        final Map<String, Object> ont =
            classWith(Map.of("table", "db.t", "primaryKey", "t.userid, t.not_mapped"));
        assertEquals(List.of("userid"), OntologyMappings.subjectColumnsFor(ont, "http://x/C"));
    }

    @Test
    void noDeclaredKeyFallsBackToHeuristicAndIsNotTreatedAsDeclared() {
        // Without a declaration we still produce a subject (layers predate this field),
        // but hasDeclaredKey must be false so no uniqueConstraint is emitted for a guess.
        final Map<String, Object> ont = classWith(Map.of("table", "db.t"));
        assertEquals(List.of("id"), OntologyMappings.subjectColumnsFor(ont, "http://x/C"));
        assertFalse(OntologyMappings.hasDeclaredKey(ont, "http://x/C"));
    }

    @Test
    void declaredKeyOfOnlyUnmappedColumnsIsNotADeclaredKey() {
        // A declaration we cannot honor must not claim to be honored — otherwise the
        // db-metadata would assert uniqueness on columns absent from the subject.
        final Map<String, Object> ont =
            classWith(Map.of("table", "db.t", "primaryKey", "t.nope"));
        assertFalse(OntologyMappings.hasDeclaredKey(ont, "http://x/C"));
        assertEquals(List.of("id"), OntologyMappings.subjectColumnsFor(ont, "http://x/C"));
    }

    @Test
    void subjectColumnsIsEmptyWhenClassMapsNoColumns() {
        final Map<String, Object> ont = Map.of(
            "classes", Map.of("http://x/C", Map.of()),
            "properties", Map.of("http://x/C/nomap", Map.of("type", "DatatypeProperty")),
            "mappings", Map.of());
        assertTrue(OntologyMappings.subjectColumnsFor(ont, "http://x/C").isEmpty());
    }

    /**
     * A reserved-word table name ({@code sets}, {@code Match}) MUST come out quoted.
     * Unquoted, JSqlParser fails to parse the OBDA source query, Ontop falls back to its
     * live-database "black box view" path, and the offline configuration throws
     * "Has no parent provider" — which broke every SPARQL query for bird_card_games
     * (table {@code sets}) and bird_european_football_2 (table {@code Match}).
     */
    @Test
    void quoteDottedTableQuotesBothSegmentsIncludingReservedWords() {
        assertEquals("\"bird_card_games\".\"sets\"",
            OntologyMappings.quoteDottedTable("bird_card_games.sets"));
        assertEquals("\"bird_european_football_2\".\"Match\"",
            OntologyMappings.quoteDottedTable("bird_european_football_2.Match"));
        // Mixed case is preserved verbatim (Athena resolves it case-insensitively).
        assertEquals("\"db\".\"postHistory\"",
            OntologyMappings.quoteDottedTable("db.postHistory"));
        // A single-segment value yields one quoted identifier, not a stray dot.
        assertEquals("\"sets\"", OntologyMappings.quoteDottedTable("sets"));
        // An embedded double quote is escaped by doubling, per SQL identifier rules.
        assertEquals("\"db\".\"we\"\"ird\"", OntologyMappings.quoteDottedTable("db.we\"ird"));
    }

    @Test
    void concatPrefixForParsesPrefixFromComment() {
        var ont = TestFixtures.coveragePartyPrefixFkOntology();
        assertEquals("PARTY#",
            OntologyMappings.concatPrefixFor(ont, "http://x/Coverage/hasParty"));
        // a property with no CONCAT in its comment yields empty
        assertEquals("",
            OntologyMappings.concatPrefixFor(ont, "http://x/Coverage/coverage_id"));
    }
}
