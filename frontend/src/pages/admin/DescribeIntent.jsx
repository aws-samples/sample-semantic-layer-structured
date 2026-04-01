import React, { useState, useEffect } from 'react';
import {
  Container,
  Header,
  SpaceBetween,
  FormField,
  Input,
  Textarea,
  Button,
  Alert,
  Box,
} from '@cloudscape-design/components';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { ontologyAPI } from '../../services/api';

export default function DescribeIntent({ user }) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const id = searchParams.get('id');

  const [name, setName] = useState('');
  const [dataSourcesDescription, setDataSourcesDescription] = useState('');
  const [useCasesDescription, setUseCasesDescription] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(false);

  useEffect(() => {
    if (id) {
      loadExistingConfig();
    }
  }, [id]);

  const loadExistingConfig = async () => {
    const result = await ontologyAPI.getOntologyConfig(id);
    if (result.success && result.data) {
      setName(result.data.name || '');
      setDataSourcesDescription(result.data.dataSourcesDescription || '');
      setUseCasesDescription(result.data.useCasesDescription || '');
    }
  };

  const handleSubmit = async () => {
    setError(null);
    setSuccess(false);
    setLoading(true);

    try {
      // Normalize name: lowercase and remove all spaces
      const normalizedName = name.toLowerCase().replace(/\s+/g, '');

      const data = {
        name: normalizedName,
        dataSourcesDescription,
        useCasesDescription,
        createdBy: user?.email || user?.username,
        status: 'draft',
      };

      if (id) {
        data.id = id;
      }

      const result = await ontologyAPI.createOntologyConfig(data);

      if (result.success) {
        setSuccess(true);
        const newOntologyId = result.data.id;

        // Navigate to next step after a brief delay
        setTimeout(() => {
          navigate(`/admin/select-datasources?id=${newOntologyId}`);
        }, 1500);
      } else {
        setError(result.error || 'Failed to save configuration');
      }
    } catch (err) {
      setError(err.message || 'An error occurred');
    } finally {
      setLoading(false);
    }
  };

  const isFormValid = () => {
    return name.trim().length > 0 && dataSourcesDescription.trim().length > 0 && useCasesDescription.trim().length > 0;
  };

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Step 1 of 5: Describe your data sources and intended use cases"
      >
        Describe Application Intent
      </Header>

      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      {success && (
        <Alert type="success">
          Configuration saved successfully! Redirecting to next step...
        </Alert>
      )}

      <Container>
        <SpaceBetween size="l">
          <FormField
            label="Semantic metadata name"
            description="A unique name for this semantic metadata (will be normalized to lowercase with no spaces for namespace)"
            constraintText="Name will be converted to lowercase and spaces removed"
          >
            <Input
              value={name}
              onChange={({ detail }) => setName(detail.value)}
              placeholder="Example: Insurance Policy Metadata (will become: insurancepolicymetadata)"
            />
          </FormField>

          <FormField
            label="Describe your data sources"
            description="Provide details about the data sources you have (e.g., DynamoDB tables, S3 data, data formats)"
          >
            <Textarea
              value={dataSourcesDescription}
              onChange={({ detail }) => setDataSourcesDescription(detail.value)}
              placeholder="Example: We have insurance policy data in DynamoDB tables including HOLDING, PARTY, COVERAGE, RELATION, and FINANCIALACTIVITY. Historical data is stored in S3 Parquet files partitioned by date."
              rows={6}
            />
          </FormField>

          <FormField
            label="Describe the use cases you want to enable"
            description="Explain what business questions or analyses you want to support with this semantic layer"
          >
            <Textarea
              value={useCasesDescription}
              onChange={({ detail }) => setUseCasesDescription(detail.value)}
              placeholder="Example: We want to enable business users to ask natural language questions about policy holders, coverage details, claims history, and financial transactions without needing to know SQL or data structures."
              rows={6}
            />
          </FormField>

          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => navigate('/admin')}>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleSubmit}
                loading={loading}
                disabled={!isFormValid() || loading}
              >
                Next: Select Data Sources
              </Button>
            </SpaceBetween>
          </Box>
        </SpaceBetween>
      </Container>

      <Container
        header={
          <Header variant="h2">
            Why This Matters
          </Header>
        }
      >
        <Box variant="p">
          Describing your data sources and use cases helps the system:
        </Box>
        <ul>
          <li>Understand the domain and context of your data</li>
          <li>Generate appropriate semantic metadata patterns and relationships</li>
          <li>Provide better natural language understanding</li>
        </ul>
      </Container>
    </SpaceBetween>
  );
}
