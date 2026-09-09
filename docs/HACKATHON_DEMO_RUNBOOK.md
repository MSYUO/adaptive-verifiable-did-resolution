# AVDR final hackathon presenter runbook

This is a 10-minute pitch followed by 5 minutes of judge Q&A for the frozen
presenter baseline at commit
`21796f9db1995a2ba37fc99b287e3ce5a382f418`, tagged
`hackathon-sepolia-live-v1`.

Keep the evidence scopes separate throughout the presentation:

- **PRODUCT** describes implemented behavior.
- **CONTROLLED DEMO** uses synthetic loopback resolvers and isolated history.
- **REAL** describes bounded interoperability observations only.
- **AUDIT** verifies AVDR receipt integrity and provenance.
- **SEPOLIA** verifies an external receipt commitment, not DID truth.

Never run `anchor_sepolia_receipt.py --execute-live` during this demo. The
blockchain segment uses only the existing transaction documented in
[`BLOCKCHAIN_LIVE_EVIDENCE.md`](BLOCKCHAIN_LIVE_EVIDENCE.md).

## Before the presentation

1. Open PowerShell in the repository root.
2. Confirm `.venv\Scripts\python.exe` exists.
3. Run `.\scripts\stop_demo.ps1` to clear an earlier AVDR-owned session.
4. Start the local presenter stack with `.\scripts\start_demo.ps1`.
5. Wait for `AVDR_DEMO_STATUS=READY`, then open the printed dashboard URL. On
   the default port it is `http://127.0.0.1:8080/dashboard/`.

The default ports are backend `8080` and controlled resolvers `8001`–`8003`.
The launcher refuses occupied ports; it does not kill an unknown listener.
Use an all-distinct alternate set when needed:

```powershell
.\scripts\start_demo.ps1 -BackendPort 18080 -ResolverAPort 18001 -ResolverBPort 18002 -ResolverCPort 18003
```

Open the `DASHBOARD_URL` printed by that command; for the example it is
`http://127.0.0.1:18080/dashboard/`.

## Presentation timing and live path

Keep the live interaction to roughly 2–3 minutes inside the 10-minute pitch:

1. **0:00–0:50 — Problem:** resolver paths vary in capability, availability,
   latency, and output acceptability.
2. **0:50–2:00 — Architecture:** qualification, adaptive minimum-set selection,
   first-acceptable-result execution, and audit receipt.
3. **2:00–4:30 — Live demo:** show **Normal**, then choose only one adverse
   scenario—prefer **Fast but unacceptable**; use the frozen screenshot for the
   other adverse scenario.
4. **4:30–6:00 — Real compatibility:** show the three frozen real-DID evidence
   screenshots without rerunning the public qualification.
5. **6:00–7:15 — Audit:** verify the selected controlled receipt locally.
6. **7:15–8:30 — Existing Sepolia anchor:** show the frozen transaction and
   matching commitment; do not broadcast.
7. **8:30–10:00 — Conclusion:** restate the scope boundaries and transition to
   the 5-minute Q&A.

The narrative order is problem, AVDR architecture, Normal, one adverse case,
real DID compatibility, audit receipt, existing Sepolia anchor, and conclusion.

## Step 1 — Product capability

Show the dashboard and say:

> AVDR adaptively selects resolver fan-out rather than always querying every
> resolver. It returns the first result that passes the configured structural
> acceptance profile.

Point out the request, policy, evidence-mode, resolver-selection, trace, audit,
and result panels. `w3c-basic-v1` is structural acceptance, not cryptographic
verification or canonical DID truth.

## Step 2 — Normal controlled scenario

Click **Normal**. Show selected fan-out `k = 1`, one provider call, and two calls
saved for that controlled request.

Say: “Under this configured healthy scenario, the frozen policy demonstrates
smaller fan-out.” Do not describe this as public-resolver performance.

## Step 3 — Slow / Failure controlled scenario

Click **Slow / Failure**. Show the larger selected set, failed first path,
accepted alternate path, and calls used.

Say: “With one intentionally degraded loopback resolver, isolated controlled
history causes the policy to select additional redundancy.”

## Step 4 — Fast but unacceptable

Click **Fast but unacceptable**. Compare the faster unacceptable result with
the later accepted result.

> Fastest response is not necessarily an acceptable DID Resolution Result.

AVDR accepts a winner only after the configured profile passes.

## Step 5 — Real compatibility

Open the frozen screenshots for `did:key`, `did:web`, and `did:ethr` from
`docs/real_compatibility_screenshots/`. Point to the visible **REAL** label.

Say:

> These bounded observations establish interoperability for the tested paths
> through one Universal Resolver deployment. They do not establish global
> performance, provider independence, or production reliability.

Do not rerun public DID qualification during the presentation merely to obtain
new numbers.

## Step 6 — Audit receipt

Return to a controlled scenario and show:

- receipt hash;
- policy and resolver-selection provenance;
- returned provider;
- **Local integrity verified** after clicking **Verify receipt**.

Say: “The verifier recomputes AVDR's receipt and disclosure commitments. The
default recorder is process-memory-only, so this is not durable storage.”

## Step 7 — Existing Sepolia anchor

Open [`BLOCKCHAIN_LIVE_EVIDENCE.md`](BLOCKCHAIN_LIVE_EVIDENCE.md) and, if
network access is available, the linked explorer page. Use only this existing
transaction:

```text
0xfd32a66a9227d5f724f2eb85b91457ed9e7e3437e30551edacc42c82e189b583
```

Show Sepolia, block `11664768`, transaction value `0 wei`, and matching local
and on-chain receipt hashes.

> The blockchain is an external commitment layer, not the DID truth oracle.

Describe the state as **mined**. Finality was not evaluated.

## Frozen controlled-evaluation numbers

Use these only if judges ask for quantitative evidence:

| Metric | Adaptive | Fixed `{local-b, local-c}` | Difference |
| --- | ---: | ---: | ---: |
| Success | 0.867045 | 0.834470 | +0.032576 |
| Calls/request | 1.905303 | 2.000000 | -0.094697 |

Label this table **CONTROLLED EVALUATION — NOT REAL-WORLD PERFORMANCE**. Do not
recompute or regenerate the research campaign.

## Screenshot order

1. `docs/audit_screenshots/dashboard_initial.png`
2. `docs/audit_screenshots/scenario_normal.png`
3. `docs/audit_screenshots/scenario_slow_failure.png`
4. `docs/audit_screenshots/scenario_fast_unacceptable.png`
5. `docs/real_compatibility_screenshots/real_did_key.png`
6. `docs/real_compatibility_screenshots/real_did_web.png`
7. `docs/real_compatibility_screenshots/real_did_ethr.png`
8. Existing Sepolia explorer link in `BLOCKCHAIN_LIVE_EVIDENCE.md`

The controlled screenshots also show the local audit receipt panel. The real
screenshots show compatibility observations with the blockchain anchor safely
`not configured`; the separate Sepolia evidence came from a controlled-demo
receipt.

## Recovery and shutdown

- If a port is occupied, use alternate ports; do not stop an unknown process.
- If a scenario is stale, call `POST /demo/reset` and retry once.
- If the service remains unhealthy, stop and restart rather than changing the
  frozen policy or target.
- Finish with `.\scripts\stop_demo.ps1` and require:

```text
RESIDUAL_SERVICE_LISTENERS=0
AVDR_DEMO_STATUS=STOPPED
```

For claim boundaries and judge questions, keep
[`HACKATHON_EVIDENCE_MATRIX.md`](HACKATHON_EVIDENCE_MATRIX.md) and
[`JUDGE_QA.md`](JUDGE_QA.md) open.
