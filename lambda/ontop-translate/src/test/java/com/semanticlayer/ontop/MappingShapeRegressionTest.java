package com.semanticlayer.ontop;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.junit.jupiter.api.Test;

/**
 * END-TO-END mapping-shape regression: translate a fixed SPARQL query through the REAL
 * Ontop reformulator and assert the emitted SQL does not carry either signature of the
 * mapping defect measured on the 2026-08-09 BIRD run.
 *
 * <p>The two signatures, both of which are silent — no error, plausible-looking SQL:
 * <ol>
 *   <li><b>Join on a non-key column.</b> Ontop treats the OBDA subject template as ROW
 *       IDENTITY, so a guessed identity makes every reference to the class join on that
 *       guess. Measured: 113/270 executed rows joined on a value column
 *       ({@code aboutme}, a free-text bio; {@code amount}; {@code avgscrmath}), scoring
 *       EX 0.0500 against 0.1313 for the rows with clean SQL.</li>
 *   <li><b>Fold to a constant.</b> When a join cannot unify, Ontop reformulates to an
 *       empty/constant node — SQL over a {@code tdummy}-style placeholder with no real
 *       relation, returning zero rows against a live table.</li>
 * </ol>
 *
 * <p>These are asserted on real reformulator output rather than on the OBDA text, because
 * the OBDA and the db-metadata must AGREE for the defect to disappear; a unit test on
 * either generator alone cannot observe the join column Ontop finally emits.
 *
 * <p>The fixtures are the shapes of the three BIRD databases whose subject columns were
 * measured wrong, reduced to the smallest ontology that reproduces each: a free-text bio
 * column that sorts first alphabetically ({@code superhero}/{@code codebase_community}),
 * a many-side FK that a {@code *_id} rule wrongly treats as a key ({@code financial}),
 * and a junction table with no single-column key at all.
 */
class MappingShapeRegressionTest {

    /** Matches {@code R<n>.<COL>} on either side of an equality in the emitted SQL. */
    private static final Pattern JOIN_EQUALITY =
        Pattern.compile("R(\\d+)\\.([A-Z0-9_]+)\\s*=\\s*R(\\d+)\\.([A-Z0-9_]+)");

    /**
     * A parent/child pair whose CHILD holds an FK to the parent, with the parent's
     * identity either DECLARED or left to the name heuristic.
     *
     * <p>The parent's first-alphabetical column is {@code aboutme} — a free-text bio, and
     * exactly the column the heuristic picked on the real {@code codebase_community}
     * layer. That makes the two outcomes maximally distinguishable: a correct mapping
     * joins on {@code USERID}, a guessed one joins on {@code ABOUTME}.
     *
     * @param declareParentKey whether the parent class declares {@code users.userid}.
     * @return ontology JSON in the producer's real shape.
     */
    private static Map<String, Object> postOwnedByUser(final boolean declareParentKey) {
        final Map<String, Object> userMapping = declareParentKey
            ? Map.of("table", "so.users", "primaryKey", "users.userid")
            : Map.of("table", "so.users");
        return Map.of(
            "classes", Map.of(
                "http://x/Post", Map.of("label", "Post"),
                "http://x/User", Map.of("label", "User")),
            "properties", Map.of(
                "http://x/Post/postid", Map.of("type", "owl#DatatypeProperty"),
                "http://x/Post/score", Map.of("type", "owl#DatatypeProperty"),
                "http://x/Post/hasOwner", Map.of(
                    "type", "http://www.w3.org/2002/07/owl#ObjectProperty",
                    "range", "http://x/User"),
                "http://x/User/aboutme", Map.of("type", "owl#DatatypeProperty"),
                "http://x/User/userid", Map.of("type", "owl#DatatypeProperty")),
            "mappings", Map.of(
                "http://x/Post", Map.of("table", "so.posts", "primaryKey", "posts.postid"),
                "http://x/Post/postid", Map.of("column", "posts.postid"),
                "http://x/Post/score", Map.of("column", "posts.score"),
                "http://x/Post/hasOwner", Map.of("column", "posts.owner_user_id"),
                "http://x/User", userMapping,
                "http://x/User/aboutme", Map.of("column", "users.aboutme"),
                "http://x/User/userid", Map.of("column", "users.userid")),
            "databases", List.of(Map.of("name", "so", "catalog", "AwsDataCatalog")));
    }

    /**
     * The {@code financial} shape: a many-side table whose only {@code *_id} column is an
     * FK repeated across rows, plus a true key. Declaring the key is the ONLY way to stop
     * the heuristic choosing {@code account_id}, which is not unique in {@code trans}.
     *
     * @param declareKey whether the class declares {@code trans.trans_id}.
     * @return ontology JSON in the producer's real shape.
     */
    private static Map<String, Object> transactions(final boolean declareKey) {
        final Map<String, Object> classMapping = declareKey
            ? Map.of("table", "fin.trans", "primaryKey", "trans.trans_id")
            : Map.of("table", "fin.trans");
        return Map.of(
            "classes", Map.of("http://x/Trans", Map.of("label", "Trans")),
            "properties", Map.of(
                "http://x/Trans/account_id", Map.of("type", "owl#DatatypeProperty"),
                "http://x/Trans/amount", Map.of("type", "owl#DatatypeProperty"),
                "http://x/Trans/trans_id", Map.of("type", "owl#DatatypeProperty")),
            "mappings", Map.of(
                "http://x/Trans", classMapping,
                "http://x/Trans/account_id", Map.of("column", "trans.account_id"),
                "http://x/Trans/amount", Map.of("column", "trans.amount"),
                "http://x/Trans/trans_id", Map.of("column", "trans.trans_id")),
            "databases", List.of(Map.of("name", "fin", "catalog", "AwsDataCatalog")));
    }

