const { chromium } = require("@playwright/test");
const path = require("path");

const BASE = "http://127.0.0.1:8000";
// Screenshots live in docs/ and are committed, because a README whose images are
// gitignored shows nothing to anyone reviewing the repository.
const OUT = path.resolve(__dirname, "..", "docs", "screenshots");

async function signIn(page) {
  await page.goto(`${BASE}/login/`);
  await page.getByLabel("Email").fill("demo@example.com");
  await page.getByLabel("Password").fill("demo123");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForLoadState("load");
}

const PAGES = [
  ["dashboard", "/app/dashboard/"],
  ["orders", "/app/orders/"],
  ["planning-new", "/app/planning/new/"],
  ["routes", "/app/routes/"],
  ["live-map", "/app/live-map/"],
  ["fleet-drivers", "/app/drivers/"],
  ["exceptions", "/app/exceptions/"],
  ["reports", "/app/reports/"],
  ["settings", "/app/settings/"],
  ["integrations", "/app/integrations/"],
];

(async () => {
  const fs = require("fs");
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();

  for (const [width, height, tag] of [[1512, 950, "desktop"], [390, 844, "mobile"]]) {
    const context = await browser.newContext({ viewport: { width, height } });
    const page = await context.newPage();
    const errors = [];
    page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
    page.on("pageerror", (e) => errors.push(e.message));

    await signIn(page);

    for (const [name, url] of PAGES) {
      if (!url) continue;
      await page.goto(`${BASE}${url}`, { waitUntil: "load" });
      await page.waitForTimeout(1200);
      await page.screenshot({ path: path.join(OUT, `${tag}-${name}.png`) });
    }

    // Driver experience
    await context.clearCookies();
    await page.goto(`${BASE}/login/`);
    await page.getByLabel("Email").fill("driver1@example.com");
    await page.getByLabel("Password").fill("demo123");
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.goto(`${BASE}/driver/today/`, { waitUntil: "load" });
    await page.waitForTimeout(1200);
    await page.screenshot({ path: path.join(OUT, `${tag}-driver-today.png`) });

    console.log(`${tag}: captured ${PAGES.length} + driver, console errors: ${errors.length}`);
    if (errors.length) console.log("  ", errors.join(" | "));
    await context.close();
  }

  // Login page both widths
  for (const [width, height, tag] of [[1512, 950, "desktop"], [390, 844, "mobile"]]) {
    const context = await browser.newContext({ viewport: { width, height } });
    const page = await context.newPage();
    await page.goto(`${BASE}/login/`, { waitUntil: "load" });
    await page.waitForTimeout(600);
    await page.screenshot({ path: path.join(OUT, `${tag}-login.png`) });
    await context.close();
  }

  await browser.close();
  console.log("screenshots ->", OUT);
})();
