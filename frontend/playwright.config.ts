import { defineConfig } from "@playwright/test";

// Browser checks for the pause / resume screens (ADR-0020). They run the real Next.js app
// against a small stateful mock of the backend and of Supabase auth (e2e/mock-api), so
// nothing here touches a database, a mailbox or a real account, and no email can be sent.
//
//   npm run e2e
//
// Set PLAYWRIGHT_CHROME_PATH to use an installed Chrome/Edge instead of Playwright's own
// download. Not part of CI.

const MOCK_PORT = Number(process.env.MOCK_API_PORT ?? 4010);
const APP_PORT = Number(process.env.E2E_APP_PORT ?? 3100);
const MOCK = `http://127.0.0.1:${MOCK_PORT}`;

export default defineConfig({
  testDir: "./e2e",
  testMatch: /.*\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  outputDir: "./e2e/test-results",
  use: {
    baseURL: `http://127.0.0.1:${APP_PORT}`,
    viewport: { width: 1280, height: 900 },
    trace: "retain-on-failure",
    launchOptions: process.env.PLAYWRIGHT_CHROME_PATH
      ? { executablePath: process.env.PLAYWRIGHT_CHROME_PATH }
      : {},
  },
  webServer: [
    {
      command: `node e2e/mock-api/server.mjs ${MOCK_PORT}`,
      url: `${MOCK}/__state`,
      reuseExistingServer: true,
      timeout: 30_000,
    },
    {
      command: `npx next dev --hostname 127.0.0.1 --port ${APP_PORT}`,
      url: `http://127.0.0.1:${APP_PORT}/auth/login`,
      reuseExistingServer: true,
      timeout: 180_000,
      env: {
        NEXT_PUBLIC_API_BASE_URL: MOCK,
        NEXT_PUBLIC_SUPABASE_URL: MOCK,
        NEXT_PUBLIC_SUPABASE_ANON_KEY: "e2e-anon-key",
        NEXT_TELEMETRY_DISABLED: "1",
      },
    },
  ],
});
