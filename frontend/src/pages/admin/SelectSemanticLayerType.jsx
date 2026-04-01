import React, { useState, useEffect } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Cards, Button, SpaceBetween, Header, Container, Box } from '@cloudscape-design/components';
import { ontologyAPI } from '../../services/api';

export default function SelectSemanticLayerType({ enableOntologyAgents = true }) {
  const { id } = useParams();
  const navigate = useNavigate();
  const [selectedType, setSelectedType] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (enableOntologyAgents) return;

    let cancelled = false;
    const autoSelect = async () => {
      setLoading(true);
      try {
        const result = await ontologyAPI.createOntologyConfig({ id, type: 'SemanticRAG' });
        if (cancelled) return;
        if (!result.success) {
          setError(result.error || 'Failed to set semantic layer type');
          return;
        }
        navigate(`/admin/build-semantic-metadata/${id}`);
      } catch (e) {
        if (!cancelled) setError(e.message || 'An error occurred');
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    autoSelect();
    return () => { cancelled = true; };
  }, [enableOntologyAgents, id]);

  const types = [
    {
      id: 'VKG',
      name: 'Virtual Knowledge Graph (VKG)',
      description: 'Generates OWL metadata stored in Amazon Neptune.',
    },
    {
      id: 'SemanticRAG',
      name: 'Semantic RAG',
      description: 'Generates metadata stored in Amazon Bedrock Knowledge Base.',
    },
  ];

  const handleContinue = async () => {
    if (!selectedType) return;
    setLoading(true);
    setError(null);
    try {
      const result = await ontologyAPI.createOntologyConfig({
        id: id,
        type: selectedType,
      });
      if (!result.success) {
        setError(result.error || 'Failed to update semantic layer configuration');
        return;
      }
      if (selectedType === 'VKG') {
        navigate(`/admin/build-graph?id=${id}`);
      } else {
        navigate(`/admin/build-semantic-metadata/${id}`);
      }
    } catch (e) {
      setError(e.message || 'An error occurred');
    } finally {
      setLoading(false);
    }
  };

  if (!enableOntologyAgents) {
    return (
      <SpaceBetween size="l">
        <Header variant="h1">Select Semantic Layer Type</Header>
        {error
          ? <Box color="text-status-error">{error}</Box>
          : <Box>Configuring semantic layer...</Box>
        }
      </SpaceBetween>
    );
  }

  return (
    <SpaceBetween size="l">
      <Header variant="h1">Select Semantic Layer Type</Header>
      <Container>
        <SpaceBetween size="m">
          <Cards
            items={types}
            cardDefinition={{
              header: item => item.name,
              sections: [{ content: item => item.description }],
            }}
            selectionType="single"
            selectedItems={selectedType ? [types.find(t => t.id === selectedType)] : []}
            onSelectionChange={({ detail }) =>
              setSelectedType(detail.selectedItems[0]?.id || null)
            }
            trackBy="id"
          />
          {error && <Box color="text-status-error">{error}</Box>}
          <Button
            variant="primary"
            disabled={!selectedType}
            loading={loading}
            onClick={handleContinue}
          >
            Continue
          </Button>
        </SpaceBetween>
      </Container>
    </SpaceBetween>
  );
}
