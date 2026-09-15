# Demo overlay

Local-only config for running `../opentelemetry-demo` (a git submodule
pointing at `elastic/opentelemetry-demo`) on a host that's already running
another instance of the same demo, or that needs the self-hosted
Elastic backend rather than Elastic Cloud. **Skip this folder entirely on
a clean host with no port conflicts** — go straight to `../DETECTORS.md` §1.

- `.env.override` — remaps `ENVOY_PORT`/`ENVOY_ADMIN_PORT` off the demo's
  default 8080/10000 (only needed if those are already taken), and
  re-derives `K6_TARGET_URL`/`FRONTEND_PROXY_ADDR` to match (`.env`
  resolves those from the *old* port before `.env.override` merges in, so
  overriding `ENVOY_PORT` alone silently breaks the load-generator).
- `docker-compose.rename.yml` — renames every demo container + the default
  network so a second instance can run alongside another one on the same
  Docker host. `compose.yaml` hardcodes `container_name:` per service, so
  two checkouts collide without this.

To use: copy both files into your `opentelemetry-demo/` submodule checkout
root, then include the compose override in your `up` command — see
`../DETECTORS.md` §1c for the exact command.
