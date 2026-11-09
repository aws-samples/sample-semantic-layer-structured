import React, { useState, useEffect, useRef } from "react";
import {
  Container,
  Header,
  SpaceBetween,
  Button,
  Alert,
  Box,
  ProgressBar,
  StatusIndicator,
} from "@cloudscape-design/components";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ontologyAPI, neptuneAPI } from "../../services/api";
import CancelDraftButton from "../../components/CancelDraftButton";

// Explainer shown under the progress bar for a VKG build (one model-driven pass).
const ontologyBuildHelp =
  "Please wait while the knowledge graph is being built. The agent is " +
  "processing tables, generating ontology classes and properties, and " +
  "persisting to Neptune.";

/**
 * Describe the Hybrid phase currently running, for the text under the progress bar.
 *
 * A Hybrid build is two phases in one agent and they do very different work, so
 * one sentence cannot honestly cover both: phase 1 calls a model per table and
 * writes to the Bedrock Knowledge Base without touching Neptune, phase 2 touches
 * Neptune and calls no model at all.
 *
 * @param {string | null} status The raw backend status token
 *   ("buildingKB" | "buildingVKG" | anything else while starting up).
 * @returns {string} The sentence to display.
 */
function hybridPhaseHelp(status) {
  if (status === "buildingKB") {
    return (
      "Phase 1 of 2: documenting your tables. For each table the agent reads " +
      "its schema and a sample of its rows and writes a description into the " +
      "Bedrock Knowledge Base, which is what the query agent retrieves later."
    );
  }
  if (status === "buildingVKG") {
    return (
      "Phase 2 of 2: mapping structure. The agent maps every table and column " +
      "into the Virtual Knowledge Graph, infers the relationships between " +
      "tables, measures the join keys in Athena, and loads the result into " +
      "Neptune. No model is involved, so this phase is quick."
    );
  }
  return "Please wait while the semantic layer is being built.";
}

/**
 * Past-participle for the progress bar's "N of M tables ..." count.
 *
 * Both Hybrid phases count the SAME tables, so a bare "processed" makes the
 * counter look like it restarted when phase 2 begins. Naming the work done makes
 * the second pass read as a second pass.
 *
 * @param {string | null} status The raw backend status token.
 * @returns {string} The verb to append to the table count.
 */
function tableCountVerb(status) {
  if (status === "buildingKB") return "documented";
  if (status === "buildingVKG") return "mapped";
  return "processed";
}

