# Skipped flags

Flags defined in `opentelemetry-demo/src/flagd/demo.flagd.json` that were
excluded from the catalog because they control test-harness behavior, not
a service failure mode.

- `loadGeneratorTraffic` — on/off switch for whether the load generator's
  synthetic traffic scenarios run at all; toggling it stops/starts demand,
  it does not break any service's own behavior.
- `loadGeneratorVUs` — sets the number of concurrent virtual users the
  load generator's HTTP scenario runs (5/10/25/50); it changes traffic
  *volume*, not any service's correctness or resource behavior.
