import React, { useState, useEffect, useRef } from 'react';
import {
  Container,
  Header,
  SpaceBetween,
  Button,
  Alert,
  Box,
  ProgressBar,
  StatusIndicator,
} from '@cloudscape-design/components';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { ontologyAPI, neptuneAPI } from '../../services/api';

export default function BuildKnowledgeGraph({ user }) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const id = searchParams.get('id');

  const [buildStatus, setBuildStatus] = useState('idle'); // idle, building, completed, failed
  const [progress, setProgress] = useState(0);
  const [statusMessage, setStatusMessage] = useState('');
  const [error, setError] = useState(null);
  const [buildStarted, setBuildStarted] = useState(false);

  // New progress tracking fields
  const [tablesProcessed, setTablesProcessed] = useState(0);
  const [totalTables, setTotalTables] = useState(0);
  const [currentTable, setCurrentTable] = useState('');

  const pollingIntervalRef = useRef(null);

  useEffect(() => {
    if (!id) {
      setError('No ontology ID provided. Please complete previous steps first.');
    }
    return () => {
      if (pollingIntervalRef.current) {
        clearInterval(pollingIntervalRef.current);
      }
    };
  }, [id]);

  const startBuild = async () => {
    setError(null);
    setBuildStatus('building');
    setBuildStarted(true);
    setProgress(0);
    setStatusMessage('Initializing ontology generation agent...');

    try {
      const result = await ontologyAPI.buildOntology(id);

      if (result.success) {
        // Start polling for build status
        startPolling();
      } else {
        setBuildStatus('failed');
        setError(result.error || 'Failed to start build');
      }
    } catch (err) {
      setBuildStatus('failed');
      setError(err.message || 'An error occurred');
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
            error: errorMsg
          } = result.data;

          // Update progress tracking fields
          if (processed !== undefined) setTablesProcessed(processed);
          if (total !== undefined) setTotalTables(total);
          if (current !== undefined) setCurrentTable(current);

          // Handle different status values
          if (status === 'pending') {
            setStatusMessage('Queued for processing...');
            setProgress(0);
          } else if (status === 'processing') {
            // Use backend-provided progress percentage if available
            if (progressPercent !== undefined) {
              setProgress(progressPercent);
            }

            // Build informative status message
            if (current && processed !== undefined && total !== undefined) {
              setStatusMessage(
                `Processing table ${processed} of ${total}: ${current}`
              );
            } else if (processed !== undefined && total !== undefined) {
              setStatusMessage(
                `Processing tables... ${processed} of ${total} completed`
              );
            } else {
              setStatusMessage('Processing ontology generation...');
            }
          } else if (status === 'completed' || status === 'built') {
            setProgress(100);
            setStatusMessage('Build completed successfully!');
            setBuildStatus('completed');
            clearInterval(pollingIntervalRef.current);
          } else if (status === 'failed') {
            setBuildStatus('failed');
            setError(errorMsg || 'Build failed');
            clearInterval(pollingIntervalRef.current);
          }
        } else {
          // Error fetching status
          console.error('Failed to fetch build status:', result.error);
        }
      } catch (err) {
        console.error('Error polling build status:', err);
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
        description="Step 4 of 5: Generate metadata with Virtual Knowledge Graph mappings using Bedrock AgentCore and AWS Strands SDK Agent"
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
          {buildStatus === 'idle' && (
            <Box>
              <SpaceBetween size="m">
                <Box variant="p">
                  Ready to build the knowledge graph. The AgentCore ontology generation agent will:
                </Box>
                <ul>
                  <li>Download and analyze uploaded reference documents (if provided)</li>
                  <li>Extract schema metadata from AWS Glue for each selected table</li>
                  <li>Retrieve relevant ontology design patterns from Bedrock Knowledge Base</li>
                  <li>Incorporate business terminology from reference documents</li>
                  <li>Generate OWL ontology in N-QUADS format with Virtual Knowledge Graph mappings</li>
                  <li>Persist ontology directly to Amazon Neptune graph database</li>
                  <li>Save Turtle format backup to S3 for reference</li>
                </ul>
                <Alert type="info">
                  This process typically takes several minutes depending on the number of tables,
                  uploaded documents, and complexity of relationships. You'll see real-time progress
                  as each table is processed.
                </Alert>
                <Box>
                  <Button variant="primary" onClick={startBuild}>
                    Start Build
                  </Button>
                </Box>
              </SpaceBetween>
            </Box>
          )}

          {buildStatus === 'building' && (
            <Box>
              <SpaceBetween size="m">
                <StatusIndicator type="loading">
                  {statusMessage}
                </StatusIndicator>
                <ProgressBar
                  value={progress}
                  description={
                    totalTables > 0
                      ? `${tablesProcessed} of ${totalTables} tables processed`
                      : 'Processing...'
                  }
                />
                {currentTable && (
                  <Box variant="small" color="text-status-info">
                    Current table: <strong>{currentTable}</strong>
                  </Box>
                )}
                <Box variant="small" color="text-status-inactive">
                  Please wait while the knowledge graph is being built. The agent is processing
                  tables, generating ontology classes and properties, and persisting to Neptune.
                </Box>
              </SpaceBetween>
            </Box>
          )}

          {buildStatus === 'completed' && (
            <Box>
              <SpaceBetween size="m">
                <Alert type="success" header="Knowledge Graph Built Successfully!">
                  The AgentCore ontology agent has generated the OWL ontology with Virtual Knowledge Graph
                  mappings and persisted it to Amazon Neptune. Successfully processed {totalTables} table{totalTables !== 1 ? 's' : ''}.
                  You can now view the knowledge graph and start querying your data using natural language.
                </Alert>
                <ProgressBar
                  value={100}
                  description={`Completed - ${totalTables} table${totalTables !== 1 ? 's' : ''} processed`}
                />
                <Box>
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button variant="primary" onClick={handleViewGraph}>
                      View Knowledge Graph
                    </Button>
                    <Button onClick={() => navigate('/query/ask')}>
                      Start Querying
                    </Button>
                  </SpaceBetween>
                </Box>
              </SpaceBetween>
            </Box>
          )}

          {buildStatus === 'failed' && (
            <Box>
              <SpaceBetween size="m">
                <Alert type="error" header="Build Failed">
                  The knowledge graph build process encountered an error.
                  {tablesProcessed > 0 && (
                    <> Processed {tablesProcessed} of {totalTables} tables before failure.</>
                  )}
                  {' '}Please check the error message above and try again.
                </Alert>
                <Box>
                  <Button onClick={() => setBuildStatus('idle')}>
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
