package com.semanticlayer.ontop;

import com.amazonaws.services.lambda.runtime.ClientContext;
import com.amazonaws.services.lambda.runtime.Context;
import com.amazonaws.services.lambda.runtime.RequestHandler;
import java.util.List;
import java.util.Map;
import java.util.SortedSet;
import java.util.logging.Level;
import java.util.logging.Logger;

/**
 * AgentCore-Gateway entrypoint for the translate-only Ontop Lambda.
 *
 * <p>The Gateway invokes this Lambda with the tool's arguments as the (flat) event
 * payload and the tool name in
 * {@code context.getClientContext().getCustom().get("bedrockAgentCoreToolName")}
 * (a value of the form {@code "<target>___translate_sparql_to_sql"}) — mirroring
 * {@code lambda/neptune-tools/index.py}. There is exactly one tool here, so the tool
 * name is read for routing/diagnostics only; a mismatch is non-fatal.
 *
 * <p><b>Input</b> args: {@code {sparql, ontologyJson, ontologyId?}} where
 * {@code ontologyJson} is the {@code get_ontology_from_neptune} payload the agent
 * already fetched in Phase 1 (the shape consumed by {@link OntologyMappings}).
 *
 * <p><b>Output</b>: {@code {sql, database, catalog}} on success, or {@code {error}}
 * on any failure. The handler NEVER throws — the agent degrades gracefully (Task 9
 * handles the error path), so every code path returns a map.
 *
 * <p><b>Warm-cache reuse.</b> The {@link OntopTranslator} is held in a single shared
 * static field so its per-ontology reformulator cache survives across warm Lambda
 * invocations (Task 4 carried concern). {@link OntopTranslator}'s cache is a
 * {@code ConcurrentHashMap}, so sharing one instance across concurrent invocations is
 * safe.
 */
public class Handler implements RequestHandler<Map<String, Object>, Map<String, Object>> {

    private static final Logger LOG = Logger.getLogger(Handler.class.getName());

    /**
     * Athena's built-in (default) data catalog. Used ONLY when {@code databases[]}
     * carries no catalog match for the chosen database — a federated S3-Tables catalog
     * (e.g. {@code s3tablescatalog/my-bucket}) found in {@code databases[]} MUST be
     * preserved instead.
     */
    private static final String DEFAULT_CATALOG = "AwsDataCatalog";

    /**
     * Single SHARED translator instance so the warm-Lambda reformulator cache is reused
     * across invocations (Task 4 concern). {@link OntopTranslator} is internally
     * thread-safe (its cache is a {@code ConcurrentHashMap}).
     */
    private static final OntopTranslator TRANSLATOR = new OntopTranslator();

    /**
     * Translate the event's SPARQL into Athena SQL for the supplied ontology and return
     * the SQL plus the {@code database} and {@code catalog} the agent's downstream
     * {@code _run_athena_sql} needs for its {@code QueryExecutionContext}.
     *
     * @param event   the tool arguments: {@code {sparql, ontologyJson, ontologyId?}}.
     * @param context the Lambda context; its client context carries the Gateway tool name.
     * @return {@code {sql, database, catalog}} on success, else {@code {error}}; never null.
     */
    @Override
    public Map<String, Object> handleRequest(final Map<String, Object> event, final Context context) {
        try {
            // Read (and log) the Gateway tool name purely for diagnostics/routing — there
            // is a single tool, so a mismatch is not treated as fatal.
            final String toolName = readToolName(context);
            LOG.info("ontop-translate invoked, bedrockAgentCoreToolName=" + toolName);

            final String sparql = stringField(event, "sparql");
            if (sparql == null || sparql.isBlank()) {
                return Map.of("error", "Missing required argument: sparql");
            }
            final Object ontologyJsonObj = event.get("ontologyJson");
            if (!(ontologyJsonObj instanceof Map)) {
                return Map.of("error", "Missing or invalid required argument: ontologyJson");
            }
            @SuppressWarnings("unchecked")
            final Map<String, Object> ontologyJson = (Map<String, Object>) ontologyJsonObj;

            // Cache key: prefer the agent-supplied ontologyId (stable across invocations);
            // otherwise a content hash of the ontology so distinct ontologies don't collide
            // on the same cached reformulator.
            final String ontologyId = stringField(event, "ontologyId");
            // The hashCode() fallback risks a (low-probability) collision → a stale cached
            // reformulator for a different ontology, which is why ontologyId is preferred.
            final String cacheKey = (ontologyId != null && !ontologyId.isBlank())
                ? ontologyId
                : "ont-" + ontologyJson.hashCode();

            final String sql = TRANSLATOR.translate(cacheKey, ontologyJson, sparql);

            final String database = deriveDatabase(ontologyJson);
            final String catalog = deriveCatalog(ontologyJson, database);

            return Map.of("sql", sql, "database", database, "catalog", catalog);
        } catch (final Throwable t) {
            // NEVER throw out of the handler: translation/parse/derivation failures are
            // returned as {error} so the agent degrades gracefully (Task 9).
            // Log the full throwable WITH stack so CloudWatch retains the diagnostic context.
            LOG.log(Level.WARNING, "ontop-translate failed", t);
            final String rawMessage = t.getMessage() != null ? t.getMessage() : t.toString();
            // TranslationException messages already self-describe; for any other throwable,
            // prefix the type so the agent/CloudWatch sees what kind of failure occurred
            // (e.g. "NullPointerException: ...") rather than a bare, type-less message.
            final String message = (t instanceof TranslationException)
                ? rawMessage
                : t.getClass().getSimpleName() + ": " + rawMessage;
            return Map.of("error", message);
        }
    }

