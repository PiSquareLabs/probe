# Probe

Configure Elasticsearch's Jira integrations from your own UI, not from Kibana.

Enter your Jira credentials once. Probe uses them to provision **both** halves of
the Elastic ↔ Jira story, and keeps its own Jira access so ticket creation works
regardless of your Elastic licence.

- **FastAPI** backend (`backend/`)
- **React + Vite** frontend (`frontend/`)
- **Elasticsearch + Kibana + elastic-connectors** via `docker-compose.yml`

---

## Read this first

Elastic has two unrelated things called "connectors", and only one of them can
create a Jira issue:

| | Content connector (`service_type: jira`) | Action connector (`.jira`) |
|---|---|---|
| Direction | Jira → Elasticsearch | Elasticsearch → Jira |
| Purpose | index tickets for search / dedupe | **create** tickets |
| Lives on | Elasticsearch | Kibana |
| Licence | Basic (free) | **Gold** |

The Kibana Jira action connector is registered `minimumLicenseRequired: 'gold'`
([elastic/kibana#67178](https://github.com/elastic/kibana/pull/67178)), and Gold
is discontinued for new customers. **On a free/Basic stack, Elasticsearch cannot
create a Jira ticket.**

So Probe runs two execution paths behind one interface, and picks between them by
reading `enabled_in_license` from `GET /api/actions/connector_types` at runtime:

1. `elastic_connector` — Kibana's `.jira` connector does the write (Gold+).
2. `direct_jira` — Probe calls `POST /rest/api/3/issue` itself (always works).

Everything works end to end on free tiers. Only the component making the
outbound call changes, and the UI says which one it used.

Full findings: [`docs/RESEARCH.md`](docs/RESEARCH.md).
Every key, its free-tier status and a placeholder value: [`docs/KEYS.md`](docs/KEYS.md).
Design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
**How to test it: [`docs/TESTING.md`](docs/TESTING.md).**

## What it does

- One setup wizard collects Elastic + Jira credentials and validates both live.
- Checks the Jira account really holds `BROWSE_PROJECTS` and `CREATE_ISSUES`
  before finishing, instead of failing on the first ticket.
- Provisions the Elasticsearch content connector, pushes the Jira settings into
  its configuration, schedules a sync and mints the connector service's API key.
- Provisions the Kibana `.jira` action connector when the licence allows it, and
  says plainly why it didn't when it doesn't.
- Creates tickets, warning about likely duplicates found in the synced index.
- Searches synced tickets, falling back to JQL before the first sync lands.
- Encrypts credentials at rest; the API only ever returns masked values.

## Try it without any accounts

`backend/mock_stack.py` fakes Elasticsearch, Kibana and Jira on one port, so you
can run the whole app — wizard, provisioning, ticket creation, duplicate
detection — with nothing signed up for and no Docker:

```bash
cd backend && uvicorn mock_stack:app --port 9999   # terminal 1
cd backend && uvicorn app.main:app --port 8000     # terminal 2
cd frontend && npm run dev                         # terminal 3
```

Then point the wizard at `http://localhost:9999` for all three URLs. Add
`MOCK_LICENSE=gold` to the mock to exercise the Kibana-connector path instead.
Step-by-step walkthrough in [`docs/TESTING.md`](docs/TESTING.md).

## Quick start

### 1. Elastic stack (optional — point at an existing one instead)

```bash
docker compose up -d setup elasticsearch kibana
```

The `setup` service sets the `kibana_system` password before Kibana starts;
`ELASTIC_PASSWORD` alone only covers the `elastic` superuser.

### 2. Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then edit it

# a real encryption key:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

uvicorn app.main:app --reload --port 8000
```

API docs at <http://localhost:8000/docs>.

### 3. Frontend

```bash
cd frontend
npm install
cp .env.example .env          # VITE_PROBE_API_KEY must match PROBE_API_KEY
npm run dev
```

Open <http://localhost:5173> and complete the wizard.

### 4. Connector service

Provisioning shows an API key **once**. Paste it into
`connectors-config/config.yml` (see `config.yml.example`), then:

```bash
docker compose up -d connectors
```

Until this service is running, Elasticsearch has a connector record but no
configuration *schema*, and Jira settings cannot be written. Probe reports this
explicitly rather than leaving you with a connector that looks fine and syncs
nothing.

## Credentials you need

Summary — see [`docs/KEYS.md`](docs/KEYS.md) for the full table with placeholders.

| | Where from | Free? |
|---|---|---|
| Jira API token | <https://id.atlassian.com/manage-profile/security/api-tokens> | ✅ Free plan |
| Jira account email | the account that owns the token | ✅ |
| Jira project key | must grant that account `BROWSE_PROJECTS` + `CREATE_ISSUES` | ✅ |
| Elastic API key | needs `manage_connector`, `manage_api_key`, `monitor` | ✅ Basic |
| `PROBE_SECRET_KEY` | `Fernet.generate_key()` | — |

No OAuth app, no Marketplace add-on, no paid Elastic tier.

> Jira API tokens use HTTP Basic auth, where **OAuth scopes are not enforced** —
> the token inherits the account's permissions. There is no token setting that
> grants "create issues"; a Jira admin grants it to the account.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | liveness (no auth) |
| `GET` | `/api/setup/defaults` | seed the setup form |
| `POST` | `/api/validate/elastic` | test ES + Kibana, report the licence gate |
| `POST` | `/api/validate/jira` | test Jira auth, permissions, issue types |
| `POST` | `/api/setup` | store credentials (encrypted) |
| `GET` | `/api/connection` | masked stored state |
| `DELETE` | `/api/connection` | forget credentials |
| `GET` | `/api/connector/capabilities` | licence → capability matrix → route |
| `POST` | `/api/connector/provision` | provision both connectors |
| `GET` | `/api/connector/status` | sync state, doc count, last error |
| `POST` | `/api/connector/sync` | trigger a full sync |
| `GET` | `/api/connector/service-config` | config.yml for the connector service |
| `GET` | `/api/jira/projects` | project picker |
| `GET` | `/api/jira/issue-types` | issue-type picker |
| `GET` | `/api/jira/permissions` | permission pre-flight |
| `POST` | `/api/tickets` | create a ticket |
| `POST` | `/api/tickets/search` | search synced tickets |
| `POST` | `/api/tickets/similar` | duplicate check |

All `/api/*` routes require `X-Probe-Key` when `PROBE_API_KEY` is set. When it is
unset the guard is inert, so first-run local development needs no secret.

## Tests

```bash
cd backend && pytest -q        # 59 tests
```

They cover the licence gate and both routing paths, provisioning step reporting,
ADF conversion, JQL escaping, encryption at rest and secret masking, using
`respx` to stand in for Elasticsearch, Kibana and Jira. No network, no accounts.

See [`docs/TESTING.md`](docs/TESTING.md) for the mock-stack walkthrough and for
what you need to test against a real Jira site.

## Limits

- Jira **Cloud** only. The content connector supports Server/Data Center
  (`data_source`), but the setup flow and validation assume Cloud.
- One connection profile per deployment — state is a single encrypted file.
- Duplicate detection uses a fixed Lucene score threshold (8.0). Lucene scores
  are not normalised, so tune `DUPLICATE_SCORE_THRESHOLD` to your corpus.
- The content connector's document shape varies by connector version; the search
  mapping uses wildcards to stay resilient, which costs some precision.
- `docker-compose.yml` has not been executed end to end — it was written against
  the documented image behaviour and reviewed, not run.
- No frontend test suite. The UI was verified by driving Chromium through setup,
  provisioning and ticket creation, but that check is not committed.
