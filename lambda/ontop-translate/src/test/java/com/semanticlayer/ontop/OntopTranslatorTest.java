package com.semanticlayer.ontop;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.List;
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
     * REGRESSION: a table whose name is a SQL RESERVED WORD must still reformulate.
     *
     * <p>Unquoted, JSqlParser cannot parse {@code SELECT * FROM db.sets}; Ontop then
     * treats the source as an opaque "black box view" and tries to fetch its metadata
     * from a LIVE database, but the offline configuration has no parent metadata
     * provider, so it threw {@code UnsupportedOperationException: Has no parent
     * provider}. That surfaced as "Failed to build Ontop reformulator" and failed EVERY
     * SPARQL query for bird_card_games (table {@code sets}) and bird_european_football_2
     * (table {@code Match}) — 398 build failures on card_games alone, since
     * {@code computeIfAbsent} never caches a failure and so rebuilt on every call.
     *
     * <p>This test covers both reserved words AND the mixed-case table that already
     * worked (quoting must not regress it), exercising the full generator + real Ontop
     * reformulator path — the only place where OBDA-source quoting and db-metadata
     * relation-name quoting must AGREE. Quoting only one side leaves the relation
     * unresolvable ("unknown to the JsonSerializedMetadataProvider"), which unit tests on
     * either generator alone cannot detect.
     *
     * @throws Exception if Ontop fails to build the reformulator or translate.
     */
    @Test
    void reservedWordAndMixedCaseTablesReformulateToSql() throws Exception {
        // `sets` and `Match` are SQL reserved words; `postHistory` is mixed case.
        for (final String table : new String[] {"sets", "Match", "postHistory"}) {
            final Map<String, Object> ont = Map.of(
                "classes", Map.of("http://x/T", Map.of("label", "T")),
                "properties", Map.of(
                    "http://x/T/theId", Map.of("type", "owl#DatatypeProperty"),
                    "http://x/T/name", Map.of("type", "owl#DatatypeProperty")),
                "mappings", Map.of(
                    "http://x/T", Map.of("table", "thedb." + table),
                    "http://x/T/theId", Map.of("column", table + ".id"),
                    "http://x/T/name", Map.of("column", table + ".name")),
                "databases", List.of(Map.of("name", "thedb", "catalog", "AwsDataCatalog")));

            final String sparql =
                "SELECT ?n WHERE { ?t a <http://x/T> . ?t <http://x/T/name> ?n }";
            // Must not throw: pre-fix this raised TranslationException for sets/Match.
            final String sql = new OntopTranslator().translate("vkg-rw-" + table + ":v1",
                ont, sparql);
            // The reserved word must appear QUOTED in the emitted SQL so Athena accepts it.
            assertTrue(sql.contains("\"" + table + "\""),
                "expected quoted \"" + table + "\" in SQL but got: " + sql);
        }
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
    void hyphenatedDynamoTableReformulatesToQuotedSql() throws Exception {
        // Raw-DynamoDB arm: the connector table name carries hyphens
        // (default.semantic-layer-dev-riders). The REAL Ontop parser must accept the
        // OBDA source + db-metadata (proving the hyphenated name is a single quoted
        // identifier, not `semantic - layer - dev - riders` subtraction) and reformulate
        // a class query into SQL that references the quoted table.
        Map<String, Object> ont = TestFixtures.hyphenatedDynamoTableOntology();
        String sparql = "SELECT ?id WHERE { "
            + "?r a <http://x/Rider> ; <http://x/Rider/rider_id> ?id . }";
        String sql = new OntopTranslator().translate("vkg-ddb:v1", ont, sparql);
        String low = sql.toLowerCase();
        // The reformulated SQL references the hyphenated table as a quoted identifier.
        assertTrue(low.contains("semantic-layer-dev-riders"),
            "expected the hyphenated DynamoDB table in SQL but got: " + sql);
        assertTrue(sql.contains("\"semantic-layer-dev-riders\""),
            "the hyphenated table must be double-quoted in the SQL but got: " + sql);
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
