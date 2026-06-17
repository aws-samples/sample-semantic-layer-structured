package com.semanticlayer.ontop;

import com.google.common.collect.ImmutableMap;
import it.unibz.inf.ontop.answering.logging.QueryLogger;
import it.unibz.inf.ontop.answering.reformulation.QueryReformulator;
import it.unibz.inf.ontop.evaluator.QueryContext;
import it.unibz.inf.ontop.injection.OntopSQLOWLAPIConfiguration;
import it.unibz.inf.ontop.iq.IQ;
import it.unibz.inf.ontop.iq.IQTree;
import it.unibz.inf.ontop.iq.node.NativeNode;
import it.unibz.inf.ontop.query.SPARQLQuery;
import java.io.StringReader;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.logging.Logger;

/**
 * Heart of the translate-only Ontop Lambda: turns the OBDA mapping
 * ({@link ObdaMappingGenerator}) + offline db-metadata
 * ({@link DbMetadataGenerator}) into a live Ontop 5.5.0 reformulator and
 * reformulates a grounded SPARQL SELECT into Athena/Trino SQL WITHOUT executing
 * it (the agent executes the SQL later via boto3).
 *
 * <p><b>Offline by construction.</b> Ontop normally opens a JDBC connection at
 * boot to extract the relational schema. This Lambda has NO Simba/Athena driver
 * and never connects: the schema is supplied up front via
 * {@code dbMetadataReader(Reader)} and the native mapping via
 * {@code nativeOntopMappingReader(Reader)}. The {@code jdbcUrl}/{@code jdbcDriver}
 * settings only select the SQL <i>dialect</i> (offline) — they are never dialed.
 *
 * <p><b>Caching.</b> Building a reformulator is expensive, so each one is cached
 * by {@code cacheKey} (ontologyId+version) in a {@link ConcurrentHashMap}. A warm
 * Lambda reuses the built reformulator across invocations for the same ontology.
 *
 * <h2>Confirmed Ontop 5.5.0 API (verified via {@code javap} against the 5.5.0 jars)</h2>
 * <pre>
 * // Configuration + builder (it.unibz.inf.ontop.injection)
 * OntopSQLOWLAPIConfiguration.defaultBuilder()
 *     -> OntopSQLOWLAPIConfiguration$Builder&lt;?&gt;
 * // setters inherited from the builder-fragment interfaces:
 * Builder.nativeOntopMappingReader(java.io.Reader) -> B   // OntopMappingSQLAllBuilderFragment
 * Builder.dbMetadataReader(java.io.Reader)         -> B   // OntopMappingSQLAllBuilderFragment (OFFLINE schema)
 * Builder.jdbcUrl(java.lang.String)                -> B   // OntopSQLCoreBuilderFragment (dialect only)
 * Builder.jdbcDriver(java.lang.String)             -> B   // OntopSQLCoreBuilderFragment
 * Builder.properties(java.util.Properties)         -> B   // OntopModelBuilderFragment
 * Builder.enableTestMode()                         -> B   // OntopModelBuilderFragment (skips driver class load)
 * Builder.build() -> OntopSQLOWLAPIConfiguration
 *
 * // Reformulator (it.unibz.inf.ontop.injection.OntopReformulationConfiguration)
 * OntopReformulationConfiguration.loadQueryReformulator()
 *     throws OBDASpecificationException -> QueryReformulator
 *
 * // QueryReformulator (it.unibz.inf.ontop.answering.reformulation)
 * QueryReformulator.getInputQueryFactory()    -> KGQueryFactory
 * QueryReformulator.getQueryContextFactory()  -> QueryContext$Factory
 * QueryReformulator.getQueryLoggerFactory()   -> QueryLogger$Factory
 * QueryReformulator.reformulateIntoNativeQuery(
 *     KGQuery&lt;?&gt;, QueryContext, QueryLogger)
 *     throws OntopReformulationException -> IQ
 *
 * // KGQueryFactory (it.unibz.inf.ontop.query)
 * KGQueryFactory.createSPARQLQuery(java.lang.String)
 *     throws OntopInvalidKGQueryException, OntopUnsupportedKGQueryException -> SPARQLQuery&lt;?&gt;
 *
 * // Factories
 * QueryContext$Factory.create(ImmutableMap&lt;String,String&gt;) -> QueryContext
 * QueryLogger$Factory.create(QueryContext)                  -> QueryLogger
 *
 * // SQL extraction: walk the reformulated IQ tree to the leaf NativeNode
 * IQ.getTree() -> IQTree
 * IQTree.getRootNode() -> QueryNode ; IQTree.getChildren() -> ImmutableList&lt;IQTree&gt;
 * NativeNode (it.unibz.inf.ontop.iq.node) extends LeafIQTree
 * NativeNode.getNativeQueryString() -> java.lang.String   // the Athena/Trino SQL
 * </pre>
 */
