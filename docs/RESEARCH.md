# Research: what Elasticsearch and Jira actually give us for tickets/tasks

This document records what was verified in the Elastic and Atlassian documentation
before any code was written. It exists because the original brief contained one
assumption that the documentation does not support, and the whole architecture
turns on it.

---

## 1. The headline finding: "connector" means two different things in Elastic

The brief said *"ES agent will handle JIRA creation via its connector"*. Elastic has
two unrelated product surfaces that are both called "connectors", and only one of
them can create a Jira issue.

| | **Elasticsearch content connector** | **Kibana action connector** |
|---|---|---|
| Identifier | `service_type: "jira"` | `connector_type_id: ".jira"` |
| Direction | Jira → Elasticsearch | Elasticsearch → Jira |
| What it does | Extracts projects, issues, comments and attachments and indexes them into an ES index | Creates / updates Jira issues from rules, cases and API calls |
| Managed by | `_connector` APIs on Elasticsearch | `/api/actions/connector` APIs on Kibana |
| Runs where | A separate `elastic-connectors` service (self-managed) or Elastic's own infra (Elastic-managed) | Inside Kibana |
| **Can create a ticket?** | **No. Read-only ingestion.** | **Yes** |
| Minimum licence | Basic (self-managed flavour) | **Gold** |

So *"configure the Jira connector on ES so the agent can create tickets"* is really
**two** provisioning jobs, and this app does both from one credential entry:

* the **content connector**, so the agent can *search existing tickets* (dedupe,
  context, "has this been reported before?"); and
* the **action connector**, so Elastic can *create* the ticket — where the licence
  permits it.

## 2. The licence problem, and why the app needs its own Jira access

