const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const api = fs.readFileSync(path.join(root, 'src/lib/api.ts'), 'utf8');
const editor = fs.readFileSync(path.join(root, 'src/pages/EditScriptPage.tsx'), 'utf8');

test('confirmation uses the server append endpoint instead of replacing approvals', () => {
  const block = api.slice(api.indexOf('export const useConfirm'), api.indexOf('export interface RewriteInput'));
  assert.match(block, /\/projects\/\$\{projectId\}\/approvals/);
  assert.match(block, /'POST'/);
  assert.doesNotMatch(block, /approvals:\s*\[/);
  assert.doesNotMatch(api, /patchProjectWithReplay/);
});

test('retry reuses a stable idempotency key per source job', () => {
  const block = api.slice(api.indexOf('export const useJobRetry'), api.indexOf('export interface RenderSegmentPlan'));
  assert.match(block, /retryTokens/);
  assert.match(block, /idempotencyKey/);
  assert.match(block, /retryTokens\.current\.get\(jobId\)/);
});

test('project patches are serialized and use the latest cached revision', () => {
  const block = api.slice(api.indexOf('export const usePatchProject'), api.indexOf('export const useDeleteProject'));
  assert.match(block, /queues\.current/);
  assert.match(block, /expectedRevision:\s*latest\.revision/);
});

test('rewrite acceptance is server-validated and branch uses the saved draft', () => {
  assert.match(editor, /script\/rewrite\/\$\{encodeURIComponent\(candidate\.jobId\)\}\/accept/);
  assert.match(editor, /baseRevisionHash/);
  assert.match(editor, /fromRevisionId:\s*latest\.currentDraftRevision/);
  assert.doesNotMatch(editor, /applyTurns\(\(prev\) => prev\.map\(\(t\) => byId\.get\(t\.id\)/);
});
