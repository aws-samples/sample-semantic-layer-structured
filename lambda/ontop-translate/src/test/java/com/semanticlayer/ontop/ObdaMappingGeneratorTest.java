package com.semanticlayer.ontop;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

/**
 * Tests for {@link ObdaMappingGenerator}, the ontology-JSON → Ontop native OBDA
 * mapping-text generator.
 */
class ObdaMappingGeneratorTest {

    @Test
    void emitsObdaSourceAndTargetPerClass() {
        String obda = new ObdaMappingGenerator().generate(TestFixtures.adminCodesOntology());
        // 2-part source SQL, NO catalog prefix (catalog applied at execution time), with
        // both identifiers QUOTED so a reserved-word table name still parses.
        assertTrue(obda.contains("SELECT * FROM \"normalized\".\"admin_codes\""),
            "OBDA should contain catalog-less, quoted 2-part source SQL; got:\n" + obda);
        // Subject is typed as the class IRI.
        assertTrue(obda.contains("a <http://x/AdminCode>"),
            "OBDA should type the subject as the class IRI; got:\n" + obda);
        // Property template references the BARE column (last dotted segment of "table.column").
        assertTrue(obda.contains("<http://x/AdminCode/code> {code_value}"),
            "OBDA should reference the bare column in the property template; got:\n" + obda);
        // Subject template must use a REAL mapped column — the *_id natural key is preferred.
        assertTrue(obda.contains("<http://x/AdminCode/{admin_code_id}>"),
            "OBDA subject should use the real *_id subject column; got:\n" + obda);
        // The synthetic __pk placeholder must NOT appear anywhere.
        assertFalse(obda.contains("__pk"), "OBDA must not contain the synthetic __pk; got:\n" + obda);
    }

    /**
     * When a class has NO natural-key column, the subject template must fall back to a
     * real bare column (the first sorted mapped column) — never the synthetic __pk.
     */
    @Test
    void subjectUsesRealColumnWhenNoKeyColumnPresent() {
        final Map<String, Object> ontology = Map.of(
            "classes", Map.of("http://x/AdminCode", Map.of("label", "Admin Code")),
            "properties", Map.of(
                "http://x/AdminCode/code", Map.of("type", "owl#DatatypeProperty")),
            "mappings", Map.of(
                "http://x/AdminCode", Map.of("table", "normalized.admin_codes"),
                "http://x/AdminCode/code", Map.of("column", "admin_codes.code_value")),
            "databases", List.of(Map.of("name", "normalized", "catalog", "AwsDataCatalog")));

        final String obda = new ObdaMappingGenerator().generate(ontology);
        // No *_id/pk → first sorted bare column (code_value) is the subject.
        assertTrue(obda.contains("<http://x/AdminCode/{code_value}>"),
            "OBDA subject should fall back to a real bare column; got:\n" + obda);
        assertFalse(obda.contains("__pk"), "OBDA must not contain the synthetic __pk; got:\n" + obda);
    }

    /**
     * A class with a table mapping but NO mapped property columns cannot form a subject
     * IRI, so it must be skipped entirely (not emitted with a synthetic subject).
     */
    @Test
    void skipsClassWithNoMappedColumns() {
        final Map<String, Object> ontology = Map.of(
            "classes", Map.of("http://x/Empty", Map.of("label", "Empty")),
            "properties", Map.of(
                "http://x/Empty/nocol", Map.of("type", "owl#DatatypeProperty")),  // no column
            "mappings", Map.of(
                "http://x/Empty", Map.of("table", "normalized.empties")),  // table but no columns
            "databases", List.of(Map.of("name", "normalized", "catalog", "AwsDataCatalog")));

        final String obda = new ObdaMappingGenerator().generate(ontology);
        assertFalse(obda.contains("http://x/Empty"),
            "class with no mapped columns must be omitted; got:\n" + obda);
    }

