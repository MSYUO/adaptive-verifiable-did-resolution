const ui = {
  form: document.querySelector("#resolveForm"),
  did: document.querySelector("#didInput"),
  button: document.querySelector("#resolveButton"),
  message: document.querySelector("#requestMessage"),
  strategies: document.querySelector("#strategyOptions"),
  targetField: document.querySelector("#targetField"),
  target: document.querySelector("#targetInput"),
  healthDot: document.querySelector("#healthDot"),
  healthText: document.querySelector("#healthText"),
  modeBadge: document.querySelector("#modeBadge"),
  primaryModeLabel: document.querySelector("#primaryModeLabel"),
  providerSummary: document.querySelector("#providerSummary"),
  demoOptions: document.querySelector("#demoOptions"),
  demoExplanation: document.querySelector("#demoExplanation"),
  demoExplanationTitle: document.querySelector("#demoExplanationTitle"),
  demoExplanationText: document.querySelector("#demoExplanationText"),
  demoMessage: document.querySelector("#demoMessage"),
  empty: document.querySelector("#emptyState"),
  panel: document.querySelector("#resultPanel"),
  status: document.querySelector("#resultStatus"),
  requestId: document.querySelector("#requestId"),
  evidence: document.querySelector("#evidenceNotice"),
  resultDid: document.querySelector("#resultDid"),
  acceptance: document.querySelector("#acceptanceValue"),
  metrics: document.querySelector("#metricsGrid"),
  auditReceiptStatus: document.querySelector("#auditReceiptStatus"),
  auditReceiptHash: document.querySelector("#auditReceiptHash"),
  auditVerificationStatus: document.querySelector("#auditVerificationStatus"),
  auditAnchorStatus: document.querySelector("#auditAnchorStatus"),
  verifyReceiptButton: document.querySelector("#verifyReceiptButton"),
  auditVerifyMessage: document.querySelector("#auditVerifyMessage"),
  selectionMode: document.querySelector("#selectionMode"),
  selected: document.querySelector("#selectedProviders"),
  trace: document.querySelector("#attemptTrace"),
  resultJson: document.querySelector("#resultJson"),
};

const policyLabels = {
  "adaptive-min-set": "Adaptive",
  "single-static": "Single",
  "all-race": "All race",
  "sequential-failover": "Sequential failover",
};

const policyOrder = [
  "adaptive-min-set",
  "single-static",
  "all-race",
  "sequential-failover",
];

let adaptiveRuntimeStatus = null;
let currentAuditReceiptId = null;

ui.form.addEventListener("submit", resolveDid);
ui.strategies.addEventListener("change", syncTargetVisibility);
ui.did.addEventListener("input", refreshAdaptiveLabel);
ui.verifyReceiptButton.addEventListener("click", verifyCurrentReceipt);
initialize();

async function initialize() {
  try {
    const [health, policies, providers, demos] = await Promise.all([
      fetchJson("/health"),
      fetchJson("/policies"),
      fetchJson("/providers"),
      fetchJson("/demo/scenarios"),
    ]);

    adaptiveRuntimeStatus = policies.adaptive?.runtime || health.adaptive_runtime;
    ui.healthDot.className = "status-dot status-dot--ok";
    ui.healthText.textContent = adaptiveRuntimeStatus?.adaptive_available
      ? adaptiveRuntimeStatus.adaptive_ready
        ? "Router online · Adaptive ready"
        : "Router online · Adaptive warming up"
      : "Router online";
    setMode(health.evidence_mode);
    setPrimaryMode(health.evidence_mode);
    const defaultTarget = policies.adaptive?.default_target_slo_probability;
    if (Number.isFinite(defaultTarget)) ui.target.value = String(defaultTarget);
    renderPolicies(policies.available || []);

    const configured = Array.isArray(providers.providers)
      ? providers.providers.length
      : 0;
    const available = Array.isArray(providers.providers)
      ? providers.providers.filter((provider) => provider.available).length
      : 0;
    ui.providerSummary.textContent = `${available} of ${configured} providers available`;
    renderDemoScenarios(demos);
  } catch (error) {
    ui.healthDot.className = "status-dot status-dot--error";
    ui.healthText.textContent = "Router unavailable";
    ui.providerSummary.textContent = "Provider status unavailable";
    ui.strategies.replaceChildren(textNode("Unable to load routing policies"));
    ui.demoOptions.replaceChildren(textNode("Controlled scenarios unavailable"));
    showMessage(error.message, true);
  }
}