    /**
     * Read the Gateway tool name from the Lambda client context, mirroring
     * {@code lambda/neptune-tools/index.py}. Returns {@code null} when any link in the
     * {@code context -> clientContext -> custom -> bedrockAgentCoreToolName} chain is
     * absent (the handler does not depend on it for correctness).
     *
     * @param context the Lambda context (may itself be null in tests).
     * @return the raw tool name (e.g. {@code "<target>___translate_sparql_to_sql"}), or null.
     */
    private String readToolName(final Context context) {
        if (context == null) {
            return null;
        }
        final ClientContext clientContext = context.getClientContext();
        if (clientContext == null) {
            return null;
        }
        final Map<String, String> custom = clientContext.getCustom();
        if (custom == null) {
            return null;
        }
        return custom.get("bedrockAgentCoreToolName");
    }

    /**
     * Derive the Athena {@code database} = the first dotted segment of the chosen mapped
     * class's {@code "database.table"} value. The chosen class is the deterministic first
     * (sorted) class IRI that carries a {@code table} mapping. (The COUNT acceptance case
     * has exactly one mapped class.)
     *
     * @param ontologyJson the ontology document.
     * @return the database name (e.g. {@code "normalized"}).
     * @throws TranslationException if no class has a usable {@code table} mapping.
     */
    private String deriveDatabase(final Map<String, Object> ontologyJson) {
        final SortedSet<String> classIris = OntologyMappings.sortedClassIris(ontologyJson);
        for (final String classIri : classIris) {
            final String dottedTable = OntologyMappings.tableFor(ontologyJson, classIri);
            if (dottedTable != null && !dottedTable.isBlank()) {
                // splitDottedTable returns [db, table]; [0] is the database segment.
                return OntologyMappings.splitDottedTable(dottedTable)[0];
            }
        }
        throw new TranslationException(
            "No mapped class with a table found in ontology — cannot derive database");
    }

    /**
     * Derive the Athena {@code catalog} by matching {@code database} against
     * {@code ontologyJson.databases[].name} and returning that entry's {@code catalog}.
     * Falls back to {@link #DEFAULT_CATALOG} ONLY when there is no matching entry or its
     * catalog is null/blank — a federated S3-Tables catalog present in {@code databases[]}
     * is preserved.
     *
     * @param ontologyJson the ontology document.
     * @param database     the database name derived from the chosen class's table.
     * @return the matched catalog, else {@link #DEFAULT_CATALOG}.
     */
    private String deriveCatalog(final Map<String, Object> ontologyJson, final String database) {
        final Object databasesObj = ontologyJson.get("databases");
        if (databasesObj instanceof List) {
            for (final Object entry : (List<?>) databasesObj) {
                if (!(entry instanceof Map)) {
                    continue;
                }
                @SuppressWarnings("unchecked")
                final Map<String, Object> dbEntry = (Map<String, Object>) entry;
                final Object name = dbEntry.get("name");
                if (name != null && database.equals(name.toString())) {
                    final Object catalog = dbEntry.get("catalog");
                    if (catalog != null && !catalog.toString().isBlank()) {
                        return catalog.toString();  // preserve federated catalogs.
                    }
                    break;  // matched name but no usable catalog — fall back below.
                }
            }
        }
        return DEFAULT_CATALOG;
    }

    /**
     * Read a string-valued event field, returning null when absent OR when present but
     * not a String. Coercing a non-String value (e.g. a numeric/object {@code sparql})
     * via {@code toString()} would feed nonsense to the reformulator; returning null
     * instead routes the caller into its existing "required argument"-style {@code error}.
     * Mirrors the {@code instanceof}-guard used for {@code ontologyJson}.
     *
     * @param event the event map.
     * @param key   the field name.
     * @return the field's value as a String, or null when absent or non-String.
     */
    private String stringField(final Map<String, Object> event, final String key) {
        final Object value = event.get(key);
        return value instanceof String ? (String) value : null;
    }
}