export default function BuildKnowledgeGraph({ user }) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const id = searchParams.get("id");

  const [buildStatus, setBuildStatus] = useState("idle"); // idle, building, completed, failed
  const [progress, setProgress] = useState(0);
  const [statusMessage, setStatusMessage] = useState("");
  const [error, setError] = useState(null);
  const [buildStarted, setBuildStarted] = useState(false);
  // The stored layer type ("VKG" | "Hybrid"). This page serves both — the build
  // endpoint routes on the stored type — but every word on it described the VKG
  // ontology agent, so a Hybrid build claimed to be generating an OWL ontology
  // from design patterns and saving a Turtle backup, none of which it does.
  const [layerType, setLayerType] = useState(null);
  // The raw backend status token, kept alongside the rendered message so the
  // explainer below the progress bar can name the phase the user is waiting on.
  const [rawStatus, setRawStatus] = useState(null);

  // New progress tracking fields
  const [tablesProcessed, setTablesProcessed] = useState(0);
  const [totalTables, setTotalTables] = useState(0);
  const [currentTable, setCurrentTable] = useState("");

  const pollingIntervalRef = useRef(null);

  useEffect(() => {
    if (!id) {
      setError(
        "No ontology ID provided. Please complete previous steps first.",
      );
      return undefined;
    }
    // Read the type the previous step stored so the copy on this page matches
    // the build that will actually run. A failure here is deliberately NOT
    // surfaced as an error: it only costs the Hybrid-specific wording, and
    // blocking the build over a copy lookup would be worse than generic text.
    (async () => {
      try {
        const result = await ontologyAPI.getOntologyConfig(id);
        if (result.success) setLayerType(result.data?.type ?? null);
      } catch (err) {
        console.error("Could not read the semantic layer type:", err);
      }
    })();
    return () => {
      if (pollingIntervalRef.current) {
        clearInterval(pollingIntervalRef.current);
      }
    };
  }, [id]);

  // Hybrid runs two phases in one agent: table documentation into the Bedrock
  // Knowledge Base, then a deterministic structural VKG into Neptune. VKG runs
  // the single model-driven ontology generation agent. The two share no wording.
  const isHybrid = layerType === "Hybrid";

  const startBuild = async () => {
    setError(null);
    setBuildStatus("building");
    setBuildStarted(true);
    setProgress(0);
    setStatusMessage(
      isHybrid
        ? "Initializing semantic layer creation agent..."
        : "Initializing ontology generation agent...",
    );

    try {
      const result = await ontologyAPI.buildOntology(id);

      if (result.success) {
        // Start polling for build status
        startPolling();
      } else {
        setBuildStatus("failed");
        setError(result.error || "Failed to start build");
      }
    } catch (err) {
      setBuildStatus("failed");
      setError(err.message || "An error occurred");
    }
  };

  const startPolling = () => {
    pollingIntervalRef.current = setInterval(async () => {
      try {
        const result = await ontologyAPI.getBuildStatus(id);

        if (result.success) {
          const {
            status,
            progressPercent,
            tablesProcessed: processed,
            totalTables: total,
            currentTable: current,
            error: errorMsg,
          } = result.data;

          setRawStatus(status);

          // Update progress tracking fields
          if (processed !== undefined) setTablesProcessed(processed);
          if (total !== undefined) setTotalTables(total);
          if (current !== undefined) setCurrentTable(current);

          // Handle different status values. The Hybrid creation agent reports
          // its own two-phase tokens (buildingKB / buildingVKG / failed:<phase>)
          // rather than the single "processing" the ontology agent writes, so
          // every branch below matches on the whole family — an unrecognized
          // token leaves the page frozen on "Initializing" for the whole build
          // and, for a failure, forever.
          const inProgressStatuses = [
            "processing",
            "buildingKB",
            "buildingVKG",
          ];
          // What the user is actually waiting on, per phase.
          const phaseVerb = {
            buildingKB: "Documenting",
            buildingVKG: "Mapping",
          };
          if (status === "pending") {
            setStatusMessage("Queued for processing...");
            setProgress(0);
          } else if (inProgressStatuses.includes(status)) {
            // Use backend-provided progress percentage if available
            if (progressPercent !== undefined) {
              setProgress(progressPercent);
            }

            // Build informative status message
            const verb = phaseVerb[status] || "Processing";
            if (current && processed !== undefined && total !== undefined) {
              setStatusMessage(
                `${verb} table ${processed} of ${total}: ${current}`,
              );
            } else if (processed !== undefined && total !== undefined) {
              setStatusMessage(
                `${verb} tables... ${processed} of ${total} completed`,
              );
            } else {
              setStatusMessage(
                status === "processing"
                  ? "Processing ontology generation..."
                  : `${verb} tables...`,
              );
            }
          } else if (status === "completed" || status === "built") {
            setProgress(100);
            setStatusMessage("Build completed successfully!");
            setBuildStatus("completed");
            clearInterval(pollingIntervalRef.current);
          } else if (status && status.startsWith("failed")) {
            // "failed:kb" / "failed:vkg" carry the phase in the token; an exact
            // equality check would miss them and poll forever.
            setBuildStatus("failed");
            setError(errorMsg || `Build failed (${status})`);
            clearInterval(pollingIntervalRef.current);
          }
        } else {
          // Error fetching status
          console.error("Failed to fetch build status:", result.error);
        }
      } catch (err) {
        console.error("Error polling build status:", err);
        // Don't stop polling on temporary errors
      }
    }, 3000); // Poll every 3 seconds
  };

  const handleViewGraph = () => {
    navigate(`/admin/view-graph?id=${id}`);
  };

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description={
          isHybrid
            ? "Step 4 of 5: Document each table in the Bedrock Knowledge Base, then map its structure into a Virtual Knowledge Graph, using Bedrock AgentCore and AWS Strands SDK Agent"
            : "Step 4 of 5: Generate metadata with Virtual Knowledge Graph mappings using Bedrock AgentCore and AWS Strands SDK Agent"
        }
        actions={<CancelDraftButton ontologyId={id} />}
      >
        Build Knowledge Graph
      </Header>

      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      <Container>
        <SpaceBetween size="l">
          {buildStatus === "idle" && (
            <Box>
              <SpaceBetween size="m">
                <Box variant="p">
                  {isHybrid
                    ? "Ready to build the semantic layer. The AgentCore semantic layer creation agent runs two phases:"
                    : "Ready to build the knowledge graph. The AgentCore ontology generation agent will:"}
                </Box>
                {isHybrid ? (
                  <ul>
                    <li>
                      <strong>Phase 1 &mdash; Documentation.</strong> For each
                      selected table, read its schema and a sample of its rows,
                      then write a description of the table, its columns and its
                      business meaning into the Bedrock Knowledge Base for
                      retrieval at query time
                    </li>
                    <li>
                      <strong>Phase 2 &mdash; Structure.</strong> Map each table
                      to an OWL class and each column to a typed property, with
                      no model in the loop, so the mappings are exact and
                      reproducible
                    </li>
                    <li>
                      Infer relationships between tables from table and column
                      names, and confirm each join key by measuring it in Athena
                      rather than assuming it
                    </li>
                    <li>
                      Replace the layer&rsquo;s graph in Amazon Neptune with the
                      new structure, and save the N-Quads to S3
                    </li>
                  </ul>
                ) : (
                  <ul>
                    <li>
                      Download and analyze uploaded reference documents (if
                      provided)
                    </li>
                    <li>
                      Extract schema metadata from AWS Glue for each selected
                      table
                    </li>
                    <li>
                      Retrieve relevant ontology design patterns from Bedrock
                      Knowledge Base
                    </li>
                    <li>
                      Incorporate business terminology from reference documents
                    </li>
                    <li>
                      Generate OWL ontology in N-QUADS format with Virtual
                      Knowledge Graph mappings
                    </li>
                    <li>
                      Persist ontology directly to Amazon Neptune graph database
                    </li>
                    <li>Save Turtle format backup to S3 for reference</li>
                  </ul>
                )}
                <Alert type="info">
                  {isHybrid
                    ? "This process typically takes several minutes. Phase 1 writes one document per table and is the slower of the two; Phase 2 is deterministic and finishes quickly. You'll see real-time progress for each phase."
                    : "This process typically takes several minutes depending on the number of tables, uploaded documents, and complexity of relationships. You'll see real-time progress as each table is processed."}
                </Alert>
                <Box>
                  <Button variant="primary" onClick={startBuild}>
                    Start Build
                  </Button>
                </Box>
              </SpaceBetween>
            </Box>
          )}

          {buildStatus === "building" && (
            <Box>
              <SpaceBetween size="m">
                <StatusIndicator type="loading">
                  {statusMessage}
                </StatusIndicator>
                <ProgressBar
                  value={progress}
                  description={
                    totalTables > 0
                      ? `${tablesProcessed} of ${totalTables} tables ${tableCountVerb(rawStatus)}`
                      : "Processing..."
                  }
                />
                {currentTable && (
                  <Box variant="small" color="text-status-info">
                    Current table: <strong>{currentTable}</strong>
                  </Box>
                )}
                <Box variant="small" color="text-status-inactive">
                  {isHybrid ? hybridPhaseHelp(rawStatus) : ontologyBuildHelp}
                </Box>
              </SpaceBetween>
            </Box>
          )}

          {buildStatus === "completed" && (
            <Box>
              <SpaceBetween size="m">
                <Alert
                  type="success"
                  header={
                    isHybrid
                      ? "Semantic Layer Built Successfully!"
                      : "Knowledge Graph Built Successfully!"
                  }
                >
                  {isHybrid ? (
                    <>
                      All {totalTables} table{totalTables !== 1 ? "s" : ""}{" "}
                      {totalTables !== 1 ? "are" : "is"} documented in the
                      Bedrock Knowledge Base, and their structure and inferred
                      relationships are mapped into Amazon Neptune. You can now
                      view the knowledge graph and start querying your data
                      using natural language.
                    </>
                  ) : (
                    <>
                      The AgentCore ontology agent has generated the OWL
                      ontology with Virtual Knowledge Graph mappings and
                      persisted it to Amazon Neptune. Successfully processed{" "}
                      {totalTables} table{totalTables !== 1 ? "s" : ""}. You can
                      now view the knowledge graph and start querying your data
                      using natural language.
                    </>
                  )}
                </Alert>
                <ProgressBar
                  value={100}
                  description={`Completed - ${totalTables} table${totalTables !== 1 ? "s" : ""} processed`}
                />
                <Box>
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button variant="primary" onClick={handleViewGraph}>
                      View Knowledge Graph
                    </Button>
                    <Button onClick={() => navigate("/query/ask")}>
                      Start Querying
                    </Button>
                  </SpaceBetween>
                </Box>
              </SpaceBetween>
            </Box>
          )}

          {buildStatus === "failed" && (
            <Box>
              <SpaceBetween size="m">
                <Alert type="error" header="Build Failed">
                  {/* The Hybrid agent reports which phase failed in the status
                      token, and that changes what is on disk: a documentation
                      failure leaves nothing, whereas a mapping failure means the
                      Knowledge Base documents were written and only the graph is
                      missing. Saying just "the build failed" hides that. */}
                  {rawStatus === "failed:kb"
                    ? "The build failed while documenting your tables in the Bedrock Knowledge Base, so no graph was created."
                    : rawStatus === "failed:vkg"
                      ? "Your tables were documented in the Bedrock Knowledge Base, but the build failed while mapping their structure into Neptune, so the layer has no graph yet."
                      : "The knowledge graph build process encountered an error."}
                  {tablesProcessed > 0 && (
                    <>
                      {" "}
                      Processed {tablesProcessed} of {totalTables} tables before
                      failure.
                    </>
                  )}{" "}
                  Please check the error message above and try again.
                </Alert>
                <Box>
                  <Button
                    onClick={() => {
                      // Clear the phase too, or a retry renders the previous
                      // attempt's phase-specific copy until the first poll lands.
                      setRawStatus(null);
                      setBuildStatus("idle");
                    }}
                  >
                    Try Again
                  </Button>
                </Box>
              </SpaceBetween>
            </Box>
          )}
        </SpaceBetween>
      </Container>

      {!buildStarted && (
        <Box float="right">
          <Button onClick={() => navigate(`/admin/review-metadata?id=${id}`)}>
            Back
          </Button>
        </Box>
      )}
    </SpaceBetween>
  );
}
