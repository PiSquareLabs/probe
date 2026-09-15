# Infra config (reference copies)

These are reference copies of the two local-only files used to bring up the
OTel demo stack for this detection method — see the main `README.md` §2 for
why each exists. **The live copies that Docker actually reads are in
`../../opentelemetry-demo/`** (`docker-compose.rename.yml` and
`.env.override` at the repo root); editing the copies here has no effect on
a running stack.

- `docker-compose.rename.yml` — renames every container + the default
  network so this stack can run alongside another checkout of the same demo
  on one host. Skip this file entirely on a clean host.
- `.env.override` — remaps `ENVOY_PORT`/`ENVOY_ADMIN_PORT` (8080/10000 were
  already taken) and the Elastic-fork image/collector settings.