    /**
     * A junction table with a COMPOSITE declared key and no single-column key at all —
     * the shape whose single-column guess is most wrong.
     *
     * @return ontology JSON in the producer's real shape.
     */
    private static Map<String, Object> enrolment() {
        return Map.of(
            "classes", Map.of("http://x/E", Map.of("label", "E")),
            "properties", Map.of(
                "http://x/E/course_id", Map.of("type", "owl#DatatypeProperty"),
                "http://x/E/grade", Map.of("type", "owl#DatatypeProperty"),
                "http://x/E/student_id", Map.of("type", "owl#DatatypeProperty")),
            "mappings", Map.of(
                "http://x/E", Map.of("table", "uni.enrolment",
                    "primaryKey", "enrolment.student_id, enrolment.course_id"),
                "http://x/E/course_id", Map.of("column", "enrolment.course_id"),
                "http://x/E/grade", Map.of("column", "enrolment.grade"),
                "http://x/E/student_id", Map.of("column", "enrolment.student_id")),
            "databases", List.of(Map.of("name", "uni", "catalog", "AwsDataCatalog")));
    }

    /**
     * Assert the emitted SQL joins only on columns that are DECLARED key columns.
     *
     * <p>Inspects every {@code R<i>.X = R<j>.Y} equality: an inter-relation equality
     * whose column is not in {@code keyColumns} is a join on a value column. Uppercased
     * because Ontop emits identifiers in upper case.
     *
     * @param sql        the reformulated SQL.
     * @param keyColumns the bare column names that are legitimately row identity.
     */
    private static void assertJoinsOnlyOnKeys(final String sql, final List<String> keyColumns) {
        final Matcher matcher = JOIN_EQUALITY.matcher(sql);
        while (matcher.find()) {
            if (matcher.group(1).equals(matcher.group(3))) {
                continue;  // same relation alias: a filter, not a join between relations.
            }
            for (final String column : new String[] {matcher.group(2), matcher.group(4)}) {
                final boolean isKey = keyColumns.stream()
                    .anyMatch(key -> key.equalsIgnoreCase(column));
                assertTrue(isKey,
                    "SQL joins on non-key column '" + column + "' (declared keys are "
                        + keyColumns + "). Ontop treats the subject template as row "
                        + "identity, so this join returns wrong rows silently. SQL: " + sql);
            }
        }
    }

    /**
     * Assert the SQL was not folded to a constant / placeholder relation.
     *
     * <p>Ontop's empty-node output references no real table; such SQL returns zero rows
     * against a populated Athena table, which scores 0 with no error anywhere.
     *
     * @param sql   the reformulated SQL.
     * @param table the bare physical table the query must actually read.
     */
    private static void assertNotFoldedToConstant(final String sql, final String table) {
        final String low = sql.toLowerCase();
        assertFalse(low.contains("tdummy"),
            "SQL was folded to a tdummy constant relation (the join did not unify), so it "
                + "returns zero rows against a populated table: " + sql);
        assertTrue(low.contains(table),
            "SQL does not read the mapped table " + table + ", so it cannot return rows: "
                + sql);
    }

    /**
     * A declared parent key must make the FK join land on that key.
     *
     * <p>This is the P0.2 acceptance criterion end-to-end: same SPARQL, same tables, and
     * the only difference is whether the ontology declares {@code users.userid}.
     *
     * @throws Exception if Ontop fails to build the reformulator or translate.
     */
    @Test
    void declaredKeyMakesTheFkJoinLandOnTheKey() throws Exception {
        final String sparql = "SELECT ?s ?a WHERE { ?p a <http://x/Post> ; "
            + "<http://x/Post/score> ?s ; <http://x/Post/hasOwner> ?u . "
            + "?u a <http://x/User> ; <http://x/User/aboutme> ?a }";
        final String sql = new OntopTranslator()
            .translate("shape-fk-key:v1", postOwnedByUser(true), sparql);

        assertNotFoldedToConstant(sql, "posts");
        assertNotFoldedToConstant(sql, "users");
        // postid/userid are the declared keys; owner_user_id is the FK matching userid.
        assertJoinsOnlyOnKeys(sql, List.of("postid", "userid", "owner_user_id"));
        // The bio column must appear only as a PROJECTED value, never as a join column.
        assertFalse(sql.toUpperCase().matches("(?s).*=\\s*R\\d+\\.ABOUTME.*"),
            "the free-text bio column must not be a join column: " + sql);
    }

