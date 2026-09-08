// Dependency-free Chrome DevTools Protocol QA for the local AVDR dashboard.
// Requires an already-running Chromium/Edge instance with remote debugging.

import fs from "node:fs";
import path from "node:path";

const [dashboardUrl, debugPort, screenshotDirectory] = process.argv.slice(2);
if (!dashboardUrl || !debugPort || !screenshotDirectory) {
  throw new Error(
    "usage: node scripts/browser_demo_qa.mjs <dashboard-url> <debug-port> <screenshot-dir>",
  );
}

const targets = await fetch(`http://127.0.0.1:${debugPort}/json/list`).then((response) => {
  if (!response.ok) throw new Error(`DevTools target listing failed: ${response.status}`);
  return response.json();
});
const target = targets.find((entry) => entry.type === "page");
if (!target?.webSocketDebuggerUrl) throw new Error("No browser page target is available");

const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener("open", resolve, { once: true });
  socket.addEventListener("error", reject, { once: true });
});

let nextId = 1;
const pending = new Map();
const eventWaiters = new Map();
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
  const waiters = eventWaiters.get(message.method) || [];
  eventWaiters.delete(message.method);
  waiters.forEach((resolve) => resolve(message.params));
});

function send(method, params = {}) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

function waitForEvent(method, timeoutMs = 10000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`Timed out waiting for ${method}`)), timeoutMs);
    const wrapped = (value) => {
      clearTimeout(timer);
      resolve(value);
    };
    const waiters = eventWaiters.get(method) || [];
    waiters.push(wrapped);
    eventWaiters.set(method, waiters);
  });
}

async function evaluate(expression) {
  const result = await send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.text || "browser evaluation failed");
  }
  return result.result.value;
}

async function waitFor(expression, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(`Boolean(${expression})`)) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out waiting for browser condition: ${expression}`);
}

async function setViewport(width, height) {
  await send("Emulation.setDeviceMetricsOverride", {
    width,
    height,
    deviceScaleFactor: 1,
    mobile: false,
  });
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
await setViewport(1920, 1080);
const loaded = waitForEvent("Page.loadEventFired");
await send("Page.navigate", { url: dashboardUrl });
await loaded;
await waitFor(
  "document.querySelectorAll('.demo-button').length === 3 && " +
    "document.querySelector('#healthText')?.textContent.includes('Router online')",
);

const initial = await evaluate(`(() => {
  const ids = ['didInput', 'strategyOptions', 'demoCard', 'primaryModeLabel'];
  const visible = Object.fromEntries(ids.map((id) => {
    const element = document.getElementById(id);
    const box = element?.getBoundingClientRect();
    return [id, Boolean(element && box.width > 0 && box.height > 0)];
  }));
  return {
    title: document.title,
    health: document.querySelector('#healthText')?.textContent,
    primaryMode: document.querySelector('#primaryModeLabel')?.textContent,
    demoLabel: document.querySelector('#demoCard .surface-label')?.textContent,
    buttons: [...document.querySelectorAll('.demo-button')].map((node) => node.textContent),
    visible,
  };
})()`);
const screenshots = [await capture("dashboard_initial")];

const scenarios = [];
let previousRequestId = null;
for (const scenarioId of ["normal", "slow_failure", "fast_unacceptable"]) {
  await evaluate(`document.querySelector('[data-scenario-id="${scenarioId}"]').click()`);
  await waitFor(
    `document.querySelector('#demoMessage')?.textContent.includes('Controlled run completed') && ` +
      `document.querySelector('#resultDid')?.textContent.includes('${scenarioId}')`,
  );
  const state = await evaluate(`(() => {
    const rows = [...document.querySelectorAll('.attempt-row')].map((row) => ({
      provider: row.querySelector('code')?.textContent,
      selected: row.querySelector('small')?.textContent,
      outcome: row.querySelector('.outcome-pill')?.textContent,
      latency: row.querySelector('.attempt-latency')?.textContent,
      classes: row.className,
      color: getComputedStyle(row.querySelector('.outcome-pill')).color,
    }));
    return {
      scenario: document.querySelector('#demoExplanationTitle')?.textContent,
      requestId: document.querySelector('#requestId')?.textContent,
      evidence: document.querySelector('#evidenceNotice')?.textContent,
      resultStatus: document.querySelector('#resultStatus')?.textContent,
      acceptance: document.querySelector('#acceptanceValue')?.textContent,
      metrics: document.querySelector('#metricsGrid')?.innerText,
      rows,
      resultVisible: !document.querySelector('#resultPanel')?.hidden,
    };
  })()`);
  state.traceReplaced = previousRequestId === null || state.requestId !== previousRequestId;
  previousRequestId = state.requestId;
  scenarios.push(state);
  screenshots.push(await capture(`scenario_${scenarioId}`));
}

await setViewport(768, 900);
await new Promise((resolve) => setTimeout(resolve, 250));
const narrow = await evaluate(`(() => {
  const bodyWidth = document.body.scrollWidth;
  const viewportWidth = document.documentElement.clientWidth;
  const demoButtons = [...document.querySelectorAll('.demo-button')].every((node) => {
    const box = node.getBoundingClientRect();
    return box.width > 0 && box.left >= 0 && box.right <= viewportWidth;
  });
  return { bodyWidth, viewportWidth, horizontalOverflow: bodyWidth > viewportWidth, demoButtons };
})()`);
screenshots.push(await capture("dashboard_narrow"));

socket.close();
console.log(JSON.stringify({ initial, scenarios, narrow, consoleErrors, screenshots }, null, 2));
