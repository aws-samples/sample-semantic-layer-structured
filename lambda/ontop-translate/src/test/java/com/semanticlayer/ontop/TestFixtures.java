package com.semanticlayer.ontop;

import com.amazonaws.services.lambda.runtime.ClientContext;
import com.amazonaws.services.lambda.runtime.CognitoIdentity;
import com.amazonaws.services.lambda.runtime.Context;
import com.amazonaws.services.lambda.runtime.LambdaLogger;
import java.util.List;
import java.util.Map;

/**
 * Shared test fixtures reflecting the REAL ontology JSON shape produced by
 * {@code lambda/neptune-tools/index.py::tool_get_ontology_from_neptune}.
 *
 * <p>Key shape facts (verified against the producer):
 * <ul>
 *   <li>{@code classes: {classIri: {label?, comment?}}}</li>
 *   <li>{@code properties: {propIri: {type, label?, comment?}}} — NO {@code domain} field.
 *       A property belongs to a class purely by IRI nesting:
 *       {@code propIri == classIri + "/" + propName}.</li>
 *   <li>{@code mappings: {iri: {table?, column?}}} where a class's {@code table} is the
 *       dotted {@code "database.table"} and a property's {@code column} is the dotted
 *       {@code "table.column"}. Both require taking the LAST dotted segment for the bare name.</li>
 *   <li>{@code databases: [{name, catalog, dataSource}]}.</li>
 * </ul>
 */
public class TestFixtures {

    /**
     * Minimal single-class / single-property ontology in the real producer shape.
     *
     * @return ontology JSON as a nested {@link Map}, ready to pass to
     *         {@link ObdaMappingGenerator#generate(Map)}.
     */
    public static Map<String, Object> adminCodesOntology() {
        return Map.of(
            "classes", Map.of(
                "http://x/AdminCode", Map.of("label", "Admin Code")),
            "properties", Map.of(
                "http://x/AdminCode/code",
                    Map.of("type", "http://www.w3.org/2002/07/owl#DatatypeProperty", "label", "code"),
                // A natural-key property so the subject-column picker has an *_id to prefer.
                "http://x/AdminCode/adminCodeId",
                    Map.of("type", "http://www.w3.org/2002/07/owl#DatatypeProperty", "label", "id")),
            "mappings", Map.of(
                "http://x/AdminCode", Map.of("table", "normalized.admin_codes"),
                "http://x/AdminCode/code", Map.of("column", "admin_codes.code_value"),  // dotted
                "http://x/AdminCode/adminCodeId", Map.of("column", "admin_codes.admin_code_id")),  // *_id key
            "databases", List.of(
                Map.of("name", "normalized", "catalog", "AwsDataCatalog")));
    }