public class OntopTranslator {

    private static final Logger LOG = Logger.getLogger(OntopTranslator.class.getName());

    /**
     * Dummy Athena JDBC URL. Selects the Athena/Trino SQL dialect for Ontop's SQL
     * generator. NEVER dialed — the offline db-metadata path means no connection is
     * opened, and the Simba driver is deliberately absent from this Lambda.
     */
    private static final String ATHENA_JDBC_URL = "jdbc:athena://";

    /**
     * Athena JDBC driver class name. Supplied to select the dialect; combined with
     * {@code enableTestMode()} Ontop does NOT load/instantiate this class (it is not
     * on the classpath), so no connection attempt is made.
     */
    private static final String ATHENA_JDBC_DRIVER = "com.simba.athena.jdbc.Driver";

    /** Reused generators (stateless). */
    private final ObdaMappingGenerator obdaGenerator = new ObdaMappingGenerator();

    private final DbMetadataGenerator dbMetadataGenerator = new DbMetadataGenerator();

    /**
     * Built reformulators keyed by {@code cacheKey} (ontologyId+version). Shared
     * across invocations of a warm Lambda; {@link QueryReformulator} is thread-safe
     * for reformulation, so a {@link ConcurrentHashMap} is sufficient.
     *
     * <p><b>Contract:</b> the caller MUST vary {@code cacheKey} whenever {@code ont}
     * changes — same key ⇒ first mapping wins, since {@code computeIfAbsent} ignores
     * the later {@code ont} for a key already present.
     */
    private final ConcurrentHashMap<String, QueryReformulator> reformulators = new ConcurrentHashMap<>();

    /**
     * Reformulate a grounded SPARQL SELECT into Athena/Trino SQL for the given
     * ontology. On cache miss, builds (and caches) an Ontop reformulator from the
     * OBDA mapping + offline db-metadata derived from {@code ont}.
     *
     * @param cacheKey unique key for the ontology version (e.g. {@code "ontologyId:version"});
     *                 a built reformulator is reused across calls with the same key.
     * @param ont      the ontology document in the producer shape consumed by
     *                 {@link ObdaMappingGenerator} / {@link DbMetadataGenerator}.
     * @param sparql   the SPARQL SELECT query to reformulate.
     * @return the native Athena/Trino SQL string Ontop generated (NOT executed here).
     * @throws TranslationException if Ontop fails to build the reformulator or to
     *                              reformulate/parse the query.
     */
    public String translate(final String cacheKey, final Map<String, Object> ont, final String sparql) {
        final QueryReformulator reformulator =
            reformulators.computeIfAbsent(cacheKey, key -> buildReformulator(key, ont));
        return reformulate(reformulator, sparql);
    }

    /**
     * Number of distinct reformulators currently cached. For tests only — lets a test
     * assert that two {@link #translate} calls with the same {@code cacheKey} hit the
     * cache (size stays 1) rather than rebuilding.
     *
     * @return the current size of the reformulator cache.
     */
    int cacheSize() {
        return reformulators.size();
    }