function renderDemoScenarios(payload) {
  ui.demoOptions.replaceChildren();
  if (!payload?.available || !Array.isArray(payload.scenarios)) {
    ui.demoOptions.append(textNode("Controlled providers are not configured."));
    return;
  }
  payload.scenarios.forEach((scenario) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "demo-button";
    button.textContent = scenario.title;
    button.dataset.scenarioId = scenario.id;
    button.addEventListener("click", () => runDemoScenario(scenario, button));
    ui.demoOptions.append(button);
  });
}

async function runDemoScenario(scenario, button) {
  setDemoBusy(true, button);
  clearResultForDemo();
  ui.demoExplanation.hidden = false;
  ui.demoExplanationTitle.textContent = scenario.title;
  ui.demoExplanationText.textContent = scenario.description;
  showDemoMessage("Resetting isolated history and running demo bootstrap…");

  try {
    const response = await fetch("/demo/run", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        scenario_id: scenario.id,
        did: `did:example:controlled-demo-${scenario.id}`,
      }),
    });
    const data = await response.json();
    if (!response.ok || !data.selection || !Array.isArray(data.attempts)) {
      throw new Error(data.detail || data.error || `Demo failed (${response.status})`);
    }
    renderResult(data);
    const observations = data.demo_bootstrap?.observations;
    showDemoMessage(
      `Controlled run completed after ${numberOrNa(observations)} demo bootstrap observations.`,
    );
  } catch (error) {
    showApiError(error.message);
    showDemoMessage(error.message, true);
  } finally {
    setDemoBusy(false);
  }
}

function clearResultForDemo() {
  ui.panel.hidden = true;
  ui.empty.hidden = false;
  ui.empty.querySelector("strong").textContent = "Controlled scenario running";
  ui.empty.querySelector("p").textContent = "The trace is cleared before every fresh scenario run.";
  ui.trace.replaceChildren();
  ui.metrics.replaceChildren();
  ui.selected.replaceChildren();
  ui.resultJson.textContent = "";
}

function setDemoBusy(busy, activeButton = null) {
  ui.demoOptions.querySelectorAll("button").forEach((button) => {
    button.disabled = busy;
    button.classList.toggle("demo-button--active", busy && button === activeButton);
  });
}

function showDemoMessage(message, error = false) {
  ui.demoMessage.textContent = message;
  ui.demoMessage.className = `request-message${error ? " request-message--error" : ""}`;
}

