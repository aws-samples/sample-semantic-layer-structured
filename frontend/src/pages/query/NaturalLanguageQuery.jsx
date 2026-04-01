import React, { useState, useEffect, useRef } from 'react';
import {
  Container,
  Header,
  SpaceBetween,
  Button,
  Alert,
  Box,
  FormField,
  Textarea,
  Select,
  StatusIndicator,
  ExpandableSection,
  Table,
  Tabs,
  Grid,
  Link,
} from '@cloudscape-design/components';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { queryAPI, ontologyAPI, submitMetadataQuery, pollMetadataQuery } from '../../services/api';
import ReactMarkdown from 'react-markdown';

export default function NaturalLanguageQuery({ user, addNotification }) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const prefilledQuestion = searchParams.get('q');

  const [question, setQuestion] = useState(prefilledQuestion || '');
  const [selectedOntology, setSelectedOntology] = useState(null);
  const [ontologies, setOntologies] = useState([]);
  const [submitting, setSubmitting] = useState(false);
  const [queryResult, setQueryResult] = useState(null);
  const [activeTab, setActiveTab] = useState('answer');
  const [error, setError] = useState(null);
  const [clarification, setClarification] = useState(null);
  // clarification shape: { question: string, options: [{id, label}] }
  const [suggestedQuestions, setSuggestedQuestions] = useState([]);
  const [loadingSuggestions, setLoadingSuggestions] = useState(false);
  const loadRequestRef = useRef(0);

  useEffect(() => {
    loadOntologies();
  }, []);

  useEffect(() => {
    if (selectedOntology?.value) {
      loadSuggestedQuestions(selectedOntology.value);
    }
  }, [selectedOntology?.value]);

  const markdownHeadingComponents = {
    h1: ({ children }) => <strong style={{ fontSize: '0.85rem' }}>{children}</strong>,
    h2: ({ children }) => <strong style={{ fontSize: '0.8rem' }}>{children}</strong>,
    h3: ({ children }) => <span style={{ fontWeight: 600, fontSize: '0.8rem' }}>{children}</span>,
    h4: ({ children }) => <span style={{ fontWeight: 600, fontSize: '0.8rem' }}>{children}</span>,
    h5: ({ children }) => <span style={{ fontWeight: 600 }}>{children}</span>,
    h6: ({ children }) => <span style={{ fontWeight: 600 }}>{children}</span>,
  };

  const loadOntologies = async () => {
    const result = await ontologyAPI.listOntologies();
    if (result.success) {
      const completed = (result.data.ontologies || []).filter(
        (o) => o.status === 'completed'
      );

      // Fetch full config for each ontology to get type and databaseName
      const options = [];
      for (const o of completed) {
        const configResult = await ontologyAPI.getOntologyConfig(o.id);
        if (configResult.success) {
          const config = configResult.data;
          options.push({
            label: o.name || o.id,
            value: o.id,
            type: config.type || 'VKG',
            id: o.id,
            name: o.name,
          });
        }
      }

      setOntologies(options);

      if (options.length > 0) {
        setSelectedOntology(options[0]);
      }
    }
  };

  const loadSuggestedQuestions = async (ontologyId) => {
    if (!ontologyId) {
      setSuggestedQuestions([]);
      return;
    }
    const requestId = ++loadRequestRef.current;
    setLoadingSuggestions(true);
    try {
      const result = await queryAPI.getSuggestedQuestions(ontologyId);
      if (requestId !== loadRequestRef.current) return; // stale response — newer request in flight
      if (result.success && Array.isArray(result.data?.suggestions)) {
        setSuggestedQuestions(result.data.suggestions);
      } else {
        setSuggestedQuestions([]);
      }
    } catch (_err) {
      if (requestId !== loadRequestRef.current) return;
      console.error('Failed to load query suggestions:', _err);
      setSuggestedQuestions([]);
    } finally {
      if (requestId === loadRequestRef.current) {
        setLoadingSuggestions(false);
      }
    }
  };

  // Normalize the metadata_service result shape to match the display components.
  // Backend returns { answer, sql_query, results[{}], n_quads[], reasoning{} }
  const normalizeMetadataQueryResult = (data) => {
    return {
      answer: data.answer || '',
      sql_query: data.sql_query || '',
      results: data.results || [],
      n_quads: data.n_quads || [],
      reasoning: data.reasoning || {},
    };
  };

  const handleSubmit = async (overrideQuestion = null) => {
    const currentQuestion = overrideQuestion || question;
    if (!currentQuestion.trim() || !selectedOntology) {
      setError('Please enter a question and select a semantic metadata layer');
      return;
    }

    setError(null);
    setSubmitting(true);
    setQueryResult(null);
    setActiveTab('answer');

    try {
      if (selectedOntology?.type === 'SemanticRAG') {
        // SemanticRAG path — render results inline (same as VKG path)
        try {
          const submitResult = await submitMetadataQuery(
            currentQuestion,
            selectedOntology.value
          );

          if (!submitResult.success) {
            setError(submitResult.error || 'Failed to submit metadata query');
            setSubmitting(false);
            return;
          }

          const { queryId } = submitResult.data;
          const notificationId = `query-${queryId}`;

          addNotification?.({
            type: 'info',
            content: 'Query submitted. Processing...',
            dismissible: true,
            id: notificationId,
          });

          const result = await pollMetadataQuery(queryId);

          if (result.success) {
            addNotification?.({
              type: 'success',
              content: 'Query completed successfully!',
              dismissible: true,
              id: notificationId,
            });
            setQueryResult(normalizeMetadataQueryResult(result.data));
            setClarification(null);
          } else {
            setError(result.error || 'Query failed');
            addNotification?.({
              type: 'error',
              content: `Query failed: ${result.error || 'Unknown error'}`,
              dismissible: true,
              id: notificationId,
            });
          }
        } catch (err) {
          setError(err.message || 'An error occurred processing metadata query');
        }
      } else {
        // Existing VKG path
        const result = await queryAPI.submitQuery(
          currentQuestion,
          selectedOntology.value
        );

        if (result.success) {
          const queryId = result.data.queryId;
          // Use a stable notification ID so subsequent updates replace this banner
          // instead of stacking a new one on top of the "Processing…" banner.
          const notificationId = `query-${queryId}`;

          addNotification?.({
            type: 'info',
            content: 'Query submitted. Processing...',
            dismissible: true,
            id: notificationId,
          });

          const finalResult = await queryAPI.pollQueryUntilComplete(queryId);

          if (finalResult.success) {
            const data = finalResult.data;
            if (data.needs_clarification) {
              setClarification({
                question: data.clarification_question,
                options: data.options || [],
              });
            } else {
              setQueryResult(data);
              setClarification(null);
            }

            // Same ID → replaces the "Processing…" banner
            addNotification?.({
              type: 'success',
              content: 'Query completed successfully!',
              dismissible: true,
              id: notificationId,
            });
          } else {
            setError(finalResult.error || 'Query failed');
            // Same ID → replaces the "Processing…" banner
            addNotification?.({
              type: 'error',
              content: `Query failed: ${finalResult.error || 'Unknown error'}`,
              dismissible: true,
              id: notificationId,
            });
          }
        } else {
          setError(result.error || 'Failed to submit query');
        }
      }
    } catch (err) {
      setError(err.message || 'An error occurred');
    } finally {
      setSubmitting(false);
    }
  };

  const handleClarification = (option) => {
    const enriched = `${question} — clarification: ${option.label}`;
    setClarification(null);
    setQuestion(enriched);
    handleSubmit(enriched);
  };

  const handleAskFollowUp = () => {
    setQuestion('');
    setQueryResult(null);
    setClarification(null);
    setActiveTab('answer');
  };

  // Extract plain-text answer — guard against the agent embedding the full
  // JSON blob inside the answer field (common with some model responses).
  const extractAnswer = (raw) => {
    if (!raw) return '';
    if (typeof raw !== 'string') return String(raw);
    const trimmed = raw.trim();
    // If it looks like JSON, try to parse and pull the inner answer field
    if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
      try {
        const parsed = JSON.parse(trimmed);
        if (parsed && typeof parsed.answer === 'string' && parsed.answer) {
          return parsed.answer;
        }
      } catch (_) {
        // Not valid JSON — fall through and use raw string
      }
    }
    return raw;
  };

  // Derive display data from result
  const results = queryResult?.results || [];
  const sqlQuery = queryResult?.sql_query || '';
  const reasoning = queryResult?.reasoning || {};
  const hasReasoning = reasoning && Object.keys(reasoning).some((k) => reasoning[k]);
  const kbSources = (queryResult?.n_quads || []).filter((s) => typeof s === 'object' && s.sourceUri !== undefined);
  const columnDefs =
    results.length > 0
      ? Object.keys(results[0]).map((key) => ({
          id: key,
          header: key,
          cell: (item) => {
            const val = item[key];
            return val === null || val === undefined || val === '' ? '-' : String(val);
          },
        }))
      : [];

  const resultsTabs = queryResult
    ? [
        {
          label: 'Answer',
          id: 'answer',
          content: (
            <Box padding="m">
              <ReactMarkdown>{extractAnswer(queryResult.answer) || 'No answer generated'}</ReactMarkdown>
            </Box>
          ),
        },
        {
          label: sqlQuery ? 'SQL Query ✓' : 'SQL Query',
          id: 'sql',
          content: sqlQuery ? (
            <Box padding="m">
              <pre
                style={{
                  margin: 0,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  fontFamily: 'monospace',
                  fontSize: '13px',
                  background: 'var(--color-background-code-editor-default, #f4f4f4)',
                  padding: '16px',
                  borderRadius: '4px',
                }}
              >
                {sqlQuery}
              </pre>
            </Box>
          ) : (
            <Box padding="m" color="text-status-inactive" textAlign="center">
              No SQL query available
            </Box>
          ),
        },
        {
          label: `Results${results.length > 0 ? ` (${results.length})` : ''}`,
          id: 'results',
          content:
            results.length > 0 ? (
              <Table
                columnDefinitions={columnDefs}
                items={results}
                variant="embedded"
                stripedRows
                stickyHeader
                empty={
                  <Box textAlign="center" color="text-status-inactive">
                    No results found
                  </Box>
                }
              />
            ) : (
              <Box padding="m" color="text-status-inactive" textAlign="center">
                No result rows returned
              </Box>
            ),
        },
      ]
    : [];

  const suggestedQuestionsSidebar = (
    <Container
      header={
        <Header
          variant="h2"
          description={
            loadingSuggestions
              ? 'Loading suggestions for this layer...'
              : "Click 'Try this' to populate the question field"
          }
        >
          Suggested Questions
        </Header>
      }
    >
      {loadingSuggestions ? (
        <Box textAlign="center" padding="m">
          <StatusIndicator type="loading">
            Generating suggestions...
          </StatusIndicator>
        </Box>
      ) : suggestedQuestions.length === 0 ? (
        <Box color="text-status-inactive" padding="m">
          Select a semantic metadata layer to see suggested questions.
        </Box>
      ) : (
        <SpaceBetween size="m">
          {suggestedQuestions.map((item, index) => (
            <Box key={index}>
              <SpaceBetween size="xxs">
                <Box variant="small" color="text-status-inactive">
                  {item.category}
                </Box>
                <Box variant="p">{item.question}</Box>
                <Link
                  onFollow={() => setQuestion(item.question)}
                  variant="secondary"
                >
                  Try this
                </Link>
              </SpaceBetween>
            </Box>
          ))}
        </SpaceBetween>
      )}
    </Container>
  );

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Ask questions about your data in natural language"
      >
        Natural Language Query
      </Header>

      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      <Grid
        gridDefinition={[
          { colspan: { default: 12, s: 8 } },
          { colspan: { default: 12, s: 4 } },
        ]}
      >
        <SpaceBetween size="l">
          <Container>
            <SpaceBetween size="l">
              <FormField
                label="Select Semantic Metadata Layer"
                description="Choose which semantic metadata to query against"
              >
                <Select
                  selectedOption={selectedOntology}
                  onChange={({ detail }) => setSelectedOntology(detail.selectedOption)}
                  options={ontologies}
                  placeholder="Select a semantic metadata layer"
                  disabled={ontologies.length === 0}
                  empty="No ontologies available"
                />
              </FormField>

              <FormField
                label="Your Question"
                description="Ask any question about your data in plain English"
              >
                <Textarea
                  value={question}
                  onChange={({ detail }) => setQuestion(detail.value)}
                  placeholder="Example: What are the active policies for customers in New York state?"
                  rows={4}
                  disabled={submitting}
                />
              </FormField>

              <Box>
                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    variant="primary"
                    onClick={() => handleSubmit()}
                    loading={submitting}
                    disabled={!question.trim() || !selectedOntology || submitting}
                  >
                    {submitting ? 'Processing...' : 'Ask Question'}
                  </Button>
                  {queryResult && (
                    <Button onClick={handleAskFollowUp}>
                      Ask Another Question
                    </Button>
                  )}
                </SpaceBetween>
              </Box>
            </SpaceBetween>
          </Container>

          {clarification && !submitting && (
            <Container
              header={<Header variant="h2">Clarification needed</Header>}
            >
              <SpaceBetween size="m">
                <Box variant="p">{clarification.question}</Box>
                <SpaceBetween direction="horizontal" size="xs">
                  {clarification.options.map((opt) => (
                    <Button key={opt.id} onClick={() => handleClarification(opt)}>
                      {opt.label}
                    </Button>
                  ))}
                </SpaceBetween>
              </SpaceBetween>
            </Container>
          )}

          {submitting && (
            <Container>
              <Box textAlign="center" padding="l">
                <StatusIndicator type="loading">
                  Processing your query. This may take a few moments...
                </StatusIndicator>
              </Box>
            </Container>
          )}

          {queryResult && (
            <SpaceBetween size="l">
              <Container
                header={<Header variant="h2">Query Results</Header>}
              >
                <Tabs
                  tabs={resultsTabs}
                  activeTabId={activeTab}
                  onChange={({ detail }) => setActiveTab(detail.activeTabId)}
                />
              </Container>

              {kbSources.length > 0 && (
                <Container>
                  <ExpandableSection
                    headerText={`Knowledge Base Sources (${kbSources.length})`}
                    variant="container"
                  >
                    <SpaceBetween size="m">
                      {kbSources.map((src, i) => (
                        <Box key={i}>
                          <SpaceBetween size="xxs">
                            <Box variant="awsui-key-label">
                              {src.tableName
                                ? `${src.tableName}${src.database ? ` (${src.database})` : ''}`
                                : src.sourceUri || `Source ${i + 1}`}
                            </Box>
                            {src.excerpt && (
                              <Box color="text-body-secondary" fontSize="body-s">
                                <ReactMarkdown components={markdownHeadingComponents}>{src.excerpt}</ReactMarkdown>
                              </Box>
                            )}
                            <Box fontSize="body-s" color="text-status-inactive">
                              Relevance: {(src.score * 100).toFixed(0)}%
                            </Box>
                          </SpaceBetween>
                        </Box>
                      ))}
                    </SpaceBetween>
                  </ExpandableSection>
                </Container>
              )}

              {hasReasoning && (
                <Container>
                  <ExpandableSection
                    headerText="View Reasoning Steps"
                    variant="container"
                  >
                    <SpaceBetween size="m">
                      {reasoning.interpretation && (
                        <Box>
                          <Box variant="awsui-key-label">1. Question Interpretation</Box>
                          <Box variant="p">{reasoning.interpretation}</Box>
                        </Box>
                      )}

                      {reasoning.graphTraversal && (
                        <Box>
                          <Box variant="awsui-key-label">2. Context Retrieval</Box>
                          <Box variant="p">{reasoning.graphTraversal}</Box>
                        </Box>
                      )}

                      {reasoning.dataSourceSelection && (
                        <Box>
                          <Box variant="awsui-key-label">3. Data Source Selection</Box>
                          <Box variant="p">{reasoning.dataSourceSelection}</Box>
                        </Box>
                      )}

                      {reasoning.sqlQuery && (
                        <Box>
                          <Box variant="awsui-key-label">4. SQL Query Generated</Box>
                          <Box padding="s">
                            <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontFamily: 'monospace', fontSize: '13px' }}>
                              {reasoning.sqlQuery}
                            </pre>
                          </Box>
                        </Box>
                      )}

                      {reasoning.summarization && (
                        <Box>
                          <Box variant="awsui-key-label">5. Result Summary</Box>
                          <Box variant="p">{reasoning.summarization}</Box>
                        </Box>
                      )}
                    </SpaceBetween>
                  </ExpandableSection>
                </Container>
              )}
            </SpaceBetween>
          )}

          {ontologies.length === 0 && (
            <Container>
              <Alert type="warning">
                No semantic metadata layers available. Please complete the admin setup process first.
              </Alert>
              <Box margin={{ top: 'm' }}>
                <Button onClick={() => navigate('/admin')}>
                  Go to Admin Dashboard
                </Button>
              </Box>
            </Container>
          )}
        </SpaceBetween>

        {suggestedQuestionsSidebar}
      </Grid>
    </SpaceBetween>
  );
}