    /**
     * Build an Ontop reformulator for one ontology using the OFFLINE configuration
     * path (native mapping reader + db-metadata reader, no live JDBC connection).
     *
     * @param cacheKey the cache key, used only for logging which ontology is built.
     * @param ont      the ontology document.
     * @return a loaded {@link QueryReformulator}.
     * @throws TranslationException wrapping any Ontop configuration/spec failure.
     */
    private QueryReformulator buildReformulator(final String cacheKey, final Map<String, Object> ont) {
        try {
            final String obda = obdaGenerator.generate(ont);
            final String dbMetadata = dbMetadataGenerator.generate(ont);
            LOG.info("Building Ontop reformulator (offline) for cacheKey=" + cacheKey);

            // OFFLINE config: native mapping + db-metadata supplied as Readers so Ontop
            // never opens a JDBC connection. jdbcUrl/jdbcDriver only pick the dialect;
            // enableTestMode() stops Ontop from loading the (absent) Simba driver class.
            final OntopSQLOWLAPIConfiguration configuration = OntopSQLOWLAPIConfiguration.defaultBuilder()
                .nativeOntopMappingReader(new StringReader(obda))
                .dbMetadataReader(new StringReader(dbMetadata))
                .jdbcUrl(ATHENA_JDBC_URL)
                .jdbcDriver(ATHENA_JDBC_DRIVER)
                .enableTestMode()
                .build();

            return configuration.loadQueryReformulator();
        } catch (final Exception e) {
            // Wrap EVERYTHING (OBDASpecificationException, parse errors, IllegalState…)
            // in our single failure type. Fail loud — no silent fallback.
            throw new TranslationException(
                "Failed to build Ontop reformulator for cacheKey=" + cacheKey + ": " + e.getMessage(), e);
        }
    }

    /**
     * Reformulate one SPARQL query into native SQL using a built reformulator.
     *
     * @param reformulator the cached reformulator for the relevant ontology.
     * @param sparql       the SPARQL SELECT query.
     * @return the extracted native SQL string.
     * @throws TranslationException wrapping any parse/reformulation failure, or if no
     *                              {@link NativeNode} (and thus no SQL) is produced.
     */
    private String reformulate(final QueryReformulator reformulator, final String sparql) {
        try {
            final SPARQLQuery<?> query = reformulator.getInputQueryFactory().createSPARQLQuery(sparql);
            // Empty HTTP-header map: no per-user context needed for translate-only.
            final QueryContext queryContext =
                reformulator.getQueryContextFactory().create(ImmutableMap.of());
            final QueryLogger queryLogger = reformulator.getQueryLoggerFactory().create(queryContext);

            final IQ iq = reformulator.reformulateIntoNativeQuery(query, queryContext, queryLogger);
            return extractNativeSql(iq);
        } catch (final TranslationException e) {
            throw e;  // already wrapped (e.g. no NativeNode) — don't double-wrap.
        } catch (final Exception e) {
            // Include the (already-grounded, non-sensitive) SPARQL in the message to
            // speed CloudWatch debugging of malformed/unsupported queries.
            throw new TranslationException(
                "Failed to reformulate SPARQL into SQL: " + e.getMessage() + " | sparql=" + sparql, e);
        }
    }

    /**
     * Walk the reformulated IQ tree to its leaf {@link NativeNode} and return the
     * generated native SQL. The reformulated query for a SELECT is typically a
     * ConstructionNode (binding the projected variables) over a single
     * {@code NativeNode} leaf that holds the SQL string.
     *
     * @param iq the reformulated query.
     * @return the native SQL string from the first {@link NativeNode} found.
     * @throws TranslationException if the tree contains no {@link NativeNode}.
     */
    private String extractNativeSql(final IQ iq) {
        final NativeNode nativeNode = findNativeNode(iq.getTree());
        if (nativeNode == null) {
            throw new TranslationException(
                "Ontop produced no native SQL (no NativeNode in reformulated IQ): " + iq);
        }
        return nativeNode.getNativeQueryString();
    }

    /**
     * Depth-first search for the first {@link NativeNode} in an IQ tree.
     *
     * @param tree the (sub)tree to search.
     * @return the first {@link NativeNode} encountered, or {@code null} if none.
     */
    private NativeNode findNativeNode(final IQTree tree) {
        if (tree.getRootNode() instanceof NativeNode nativeNode) {
            return nativeNode;
        }
        for (final IQTree child : tree.getChildren()) {
            final NativeNode found = findNativeNode(child);
            if (found != null) {
                return found;
            }
        }
        return null;
    }
}