The Kibana Jira action connector is registered with `minimumLicenseRequired: 'gold'`.
This was a deliberate change in Kibana PR
[elastic/kibana#67178](https://github.com/elastic/kibana/pull/67178) ("Moving jira to
a gold license"); the surrounding licence framework
([elastic/kibana#59070](https://github.com/elastic/kibana/pull/59070)) states the rule
plainly: built-in action types may be Basic+, **non-built-in (third-party) action
types may only be Gold+**. Gold is discontinued for new customers.

**Consequence on a free/Basic stack: Elasticsearch cannot create a Jira ticket.**
`POST /api/actions/connector` with `.jira` is rejected by the licence check.

This is not a reason to abandon the design — it is the reason the brief's own
instinct ("because we also need access to JIRA") is correct. The app therefore
implements **two execution paths behind one interface**:

* **Path A — `elastic_connector`**: execute the Kibana `.jira` connector with
  `subAction: "pushToService"`. Used when the licence allows it.
* **Path B — `direct_jira`**: `POST /rest/api/3/issue` straight to the Jira Cloud
  REST API with the same stored credentials. Used on Basic/free.

The path is **detected at runtime, not guessed**. `GET /api/actions/connector_types`
returns, for every connector type, `minimum_license_required` and
`enabled_in_license`. The app reads that and reports a capability matrix to the UI,
so the operator can see exactly what their licence does and does not buy them.
Either way the Jira issue gets created and the ES index gets searched — the user
never has to know which door it went through.

## 3. Elasticsearch content connector (`service_type: jira`)

### Provisioning sequence (all via API, no Kibana UI)

```
PUT  _connector/<connector_id>          { index_name, name, service_type: "jira" }
POST _security/api_key                  -> key the connector service authenticates with
PUT  _connector/<connector_id>/_api_key_id
PUT  _connector/<connector_id>/_configuration   { values: { ...jira fields... } }
PUT  _connector/<connector_id>/_scheduling      { full: { enabled, interval } }
POST _connector/_sync_job               { id, job_type: "full" }   -> kick a sync
GET  _connector/<connector_id>                  -> status / last_sync_error
```

`api_key_secret_id` is only for Elastic-managed connectors; self-managed connectors
leave it unset.

The caller needs cluster privileges `manage_connector`, `manage_api_key` and
(for Elastic-managed only) `write_connector_secrets`.

### Configuration fields the Jira connector accepts

| Field | Meaning | Notes |
|---|---|---|
| `data_source` | `jira_cloud`, `jira_server` or `jira_data_center` | this app targets `jira_cloud` |
| `jira_url` | host, e.g. `https://your-org.atlassian.net/` | |
| `account_email` | Atlassian account email (Cloud) | pairs with `api_token` |
| `api_token` | Atlassian API token (Cloud) | for Data Center this is the account *password* |
| `username` / `password` | Server / Data Center auth | unused for Cloud |
| `projects` | comma-separated project keys, `*` for all | default `*` |
| `ssl_enabled` | verify TLS | default `false` |
| `ssl_ca` | PEM content, ignored when `ssl_enabled` is false | |
| `retry_count` | retries on failed Jira requests | default `3` |
| `concurrent_downloads` | parallel attachment downloads | default `100` |
| `use_text_extraction_service` | offload attachment extraction | default `false` |
| `use_document_level_security` | index Jira permissions alongside docs | Platinum-class feature |

**Important:** the configuration *schema* is registered by the connector service on
startup, not by Elasticsearch. Writing `_configuration` before the
`elastic-connectors` service has checked in will not stick. The app handles this by
polling `GET _connector/<id>` until `configuration` is populated, and surfacing a
clear "connector service has not checked in yet" error instead of failing silently.

### What gets synced

Projects (description, key, type, lead), every issue type (Task, Bug, Sub-task,
Story, Enhancement), issue metadata (type, parent, fix/affected versions,
resolution, priority, custom fields), comments, sub-tasks, and attachment content.
That is more than enough for duplicate detection and context retrieval.

### Running the connector service (self-managed, free)

```
docker run -v "$(pwd)/connectors-config:/config" --rm -it --network host \
  docker.elastic.co/integrations/elastic-connectors:<version> \
  /app/bin/elastic-ingest -c /config/config.yml
```

with `config.yml` carrying `elasticsearch.host`, `elasticsearch.api_key`, and a
`connectors:` list of `{connector_id, service_type, api_key}`.

## 4. Kibana action connector (`.jira`)

Creates issues through the Jira **REST API v2**. Jira Cloud and Data Center are
supported; on-premise Jira Server is not.

```jsonc
POST /api/actions/connector/<id>      // kbn-xsrf header required
{
  "name": "Probe Jira",
  "connector_type_id": ".jira",
  "config":  { "apiUrl": "https://your-org.atlassian.net", "projectKey": "PROBE" },
  "secrets": { "email": "...", "apiToken": "..." }
}
```

Execution subactions:

| subAction | Use |
|---|---|
| `pushToService` | create or update an issue (the one the app uses to create) |
| `getFields` | fields available on the project |
| `issueTypes` | issue types available on the project |
| `fieldsByIssueType` | fields for one issue type |
| `issues` / `issue` | search / fetch, used for "parent" pickers |

`pushToService` takes `incident` (`summary`, `description`, `issueType`, `priority`,
`labels`, `parent`) plus `comments`.

## 5. Jira Cloud: what access we actually need

### Authentication — Basic auth with an API token

`Authorization: Basic base64(<account_email>:<api_token>)`.

The single most useful thing verified here: **OAuth scopes are not enforced for
API-token Basic auth.** The token inherits whatever the Atlassian account can do.
So the questions "which scopes do I need?" and "what can this integration do?" have
the same answer: *whatever that Jira user's permissions are*. Scopes only matter if
you switch to OAuth 2.0 (3LO).

### Permissions required to create a ticket

`POST /rest/api/3/issue` requires the **`BROWSE_PROJECTS`** and **`CREATE_ISSUES`**
project permissions in the target project. The app verifies this up front rather
than discovering it on the first failed ticket:

```
GET /rest/api/3/mypermissions?projectKey=<KEY>&permissions=BROWSE_PROJECTS,CREATE_ISSUES
```

### Scopes, for the record (only if OAuth 2.0 is ever adopted)

* Classic: `read:jira-work`, `write:jira-work`, `read:jira-user`, `offline_access`
* Granular: `read:issue:jira`, `write:issue:jira`, `read:project:jira`,
  `write:comment:jira`, `read:issue-meta:jira`

### Endpoints the app uses

| Endpoint | Purpose |
|---|---|
| `GET /rest/api/3/myself` | validate credentials, resolve `accountId` |
| `GET /rest/api/3/project/search` | populate the project picker in our UI |
| `GET /rest/api/3/mypermissions` | pre-flight the create permission |
| `GET /rest/api/3/issue/createmeta/{projectKey}/issuetypes` | issue-type picker |
| `POST /rest/api/3/issue` | create the ticket (Path B) |
| `POST /rest/api/3/search/jql` | duplicate check when ES has no synced index yet |

### API v3 wants ADF, not a string

`fields.description` in v3 is **Atlassian Document Format**, not plain text:

```json
{ "type": "doc", "version": 1,
  "content": [{ "type": "paragraph", "content": [{ "type": "text", "text": "..." }] }] }
```

Passing a bare string returns a 400. The app converts plain text to ADF centrally
(`backend/app/clients/jira.py::text_to_adf`) so callers never deal with it.
Note the Kibana `.jira` connector uses **v2**, which takes a plain string — another
reason the two paths need separate payload builders rather than one shared one.

### Free-plan viability

Everything above works on the **Jira Cloud Free plan** (up to 10 users): API tokens,
REST API v3, issue creation, JQL search. No paid add-on is involved. Rate limiting
is by per-tenant *burst* limits for API-token traffic rather than the newer
points-based quota; Atlassian does not publish fixed numbers, so the app treats
`429` as retryable with backoff and honours `Retry-After`.

## 6. Net effect on free tiers

| Capability | Free tier verdict |
|---|---|
| Self-managed Elasticsearch + Kibana (Basic) | Free |
| `_connector` APIs / self-managed Jira content connector | Works on Basic |
| Elastic-managed (native) connector | Needs a paid Cloud tier — app defaults to self-managed |
| Kibana `.jira` action connector | **Gold+. Blocked on free** → Path B fallback |
| Document-level security on the connector | Paid — left off by default |
| Jira Cloud API token + create issue | Free plan |

**Bottom line: the app is fully functional end to end on free tiers**, with ticket
creation going direct to Jira instead of through Kibana. Nothing degrades except
which component makes the outbound call.

---

## Sources

- [Elastic Jira connector reference](https://www.elastic.co/docs/reference/search-connectors/es-connectors-jira)
- [Self-managed connectors](https://www.elastic.co/docs/reference/search-connectors/self-managed-connectors)
- [Connector API tutorial](https://www.elastic.co/docs/reference/search-connectors/api-tutorial)
- [Running connectors from Docker](https://www.elastic.co/docs/reference/search-connectors/es-connectors-run-from-docker)
- [Update the connector configuration API](https://www.elastic.co/guide/en/elasticsearch/reference/master/update-connector-configuration-api.html)
- [Update the connector API key ID](https://www.elastic.co/guide/en/elasticsearch/reference/current/update-connector-api-key-id-api.html)
- [Kibana Jira connector and action](https://www.elastic.co/docs/reference/kibana/connectors-kibana/jira-action-type)
- [Kibana: get connector types](https://www.elastic.co/docs/api/doc/kibana/v9/operation/operation-get-actions-connector-types)
- [Kibana: create a connector](https://www.elastic.co/docs/api/doc/kibana/operation/operation-post-actions-connector-id)
- [Preconfigured connectors](https://www.elastic.co/docs/reference/kibana/connectors-kibana/pre-configured-connectors)
- [elastic/kibana#67178 — Moving jira to a gold license](https://github.com/elastic/kibana/pull/67178)
- [elastic/kibana#59070 — License checks for actions plugin](https://github.com/elastic/kibana/pull/59070)
- [Elastic subscriptions](https://www.elastic.co/subscriptions)
- [Jira Cloud REST API v3 — Issues](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/)
- [Jira Cloud REST API v3 — Permissions](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-permissions/)
- [Jira Cloud rate limiting](https://developer.atlassian.com/cloud/jira/platform/rate-limiting/)