    /**
     * Two-class ontology with an FK {@code owl:ObjectProperty} and an
     * {@code xsd:boolean} column — the shape that exercised the gt-03 (FK join
     * reformulates to EMPTY) and gt-08 (boolean column → lower(boolean)) failures.
     *
     * <p>{@code Coverage} (table {@code normalized.coverage}) has:
     * <ul>
     *   <li>{@code coverage_id} (datatype, the *_id subject key);</li>
     *   <li>{@code is_deleted} (datatype, {@code xsd:boolean});</li>
     *   <li>{@code hasHolding} (object property, range {@code Holding}, mapped to
     *       {@code coverage.holding_id}) — the FK.</li>
     * </ul>
     * {@code Holding} (table {@code normalized.holding}) has {@code holding_id}
     * (datatype, *_id key) so its subject template is {@code <…/Holding/{holding_id}>},
     * which the FK IRI template must match for the join to resolve.
     *
     * @return ontology JSON as a nested {@link Map}.
     */
    public static Map<String, Object> coverageHoldingFkOntology() {
        return Map.of(
            "classes", Map.of(
                "http://x/Coverage", Map.of("label", "Coverage"),
                "http://x/Holding", Map.of("label", "Holding")),
            "properties", Map.of(
                "http://x/Coverage/coverage_id",
                    Map.of("type", "http://www.w3.org/2002/07/owl#DatatypeProperty"),
                "http://x/Coverage/is_deleted", Map.of(
                    "type", "http://www.w3.org/2002/07/owl#DatatypeProperty",
                    "range", "http://www.w3.org/2001/XMLSchema#boolean"),
                "http://x/Coverage/hasHolding", Map.of(
                    "type", "http://www.w3.org/2002/07/owl#ObjectProperty",
                    "range", "http://x/Holding"),
                "http://x/Holding/holding_id",
                    Map.of("type", "http://www.w3.org/2002/07/owl#DatatypeProperty")),
            "mappings", Map.of(
                "http://x/Coverage", Map.of("table", "normalized.coverage"),
                "http://x/Coverage/coverage_id", Map.of("column", "coverage.coverage_id"),
                "http://x/Coverage/is_deleted", Map.of("column", "coverage.is_deleted"),
                "http://x/Coverage/hasHolding", Map.of("column", "coverage.holding_id"),
                "http://x/Holding", Map.of("table", "normalized.holding"),
                "http://x/Holding/holding_id", Map.of("column", "holding.holding_id")),
            "databases", List.of(
                Map.of("name", "normalized", "catalog", "AwsDataCatalog")));
    }

    /**
     * Two-class ontology where the FK uses a KEY-PREFIX TRANSFORM (the gt-03/gt-04
     * shape): {@code Coverage.party_id} stores an UNPREFIXED id ('P000042') but
     * {@code Party.party_id} (the PK / subject key) is PREFIXED ('PARTY#P000042').
     * The FK object property {@code Coverage/hasParty} carries an rdfs:comment
     * documenting {@code CONCAT('PARTY#', coverage.party_id) = party.party_id}, which
     * the OBDA generator must parse to bake the prefix into the FK IRI template.
     *
     * @return ontology JSON as a nested {@link Map}.
     */
    public static Map<String, Object> coveragePartyPrefixFkOntology() {
        return Map.of(
            "classes", Map.of(
                "http://x/Coverage", Map.of("label", "Coverage"),
                "http://x/Party", Map.of("label", "Party")),
            "properties", Map.of(
                "http://x/Coverage/coverage_id",
                    Map.of("type", "http://www.w3.org/2002/07/owl#DatatypeProperty"),
                // The CONCAT transform is authored on the FK DATATYPE property (the
                // realistic shape — the metadata agent annotates the column property,
                // not the object property), which maps to the SAME coverage.party_id
                // column as hasParty. concatPrefixFor must find it via the sibling.
                "http://x/Coverage/party_id", Map.of(
                    "type", "http://www.w3.org/2002/07/owl#DatatypeProperty",
                    "comment", "Bridge FK to party, stored UNPREFIXED. JOIN party p ON "
                        + "CONCAT('PARTY#', coverage.party_id) = p.party_id."),
                "http://x/Coverage/hasParty", Map.of(
                    "type", "http://www.w3.org/2002/07/owl#ObjectProperty",
                    "range", "http://x/Party",
                    "comment", "Links Coverage to Party via party_id"),
                "http://x/Party/party_id",
                    Map.of("type", "http://www.w3.org/2002/07/owl#DatatypeProperty",
                           "comment", "Canonical PK, PREFIXED (e.g. 'PARTY#P000042').")),
            "mappings", Map.of(
                "http://x/Coverage", Map.of("table", "normalized.coverage"),
                "http://x/Coverage/coverage_id", Map.of("column", "coverage.coverage_id"),
                "http://x/Coverage/party_id", Map.of("column", "coverage.party_id"),
                "http://x/Coverage/hasParty", Map.of("column", "coverage.party_id"),
                "http://x/Party", Map.of("table", "normalized.party"),
                "http://x/Party/party_id", Map.of("column", "party.party_id")),
            "databases", List.of(Map.of("name", "normalized", "catalog", "AwsDataCatalog")));
    }

