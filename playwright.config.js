const { defineConfig, devices } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:8000",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    // The command centre is a wide desktop console. Pin the viewport so layout
    // assertions do not shift with the host window, and give Alpine/Leaflet a
    // realistic budget to boot before interaction begins.
    viewport: { width: 1512, height: 950 },
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1512, height: 950 } } },
    { name: "mobile", use: { ...devices["Pixel 7"] } },
  ],
});

