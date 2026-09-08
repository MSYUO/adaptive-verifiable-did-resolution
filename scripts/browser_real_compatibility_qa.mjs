// Minimal Chrome DevTools Protocol QA for three real DID compatibility cases.
// Requires an already-running AVDR dashboard and Chromium/Edge debug target.

import fs from "node:fs";
import path from "node:path";

const [dashboardUrl, debugPort, screenshotDirectory] = process.argv.slice(2);
if (!dashboardUrl || !debugPort || !screenshotDirectory) {
  throw new Error(
    "usage: node scripts/browser_real_compatibility_qa.mjs " +
      "<dashboard-url> <debug-port> <screenshot-dir>",
  );
}

const cases = [
  {
    method: "key",
    did: "did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK",
  },
  { method: "web", did: "did:web:danubetech.com" },
  {
    method: "ethr",
    did: "did:ethr:0xb9c5714089478a327f09197987f16f9e5d936e8a",
  },
];

const targets = await fetch(`http://127.0.0.1:${debugPort}/json/list`).then(
  async (response) => {
    if (!response.ok) throw new Error(`DevTools target listing failed: ${response.status}`);
    return response.json();
  },
);
const target = targets.find((entry) => entry.type === "page");
if (!target?.webSocketDebuggerUrl) throw new Error("No browser page target is available");

const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener("open", resolve, { once: true });
  socket.addEventListener("error", reject, { once: true });
});

let nextId = 1;
const pending = new Map();
const events = new Map();
const consoleErrors = [];
socket.addEventListener("message", (event) => {
  const message = JSON.parse(event.data);
  if (message.id) {
    const waiter = pending.get(message.id);
    if (!waiter) return;
    pending.delete(message.id);
    if (message.error) waiter.reject(new Error(JSON.stringify(message.error)));
    else waiter.resolve(message.result);
    return;
  }
  if (message.method === "Runtime.exceptionThrown") {
    consoleErrors.push(message.params.exceptionDetails.text);
  }
  const waiters = events.get(message.method) || [];
  events.delete(message.method);
  waiters.forEach((resolve) => resolve(message.params));
});

function send(method, params = {}) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

function waitForEvent(method, timeoutMs = 15000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`Timed out waiting for ${method}`)), timeoutMs);
    const wrapped = (value) => {
      clearTimeout(timer);
      resolve(value);
    };
    const waiters = events.get(method) || [];
    waiters.push(wrapped);
    events.set(method, waiters);
  });
}

async function evaluate(expression) {
  const result = await send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || "evaluation failed");
  return result.result.value;
}

async function waitFor(expression, timeoutMs = 45000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(`Boolean(${expression})`)) return;
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  throw new Error(`Timed out waiting for browser condition: ${expression}`);
}

async function capture(name) {
  const result = await send("Page.captureScreenshot", {
    format: "png",
    fromSurface: true,
    captureBeyondViewport: true,
  });
  fs.mkdirSync(screenshotDirectory, { recursive: true });
  const output = path.join(screenshotDirectory, `${name}.png`);
  fs.writeFileSync(output, Buffer.from(result.data, "base64"));
  return output;
}

await send("Page.enable");
await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", {
  width: 1920,
  height: 1080,
  deviceScaleFactor: 1,
  mobile: false,
});
const loaded = waitForEvent("Page.loadEventFired");
await send("Page.navigate", { url: dashboardUrl });
await loaded;
await waitFor(
  "document.querySelector('input[value=\"single-static\"]') && " +
    "document.querySelector('#healthText')?.textContent.includes('Router online')",
);
await evaluate("document.querySelector('input[value=\"single-static\"]').click()");

const results = [];
let previousRequestId = null;
for (const item of cases) {
  await evaluate(`(() => {
    const input = document.querySelector('#didInput');
    input.value = ${JSON.stringify(item.did)};
    input.dispatchEvent(new Event('input', { bubbles: true }));
    document.querySelector('#resolveForm').requestSubmit();
  })()`);
  await waitFor(
    `!document.querySelector('#resultPanel')?.hidden && ` +
      `document.querySelector('#resultDid')?.textContent === ${JSON.stringify(item.did)} && ` +
      `!document.querySelector('#resolveButton')?.disabled`,
  );
  await waitFor("!document.querySelector('#verifyReceiptButton')?.hidden");
  await evaluate("document.querySelector('#verifyReceiptButton').click()");
  await waitFor(
    "document.querySelector('#auditVerifyMessage')?.textContent.includes('verified locally')",
  );

  const state = await evaluate(`(() => {
    const metrics = Object.fromEntries(
      [...document.querySelectorAll('#metricsGrid .metric')].map((node) => [
        node.querySelector('.metric-label')?.textContent,
        node.querySelector('.metric-value')?.textContent,
      ]),
    );
    return {
      status: document.querySelector('#resultStatus')?.textContent,
      requestId: document.querySelector('#requestId')?.textContent,
      did: document.querySelector('#resultDid')?.textContent,
      mode: document.querySelector('#modeBadge')?.textContent,
      primaryMode: document.querySelector('#primaryModeLabel')?.textContent,
      evidence: document.querySelector('#evidenceNotice')?.textContent,
      acceptance: document.querySelector('#acceptanceValue')?.textContent,
      provider: metrics['Returned by'],
      auditReceipt: document.querySelector('#auditReceiptStatus')?.textContent,
      receiptHash: document.querySelector('#auditReceiptHash')?.textContent,
      verification: document.querySelector('#auditVerificationStatus')?.textContent,
      verifyMessage: document.querySelector('#auditVerifyMessage')?.textContent,
      anchor: document.querySelector('#auditAnchorStatus')?.textContent,
    };
  })()`);
  state.method = item.method;
  state.traceReplaced = previousRequestId === null || state.requestId !== previousRequestId;
  previousRequestId = state.requestId;
  state.qualified =
    state.status === "Accepted" &&
    state.did === item.did &&
    state.mode === "REAL" &&
    state.primaryMode === "REAL" &&
    state.evidence?.startsWith("REAL PROVIDER MODE") &&
    state.acceptance?.includes("W3C basic acceptance: passed") &&
    state.provider === "uniresolver-dif-dev" &&
    state.auditReceipt === "Recorded" &&
    state.receiptHash?.startsWith("sha256:") &&
    state.verification === "Local integrity verified" &&
    state.verifyMessage === "Receipt integrity verified locally." &&
    state.anchor === "not configured" &&
    state.traceReplaced;
  if (state.qualified) {
    state.screenshot = await capture(`real_did_${item.method}`);
  }
  results.push(state);
}

socket.close();
const output = {
  requestCount: cases.length,
  qualifiedMethodCount: results.filter((row) => row.qualified).length,
  consoleErrors,
  results,
};
console.log(JSON.stringify(output, null, 2));
if (output.qualifiedMethodCount !== cases.length || consoleErrors.length) process.exitCode = 1;
