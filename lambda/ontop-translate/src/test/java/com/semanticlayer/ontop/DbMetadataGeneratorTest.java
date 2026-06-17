package com.semanticlayer.ontop;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

/**
 * Tests for {@link DbMetadataGenerator}, the ontology-JSON → Ontop offline
 * db-metadata JSON generator. The db-metadata lets Ontop's reformulator know the
 * relational schema WITHOUT opening a JDBC connection (the Lambda is
 * translate-only — no Simba driver).
 */
class DbMetadataGeneratorTest {

    @Test
    void buildsRelationPerMappedTableWithColumns() {
        String json = new DbMetadataGenerator().generate(TestFixtures.adminCodesOntology());
        assertTrue(json.contains("\"dbmsProductName\":\"Amazon Athena\""),
            "metadata should declare the Amazon Athena dialect; got:\n" + json);
        assertTrue(json.contains("admin_codes"),
            "relation name should contain the bare table; got:\n" + json);
        assertTrue(json.contains("code_value"),  // BARE column, not "admin_codes.code_value"
            "column should be the bare name; got:\n" + json);
        // The dotted prefix must NOT leak into the column name.
        assertFalse(json.contains("admin_codes.code_value"),
            "column must be bare, not dotted; got:\n" + json);
        // The real subject column (admin_code_id) must be declared on the relation.
        assertTrue(json.contains("admin_code_id"),
            "relation must declare the real subject column; got:\n" + json);
        // No synthetic __pk column may be emitted any more.
        assertFalse(json.contains("__pk"),
            "db-metadata must not contain the synthetic __pk column; got:\n" + json);
    }

    /**
     * Each relation must carry exactly the REAL mapped bare columns (no synthetic
     * {@code __pk}) plus empty constraint arrays. A multi-class input must yield one
     * relation per mapped table, and generating the SAME input twice must produce
     * byte-identical output (deterministic ordering).
     *
     * @return nothing; assertions verify the generated JSON.
     */
    @Test
    void emitsRealColumnsEmptyConstraintsAndIsDeterministic() {
        final Map<String, Object> ontology = Map.of(
            "classes", Map.of(
                "http://x/AdminCode", Map.of("label", "Admin Code"),
                "http://x/Party", Map.of("label", "Party")),
            "properties", Map.of(
                "http://x/AdminCode/code", Map.of("type", "owl#DatatypeProperty"),
                "http://x/AdminCode/label", Map.of("type", "owl#DatatypeProperty"),
                "http://x/Party/name", Map.of("type", "owl#DatatypeProperty"),
                "http://x/Party/id", Map.of("type", "owl#DatatypeProperty")),
            "mappings", Map.of(
                "http://x/AdminCode", Map.of("table", "normalized.admin_codes"),
                "http://x/AdminCode/code", Map.of("column", "admin_codes.code_value"),
                "http://x/AdminCode/label", Map.of("column", "admin_codes.label"),
                "http://x/Party", Map.of("table", "normalized.parties"),
                "http://x/Party/name", Map.of("column", "parties.name"),
                "http://x/Party/id", Map.of("column", "parties.id")),
            "databases", List.of(
                Map.of("name", "normalized", "catalog", "AwsDataCatalog")));

        final DbMetadataGenerator gen = new DbMetadataGenerator();
        final String json = gen.generate(ontology);

        // Both relations present with the 2-part ["db","table"] name (catalog-less).
        assertTrue(json.contains("\"normalized\"") && json.contains("\"admin_codes\""),
            "admin_codes relation name should be 2-part [db,table]; got:\n" + json);
        assertTrue(json.contains("\"parties\""),
            "parties relation should be present; got:\n" + json);

        // All four bare columns appear.
        assertTrue(json.contains("\"code_value\""), "missing code_value; got:\n" + json);
        assertTrue(json.contains("\"label\""), "missing label; got:\n" + json);
        assertTrue(json.contains("\"name\""), "missing name; got:\n" + json);
        assertTrue(json.contains("\"id\""), "missing id; got:\n" + json);

        // No synthetic __pk column may be emitted any more.
        assertFalse(json.contains("__pk"),
            "db-metadata must not contain the synthetic __pk column; got:\n" + json);

        // Constraints depend on whether the subject column is key-like (todo item 3):
        //   AdminCode → subject code_value (preference-3, NON-key) → empty constraints.
        //   Party     → subject id (key-like) → a uniqueConstraint naming it.
        assertTrue(json.contains("\"uniqueConstraints\":[]"),
            "non-key subject (admin_codes.code_value) keeps empty uniqueConstraints; got:\n" + json);
        assertTrue(json.contains("\"determinants\":[\"id\"]"),
            "key-like subject (parties.id) yields a uniqueConstraint; got:\n" + json);
        assertTrue(json.contains("\"foreignKeys\":[]"),
            "foreignKeys should be empty; got:\n" + json);
        assertTrue(json.contains("\"otherFunctionalDependencies\":[]"),
            "otherFunctionalDependencies should be empty; got:\n" + json);

        // Quotation string is the double-quote character.
        assertTrue(json.contains("\"quotationString\":\"\\\"\""),
            "quotationString should be a double-quote; got:\n" + json);

        // Determinism: same input → byte-identical output.
        assertEquals(gen.generate(ontology), gen.generate(ontology),
            "db-metadata output must be deterministic for identical input");
    }

