import { useEffect, useState } from 'react';
import { api } from '../lib/api.js';
import { Banner, CheckList, ErrorBanner, Field, Spinner } from './Primitives.jsx';

const STEPS = ['Elasticsearch', 'Jira', 'Connector'];

const EMPTY_ELASTIC = {
  es_url: 'http://localhost:9200',
  kibana_url: 'http://localhost:5601',
  api_key: '',
  username: '',
  password: '',
  verify_tls: false,
};

const EMPTY_JIRA = {
  base_url: '',
  account_email: '',
  api_token: '',
  project_key: '',
  default_issue_type: 'Task',
};

const EMPTY_CONNECTOR = {
  connector_id: 'probe-jira-connector',
  index_name: 'search-jira-probe',
  name: 'Probe Jira content connector',
  projects: '*',
  sync_interval: '0 0 */3 * * ?',
  schedule_enabled: true,
  ssl_enabled: false,
  retry_count: 3,
  concurrent_downloads: 50,
};

/**
 * Three-step setup. The point of the product is that the operator fills this
 * in once and never opens Kibana, so each step validates against the real
 * service before letting them move on.
 */
export default function SetupWizard({ onComplete }) {
  const [step, setStep] = useState(0);
  const [elastic, setElastic] = useState(EMPTY_ELASTIC);
  const [jira, setJira] = useState(EMPTY_JIRA);
  const [connector, setConnector] = useState(EMPTY_CONNECTOR);
  const [validation, setValidation] = useState(null);
  const [projects, setProjects] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    api
      .defaults()
      .then((defaults) => {
        setElastic((prev) => ({
          ...prev,
          es_url: defaults.elastic.es_url || prev.es_url,
          kibana_url: defaults.elastic.kibana_url || prev.kibana_url,
          username: defaults.elastic.username || '',
          verify_tls: defaults.elastic.verify_tls,
        }));
        setJira((prev) => ({
          ...prev,
          base_url: defaults.jira.base_url || '',
          account_email: defaults.jira.account_email || '',
          project_key: defaults.jira.project_key || '',
          default_issue_type: defaults.jira.default_issue_type || 'Task',
        }));
        setConnector((prev) => ({ ...prev, ...defaults.connector }));
      })
      .catch(() => {
        /* Defaults are a convenience; the form works without them. */
      });
  }, []);

  async function run(work) {
    setBusy(true);
    setError(null);
    try {
      return await work();
    } catch (err) {
      setError(err);
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function checkElastic() {
    const result = await run(() => api.validateElastic(elastic));
    if (result) {
      setValidation(result);
      if (result.ok) setStep(1);
    }
  }

  async function checkJira() {
    const result = await run(() => api.validateJira(jira));
    if (result) {
      setValidation(result);
      if (result.ok) setStep(2);
    }
  }

  async function finish() {
    const saved = await run(() =>
      api.saveSetup({ elastic, jira, connector, ticket_route: 'auto' })
    );
    if (saved) onComplete(saved);
  }

  // Projects can only be listed once Jira credentials are saved, so the picker
  // appears on the connector step rather than the Jira step.
  async function loadProjects() {
    const saved = await run(() =>
      api.saveSetup({ elastic, jira, connector, ticket_route: 'auto' })
    );
    if (!saved) return;
    const result = await run(() => api.projects());
    if (result) setProjects(result.projects);
  }

  return (
    <div className="wizard">
      <ol className="steps">
        {STEPS.map((name, index) => (
          <li
            key={name}
            className={index === step ? 'step active' : index < step ? 'step done' : 'step'}
          >
            <span className="step-number">{index + 1}</span> {name}
          </li>
        ))}
      </ol>

      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      {step === 0 && (
        <section className="card">
          <h2>Connect to Elasticsearch</h2>
          <p className="muted">
            Kibana is checked separately — the Jira <em>action</em> connector lives
            there, not on Elasticsearch.
          </p>

          <Field label="Elasticsearch URL" required>
            <input
              value={elastic.es_url}
              onChange={(e) => setElastic({ ...elastic, es_url: e.target.value })}
              placeholder="http://localhost:9200"
            />
          </Field>

          <Field label="Kibana URL" required>
            <input
              value={elastic.kibana_url}
              onChange={(e) => setElastic({ ...elastic, kibana_url: e.target.value })}
              placeholder="http://localhost:5601"
            />
          </Field>

          <Field
            label="API key"
            hint="Encoded key (base64 of id:api_key). Needs manage_connector and manage_api_key. Leave blank to use a username and password."
          >
            <input
              type="password"
              value={elastic.api_key}
              onChange={(e) => setElastic({ ...elastic, api_key: e.target.value })}
              autoComplete="off"
            />
          </Field>

          <div className="row">
            <Field label="Username">
              <input
                value={elastic.username}
                onChange={(e) => setElastic({ ...elastic, username: e.target.value })}
                autoComplete="off"
              />
            </Field>
            <Field label="Password">
              <input
                type="password"
                value={elastic.password}
                onChange={(e) => setElastic({ ...elastic, password: e.target.value })}
                autoComplete="off"
              />
            </Field>
          </div>

          <label className="checkbox">
            <input
              type="checkbox"
              checked={elastic.verify_tls}
              onChange={(e) => setElastic({ ...elastic, verify_tls: e.target.checked })}
            />
            Verify TLS certificates
          </label>

          <div className="actions">
            <button className="primary" onClick={checkElastic} disabled={busy}>
              {busy ? <Spinner label="Checking…" /> : 'Test and continue'}
            </button>
          </div>

          {validation && <CheckList checks={validation.checks} />}
        </section>
      )}

      {step === 1 && (
        <section className="card">
          <h2>Connect to Jira</h2>
          <p className="muted">
            These credentials do double duty: they configure the Elasticsearch
            content connector <em>and</em> give this app its own Jira access.
            Entered once, here.
          </p>

          <Field label="Jira base URL" required hint="e.g. https://your-org.atlassian.net">
            <input
              value={jira.base_url}
              onChange={(e) => setJira({ ...jira, base_url: e.target.value })}
              placeholder="https://your-org.atlassian.net"
            />
          </Field>

          <Field label="Account email" required>
            <input
              type="email"
              value={jira.account_email}
              onChange={(e) => setJira({ ...jira, account_email: e.target.value })}
              autoComplete="off"
            />
          </Field>

          <Field
            label="API token"
            required
            hint="Create one at id.atlassian.com → Security → API tokens. Available on the free plan."
          >
            <input
              type="password"
              value={jira.api_token}
              onChange={(e) => setJira({ ...jira, api_token: e.target.value })}
              autoComplete="off"
            />
          </Field>

          <div className="row">
            <Field label="Project key" required hint="Where new tickets are created.">
              <input
                value={jira.project_key}
                onChange={(e) => setJira({ ...jira, project_key: e.target.value })}
                placeholder="PROBE"
              />
            </Field>
            <Field label="Default issue type">
              <input
                value={jira.default_issue_type}
                onChange={(e) =>
                  setJira({ ...jira, default_issue_type: e.target.value })
                }
              />
            </Field>
          </div>

          <div className="actions">
            <button onClick={() => setStep(0)} disabled={busy}>
              Back
            </button>
            <button className="primary" onClick={checkJira} disabled={busy}>
              {busy ? <Spinner label="Checking…" /> : 'Test and continue'}
            </button>
          </div>

          {validation && (
            <>
              <CheckList checks={validation.checks} />
              {validation.checks?.some(
                (c) => c.name === 'permission:CREATE_ISSUES' && !c.ok
              ) && (
                <Banner kind="warn" title="This account cannot create issues">
                  A Jira admin needs to grant the <code>Create issues</code>{' '}
                  permission in <code>{jira.project_key}</code>. The API token
                  inherits the account's permissions — there is no token setting
                  that can override this.
                </Banner>
              )}
            </>
          )}
        </section>
      )}

      {step === 2 && (
        <section className="card">
          <h2>Content connector</h2>
          <p className="muted">
            This is the Elasticsearch connector that syncs Jira issues into an
            index so the app can search them and spot duplicates.
          </p>

          <div className="row">
            <Field label="Connector ID">
              <input
                value={connector.connector_id}
                onChange={(e) =>
                  setConnector({ ...connector, connector_id: e.target.value })
                }
              />
            </Field>
            <Field
              label="Index name"
              hint="Prefix with search- so Kibana's connector UI adopts it."
            >
              <input
                value={connector.index_name}
                onChange={(e) =>
                  setConnector({ ...connector, index_name: e.target.value })
                }
              />
            </Field>
          </div>

          <Field
            label="Projects to sync"
            hint="Comma-separated project keys, or * for all."
          >
            <input
              value={connector.projects}
              onChange={(e) => setConnector({ ...connector, projects: e.target.value })}
            />
          </Field>

          {projects.length > 0 && (
            <div className="project-chips">
              {projects.map((p) => (
                <button
                  type="button"
                  key={p.key}
                  className="chip"
                  onClick={() =>
                    setConnector({
                      ...connector,
                      projects:
                        connector.projects === '*'
                          ? p.key
                          : `${connector.projects},${p.key}`,
                    })
                  }
                >
                  {p.key} — {p.name}
                </button>
              ))}
            </div>
          )}

          <div className="row">
            <Field label="Sync schedule (cron)">
              <input
                value={connector.sync_interval}
                onChange={(e) =>
                  setConnector({ ...connector, sync_interval: e.target.value })
                }
              />
            </Field>
            <Field label="Retry count">
              <input
                type="number"
                min="0"
                max="10"
                value={connector.retry_count}
                onChange={(e) =>
                  setConnector({
                    ...connector,
                    retry_count: Number(e.target.value),
                  })
                }
              />
            </Field>
          </div>

          <div className="actions">
            <button onClick={() => setStep(1)} disabled={busy}>
              Back
            </button>
            <button onClick={loadProjects} disabled={busy}>
              Load projects from Jira
            </button>
            <button className="primary" onClick={finish} disabled={busy}>
              {busy ? <Spinner label="Saving…" /> : 'Save and continue'}
            </button>
          </div>
        </section>
      )}
    </div>
  );
}
