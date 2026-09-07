# Known errata

## E1 — `feature_schema_hash` in the frozen V3 estimator metadata

**What.** `frozen/frozen_estimator_v3.json` records

```
"feature_schema_hash": "sha256:25862d20895318ec573de3c802919173bab8eaa05509c21b3062c022e39935fd"
```

which is the hash produced by the **V1** feature-schema helper
(`avdr.learning.features.feature_schema_hash`), not the **V2**
partial-observability schema hash
(`avdr.learning.closedloop.feature_schema_hash_v2`,
`sha256:efb68d8cfd2fbcd5701...`). The freeze helper
(`avdr.learning.estimators.freeze_estimator`) calls the V1 helper
unconditionally.

**Severity: metadata labelling only.**

**Why no result is affected.** The frozen estimator is
`b1-rolling-empirical` (`RollingEmpiricalEstimator`). It computes q̂(S) from
the per-subset outcome history supplied in the context and **never reads the
feature vector**. Its `estimate()` path touches no feature-schema-dependent
code, so the recorded schema hash is inert for this artifact. V4 outcome
behaviour is therefore unaffected, and no number in this package changes.

**What was deliberately NOT done.** The historical frozen artifact and its
metadata were **not rewritten**. Silently correcting a frozen artifact after
the confirmatory run would destroy the very property the freeze exists to
guarantee. The erratum is recorded here instead.

**Forward fix.** Only *future* artifact-generation code should be corrected, so
that `freeze_estimator` records the schema hash matching the feature contract
the estimator actually consumes. That is a separate change and must not touch
the V3/V4 artifacts.

## E2 — raw run reports are untracked experiment output

`artifacts/learning*/` is gitignored: the V3 and V4 raw run reports are not
committed objects. They are bound to this package by SHA-256 in
`provenance/frozen_hashes.json`, and every number the package publishes is
recomputed into `data/*.csv`, which **is** committed. The package is therefore
self-contained even though the upstream run reports are not part of the Git
history.

**Related historical gap (not affecting V4).** The V1 and V2 holdouts persisted
aggregate metrics only, so their per-trial ground truth cannot be recovered.
V3 persisted per-trial ground truth but not per-trial adaptive selections. V4
persists all 2 640 trials in full, which is why the confirmatory analysis is
exactly reproducible from the raw record.