    /**
     * A two-class / two-property-each ontology must emit both classes and all four
     * bare columns, and — critically — generating the SAME input twice must produce
     * byte-identical output. This locks in the deterministic-ordering fix: without
     * sorting, class- and property-keySet iteration order is unspecified for
     * {@code Map.of} inputs and the output could flake between runs.
     *
     * @return nothing; assertions verify the generated OBDA.
     */
    @Test
    void emitsMultipleClassesAndPropertiesDeterministically() {
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

        final ObdaMappingGenerator gen = new ObdaMappingGenerator();
        final String obda = gen.generate(ontology);

        // Both class sources are present (2-part, catalog-less, quoted).
        assertTrue(obda.contains("SELECT * FROM \"normalized\".\"admin_codes\""),
            "OBDA should contain the admin_codes source; got:\n" + obda);
        assertTrue(obda.contains("SELECT * FROM \"normalized\".\"parties\""),
            "OBDA should contain the parties source; got:\n" + obda);

        // All four bare columns appear in property templates.
        assertTrue(obda.contains("{code_value}"), "missing {code_value}; got:\n" + obda);
        assertTrue(obda.contains("{label}"), "missing {label}; got:\n" + obda);
        assertTrue(obda.contains("{name}"), "missing {name}; got:\n" + obda);
        assertTrue(obda.contains("{id}"), "missing {id}; got:\n" + obda);

        // Determinism: the same input must yield byte-identical output.
        assertEquals(gen.generate(ontology), gen.generate(ontology),
            "OBDA output must be deterministic for identical input");
    }

    /**
     * Classes without a {@code table} mapping and properties without a {@code column}
     * mapping must be silently omitted (only logged), while the mapped siblings remain.
     *
     * @return nothing; assertions verify presence/absence of IRIs in the output.
     */
    @Test
    void skipsUnmappedClassesAndProperties() {
        final Map<String, Object> ontology = Map.of(
            "classes", Map.of(
                "http://x/AdminCode", Map.of("label", "Admin Code"),
                "http://x/Unmapped", Map.of("label", "Unmapped")),  // no table mapping
            "properties", Map.of(
                "http://x/AdminCode/code", Map.of("type", "owl#DatatypeProperty"),
                "http://x/AdminCode/nocol", Map.of("type", "owl#DatatypeProperty")),  // no column
            "mappings", Map.of(
                "http://x/AdminCode", Map.of("table", "normalized.admin_codes"),
                "http://x/AdminCode/code", Map.of("column", "admin_codes.code_value")),
            "databases", List.of(
                Map.of("name", "normalized", "catalog", "AwsDataCatalog")));

        final String obda = new ObdaMappingGenerator().generate(ontology);

        // Mapped class + property are present.
        assertTrue(obda.contains("SELECT * FROM \"normalized\".\"admin_codes\""),
            "mapped class should be present; got:\n" + obda);
        assertTrue(obda.contains("<http://x/AdminCode/code> {code_value}"),
            "mapped property should be present; got:\n" + obda);

        // Unmapped class IRI (no table) must NOT appear.
        assertFalse(obda.contains("http://x/Unmapped"),
            "class with no table mapping must be omitted; got:\n" + obda);
        // Unmapped property IRI (no column) must NOT appear.
        assertFalse(obda.contains("http://x/AdminCode/nocol"),
            "property with no column mapping must be omitted; got:\n" + obda);
    }

