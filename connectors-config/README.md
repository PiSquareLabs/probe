# Connector service configuration

The self-managed `elastic-connectors` service reads `config.yml` from this
directory. It is **not** committed — it holds an API key.

## Getting one

1. Run setup in the Probe UI and click **Provision connectors**.
2. Provisioning mints an API key via `POST _security/api_key` and shows it once.
   Elasticsearch never returns a key's secret again, so copy it then.
3. Save the YAML it shows as `connectors-config/config.yml`.
4. `docker compose up -d connectors`

`config.yml.example` shows the shape.

## Why the service has to be running before configuration works

Elasticsearch stores the connector document, but the **configuration schema** —
which fields a `jira` connector accepts — is registered by this service when it
checks in. Writing configuration values before then silently does nothing.

Probe detects this and reports *"the elastic-connectors service has not checked
in"* rather than leaving you with a connector that looks configured but syncs
nothing. If you see that message: start this service, then provision again.
