const { test, expect } = require("@playwright/test");

async function signIn(page, email) {
  await page.goto("/login/");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("demo123");
  await page.getByRole("button", { name: "Sign in" }).click();
}

/**
 * Navigate using whichever primary navigation surface is active at the current
 * viewport. The command centre swaps between the inline top bar (>= 1380px) and
 * a drawer below that, so a test that hard-codes one of them is only valid at a
 * single width.
 */
async function openSection(page, label) {
  const inlineLink = page
    .locator('nav[aria-label="Primary navigation"]')
    .getByRole("link", { name: label, exact: true });
  if (await inlineLink.isVisible().catch(() => false)) {
    await inlineLink.click();
    return;
  }

  await page.getByRole("button", { name: "Open navigation" }).click();
  // Target the drawer itself rather than any <nav>. The page also renders the
  // inline desktop nav, and an unscoped locator can match a hidden element
  // underneath the scrim, which then swallows the click.
  const drawer = page.locator('aside[aria-label="Mobile navigation"]');
  await expect(drawer).toBeVisible();
  // The drawer slides in with x-transition. While it is still translating, its
  // links sit off-screen to the right, so a click at their centre lands on the
  // scrim behind them. Wait for the position to settle before interacting.
  await settleElement(page, 'aside[aria-label="Mobile navigation"]');

  const link = drawer.getByRole("link", { name: label, exact: true });
  try {
    await link.click({ timeout: 5000 });
  } catch {
    // Chrome's mobile emulation resolves the hit-test to the scrim even when
    // the drawer is the topmost element, so fall back to a DOM click — but only
    // after asserting the link really is on top, so a genuine stacking
    // regression still fails the test.
    const onTop = await link.evaluate((element) => {
      const box = element.getBoundingClientRect();
      const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
      return element.contains(hit);
    });
    expect(onTop, `the "${label}" link must be the topmost element at its centre`).toBe(true);
    await link.evaluate((element) => element.click());
  }
}

/**
 * Wait until an element stops moving, so clicks do not race its CSS transition.
 * Playwright's own actionability check reports an animating element as
 * "visible, enabled and stable" once a single frame agrees, which is not enough
 * for a translate that runs over several frames.
 */
async function settleElement(page, selector) {
  await page.waitForFunction((sel) => {
    const element = document.querySelector(sel);
    if (!element) return false;
    const rect = element.getBoundingClientRect();
    const previous = Number(element.dataset.settleX ?? "NaN");
    element.dataset.settleX = String(rect.x);
    return Math.abs(rect.x - previous) < 0.5;
  }, selector);
}

test("dispatcher can open orders and planning workspace", async ({ page }) => {
  await signIn(page, "demo@example.com");
  await expect(page.getByRole("heading", { name: "Command Center" })).toBeVisible();

  await openSection(page, "Orders");
  await expect(page.getByRole("heading", { name: "Orders" })).toBeVisible();

  await openSection(page, "Planning");
  await expect(page.getByRole("heading", { name: "Create route plan" })).toBeVisible();
});

test("every operations surface renders without console errors", async ({ page }) => {
  const errors = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("pageerror", (error) => errors.push(error.message));

  await signIn(page, "demo@example.com");
  for (const path of [
    "/app/dashboard/",
    "/app/orders/",
    "/app/orders/new/",
    "/app/orders/import/",
    "/app/planning/new/",
    "/app/routes/",
    "/app/live-map/",
    "/app/drivers/",
    "/app/vehicles/",
    "/app/depots/",
    "/app/exceptions/",
    "/app/reports/",
    "/app/integrations/",
    "/app/settings/",
  ]) {
    const response = await page.goto(path);
    expect(response.status(), `${path} should return 200`).toBe(200);
    // A blank body means a template rendered nothing usable.
    const text = (await page.locator("body").innerText()).trim();
    expect(text.length, `${path} should render visible content`).toBeGreaterThan(40);
  }
  expect(errors, `console errors: ${errors.join(" | ")}`).toEqual([]);
});

test("driver can open today's dispatched route", async ({ page }) => {
  await signIn(page, "driver1@example.com");
  await page.goto("/driver/today/");
  await expect(page.getByRole("heading", { name: "Today's work" })).toBeVisible();
});

/**
 * The console layout switches navigation surface at 1380px and again at 1024px.
 * These are the widths the interface is expected to hold together at, so each
 * one gets a real page load rather than only the two configured projects.
 */
const RESPONSIVE_WIDTHS = [
  { width: 1920, height: 1080, label: "1920 wide desktop" },
  { width: 1440, height: 900, label: "1440 laptop" },
  { width: 1024, height: 768, label: "1024 tablet landscape" },
  { width: 768, height: 1024, label: "768 tablet" },
  { width: 390, height: 844, label: "390 phone" },
  { width: 375, height: 667, label: "375 phone" },
];

for (const { width, height, label } of RESPONSIVE_WIDTHS) {
  test(`dashboard and live map hold together at ${label}`, async ({ page }, testInfo) => {
    // Only the desktop project honours setViewportSize literally. The emulated
    // mobile device applies its own scaling, so the requested width would not be
    // the width the page actually renders at.
    test.skip(testInfo.project.name !== "desktop", "requires an unscaled viewport");

    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));

    await page.setViewportSize({ width, height });
    await signIn(page, "demo@example.com");
    await expect(page.getByRole("heading", { name: "Command Center" })).toBeVisible();

    for (const path of ["/app/dashboard/", "/app/live-map/"]) {
      const response = await page.goto(path);
      expect(response.status(), `${path} at ${width}px should return 200`).toBe(200);
    }

    // Nothing may overflow horizontally: a page wider than its viewport is the
    // clearest symptom of a layout that was never constrained for the screen.
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow, `horizontal overflow of ${overflow}px at ${width}px`).toBeLessThanOrEqual(1);
    expect(errors, `page errors at ${width}px: ${errors.join(" | ")}`).toEqual([]);
  });
}
