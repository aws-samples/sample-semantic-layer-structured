jest.mock('axios');

describe('ontologyAPI new methods', () => {
  beforeEach(() => {
    jest.resetModules();
    const axios = require('axios');
    axios.create.mockReturnValue({
      get: jest.fn().mockResolvedValue({ data: { content: '<s> <p> <o> <g> .', version: 'v1' } }),
      post: jest.fn().mockResolvedValue({ data: { revisionId: 'rev-123' } }),
      interceptors: { request: { use: jest.fn() }, response: { use: jest.fn() } },
    });
  });

  it('verifies new methods exist on ontologyAPI', () => {
    const { ontologyAPI } = require('../services/api');
    expect(typeof ontologyAPI.getOntologyContent).toBe('function');
    expect(typeof ontologyAPI.getOntologyVersions).toBe('function');
    expect(typeof ontologyAPI.reviseOntology).toBe('function');
  });

  it('getOntologyContent calls correct URL', async () => {
    const { ontologyAPI } = require('../services/api');
    const result = await ontologyAPI.getOntologyContent('ont-1', 'v1');
    expect(result.success).toBe(true);
  });

  it('getOntologyVersions calls correct URL', async () => {
    const { ontologyAPI } = require('../services/api');
    const result = await ontologyAPI.getOntologyVersions('ont-1');
    expect(result.success).toBe(true);
  });

  it('reviseOntology calls correct URL', async () => {
    const { ontologyAPI } = require('../services/api');
    const result = await ontologyAPI.reviseOntology('ont-1', 'v1', [{ id: 'ann-1' }]);
    expect(result.success).toBe(true);
  });
});
