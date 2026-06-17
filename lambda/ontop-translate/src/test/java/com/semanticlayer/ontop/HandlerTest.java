package com.semanticlayer.ontop;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.Map;
import org.junit.jupiter.api.Test;

/**
 * TDD test for {@link Handler}: the AgentCore-Gateway entrypoint that turns
 * {@code {sparql, ontologyJson, ontologyId?}} into {@code {sql, database, catalog}}
 * (or {@code {error}}). Verifies both the default-catalog path and the federated
 * S3-Tables catalog path (the latter must NOT collapse to {@code AwsDataCatalog}).
 */
class HandlerTest {

    /**
     * A COUNT query over the admin-codes ontology yields SQL hitting
     * {@code admin_codes}, {@code database} = {@code normalized} (first dotted segment
     * of {@code normalized.admin_codes}) and {@code catalog} = {@code AwsDataCatalog}
     * (from {@code databases[].catalog}); no error.
     */
    @Test
    void handlerReturnsSqlDatabaseCatalog() {
        Map<String, Object> event = Map.of(
            "sparql", "SELECT (COUNT(?a) AS ?n) WHERE { ?a a <http://x/AdminCode> }",
            "ontologyJson", TestFixtures.adminCodesOntology(), "ontologyId", "vkg-test-1:v1");
        Map<String, Object> out =
            new Handler().handleRequest(event, TestFixtures.ctx("t___translate_sparql_to_sql"));
        assertTrue(((String) out.get("sql")).toLowerCase().contains("admin_codes"));
        assertEquals("normalized", out.get("database"));      // first segment of db.table
        assertEquals("AwsDataCatalog", out.get("catalog"));   // from databases[].catalog
        assertNull(out.get("error"));
    }

    /**
     * For an ontology mapped to a federated S3-Tables catalog, the handler must
     * resolve {@code catalog} = {@code s3tablescatalog/my-bucket} from
     * {@code databases[]} rather than the {@code AwsDataCatalog} default.
     */
    @Test
    void handlerResolvesFederatedCatalogFromDatabases() {
        Map<String, Object> event = Map.of(
            "sparql", "SELECT (COUNT(?e) AS ?n) WHERE { ?e a <http://x/Event> }",
            "ontologyJson", TestFixtures.s3TablesOntology());
        Map<String, Object> out =
            new Handler().handleRequest(event, TestFixtures.ctx("t___translate_sparql_to_sql"));
        assertEquals("analytics", out.get("database"));
        assertEquals("s3tablescatalog/my-bucket", out.get("catalog"));   // NOT the default
    }

    /**
     * Locks the never-throws + early-guard contract: invalid input must yield an
     * {@code {error}} map (non-null {@code error}, null {@code sql}) rather than
     * propagating an exception out of {@code handleRequest}. Covers (a) a missing
     * {@code sparql} argument and (b) an {@code ontologyJson} that is not a Map
     * (here a String) — the latter exercises the {@code instanceof} guard.
     */
    @Test
    void handlerReturnsErrorOnInvalidInput() {
        // (a) sparql absent — ontologyJson present so we isolate the missing-sparql guard.
        Map<String, Object> missingSparql = Map.of(
            "ontologyJson", TestFixtures.adminCodesOntology(), "ontologyId", "vkg-test-1:v1");
        Map<String, Object> outMissing =
            new Handler().handleRequest(missingSparql, TestFixtures.ctx("t___translate_sparql_to_sql"));
        assertNotNull(outMissing.get("error"));
        assertNull(outMissing.get("sql"));

        // (b) ontologyJson is a String, not a Map — must hit the instanceof guard.
        Map<String, Object> badOntology = Map.of(
            "sparql", "SELECT (COUNT(?a) AS ?n) WHERE { ?a a <http://x/AdminCode> }",
            "ontologyJson", "not-a-map");
        Map<String, Object> outBad =
            new Handler().handleRequest(badOntology, TestFixtures.ctx("t___translate_sparql_to_sql"));
        assertNotNull(outBad.get("error"));
        assertNull(outBad.get("sql"));
    }
}
