/**
 * Synth-time flag-matrix tests.
 *
 * The CDK app at `cdk/bin/app.ts` calls `app.synth()` as a top-level side effect, so
 * `require('../app')` inside Jest cannot safely be repeated. Instead, each test shells
 * out to `cdk synth` with a different `--context` combination and greps the resulting
 * cloud-assembly templates to assert which resources are present.
 *
 * This is slower than in-process synth (~30–60s per case on cold cache) but correctly
 * exercises the same code path operators run.
 */

import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';

const CDK_DIR = path.resolve(__dirname, '..', '..');

function synthAll(contextOverrides: Record<string, string>): string {
  // Use a unique --output dir so parallel runs don't stomp on each other.
  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cdk-synth-'));
  try {
    const args = ['cdk', 'synth', '--all', '--quiet', '--output', outDir];
    for (const [k, v] of Object.entries(contextOverrides)) {
      args.push('--context', `${k}=${v}`);
    }

    execFileSync('npx', args, {
      cwd: CDK_DIR,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, CI: 'true' },
    });

    // Concatenate every synthesized template into one searchable blob.
    const files = fs.readdirSync(outDir).filter((f) => f.endsWith('.template.json'));
    return files.map((f) => fs.readFileSync(path.join(outDir, f), 'utf8')).join('\n'); // nosemgrep: path-join-resolve-traversal,detect-non-literal-fs-filename — CDK synth-time test reading its own output dir; f is from readdirSync, not user input
  } finally {
    fs.rmSync(outDir, { recursive: true, force: true });
  }
}

describe('CDK flag matrix', () => {
  it('default flags: no semantic-rag KB, no metadata runtime, no synthetic loader', () => {
    const all = synthAll({});
    expect(all).not.toMatch(/semantic-rag/i);
    expect(all).not.toMatch(/MetadataRuntime/);
    expect(all).not.toMatch(/QuerySuggestionsRuntime/);
    expect(all).not.toMatch(/DynamoDBDataLoader/);
  });

  it('enableSemanticRag=true provisions the semantic-rag KB and metadata runtimes', () => {
    const all = synthAll({ enableSemanticRag: 'true' });
    expect(all).toMatch(/semantic-rag/i);
    expect(all).toMatch(/MetadataRuntime/);
  });

  it('enableAcordSampleData=true provisions the synthetic-data loader', () => {
    const all = synthAll({ enableAcordSampleData: 'true' });
    expect(all).toMatch(/DynamoDBDataLoader/);
  });

  it('both flags off: synthetic loader and metadata runtimes are both absent', () => {
    const all = synthAll({});
    expect(all).not.toMatch(/DynamoDBDataLoader/);
    expect(all).not.toMatch(/MetadataRuntime/);
  });
});
