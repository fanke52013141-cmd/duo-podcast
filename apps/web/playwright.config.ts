import { defineConfig, devices } from '@playwright/test';
import path from 'node:path';

const port = Number(process.env.DUOCAST_E2E_PORT ?? 18327);
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Invalid DUOCAST_E2E_PORT');
const repo = path.resolve(import.meta.dirname, '../..');
const run = process.env.DUOCAST_E2E_RUN ?? `${Date.now()}-${process.pid}`;
if (!/^[a-zA-Z0-9-]+$/.test(run)) throw new Error('Invalid DUOCAST_E2E_RUN');
process.env.DUOCAST_E2E_RUN = run;
const workspace = path.join(repo, '.review', 'browser-regression', run);
process.env.DUOCAST_E2E_WORKSPACE = workspace;
process.env.DUOCAST_E2E_PORT = String(port);

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: true,
  globalSetup: './tests/e2e/setup.ts',
  timeout: 40_000,
  globalTimeout: 180_000,
  expect: {timeout: 8_000},
  outputDir: path.join(workspace, 'results'),
  reporter: [['list'], ['html', {outputFolder:path.join(workspace,'report'),open:'never'}]],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    ...devices['Desktop Chrome'],
    viewport: {width:1440,height:1000},
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    serviceWorkers: 'block',
  },
});
