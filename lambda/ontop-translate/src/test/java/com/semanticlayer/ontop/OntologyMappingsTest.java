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

    @Test
    void isKeyLikeSubjectMatchesIdPkSuffix() {
        // Key-like columns (id / *_id / pk) are treated as unique so the
        // db-metadata can declare a uniqueConstraint (todo item 3).
        assertTrue(OntologyMappings.isKeyLikeSubject("id"));
        assertTrue(OntologyMappings.isKeyLikeSubject("admin_code_id"));
        assertTrue(OntologyMappings.isKeyLikeSubject("pk"));
        assertFalse(OntologyMappings.isKeyLikeSubject("code_value"));
        assertFalse(OntologyMappings.isKeyLikeSubject("name"));
        assertFalse(OntologyMappings.isKeyLikeSubject(null));
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
