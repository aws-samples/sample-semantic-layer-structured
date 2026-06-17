import React, { useState, useEffect } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Button,
  SpaceBetween,
  Header,
  Container,
  Box,
  RadioGroup,
} from "@cloudscape-design/components";
import { ontologyAPI } from "../../services/api";
import CancelDraftButton from "../../components/CancelDraftButton";

export default function SelectSemanticLayerType({
  enableOntologyAgents = true,
  enableSemanticRag = false,
}) {
  const { id } = useParams();
  const navigate = useNavigate();
  const [selectedType, setSelectedType] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Auto-select SemanticRAG when ontology agents are off but Semantic RAG is on
  // (degenerate single-mode deployments).
  useEffect(() => {
    if (enableOntologyAgents || !enableSemanticRag) return;

    let cancelled = false;
    const autoSelect = async () => {
      setLoading(true);
      try {
        const result = await ontologyAPI.createOntologyConfig({
          id,
          type: "SemanticRAG",
        });
        if (cancelled) return;
        if (!result.success) {
          setError(result.error || "Failed to set semantic layer type");
          return;
        }
        navigate(`/admin/build-semantic-metadata/${id}`);
      } catch (e) {
        if (!cancelled) setError(e.message || "An error occurred");
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    autoSelect();
    return () => {
      cancelled = true;
    };
  }, [enableOntologyAgents, enableSemanticRag, id]);

  const allTypes = [
    {
      id: "VKG",
      name: "Virtual Knowledge Graph (VKG)",
      description: "Generates OWL metadata stored in Amazon Neptune.",
      enabled: enableOntologyAgents,
    },
    {
      id: "SemanticRAG",
      name: "Semantic RAG",
      description:
        "Generates metadata stored in Amazon Bedrock Knowledge Base.",
      enabled: enableSemanticRag,
    },
  ];
  const types = allTypes.filter((t) => t.enabled);

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
        setError(
          result.error || "Failed to update semantic layer configuration",
        );
        return;
      }
      if (selectedType === "VKG") {
        navigate(`/admin/build-graph?id=${id}`);
      } else {
        navigate(`/admin/build-semantic-metadata/${id}`);
      }
    } catch (e) {
      setError(e.message || "An error occurred");
    } finally {
      setLoading(false);
    }
  };

  // Degenerate config — no semantic-layer modes enabled.
  if (types.length === 0) {
    return (
      <SpaceBetween size="l">
        <Header variant="h1">Select Semantic Layer Type</Header>
        <Box color="text-status-info">
          No semantic-layer modes are enabled in this deployment.
        </Box>
      </SpaceBetween>
    );
  }

  // Single-mode SemanticRAG deployment — auto-redirect (handled by useEffect above).
  if (!enableOntologyAgents && enableSemanticRag) {
    return (
      <SpaceBetween size="l">
        <Header variant="h1">Select Semantic Layer Type</Header>
        {error ? (
          <Box color="text-status-error">{error}</Box>
        ) : (
          <Box>Configuring semantic layer...</Box>
        )}
      </SpaceBetween>
    );
  }

  return (
    <SpaceBetween size="l">
      <Header variant="h1" actions={<CancelDraftButton ontologyId={id} />}>
        Select Semantic Layer Type
      </Header>
      <Container>
        <SpaceBetween size="m">
          {/* Whole-card click selects the type — not just the radio. Each card
              is a clickable Container; the RadioGroup inside is the visual +
              a11y control, kept in sync with the card's selection state. */}
          {types.map((t) => {
            const isSelected = selectedType === t.id;
            return (
              <div
                key={t.id}
                role="radio"
                aria-checked={isSelected}
                tabIndex={0}
                onClick={() => setSelectedType(t.id)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    setSelectedType(t.id);
                  }
                }}
                style={{ cursor: "pointer" }}
              >
                <Container
                  variant="stacked"
                  disableContentPaddings={false}
                  data-selected={isSelected}
                  className={
                    isSelected
                      ? "layer-type-card layer-type-card--selected"
                      : "layer-type-card"
                  }
                >
                  <SpaceBetween size="xs">
                    <RadioGroup
                      // Single-option group per card; clicking the radio and
                      // clicking the card body both call setSelectedType(t.id).
                      value={isSelected ? t.id : null}
                      onChange={({ detail }) => setSelectedType(detail.value)}
                      items={[{ value: t.id, label: t.name }]}
                    />
                    <Box color="text-body-secondary" padding={{ left: "xl" }}>
                      {t.description}
                    </Box>
                  </SpaceBetween>
                </Container>
              </div>
            );
          })}
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