    @Test
    void emitsObjectPropertyAsTargetSubjectIriTemplate() {
        // gt-03 fix: an owl:ObjectProperty FK (Coverage/hasHolding range=Holding,
        // mapsToColumn coverage.holding_id) must be emitted as an IRI template matching
        // the TARGET class's subject template (<.../Holding/{holding_id}>), NOT a bare
        // literal {holding_id}. With a literal, Ontop reformulates the join to EMPTY.
        final Map<String, Object> ontology = Map.of(
            "classes", Map.of(
                "http://x/Coverage", Map.of("label", "Coverage"),
                "http://x/Holding", Map.of("label", "Holding")),
            "properties", Map.of(
                "http://x/Coverage/coverage_id",
                    Map.of("type", "http://www.w3.org/2002/07/owl#DatatypeProperty"),
                "http://x/Coverage/hasHolding", Map.of(
                    "type", "http://www.w3.org/2002/07/owl#ObjectProperty",
                    "range", "http://x/Holding"),
                "http://x/Holding/holding_id",
                    Map.of("type", "http://www.w3.org/2002/07/owl#DatatypeProperty")),
            "mappings", Map.of(
                "http://x/Coverage", Map.of("table", "normalized.coverage"),
                "http://x/Coverage/coverage_id", Map.of("column", "coverage.coverage_id"),
                "http://x/Coverage/hasHolding", Map.of("column", "coverage.holding_id"),
                "http://x/Holding", Map.of("table", "normalized.holding"),
                "http://x/Holding/holding_id", Map.of("column", "holding.holding_id")),
            "databases", List.of(Map.of("name", "normalized", "catalog", "AwsDataCatalog")));

        final String obda = new ObdaMappingGenerator().generate(ontology);

        // FK object property emitted as the target's subject-IRI template.
        assertTrue(obda.contains("<http://x/Coverage/hasHolding> <http://x/Holding/{holding_id}>"),
            "ObjectProperty FK must be an IRI template <Holding/{holding_id}>; got:\n" + obda);
        // It must NOT be a bare literal {holding_id} (the broken form).
        assertFalse(obda.contains("<http://x/Coverage/hasHolding> {holding_id}"),
            "ObjectProperty FK must NOT be a bare literal; got:\n" + obda);
        // Holding's own subject template uses the same column → the join unifies.
        assertTrue(obda.contains("<http://x/Holding/{holding_id}>"),
            "Holding subject template should use holding_id; got:\n" + obda);
        // The datatype property stays a literal.
        assertTrue(obda.contains("<http://x/Coverage/coverage_id> {coverage_id}"),
            "DatatypeProperty must stay a bare literal; got:\n" + obda);
    }

    @Test
    void quotesHyphenatedDynamoTableInSourceSql() {
        // Raw-DynamoDB arm: the connector table name carries hyphens
        // (semantic-layer-dev-riders). Unquoted, Ontop's SQL parser reads the tail as
        // `semantic - layer - dev - riders` (subtraction) and mis-resolves the relation.
        // Per-segment quoting emits it as one literal identifier.
        final String obda = new ObdaMappingGenerator()
            .generate(TestFixtures.hyphenatedDynamoTableOntology());
        assertTrue(obda.contains("FROM \"default\".\"semantic-layer-dev-riders\""),
            "hyphenated table segment must be double-quoted in the source SQL; got:\n" + obda);
        // The un-quoted hyphenated form (which Ontop would misparse) must NOT appear.
        assertFalse(obda.contains("FROM default.semantic-layer-dev-riders"),
            "the un-quoted hyphenated table must not be emitted; got:\n" + obda);
    }

    @Test
    void bakesKeyPrefixIntoFkIriTemplateFromComment() {
        // gt-03/gt-04: FK stores UNPREFIXED id but target PK is PREFIXED; the comment
        // documents CONCAT('PARTY#', fk). The OBDA FK template must bake the prefix
        // (<Party/PARTY#{party_id}>) so it matches the target subject template.
        final String obda = new ObdaMappingGenerator()
            .generate(TestFixtures.coveragePartyPrefixFkOntology());
        // Prefix transform → computed source column + FK template referencing it.
        assertTrue(obda.contains("CONCAT('PARTY#', party_id) AS party_id__ref"),
            "source must add a computed prefix-transform column; got:\n" + obda);
        assertTrue(obda.contains("<http://x/Coverage/hasParty> <http://x/Party/{party_id__ref}>"),
            "FK template must reference the computed prefixed column; got:\n" + obda);
        // must NOT be the raw-column form that never unifies with the prefixed PK
        assertFalse(obda.contains("<http://x/Coverage/hasParty> <http://x/Party/{party_id}>"),
            "FK template must not use the raw unprefixed column; got:\n" + obda);
    }

    // ── Declared primary key drives the subject template ─────────────────────────
    //
    // The subject template IS row identity to Ontop: every property access joins on it.
    // A name guess that lands on a value column turns each access into a self-join on a
    // non-key column, which returns plausible-looking but wrong rows. These tests pin the
    // DECLARED key overriding the guess, and the composite form a guess cannot express.