async function resolveDid(event) {
  event.preventDefault();
  const did = ui.did.value.trim();
  const policy = selectedPolicy();

  if (!did || !policy) {
    showMessage("Enter a DID and choose an available strategy.", true);
    return;
  }

  const payload = { did, policy };
  if (policy === "adaptive-min-set") {
    const target = Number(ui.target.value);
    if (!Number.isFinite(target) || target <= 0 || target > 1) {
      showMessage("Target success must be greater than 0 and no more than 1.", true);
      return;
    }
    payload.target_slo_probability = target;
  }

  setBusy(true);
  showMessage("Routing request in progress…");

  try {
    const response = await fetch("/resolve", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();

    if (data.selection && data.result && Array.isArray(data.attempts)) {
      renderResult(data);
      showMessage(
        response.ok
          ? "Resolution completed."
          : `${humanize(data.error || "resolution failed")}.`,
        !response.ok,
      );
    } else {
      throw new Error(data.detail || data.error || `Request failed (${response.status})`);
    }
  } catch (error) {
    showApiError(error.message);
  } finally {
    setBusy(false);
  }
}

function renderPolicies(available) {
  ui.strategies.replaceChildren();
  const ordered = [...available].sort((left, right) => {
    const a = policyOrder.indexOf(left);
    const b = policyOrder.indexOf(right);
    return (a === -1 ? 99 : a) - (b === -1 ? 99 : b);
  });
  const preferred = ordered.includes("adaptive-min-set") && adaptiveRuntimeStatus?.adaptive_ready
    ? "adaptive-min-set"
    : ordered.includes("sequential-failover")
      ? "sequential-failover"
      : ordered[0];

  ordered.forEach((policy) => {
    const label = document.createElement("label");
    label.className = "strategy-choice";
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "policy";
    input.value = policy;
    input.checked = policy === preferred;
    const name = document.createElement("span");
    name.textContent = policyLabels[policy] || humanize(policy);
    name.dataset.policyLabel = policy;
    label.append(input, name);
    ui.strategies.append(label);
  });
  refreshAdaptiveLabel();
  syncTargetVisibility();
}

function refreshAdaptiveLabel() {
  const label = ui.strategies.querySelector('[data-policy-label="adaptive-min-set"]');
  if (!label || !adaptiveRuntimeStatus) return;
  const didMethod = parseDidMethod(ui.did.value);
  const status = didMethod
    ? adaptiveRuntimeStatus.by_did_method?.[didMethod] || adaptiveRuntimeStatus
    : adaptiveRuntimeStatus;
  label.textContent = status.adaptive_ready
    ? "Adaptive · ready"
    : "Adaptive · warming up";
  label.title = humanize(status.reason || "readiness unavailable");
}

function renderResult(data) {
  const selection = data.selection;
  const result = data.result;
  const cost = data.cost || {};
  const evidenceMode = data.evidence?.mode || "unknown";
  const audit = data.audit || {};

  ui.empty.hidden = true;
  ui.panel.hidden = false;
  ui.status.textContent = data.success ? "Accepted" : "Failed";
  ui.status.className = `result-status result-status--${data.success ? "success" : "failure"}`;
  ui.requestId.textContent = data.request_id || "request id unavailable";
  ui.requestId.title = data.request_id || "";
  ui.resultDid.textContent = data.did || data.requested_did || "N/A";
  setMode(evidenceMode);
  renderEvidenceNotice(evidenceMode);

  ui.acceptance.replaceChildren();
  ui.acceptance.className = `acceptance-value${data.success ? "" : " acceptance-value--failed"}`;
  ui.acceptance.append(
    textNode(data.success ? "W3C basic acceptance: passed" : "Structural acceptance: not passed"),
  );
  const profile = document.createElement("small");
  profile.textContent = result.acceptance_profile || "Acceptance profile N/A";
  ui.acceptance.append(profile);

  renderMetrics([
    metric("Strategy", policyLabels[data.strategy] || humanize(data.strategy)),
    metric(
      "Selected fan-out",
      numberOrNa(selection.selected_count, (value) => `k = ${value}`),
      numberOrNa(selection.candidate_count, (value) => `${value} eligible candidate${value === 1 ? "" : "s"}`),
    ),
    metric("Estimated success", percentOrNa(selection.estimated_success), "Current request estimate"),
    metric("Target success", percentOrNa(selection.target_success), "Adaptive requests only"),
    metric("Returned by", result.returned_by || "N/A"),
    metric("Calls used", numberOrNa(cost.calls_used), "Dispatched provider calls"),
    metric("Calls saved", numberOrNa(cost.calls_saved_vs_all_race), "Versus this request's eligible set"),
    metric(
      "Audit receipt",
      audit.recorded ? "Recorded" : "Not recorded",
      audit.receipt_hash || humanize(audit.status || "not configured"),
    ),
  ]);

  renderAudit(audit);

  ui.selectionMode.textContent = `Selection: ${humanize(selection.selection_mode || "N/A")}`;
  ui.selected.replaceChildren();
  if (selection.selected_providers?.length) {
    selection.selected_providers.forEach((provider) => {
      const chip = document.createElement("code");
      chip.className = "provider-chip";
      chip.textContent = provider;
      ui.selected.append(chip);
    });
  } else {
    ui.selected.append(textNode("No provider subset was selected."));
  }

  renderAttempts(data.attempts);
  ui.resultJson.textContent = JSON.stringify(
    {
      didResolutionMetadata: result.didResolutionMetadata,
      didDocument: result.didDocument,
      didDocumentMetadata: result.didDocumentMetadata,
    },
    null,
    2,
  );
}

function renderAudit(audit) {
  currentAuditReceiptId = audit.recorded ? audit.receipt_id : null;
  ui.auditReceiptStatus.textContent = audit.recorded ? "Recorded" : "Not recorded";
  ui.auditReceiptHash.textContent = audit.receipt_hash || "N/A";
  ui.auditReceiptHash.title = audit.receipt_hash || "";
  ui.auditVerificationStatus.textContent = audit.integrity_verified
    ? "Local integrity verified"
    : audit.recorded
      ? "Verification pending"
      : "Not available";
  ui.auditAnchorStatus.textContent = humanize(
    audit.anchor?.status || "not configured",
  );
  ui.verifyReceiptButton.hidden = !currentAuditReceiptId;
  ui.verifyReceiptButton.disabled = false;
  ui.auditVerifyMessage.textContent = "";
}

async function verifyCurrentReceipt() {
  if (!currentAuditReceiptId) return;
  ui.verifyReceiptButton.disabled = true;
  ui.auditVerifyMessage.textContent = "Recomputing local receipt integrity…";
  try {
    const response = await fetch("/audit/verify", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ receipt_id: currentAuditReceiptId }),
    });
    const verification = await response.json();
    if (!response.ok || !verification.valid) {
      throw new Error(verification.error || "Receipt integrity mismatch");
    }
    ui.auditVerificationStatus.textContent = "Local integrity verified";
    ui.auditVerifyMessage.textContent = "Receipt integrity verified locally.";
  } catch (error) {
    ui.auditVerificationStatus.textContent = "Verification failed";
    ui.auditVerifyMessage.textContent = error.message;
  } finally {
    ui.verifyReceiptButton.disabled = false;
  }
}

