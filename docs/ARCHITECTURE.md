# Architecture

```
┌──────────────────┐   X-Probe-Key    ┌──────────────────────┐
│  React SPA       │ ───────────────► │  FastAPI backend     │
│  (Vite, :5173)   │ ◄─────────────── │  (:8000)             │
└──────────────────┘   masked state   └──────┬───────────────┘
   holds no secrets                          │
                                             │ encrypted at rest
                                    ┌────────▼────────┐
                                    │ probe-state.json│  Fernet-encrypted
                                    └─────────────────┘  credentials
                                             │
             ┌───────────────────────────────┼────────────────────────────┐
             ▼                               ▼                            ▼
   ┌──────────────────┐         ┌────────────────────────┐    ┌────────────────────┐
   │ Elasticsearch    │         │ Kibana                 │    │ Jira Cloud         │
   │ _connector APIs  │         │ /api/actions/connector │    │ REST API v3        │
   └────────┬─────────┘         └───────────┬────────────┘    └─────────┬──────────┘
            │ configures                    │ `.jira` (Gold+)           │ direct
            ▼                               └──────────┐                │
   ┌──────────────────┐                                ▼                ▼
   │ elastic-connectors│  syncs Jira ──► index    ┌──────────────────────────┐
   │ service (Docker)  │  search-jira-probe       │   Jira issue created     │
   └──────────────────┘                           └──────────────────────────┘
```

## The two Elastic surfaces

| | Content connector | Action connector |
|---|---|---|
| Lives on | Elasticsearch | Kibana |
| Direction | Jira → ES | ES → Jira |
| Managed by | `app/clients/elastic.py` | `app/clients/kibana.py` |
| Licence | Basic | **Gold** |
| Used for | duplicate detection, search | ticket creation |

Both are provisioned from **one** Jira credential entry — see
`build_connector_configuration()` in `app/clients/elastic.py`, which maps the
UI's Jira form onto the connector's `data_source` / `jira_url` /
`account_email` / `api_token` fields, and `create_jira_connector()` in
`app/clients/kibana.py`, which maps the same form onto the action connector's
`config` and `secrets`.

## Ticket routing

`app/services/capabilities.py::resolve_route` decides, per request:

```
preferred = "direct_jira"        → direct_jira
preferred = "elastic_connector"  → elastic_connector if licensed else direct_jira
preferred = "auto"               → elastic_connector if licensed else direct_jira
```

"Licensed" is not assumed. `GET /api/actions/connector_types` is read at request
time and `enabled_in_license` on the `.jira` entry is the authority.

There is a second fallback at execution time: if the Kibana connector is
licensed but *fails* (rotated secret, Kibana restarting), `create_ticket` logs
it and retries through the direct Jira path. A ticket the user asked for matters
more than which component created it.

## Payload differences between the two paths

They are not interchangeable, which is why each has its own builder:

| | Direct (`POST /rest/api/3/issue`) | Connector (`pushToService`) |
|---|---|---|
| Jira API version | v3 | v2 |
| `description` | **ADF document** (`text_to_adf`) | plain string |
| Project | `fields.project.key` per request | fixed in connector `config.projectKey` |
| Issue type | `fields.issuetype.name` | `incident.issueType` |

## Backend layout

```
backend/app/
├── main.py             FastAPI app, CORS, error envelope
├── config.py           pydantic-settings
├── models.py           request/response schemas
├── deps.py             DI: store, auth guard, client lifecycles
├── crypto.py           Fernet encryption + masking
├── store.py            encrypted JSON state file
├── errors.py           ProbeError / UpstreamError / LicenseError
├── clients/
│   ├── base.py         retries, Retry-After, uniform error translation
│   ├── jira.py         REST v3, ADF, JQL escaping, permission checks
│   ├── elastic.py      cluster, licence, connector lifecycle, search
│   └── kibana.py       actions API, licence probing, pushToService
├── services/
│   ├── capabilities.py licence → capability matrix → route
│   ├── provisioning.py per-step orchestration
│   └── tickets.py      dual-path creation, duplicate detection
└── routers/            setup, connectors, jira, tickets, health
```

## Design decisions worth knowing

**Credentials are encrypted at rest and never returned.** `store.py` encrypts
`jira.api_token`, `elastic.api_key` and `elastic.password` individually, so the
state file stays inspectable. `ConnectionStore.state()` is the only view the API
exposes and it masks everything (`ATATT3…4e2a`).

**Provisioning reports per-step outcomes.** A partial success — content
connector created, action connector skipped on licence — is the *normal* free-tier
result. Collapsing that into one boolean would make the expected case look like a
failure.

**`skipped` is not `failed`.** `ProvisionResult.ok` ignores skipped steps.

**Waiting for the connector service.** `wait_for_configuration()` polls until the
`elastic-connectors` service registers its schema, because writing configuration
before then silently no-ops. This is the single most confusing failure in the
integration and it gets an explicit message.

**Duplicate detection prefers Elasticsearch.** It is fuzzier, faster, and does
not spend Jira's per-tenant burst rate limit. JQL is the fallback for before the
first sync completes.

**Search/JQL injection.** `escape_jql` doubles backslashes *before* escaping
quotes; the reverse order would let `\"` break out of the literal. Index names
are URL-encoded in `index_path`.

**The frontend holds nothing.** It posts credentials once and thereafter only
ever sees masked values. The `X-Probe-Key` shared secret is inert when unset, so
first-run local development has no friction.