    /**
     * CONTROL: without the declared key the same query joins on the bio column.
     *
     * <p>Pins the defect this fix removes, so the assertions above cannot pass merely
     * because the fixture happens to be benign. It also records the second-order damage:
     * with identity on {@code aboutme}, the requested {@code ?a} is dropped from the
     * projection entirely — the query answers a different question than it was asked.
     *
     * @throws Exception if Ontop fails to build the reformulator or translate.
     */
    @Test
    void withoutADeclaredKeyTheSameQueryJoinsOnTheBioColumn() throws Exception {
        final String sparql = "SELECT ?s ?a WHERE { ?p a <http://x/Post> ; "
            + "<http://x/Post/score> ?s ; <http://x/Post/hasOwner> ?u . "
            + "?u a <http://x/User> ; <http://x/User/aboutme> ?a }";
        final String sql = new OntopTranslator()
            .translate("shape-fk-nokey:v1", postOwnedByUser(false), sparql);

        assertTrue(sql.toUpperCase().matches("(?s).*=\\s*R\\d+\\.ABOUTME.*"),
            "expected the heuristic to join on ABOUTME (the defect being fixed) — if this "
                + "no longer holds, the control fixture stopped reproducing it: " + sql);
    }

    /**
     * A declared key must not be shadowed by a same-table {@code *_id} FK column.
     *
     * <p>{@code trans.account_id} is a many-side FK; the old name rule both used it as
     * row identity and declared it unique, which licensed Ontop to drop de-duplication on
     * a column that genuinely repeats.
     *
     * @throws Exception if Ontop fails to build the reformulator or translate.
     */
    @Test
    void declaredKeyWinsOverASameTableFkColumn() throws Exception {
        final String sparql = "SELECT ?amt WHERE { ?t a <http://x/Trans> ; "
            + "<http://x/Trans/amount> ?amt }";
        final String sql = new OntopTranslator()
            .translate("shape-fin-key:v1", transactions(true), sparql);

        assertNotFoldedToConstant(sql, "trans");
        assertJoinsOnlyOnKeys(sql, List.of("trans_id"));
        // Row identity is trans_id, so its NOT NULL guard must be present; a query whose
        // identity were account_id would guard on that instead.
        assertTrue(sql.toUpperCase().contains("TRANS_ID"),
            "expected the declared key TRANS_ID to drive row identity: " + sql);
    }

    /**
     * A declared key licenses dropping the defensive {@code DISTINCT} on a class COUNT.
     *
     * <p>Without a key Ontop must emit
     * {@code COUNT(*) FROM (SELECT DISTINCT <subjectCol> ...)}, which UNDERCOUNTS whenever
     * the guessed column repeats. With a verified key the dedup subquery is unnecessary
     * and the count is exact. This is the one place a uniqueness claim changes results, so
     * it is also why the producer emits no key triple unless uniqueness was measured.
     *
     * @throws Exception if Ontop fails to build the reformulator or translate.
     */
    @Test
    void declaredKeyRemovesTheUndercountingDistinctFromAClassCount() throws Exception {
        final String sparql = "SELECT (COUNT(?t) AS ?n) WHERE { ?t a <http://x/Trans> }";
        final OntopTranslator translator = new OntopTranslator();

        final String guessed = translator.translate("shape-cnt-nokey:v1",
            transactions(false), sparql);
        assertTrue(guessed.toUpperCase().contains("DISTINCT"),
            "without a declared key the count must stay defensively DISTINCT: " + guessed);

        final String declared = translator.translate("shape-cnt-key:v1",
            transactions(true), sparql);
        assertFalse(declared.toUpperCase().contains("DISTINCT"),
            "a verified key makes the dedup subquery unnecessary; keeping it means the "
                + "uniqueConstraint never reached the db-metadata: " + declared);
        assertNotFoldedToConstant(declared, "trans");
    }

    /**
     * A COMPOSITE declared key must reformulate to real SQL over the junction table.
     *
     * <p>The composite subject template ({@code <Class/{a};{b}>}) is a shape Ontop must
     * still resolve; if it did not, the whole class would fold to a constant and every
     * junction-table query would silently return nothing.
     *
     * @throws Exception if Ontop fails to build the reformulator or translate.
     */
    @Test
    void compositeKeyStillReformulatesOverTheRealTable() throws Exception {
        final String sparql = "SELECT ?g ?c WHERE { ?e a <http://x/E> ; "
            + "<http://x/E/grade> ?g ; <http://x/E/course_id> ?c }";
        final String sql = new OntopTranslator()
            .translate("shape-composite:v1", enrolment(), sparql);

        assertNotFoldedToConstant(sql, "enrolment");
        assertJoinsOnlyOnKeys(sql, List.of("student_id", "course_id"));
        // Both key columns must drive identity, not just the first.
        final String upper = sql.toUpperCase();
        assertTrue(upper.contains("STUDENT_ID") && upper.contains("COURSE_ID"),
            "both composite key columns must appear in the SQL: " + sql);
    }
}
