module.exports = {
  preset: 'ts-jest',
  testEnvironment: 'node',
  testMatch: ['**/__tests__/**/*.test.ts'],
  // CDK synth in a child process can take a while on cold cache.
  testTimeout: 180_000,
};