function renderMetrics(items) {
  ui.metrics.replaceChildren();
  items.forEach((item) => {
    const node = document.createElement("div");
    node.className = "metric";
    const label = document.createElement("span");
    label.className = "metric-label";
    label.textContent = item.label;
    const value = document.createElement("span");
    value.className = "metric-value";
    value.textContent = item.value;
    value.title = item.value;
    node.append(label, value);
    if (item.note) {
      const note = document.createElement("span");
      note.className = "metric-note";
      note.textContent = item.note;
      node.append(note);
    }
    ui.metrics.append(node);
  });
}

function renderAttempts(attempts) {
  ui.trace.replaceChildren();
  if (!attempts.length) {
    ui.trace.append(textNode("No provider attempt data is available."));
    return;
  }

  attempts.forEach((attempt) => {
    const outcome = safeOutcome(attempt.outcome);
    const row = document.createElement("div");
    row.className = `attempt-row attempt-row--${outcome}`;

    const provider = document.createElement("div");
    provider.className = "attempt-provider";
    const providerName = document.createElement("code");
    providerName.textContent = attempt.provider;
    const providerState = document.createElement("small");
    providerState.textContent = attempt.selected ? "selected" : "not selected";
    provider.append(providerName, providerState);

    const status = document.createElement("div");
    status.className = "attempt-outcome";
    const pill = document.createElement("span");
    pill.className = `outcome-pill outcome-pill--${outcome}`;
    pill.textContent = humanize(outcome);
    status.append(pill);
    if (attempt.reason) {
      const reason = document.createElement("small");
      reason.textContent = humanize(attempt.reason);
      reason.title = attempt.reason;
      status.append(reason);
    }

    const latency = document.createElement("span");
    latency.className = "attempt-latency";
    latency.textContent = formatLatency(attempt.latency_ms);
    row.append(provider, status, latency);
    ui.trace.append(row);
  });
}

