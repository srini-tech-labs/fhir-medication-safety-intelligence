// Real-browser (headless Chromium via Playwright) smoke check of the running dev/preview server.
// Not a test suite (see src/App.test.tsx for that) -- a manual verification aid for UI/visual changes
// that jsdom-based component tests can't catch (real layout, real CSS, real screenshots).
//
// Usage (from frontend/):
//   npm run dev                        # in one terminal
//   node scripts/visual_check.mjs      # in another; screenshots land in /tmp/ui_screens
//
// Requires the Playwright browser binary once: npx playwright install chromium
import { chromium } from "playwright";
import fs from "node:fs";

const BASE_URL = process.env.VISUAL_CHECK_URL ?? "http://localhost:5173/";
const PATIENTS = (process.env.VISUAL_CHECK_PATIENTS ?? "P001,P006,P009,P010").split(",");
const OUT = process.env.VISUAL_CHECK_OUT ?? "/tmp/ui_screens";
fs.mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

const consoleErrors = [];
page.on("console", (msg) => { if (msg.type() === "error") consoleErrors.push(msg.text()); });
page.on("pageerror", (err) => consoleErrors.push(String(err)));

await page.goto(BASE_URL, { waitUntil: "networkidle" });
await page.waitForSelector(".api-status.ok", { timeout: 15000 });
console.log("API status:", await page.locator(".api-status").innerText());

for (const pid of PATIENTS) {
  await page.locator(".patient-btn", { hasText: pid }).click();
  await page.waitForSelector(".patient-head h2");
  await page.locator("button.btn-primary").click();
  await page.waitForSelector(".overall", { timeout: 15000 });
  await page.waitForFunction(() => {
    const body = document.querySelector("section.layer.ai .body");
    return body && !body.querySelector('[role="status"]'); // wait past "generating..."
  }, { timeout: 30000 });

  console.log(`\n=== ${pid} ===`);
  console.log("overall severity:", await page.locator(".overall .sev-badge").first().innerText());
  console.log("ai-mode:", (await page.locator(".ai-mode").count()) ? await page.locator(".ai-mode").innerText() : "(none)");
  await page.screenshot({ path: `${OUT}/${pid}.png`, fullPage: true });
}

console.log("\nconsole errors:", consoleErrors.length ? consoleErrors : "(none)");
console.log(`screenshots: ${OUT}/`);
await browser.close();
process.exit(consoleErrors.length ? 1 : 0);
