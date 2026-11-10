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

        // Both relations present with the 2-part [db,table] name (catalog-less), each
        // segment QUOTED so the RelationID matches the quoted OBDA source identifier.
        // Ontop case-folds only unquoted IDs, so quoting one side alone makes the
        // relation unresolvable — assert the exact quoted array, not a loose substring.
        assertTrue(json.contains("\"name\":[\"\\\"normalized\\\"\",\"\\\"admin_codes\\\"\"]"),
            "admin_codes relation name should be quoted 2-part [db,table]; got:\n" + json);
        assertTrue(json.contains("\"name\":[\"\\\"normalized\\\"\",\"\\\"parties\\\"\"]"),
            "parties relation name should be quoted 2-part [db,table]; got:\n" + json);

        // All four bare columns appear.
        assertTrue(json.contains("\"code_value\""), "missing code_value; got:\n" + json);
        assertTrue(json.contains("\"label\""), "missing label; got:\n" + json);
        assertTrue(json.contains("\"name\""), "missing name; got:\n" + json);
        assertTrue(json.contains("\"id\""), "missing id; got:\n" + json);

        // No synthetic __pk column may be emitted any more.
        assertFalse(json.contains("__pk"),
            "db-metadata must not contain the synthetic __pk column; got:\n" + json);

        // NEITHER class declares a primaryKey, so NEITHER may carry a uniqueConstraint —
        // not even Party, whose subject column is named `id`. A uniqueConstraint licenses
        // Ontop to drop de-duplication, so inferring it from a column NAME is unsound: the
        // old rule asserted it on every `*_id` subject and was measurably false on
        // many-side FK columns (trans/disp/order.account_id all repeat).
        assertFalse(json.contains("\"determinants\""),
            "no class declares a primaryKey, so no uniqueConstraint may be emitted "
                + "(a name like 'id' is not evidence of uniqueness); got:\n" + json);
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
     * A class with a DECLARED {@code primaryKey} carries a {@code uniqueConstraint} naming
     * it, so Ontop drops the defensive DISTINCT + IS-NOT-NULL that otherwise undercounts a
     * class-level COUNT. A name-heuristic subject must NOT — see
     * {@link #omitsUniqueConstraintWhenNoPrimaryKeyIsDeclared}.
     */
    @Test
    void emitsUniqueConstraintForDeclaredPrimaryKey() {
        final Map<String, Object> ontology = Map.of(
            "classes", Map.of("http://x/AdminCode", Map.of("label", "Admin Code")),
            "properties", Map.of(
                "http://x/AdminCode/code", Map.of("type", "owl#DatatypeProperty"),
                "http://x/AdminCode/adminCodeId", Map.of("type", "owl#DatatypeProperty")),
            "mappings", Map.of(
                "http://x/AdminCode",
                    Map.of("table", "normalized.admin_codes",
                           "primaryKey", "admin_codes.admin_code_id"),
                "http://x/AdminCode/code", Map.of("column", "admin_codes.code_value"),
                "http://x/AdminCode/adminCodeId", Map.of("column", "admin_codes.admin_code_id")),
            "databases", List.of(Map.of("name", "normalized", "catalog", "AwsDataCatalog")));

        final String json = new DbMetadataGenerator().generate(ontology);
        assertTrue(json.contains("\"determinants\":[\"admin_code_id\"]"),
            "declared primaryKey should yield a uniqueConstraint naming it; got:\n" + json);
    }

    /**
     * A COMPOSITE declared key constrains the COMBINATION of columns, not each one — the
     * correct identity for a junction table, and precisely where a single-column name
     * guess is most wrong.
     */
    @Test
    void emitsCompositeUniqueConstraintForCompositeDeclaredKey() {
        final Map<String, Object> ontology = Map.of(
            "classes", Map.of("http://x/Enrolment", Map.of()),
            "properties", Map.of(
                "http://x/Enrolment/studentId", Map.of("type", "owl#DatatypeProperty"),
                "http://x/Enrolment/courseId", Map.of("type", "owl#DatatypeProperty"),
                "http://x/Enrolment/grade", Map.of("type", "owl#DatatypeProperty")),
            "mappings", Map.of(
                "http://x/Enrolment",
                    Map.of("table", "uni.enrolment",
                           "primaryKey", "enrolment.student_id, enrolment.course_id"),
                "http://x/Enrolment/studentId", Map.of("column", "enrolment.student_id"),
                "http://x/Enrolment/courseId", Map.of("column", "enrolment.course_id"),
                "http://x/Enrolment/grade", Map.of("column", "enrolment.grade")),
            "databases", List.of(Map.of("name", "uni", "catalog", "AwsDataCatalog")));

        final String json = new DbMetadataGenerator().generate(ontology);
        assertTrue(json.contains("\"determinants\":[\"student_id\",\"course_id\"]"),
            "composite key must constrain the combination in declaration order; got:\n" + json);
    }

    /**
     * With NO declared {@code primaryKey}, the subject column is a name GUESS, so no
     * uniqueConstraint may be emitted even when the guess is named {@code *_id}. The
     * constraint tells Ontop it may skip de-duplication; asserting that from a name was
     * measurably false on many-side FK columns.
     */
    @Test
    void omitsUniqueConstraintWhenNoPrimaryKeyIsDeclared() {
        // The shared fixture's heuristic subject column is admin_code_id (*_id), but the
        // fixture declares no primaryKey.
        final String json = new DbMetadataGenerator().generate(TestFixtures.adminCodesOntology());
        assertTrue(json.contains("admin_code_id"),
            "subject column must still be declared as a column; got:\n" + json);
        assertTrue(json.contains("\"uniqueConstraints\":[]"),
            "a guessed subject must stay unconstrained; got:\n" + json);
    }

    /**
     * A numeric xsd:range must promote the column to a real SQL numeric type so
     * Ontop can reformulate SUM/AVG natively instead of failing to serialize a
     * decimal cast over a VARCHAR-declared column (the dominant BIRD/CWD failure).
     * A string-ranged column stays VARCHAR.
     */
    @Test
    void numericRangesPromoteColumnToNumericSqlType() {
        final Map<String, Object> ontology = Map.of(
            "classes", Map.of("http://x/YearMonth", Map.of("label", "Year Month")),
            "properties", Map.of(
                "http://x/YearMonth/id",
                    Map.of("type", "owl#DatatypeProperty",
                           "range", "http://www.w3.org/2001/XMLSchema#string"),
                "http://x/YearMonth/consumption",
                    Map.of("type", "owl#DatatypeProperty",
                           "range", "http://www.w3.org/2001/XMLSchema#double"),
                "http://x/YearMonth/customerid",
                    Map.of("type", "owl#DatatypeProperty",
                           "range", "http://www.w3.org/2001/XMLSchema#long")),
            "mappings", Map.of(
                "http://x/YearMonth", Map.of("table", "bird_x.yearmonth"),
                "http://x/YearMonth/id", Map.of("column", "yearmonth.id"),
                "http://x/YearMonth/consumption", Map.of("column", "yearmonth.consumption"),
                "http://x/YearMonth/customerid", Map.of("column", "yearmonth.customerid")),
            "databases", List.of(Map.of("name", "bird_x", "catalog", "AwsDataCatalog")));

        final String json = new DbMetadataGenerator().generate(ontology);

        assertTrue(json.contains("\"name\":\"consumption\",\"datatype\":\"DOUBLE\""),
            "numeric #double column must be declared DOUBLE; got:\n" + json);
        assertTrue(json.contains("\"name\":\"customerid\",\"datatype\":\"BIGINT\""),
            "numeric #long column must be declared BIGINT; got:\n" + json);
        assertTrue(json.contains("\"name\":\"id\",\"datatype\":\"VARCHAR\""),
            "string column must stay VARCHAR; got:\n" + json);
    }
}
