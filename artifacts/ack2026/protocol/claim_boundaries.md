# Claim boundaries

This package supports a **narrow, controlled claim**. The boundary below is
part of the evidence, not a disclaimer bolted on afterwards.

## What this evidence supports

- Results obtained in a **controlled local DID resolver environment**.
- **Adaptive resolver fan-out**: selecting a minimum resolver subset per
  request rather than a fixed subset.
- **W3C DID Resolution-oriented routing**: responses are normalized and checked
  against a structural acceptance profile (`w3c-basic-v1`).
- A **frozen confirmatory V4 evaluation** on data independent of the V3
  discovery stage, with the estimator, policy, comparator and all protocol
  parameters fixed beforehand.
- The statement that, in this environment, **adaptive improved the controlled
  cost-success frontier** relative to the frozen fixed-pair comparator.

## What this evidence does NOT support

None of the following may be claimed from this package:

- "Real public DID resolver performance was improved." No public resolver was
  contacted in V3 or V4.
- "A production DID SLO was proven." The SLO target is a controlled protocol
  parameter, not a service-level commitment.
- "Independent real providers." The three resolvers are local mocks on one
  shared Docker host and are not statistically independent.
- "ML outperformed heuristics." It did not — see below.
- "A fully W3C-certified resolver." The acceptance profile is structural only;
  no certification, signature verification or proof checking is performed.
- "Byzantine-fault-tolerant DID resolution" or any **3f+1 gateway rule**. No
  consensus protocol is implemented and no such rule is used or implied.

## Environment facts that must stay explicit

- Controlled/local resolver conditions on a single shared host.
- Synthetic control documents.
- Injected dynamic conditions (**CONTROLLED INJECTION**): delays, errors,
  structurally unacceptable responses, and correlated degradation.
- Hidden generator states are audit-only and are never estimator inputs.

## Estimator honesty

The frozen estimator is **`b1-rolling-empirical`** — a rolling empirical
success-rate estimator with an explicit hierarchical backoff. It is **not** a
machine-learning model and must not be described as AI or ML.

Across every development stage, simple rolling/EWMA estimators outperformed
the learned candidates (logistic regression, histogram gradient boosting) on
validation probability quality. The permissible secondary observation is:

> more complex learned estimators were not required in the controlled setting.

The contribution is the **adaptive fan-out mechanism**, not the estimator.
