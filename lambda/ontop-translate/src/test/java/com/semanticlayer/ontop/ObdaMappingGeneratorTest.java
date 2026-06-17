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
        // 2-part source SQL, NO catalog prefix (catalog applied at execution time).
        assertTrue(obda.contains("SELECT * FROM normalized.admin_codes"),
            "OBDA should contain catalog-less 2-part source SQL; got:\n" + obda);
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

        // Both class sources are present (2-part, catalog-less).
        assertTrue(obda.contains("SELECT * FROM normalized.admin_codes"),
            "OBDA should contain the admin_codes source; got:\n" + obda);
        assertTrue(obda.contains("SELECT * FROM normalized.parties"),
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
        assertTrue(obda.contains("SELECT * FROM normalized.admin_codes"),
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
}
