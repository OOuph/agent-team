// Starter Playwright config for a game repository run by agent-team (copy to the repository root).
// The dispatcher starts the server itself and passes BASE_URL; locally `npm run start` and `npx playwright test` work too.
import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.BASE_URL || process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:8080";

export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 30_000,
  expect: { timeout: 5_000 },
  retries: 0,
  reporter: process.env.CI ? [["json"]] : [["list"]],
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "phone", use: { ...devices["Pixel 7"], viewport: { width: 360, height: 740 } } },
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
  ],
});
