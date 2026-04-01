import React, { useState, useEffect, useRef } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  SpaceBetween, Header, Container, Button, ProgressBar,
  StatusIndicator, Box, Alert,
} from '@cloudscape-design/components';
import { startMetadataEnrichment, getMetadataEnrichmentStatus } from '../../services/api';

export default function BuildSemanticMetadata() {
  const { id } = useParams();
  const navigate = useNavigate();

  const [buildStatus, setBuildStatus] = useState('idle'); // idle | building | completed | failed
  const [progress, setProgress] = useState(0);
  const [tablesProcessed, setTablesProcessed] = useState(0);
  const [totalTables, setTotalTables] = useState(0);
  const [currentTable, setCurrentTable] = useState('');
  const [statusMessage, setStatusMessage] = useState('');
  const [error, setError] = useState(null);
  const [buildStarted, setBuildStarted] = useState(false);

  const pollingRef = useRef(null);

  useEffect(() => () => { if (pollingRef.current) clearInterval(pollingRef.current); }, []);

  const startBuild = async () => {
    setError(null);
    setBuildStatus('building');
    setBuildStarted(true);
    setProgress(0);
    setStatusMessage('Initializing metadata enrichment agent...');

    try {
      // Pass the ontology config ID — the backend reads dataSources (tables + catalogId) from DynamoDB
      const result = await startMetadataEnrichment(id);
      if (!result.success) {
        setBuildStatus('failed');
        setError(result.error || 'Failed to start enrichment');
        return;
      }
      startPolling(result.data.jobId);
    } catch (e) {
      setBuildStatus('failed');
      setError(e.message || 'An error occurred');
    }
  };

  const startPolling = (jobId) => {
    pollingRef.current = setInterval(async () => {
      try {
        const result = await getMetadataEnrichmentStatus(jobId);
        if (!result.success) return;

        const { status, progressPercent, tablesProcessed: processed, totalTables: total, currentTable: current, error: errMsg } = result.data;

        if (processed !== undefined) setTablesProcessed(processed);
        if (total !== undefined) setTotalTables(total);
        if (current !== undefined) setCurrentTable(current);
        if (progressPercent !== undefined) setProgress(progressPercent);

        if (status === 'pending') {
          setStatusMessage('Queued for processing...');
          setProgress(0);
        } else if (status === 'processing') {
          if (current && processed !== undefined && total !== undefined) {
            setStatusMessage(`Processing table ${processed} of ${total}: ${current}`);
          } else if (processed !== undefined && total !== undefined) {
            setStatusMessage(`Processing tables... ${processed} of ${total} completed`);
          } else {
            setStatusMessage('Processing metadata enrichment...');
          }
        } else if (status === 'completed') {
          setProgress(100);
          setStatusMessage('Enrichment completed successfully!');
          setBuildStatus('completed');
          clearInterval(pollingRef.current);
        } else if (status === 'failed') {
          setBuildStatus('failed');
          setError(errMsg || 'Enrichment failed');
          clearInterval(pollingRef.current);
        }
      } catch (e) {
        console.error('Error polling enrichment status:', e);
      }
    }, 3000);
  };

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Step 4 of 5: Create Semantic Metadata Layer"
      >
        Build Semantic RAG Metadata
      </Header>

      {error && <Alert type="error" dismissible onDismiss={() => setError(null)}>{error}</Alert>}

      <Container>
        <SpaceBetween size="l">
          {buildStatus === 'idle' && (
            <Box>
              <SpaceBetween size="m">
                <Box variant="p">
                  Ready to build semantic metadata. The AgentCore metadata agent will:
                </Box>
                <ul>
                  <li>Process each selected table from your configuration</li>
                  <li>Sample live data from Athena for additional context</li>
                  <li>Generate business-friendly descriptions for tables and all columns</li>
                  <li>Save markdown metadata documents to Amazon S3</li>
                  <li>Sync to the Amazon Bedrock Knowledge Base for natural language query support</li>
                </ul>
                <Alert type="info">
                  This process typically takes several minutes depending on the number of tables
                  and complexity of your schema. You'll see real-time progress as each table is
                  processed and enriched.
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
                <StatusIndicator type="loading">{statusMessage}</StatusIndicator>
                <ProgressBar
                  value={progress}
                  description={totalTables > 0 ? `${tablesProcessed} of ${totalTables} tables processed` : 'Processing...'}
                />
                {currentTable && (
                  <Box variant="small" color="text-status-info">
                    Current table: <strong>{currentTable}</strong>
                  </Box>
                )}
                <Box variant="small" color="text-status-inactive">
                  Please wait while semantic metadata is being generated. The agent is sampling
                  data, generating descriptions, and updating the Amazon Bedrock Knowledge Base.
                </Box>
              </SpaceBetween>
            </Box>
          )}

          {buildStatus === 'completed' && (
            <Box>
              <SpaceBetween size="m">
                <Alert type="success" header="Semantic Metadata Built Successfully!">
                  Successfully processed {totalTables} table{totalTables !== 1 ? 's' : ''}. You can now query your data using natural language.
                </Alert>
                <ProgressBar value={100} description={`Completed — ${totalTables} table${totalTables !== 1 ? 's' : ''} processed`} />
                <Box>
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button variant="primary" onClick={() => navigate(`/admin/view-semantic-metadata/${id}`)}>
                      View Results
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
                <Alert type="error" header="Enrichment Failed">
                  The metadata enrichment process encountered an error.
                  {tablesProcessed > 0 && <> Processed {tablesProcessed} of {totalTables} tables before failure.</>}
                  {' '}Please check the error message above and try again.
                </Alert>
                <Box>
                  <Button onClick={() => setBuildStatus('idle')}>Try Again</Button>
                </Box>
              </SpaceBetween>
            </Box>
          )}
        </SpaceBetween>
      </Container>

      {!buildStarted && (
        <Box float="right">
          <Button onClick={() => navigate(`/admin/review-metadata/${id}`)}>
            Back
          </Button>
        </Box>
      )}
    </SpaceBetween>
  );
}