    /**
     * A declared {@code primaryKey} must win over the name heuristic even when a
     * differently-named column looks more key-like.
     */
    @Test
    void declaredPrimaryKeyOverridesNameHeuristicInSubjectTemplate() {
        // `id` would win on name; `user_id` is DECLARED and must be the subject instead.
        final Map<String, Object> ontology = Map.of(
            "classes", Map.of("http://x/Post", Map.of()),
            "properties", Map.of(
                "http://x/Post/id", Map.of("type", "owl#DatatypeProperty"),
                "http://x/Post/userId", Map.of("type", "owl#DatatypeProperty"),
                "http://x/Post/body", Map.of("type", "owl#DatatypeProperty")),
            "mappings", Map.of(
                "http://x/Post", Map.of("table", "so.posts", "primaryKey", "posts.user_id"),
                "http://x/Post/id", Map.of("column", "posts.id"),
                "http://x/Post/userId", Map.of("column", "posts.user_id"),
                "http://x/Post/body", Map.of("column", "posts.body")),
            "databases", List.of(Map.of("name", "so", "catalog", "AwsDataCatalog")));

        final String obda = new ObdaMappingGenerator().generate(ontology);
        assertTrue(obda.contains("<http://x/Post/{user_id}> a <http://x/Post>"),
            "declared primaryKey must form the subject template; got:\n" + obda);
        assertFalse(obda.contains("<http://x/Post/{id}> a"),
            "the name heuristic must not override a declared key; got:\n" + obda);
    }

    /**
     * A COMPOSITE declared key renders as {@code <Class/{a};{b}>}. Without the separator
     * two distinct column pairs could concatenate into the same IRI and merge unrelated
     * rows into one subject — a junction table's rows would collapse.
     */
    @Test
    void compositeDeclaredKeyRendersSeparatedSubjectTemplate() {
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

        final String obda = new ObdaMappingGenerator().generate(ontology);
        assertTrue(obda.contains("<http://x/Enrolment/{student_id};{course_id}> a"),
            "composite key must render as {a};{b} in declaration order; got:\n" + obda);
    }

    /**
     * Ontop unifies an FK template with the target's SUBJECT template structurally, so a
     * single FK column can never match a COMPOSITE target subject — that join reformulates
     * to EMPTY (zero rows, no error, the worst possible failure). The generator must emit
     * the FK as a literal instead so the value stays queryable and the un-joinable edge is
     * visible, rather than silently producing an empty result set.
     */
    @Test
    void singleColumnFkToCompositeKeyTargetFallsBackToLiteral() {
        final Map<String, Object> ontology = Map.of(
            "classes", Map.of(
                "http://x/Grade", Map.of(),
                "http://x/Enrolment", Map.of()),
            "properties", Map.of(
                "http://x/Grade/gradeId", Map.of("type", "owl#DatatypeProperty"),
                "http://x/Grade/ofEnrolment", Map.of(
                    "type", "http://www.w3.org/2002/07/owl#ObjectProperty",
                    "range", "http://x/Enrolment"),
                "http://x/Enrolment/studentId", Map.of("type", "owl#DatatypeProperty"),
                "http://x/Enrolment/courseId", Map.of("type", "owl#DatatypeProperty")),
            "mappings", Map.of(
                "http://x/Grade", Map.of("table", "uni.grades", "primaryKey", "grades.grade_id"),
                "http://x/Grade/gradeId", Map.of("column", "grades.grade_id"),
                // A single FK column pointing at a 2-column subject.
                "http://x/Grade/ofEnrolment", Map.of("column", "grades.student_id"),
                "http://x/Enrolment",
                    Map.of("table", "uni.enrolment",
                           "primaryKey", "enrolment.student_id, enrolment.course_id"),
                "http://x/Enrolment/studentId", Map.of("column", "enrolment.student_id"),
                "http://x/Enrolment/courseId", Map.of("column", "enrolment.course_id")),
            "databases", List.of(Map.of("name", "uni", "catalog", "AwsDataCatalog")));

        final String obda = new ObdaMappingGenerator().generate(ontology);
        assertTrue(obda.contains("<http://x/Grade/ofEnrolment> {student_id}"),
            "un-unifiable FK must degrade to a literal; got:\n" + obda);
        // The half-formed single-column IRI template must NOT be emitted — it would look
        // like a working join while matching nothing.
        assertFalse(obda.contains("<http://x/Grade/ofEnrolment> <http://x/Enrolment/{student_id}>"),
            "must not emit a single-column template against a composite subject; got:\n" + obda);
    }
}
