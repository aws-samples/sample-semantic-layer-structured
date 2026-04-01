import React from 'react';
import {
  Container,
  Header,
  SpaceBetween,
  Box,
  FormField,
  Input,
  Alert,
} from '@cloudscape-design/components';

export default function Settings({ user }) {
  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Configure your semantic layer preferences"
      >
        Settings
      </Header>

      <Container
        header={
          <Header variant="h2">
            User Profile
          </Header>
        }
      >
        <SpaceBetween size="m">
          <FormField label="Email">
            <Input value={user?.email || 'Not available'} disabled />
          </FormField>
          <FormField label="Username">
            <Input value={user?.username || 'Not available'} disabled />
          </FormField>
        </SpaceBetween>
      </Container>

      <Container
        header={
          <Header variant="h2">
            API Configuration
          </Header>
        }
      >
        <Alert type="info">
          API configuration is managed through environment variables.
          Contact your administrator for changes.
        </Alert>
        <SpaceBetween size="m">
          <FormField label="API Endpoint" description="Backend API URL">
            <Input
              value={process.env.REACT_APP_API_URL || '/api'}
              disabled
            />
          </FormField>
        </SpaceBetween>
      </Container>

      <Container
        header={
          <Header variant="h2">
            About
          </Header>
        }
      >
        <Box variant="p">
          <strong>AWS Semantic Layer</strong>
        </Box>
        <Box variant="p" color="text-body-secondary">
          Version 1.0.0
        </Box>
        <Box variant="p" color="text-body-secondary" margin={{ top: 's' }}>
          A unified semantic layer for querying operational and historical data
          using natural language, powered by Amazon Bedrock, Amazon Neptune,
          and AWS Glue.
        </Box>
      </Container>
    </SpaceBetween>
  );
}
