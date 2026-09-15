# Testing Probe

Three levels, cheapest first. **Levels 1 and 2 need no accounts at all** — no
Atlassian signup, no Elastic cluster, no Docker.

---

## Level 1 — Unit tests (~2 minutes, nothing to install but Python)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

Expect **59 passed**. Elasticsearch, Kibana and Jira are all faked at the HTTP
layer with `respx`, so this exercises the licence gate, both routing paths,
provisioning step reporting, ADF conversion, JQL escaping and encryption at
rest without touching a network.

Worth running individually to see the core behaviour:

```bash
pytest tests/test_licensing_and_routing.py -v   # the Gold gate and both routes
pytest tests/test_provisioning.py -v            # per-step provisioning outcomes
```

## Level 2 — The whole app against a mock stack (~5 minutes, still no accounts)

`backend/mock_stack.py` stands in for Elasticsearch, Kibana **and** Jira on one
port. This is the fastest way to see the real UI do real work.

**Terminal 1 — the fake upstreams:**

```bash
cd backend && source .venv/bin/activate
uvicorn mock_stack:app --port 9999
```

**Terminal 2 — the backend:**

```bash
cd backend && source .venv/bin/activate
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

The two `.env.example` files ship with matching `PROBE_API_KEY` /
`VITE_PROBE_API_KEY` values, so copying both is all the auth setup there is. (If
you blank out `PROBE_API_KEY`, the guard turns off entirely.)

**Terminal 3 — the frontend:**

```bash
cd frontend && npm install
cp .env.example .env
npm run dev
```

**Then, at <http://localhost:5173>:**

1. **Step 1 — Elasticsearch.** Put `http://localhost:9999` in *both* the
   Elasticsearch and Kibana URL fields. API key can be anything. *Test and
   continue.* You should see three green checks and one **amber ⓘ** telling you
   the Jira action connector needs a Gold licence — that is the whole design
   decision surfacing.
2. **Step 2 — Jira.** Base URL `http://localhost:9999`, any email, any token of
   8+ characters, project key `PROBE`. *Test and continue.*
3. **Step 3 — Connector.** Accept the defaults. *Load projects from Jira* should
   show `PROBE` and `OPS`. *Save and continue.*
4. **Connector tab** → *Provision connectors*. Six steps go green; the action
   connector is **skipped** with the licence reason. Copy the `config.yml` it
   prints.
5. **Tickets tab.** Type `Checkout returns 500 for guest users`. After about
   half a second an amber panel warns that **PROBE-12** already exists — that
   match came from the (mock) Elasticsearch index, not Jira. Create it anyway;
   the success banner says *"via the Jira REST API"*.

**Confirm the credentials really reached Elasticsearch:**

```bash
curl -s localhost:9999/__state | python -m json.tool
```

`connector_config` holds the Jira URL, email and token your UI form put there —
that is the product working. You never opened Kibana.

### Testing the Gold path

Restart the mock with a Gold licence and repeat:

```bash
MOCK_LICENSE=gold uvicorn mock_stack:app --port 9999
```

Now provisioning creates the action connector, the capability matrix turns
green, and the ticket banner says *"via the Kibana Jira connector"*.

### Testing the failure paths

| Env var on `mock_stack` | What it simulates |
|---|---|
| `MOCK_CONNECTOR_SERVICE_DOWN=1` | the `elastic-connectors` service has not checked in — provisioning should **fail** `connector_configuration` with a clear message rather than silently writing nothing |
| `MOCK_NO_CREATE_PERMISSION=1` | the Jira account lacks `CREATE_ISSUES` — setup should stop before touching Elasticsearch |

Both are worth running once: they are the two failures most likely to hit you
for real, and the point of the app is that it names them instead of failing
opaquely.

## Level 3 — Real Jira and a real Elastic stack

### What you need

| | How to get it | Cost |
|---|---|---|
| Jira Cloud site | <https://www.atlassian.com/software/jira/free> — sign up, note the `your-org.atlassian.net` URL | Free (10 users) |
| A Jira project | Create one; note its key, e.g. `PROBE` | Free |
| Jira API token | <https://id.atlassian.com/manage-profile/security/api-tokens> → *Create API token*. Copy it — it is shown once | Free |
| Elasticsearch + Kibana | `docker compose up -d setup elasticsearch kibana` (needs ~4 GB free to Docker) | Free (Basic) |
| Elastic API key | See below | Free |

Your Jira account must hold **Browse projects** and **Create issues** in the
target project. If you created the site, you are admin and already do. Probe
checks this during setup and refuses to finish without it, so you will know.

### Bringing up the stack

```bash
docker compose up -d setup elasticsearch kibana
docker compose logs -f kibana        # wait for "http server running"
```

Kibana takes a couple of minutes cold. The `setup` service exists because
`ELASTIC_PASSWORD` only sets the `elastic` superuser — Kibana logs in as
`kibana_system`, whose password has to be set through the API first.

### Minting the Elastic API key

```bash
curl -u elastic:probe-dev-es-passw0rd -X POST localhost:9200/_security/api_key \
  -H 'Content-Type: application/json' -d '{
    "name": "probe-app",
    "role_descriptors": {
      "probe": {
        "cluster": ["monitor", "manage_connector", "manage_api_key"],
        "indices": [{"names": ["search-*", ".elastic-connectors*"], "privileges": ["all"]}]
      }
    }
  }'
```

Use the **`encoded`** value from the response as the API key in Probe's setup
wizard.

### Then

Run the wizard with the real URLs (`http://localhost:9200`,
`http://localhost:5601`, `https://your-org.atlassian.net`), provision, and paste
the printed `config.yml` into `connectors-config/config.yml`. Start the
connector service:

```bash
docker compose up -d connectors
docker compose logs -f connectors
```

Once it has checked in, re-run **Provision connectors** so the Jira settings
land, then *Run full sync*. Watch the document count climb on the Connector
tab. When it is non-zero, the Search tab is querying your real Jira content out
of Elasticsearch, and duplicate detection has something to work with.

Creating a ticket puts a real issue in your real project. It will go via the
direct Jira path, because a self-managed stack is on a Basic licence.

## What "working" looks like

| Check | Expected |
|---|---|
| `pytest -q` | 59 passed |
| Capability matrix on Basic | content connector + direct Jira available; action connector unavailable (gold) |
| Provisioning on Basic | 6 ok, 1 skipped, `ok: true` — **skipped is not a failure** |
| Ticket on Basic | created, `route_used: "direct_jira"` |
| Ticket on Gold | created, `route_used: "elastic_connector"` |
| Stored state file | `api_token` starts `enc::`; the plaintext token appears nowhere |
| Any API response | secrets masked as `ATATT3…4e2a` |

## Known gaps in this test setup

- The mock stack's relevance scoring is crude term overlap, not Lucene. It is
  calibrated to straddle Probe's duplicate threshold so the warning demonstrably
  fires and does not fire — it is not a substitute for tuning
  `DUPLICATE_SCORE_THRESHOLD` against your real corpus.
- `docker-compose.yml` has not been run end to end in CI; it was written against
  the documented image behaviour and reviewed, not executed. If the stack
  misbehaves, that file is the first place to look.
- There is no frontend test suite. The UI was verified by driving Chromium
  through setup, provisioning and ticket creation, but that check is not
  committed as an automated test.
