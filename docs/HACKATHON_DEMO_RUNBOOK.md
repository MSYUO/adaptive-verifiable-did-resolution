# AVDR Hackathon Demo Runbook

This is a 3–5 minute presenter flow for the local MVP. All three scenarios are
**CONTROLLED DEMO** runs against synthetic loopback providers. They are not
measurements of public DID resolver reliability or performance.

## 1. Before presentation

- Open PowerShell in the repository root.
- Confirm `.venv\Scripts\python.exe` exists and dependencies are installed.
- Close any old AVDR presenter session with `./scripts/stop_demo.ps1`.
- Default ports must be free: backend `8080`, resolver A `8001`, resolver B
  `8002`, and resolver C `8003`. The launcher refuses occupied ports and never
  kills a process merely because it owns one of them.
- Keep this sentence ready: AVDR does not always call one fixed resolver and
  does not always broadcast to every resolver. It uses observed behavior to
  choose the smallest resolver set that can satisfy the configured target,
  then returns the first structurally acceptable DID resolution result.

## 2. Start service

```powershell
.\scripts\start_demo.ps1
```

Wait for `AVDR_DEMO_STATUS=READY`. The launcher starts three controlled
resolvers, waits for their health endpoints, starts the API/dashboard, checks
the dashboard and scenario inventory, and prints the URL.

If the default ports are intentionally unavailable, choose four unused ports:

```powershell
.\scripts\start_demo.ps1 -BackendPort 18080 -ResolverAPort 18001 -ResolverBPort 18002 -ResolverCPort 18003
```

## 3. Open dashboard

Open the printed URL, normally:

```text
http://127.0.0.1:8080/dashboard/
```

Point out the mode labels before running anything. The primary area is the
real service surface. The amber scenario area is explicitly `CONTROLLED DEMO`.

## 4. Explain the real resolution area

Show the DID input, strategy selector, and Adaptive readiness label. A fresh
real runtime should say Adaptive is available but warming up. That is honest:
availability means the frozen components loaded; readiness requires legitimate
real observed history. Do not warm real history with demo observations.

The acceptance profile is `w3c-basic-v1`. It is structural acceptance—not
cryptographic verification, consensus, canonical truth, or blockchain proof.
The audit panel should show a locally recorded receipt, a readable
`sha256:...` commitment, `Local integrity verified`, and blockchain anchor
`Not configured`. This proves only receipt integrity. It does not make the DID
or resolver blockchain-verified. Use **Verify receipt** to recompute the
stored local receipt and hash-chain integrity through the backend.

## 5. Scenario 1 — Normal

Click **Normal** in the CONTROLLED DEMO panel.

Say: “Under healthy controlled conditions, the frozen adaptive policy can use
a smaller fan-out and avoid unnecessary resolver calls.”

Show the selected subset, accepted provider, calls used, and calls saved. Use
the values on screen; do not quote research-run metrics.

## 6. Scenario 2 — Slow / Failure

Click **Slow / Failure**.

Say: “One controlled resolver is intentionally degraded. Based on isolated
observed demo history, the existing adaptive policy uses additional paths.”

Show the failed attempt, the alternate accepted result, provider timings, and
the call count. This demonstrates only the configured scenario.

## 7. Scenario 3 — Fast but Unacceptable

Click **Fast but unacceptable**.

Say: “The fastest response is intentionally unacceptable under the existing
structural profile. AVDR rejects it and returns the first later acceptable
result.”

Show that the fast provider has a lower measured latency and `unacceptable`
state, while the later provider is `accepted` and appears as `Returned by`.

## 8. Recovery if something fails

- **Port occupied:** read the owner printed by the launcher. Do not kill it by
  port. If it is a known AVDR Docker Compose stack you intentionally started,
  stop that stack with `docker compose down`; otherwise use alternate ports.
- **Backend already running:** if it belongs to this launcher, run the stop
  script first. Otherwise use an alternate backend port.
- **Session state is stale:** run `./scripts/stop_demo.ps1`. It verifies the
  recorded executable, module, port, and process start time before stopping a
  PID. If identity differs, it refuses and retains the state for inspection.
- **Demo state looks stale:** each scenario starts with fresh controlled
  history. You can also call `POST /demo/reset`, then rerun the scenario.
- **Adaptive says warming up:** expected for a fresh real runtime. Controlled
  scenarios bootstrap only their isolated namespace automatically.
- **A demo request fails:** check `/health` and each resolver health endpoint,
  call `/demo/reset`, and retry once. If still failing, stop and restart the
  presenter stack rather than changing the target or frozen policy.

## 9. Stop service

```powershell
.\scripts\stop_demo.ps1
```

The stop helper terminates only PIDs recorded by the launcher after verifying
their identity. Finish only when it prints:

```text
RESIDUAL_SERVICE_LISTENERS=0
AVDR_DEMO_STATUS=STOPPED
```