function renderEvidenceNotice(mode) {
  const controlled = mode === "controlled_demo";
  ui.evidence.className = `evidence-notice${controlled ? " evidence-notice--controlled" : ""}`;
  ui.evidence.textContent = controlled
    ? "CONTROLLED DEMO — local or synthetic provider behavior. These values are not real DID infrastructure measurements."
    : mode === "real"
      ? "REAL PROVIDER MODE — values below describe this request only; no research-run metrics are reused."
      : "Evidence mode was not reported by the backend.";
}

function showApiError(message) {
  ui.panel.hidden = true;
  ui.empty.hidden = false;
  ui.empty.querySelector("strong").textContent = "Resolution request did not complete";
  ui.empty.querySelector("p").textContent = message;
  showMessage(message, true);
}

function setMode(mode = "unknown") {
  ui.modeBadge.className = `mode-badge mode-badge--${mode}`;
  ui.modeBadge.textContent = mode === "controlled_demo"
    ? "CONTROLLED DEMO"
    : mode === "real"
      ? "REAL"
      : "MODE N/A";
}

function setPrimaryMode(mode = "unknown") {
  ui.primaryModeLabel.className = `surface-label surface-label--${mode}`;
  ui.primaryModeLabel.textContent = mode === "real"
    ? "REAL"
    : mode === "controlled_demo"
      ? "CONTROLLED"
      : "MODE N/A";
}

function selectedPolicy() {
  return ui.strategies.querySelector('input[name="policy"]:checked')?.value;
}

function parseDidMethod(value) {
  const match = /^did:([a-z0-9]+):/i.exec(String(value || "").trim());
  return match ? match[1].toLowerCase() : null;
}

function syncTargetVisibility() {
  ui.targetField.hidden = selectedPolicy() !== "adaptive-min-set";
}

function setBusy(busy) {
  ui.button.disabled = busy;
  ui.button.querySelector("span:first-child").textContent = busy ? "Resolving…" : "Resolve DID";
}

function showMessage(message, error = false) {
  ui.message.textContent = message;
  ui.message.className = `request-message${error ? " request-message--error" : ""}`;
}

async function fetchJson(url) {
  const response = await fetch(url, { headers: { accept: "application/json" }, cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Backend request failed (${response.status})`);
  }
  return response.json();
}

function metric(label, value, note = "") { return { label, value: value ?? "N/A", note }; }

function numberOrNa(value, format = String) {
  return typeof value === "number" && Number.isFinite(value) ? format(value) : "N/A";
}

function percentOrNa(value) {
  return typeof value === "number" && Number.isFinite(value)
    ? `${(value * 100).toFixed(1)}%`
    : "N/A";
}

function formatLatency(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "N/A";
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ms`;
}

function humanize(value) {
  if (value === null || value === undefined || value === "") return "N/A";
  return String(value).replaceAll("_", " ").replaceAll("-", " ");
}

function safeOutcome(value) {
  const allowed = new Set([
    "accepted", "unacceptable", "failed", "canceled",
    "not_selected", "not_dispatched", "skipped",
  ]);
  return allowed.has(value) ? value : "failed";
}

function textNode(value) {
  const span = document.createElement("span");
  span.textContent = value;
  return span;
}