    /**
     * Ontology mapped to a FEDERATED S3-Tables catalog (not the default
     * {@code AwsDataCatalog}). Single class {@code http://x/Event} → table
     * {@code "analytics.events"} with one column property
     * {@code http://x/Event/id} → {@code "events.id"}; the {@code databases[]}
     * entry pins {@code analytics} to catalog {@code "s3tablescatalog/my-bucket"}.
     *
     * <p>Used to prove the handler preserves a federated catalog instead of
     * falling back to the {@code AwsDataCatalog} default.
     *
     * @return ontology JSON as a nested {@link Map}.
     */
    public static Map<String, Object> s3TablesOntology() {
        return Map.of(
            "classes", Map.of(
                "http://x/Event", Map.of("label", "Event")),
            "properties", Map.of(
                "http://x/Event/id",
                    Map.of("type", "http://www.w3.org/2002/07/owl#DatatypeProperty", "label", "id")),
            "mappings", Map.of(
                "http://x/Event", Map.of("table", "analytics.events"),
                "http://x/Event/id", Map.of("column", "events.id")),  // dotted
            "databases", List.of(
                Map.of("name", "analytics", "catalog", "s3tablescatalog/my-bucket")));
    }

    /**
     * A minimal {@link Context} stub for AgentCore Gateway invocations whose
     * {@code getClientContext().getCustom()} returns
     * {@code {"bedrockAgentCoreToolName": toolName}}. Every other Context accessor
     * is a no-op (null / 0) — the handler only ever reads the tool name, mirroring
     * how {@code lambda/neptune-tools/index.py} reads
     * {@code context.client_context.custom['bedrockAgentCoreToolName']}.
     *
     * @param toolName the Gateway tool name, e.g.
     *                 {@code "<target>___translate_sparql_to_sql"}.
     * @return a hand-rolled {@link Context} exposing only the tool name.
     */
    public static Context ctx(final String toolName) {
        return new StubContext(toolName);
    }

    /**
     * Hand-rolled (Mockito-free) {@link Context} that surfaces ONLY a
     * {@code bedrockAgentCoreToolName} custom value via its {@link ClientContext};
     * all other Context/ClientContext methods return null/empty.
     */
    private static final class StubContext implements Context {

        /** The wrapped client context carrying the tool-name custom map. */
        private final ClientContext clientContext;

        /**
         * @param toolName the Gateway tool name to expose under
         *                 {@code custom["bedrockAgentCoreToolName"]}.
         */
        StubContext(final String toolName) {
            this.clientContext = new ClientContext() {
                @Override
                public com.amazonaws.services.lambda.runtime.Client getClient() {
                    return null;
                }

                @Override
                public Map<String, String> getCustom() {
                    return Map.of("bedrockAgentCoreToolName", toolName);
                }

                @Override
                public Map<String, String> getEnvironment() {
                    return Map.of();
                }
            };
        }

        @Override
        public ClientContext getClientContext() {
            return clientContext;
        }

        @Override
        public String getAwsRequestId() {
            return null;
        }

        @Override
        public String getLogGroupName() {
            return null;
        }

        @Override
        public String getLogStreamName() {
            return null;
        }

        @Override
        public String getFunctionName() {
            return null;
        }

        @Override
        public String getFunctionVersion() {
            return null;
        }

        @Override
        public String getInvokedFunctionArn() {
            return null;
        }

        @Override
        public CognitoIdentity getIdentity() {
            return null;
        }

        @Override
        public int getRemainingTimeInMillis() {
            return 0;
        }

        @Override
        public int getMemoryLimitInMB() {
            return 0;
        }

        @Override
        public LambdaLogger getLogger() {
            return null;
        }
    }
}
