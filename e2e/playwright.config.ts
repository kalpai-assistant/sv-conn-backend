import { defineConfig } from "@playwright/test";

/**
 * SC-52 nightly smoke config. Deliberately no `use: { browser }` — this suite
 * exercises the HTTP API directly via Playwright's `request` context, so no
 * browser is booted. Full browser E2E is a follow-up.
 *
 * Required env vars at run time:
 *   STAGING_API_URL       — base URL, e.g. https://api.staging.eazyserv.in
 *   E2E_TEST_PHONE        — a whitelisted phone (matches backend E2E_TEST_PHONES)
 *   E2E_TEST_OTP          — matches backend E2E_TEST_OTP
 */
export default defineConfig({
  testDir: "./tests",
  // One worker so tests can share a phone number without stomping each other.
  workers: 1,
  // Nightly cron — a slow test is better than a false failure.
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  retries: process.env.CI ? 1 : 0,
  use: {
    baseURL: process.env.STAGING_API_URL,
    extraHTTPHeaders: {
      "content-type": "application/json",
    },
  },
});