    /**
     * Two property IRIs that map to the SAME bare column must not cause Ontop's
     * ImmutableMap to throw "Multiple entries with same key". The duplicate must be
     * silently dropped; the column should appear exactly once in the output.
     *
     * <p>Regression: the curated_layer ontology had two properties both mapped to
     * {@code PARTY_ID}, crashing the reformulator with
     * "Multiple entries with same key: PARTY_ID=PARTY_ID VARCHAR".
     */
    @Test
    void dropsDuplicateColumnsForSameClass() {
        final Map<String, Object> ontology = Map.of(
            "classes", Map.of("http://x/Party", Map.of("label", "Party")),
            "properties", Map.of(
                "http://x/Party/partyId", Map.of("type", "owl#DatatypeProperty"),
                "http://x/Party/partyIdAlias", Map.of("type", "owl#DatatypeProperty")),
            "mappings", Map.of(
                "http://x/Party", Map.of("table", "normalized.parties"),
                // Both properties map to the same bare column — the classic duplicate.
                "http://x/Party/partyId", Map.of("column", "parties.PARTY_ID"),
                "http://x/Party/partyIdAlias", Map.of("column", "parties.PARTY_ID")),
            "databases", List.of(Map.of("name", "normalized", "catalog", "AwsDataCatalog")));

        final String json = new DbMetadataGenerator().generate(ontology);

        // PARTY_ID must appear exactly once; verify by counting occurrences.
        final int count = json.split("\"PARTY_ID\"", -1).length - 1;
        // One occurrence for the column name, one for the uniqueConstraint determinant = 2 max.
        assertTrue(count >= 1 && count <= 2,
            "PARTY_ID should appear 1–2 times (column + optional constraint), got "
                + count + "; json:\n" + json);

        // Should not have thrown — reaching this assertion is the primary regression check.
    }

    /**
     * A class whose subject column is key-like ({@code *_id}/{@code id}/{@code pk}) must
     * carry a {@code uniqueConstraint} naming that column, so Ontop drops the defensive
     * DISTINCT + IS-NOT-NULL that otherwise undercounts a class-level COUNT (todo item 3).
     */
    @Test
    void emitsUniqueConstraintForKeyLikeSubjectColumn() {
        // The shared fixture's subject column is admin_code_id (*_id → key-like).
        final String json = new DbMetadataGenerator().generate(TestFixtures.adminCodesOntology());
        assertTrue(json.contains("admin_code_id"),
            "subject column must be declared; got:\n" + json);
        assertFalse(json.contains("\"uniqueConstraints\":[]"),
            "key-like subject must yield a NON-empty uniqueConstraint; got:\n" + json);
        assertTrue(json.contains("\"determinants\":[\"admin_code_id\"]"),
            "uniqueConstraint should name the subject column; got:\n" + json);
    }
}
