package com.semanticlayer.ontop;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.Map;
import org.junit.jupiter.api.Test;

/**
 * TDD test for {@link OntopTranslator}: a COUNT-over-class SPARQL query must be
 * reformulated by the real Ontop 5.5.0 reformulator into Athena/Trino SQL over the
 * mapped relation {@code normalized.admin_codes} — WITHOUT opening any JDBC
 * connection (offline db-metadata path).
 */
class OntopTranslatorTest {

    /**
     * Reformulate {@code COUNT(?a)} over {@code <http://x/AdminCode>} into SQL and
     * assert the generated SQL aggregates ({@code count}) over the mapped table
     * ({@code admin_codes}).
     *
     * @throws Exception if Ontop fails to build the reformulator or translate.
     */
    @Test
    void translatesCountSparqlToAthenaSql() throws Exception {
        Map<String, Object> ont = TestFixtures.adminCodesOntology();
        String sparql = "SELECT (COUNT(?a) AS ?n) WHERE { ?a a <http://x/AdminCode> }";
        String sql = new OntopTranslator().translate("vkg-test-1:v1", ont, sparql);
        String low = sql.toLowerCase();
        assertTrue(low.contains("count"), "expected COUNT in SQL but got: " + sql);
        assertTrue(low.contains("admin_codes"), "expected admin_codes in SQL but got: " + sql);
        // The translated SQL must reference a REAL column (the *_id subject column),
        // and must NOT reference the old synthetic __pk that Athena rejected.
        assertTrue(low.contains("admin_code_id"),
            "expected the real subject column admin_code_id in SQL but got: " + sql);
        assertFalse(low.contains("__pk"),
            "translated SQL must not reference the synthetic __pk but got: " + sql);
    }

    /**
     * Malformed SPARQL must surface as a {@link TranslationException}, confirming the
     * {@code reformulate(...)} catch-and-wrap path turns Ontop's parse failure into our
     * single failure type rather than leaking an internal exception.
     */
    @Test
    void malformedSparqlThrowsTranslationException() {
        Map<String, Object> ont = TestFixtures.adminCodesOntology();
        assertThrows(
            TranslationException.class,
            () -> new OntopTranslator().translate("vkg-bad:v1", ont, "SELECT WHERE {"));
    }

    /**
     * Two {@code translate} calls with the SAME cacheKey + ontology + SPARQL must reuse
     * the cached reformulator (cache size stays 1) and return equal SQL; a third call
     * with a DIFFERENT cacheKey must build a second reformulator (size 2).
     *
     * @throws Exception if Ontop fails to build the reformulator or translate.
     */
    @Test
    void cacheReusesReformulatorForSameKey() throws Exception {
        Map<String, Object> ont = TestFixtures.adminCodesOntology();
        String sparql = "SELECT (COUNT(?a) AS ?n) WHERE { ?a a <http://x/AdminCode> }";
        OntopTranslator translator = new OntopTranslator();

        String sql1 = translator.translate("vkg-cache:v1", ont, sparql);
        String sql2 = translator.translate("vkg-cache:v1", ont, sparql);
        assertEquals(sql1, sql2, "same key must yield identical SQL");
        assertEquals(1, translator.cacheSize(), "same key must not rebuild the reformulator");

        translator.translate("vkg-cache:v2", ont, sparql);
        assertEquals(2, translator.cacheSize(), "different key must build a second reformulator");
    }

    @Test
    void fkObjectPropertyJoinReformulatesToSql() throws Exception {
        // gt-03: an FK object-property join must reformulate to real SQL (not EMPTY).
        Map<String, Object> ont = TestFixtures.coverageHoldingFkOntology();
        String sparql =
            "SELECT ?cid ?hid WHERE { "
            + "?c a <http://x/Coverage> ; "
            + "<http://x/Coverage/coverage_id> ?cid ; "
            + "<http://x/Coverage/hasHolding> ?h . "
            + "?h a <http://x/Holding> ; <http://x/Holding/holding_id> ?hid . }";
        String sql = new OntopTranslator().translate("vkg-fk:v1", ont, sparql);
        String low = sql.toLowerCase();
        // Reformulated to real SQL over both tables (the join resolved, not EMPTY).
        assertTrue(low.contains("coverage"), "expected coverage table in SQL: " + sql);
        assertTrue(low.contains("holding"), "expected holding table in SQL: " + sql);
    }

    @Test
    void booleanColumnFilterDoesNotWrapBooleanInLower() throws Exception {
        // gt-08: filtering a boolean column must NOT translate to lower(<boolean>).
        // With the column declared BOOLEAN in db-metadata, a direct boolean filter
        // translates cleanly.
        Map<String, Object> ont = TestFixtures.coverageHoldingFkOntology();
        String sparql =
            "SELECT ?cid WHERE { "
            + "?c a <http://x/Coverage> ; "
            + "<http://x/Coverage/coverage_id> ?cid ; "
            + "<http://x/Coverage/is_deleted> ?d . FILTER(!?d) }";
        String sql = new OntopTranslator().translate("vkg-bool:v1", ont, sparql);
        String low = sql.toLowerCase();
        assertTrue(low.contains("coverage"), "expected coverage table in SQL: " + sql);
        // The boolean is used directly — no lower() applied to the boolean column.
        assertFalse(low.matches("(?s).*lower\\s*\\(\\s*is_deleted.*"),
            "boolean column must not be wrapped in lower(): " + sql);
    }

    @Test
    void prefixTransformFkJoinReformulatesToSql() throws Exception {
        // gt-03/gt-04: the prefixed-FK join must reformulate to real SQL (not EMPTY).
        Map<String, Object> ont = TestFixtures.coveragePartyPrefixFkOntology();
        String sparql =
            "SELECT ?cid ?pid WHERE { "
            + "?c a <http://x/Coverage> ; "
            + "<http://x/Coverage/coverage_id> ?cid ; "
            + "<http://x/Coverage/hasParty> ?p . "
            + "?p a <http://x/Party> ; <http://x/Party/party_id> ?pid . }";
        String sql = new OntopTranslator().translate("vkg-pfx:v1", ont, sparql);
        String low = sql.toLowerCase();
        assertTrue(low.contains("coverage"), "expected coverage table in SQL: " + sql);
        assertTrue(low.contains("party"), "expected party table in SQL: " + sql);
        // The join must apply the prefix transform (CONCAT 'PARTY#' or equivalent
        // string-concatenation) so unprefixed FK matches prefixed PK.
        assertTrue(low.contains("party#") || low.contains("concat") || low.contains("||"),
            "expected the PARTY# prefix transform in the join SQL: " + sql);
    }
}
