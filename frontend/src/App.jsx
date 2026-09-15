import { useCallback, useEffect, useState } from 'react';
import { api } from './lib/api.js';
import ConnectorPanel from './components/ConnectorPanel.jsx';
import SearchPanel from './components/SearchPanel.jsx';
import SetupWizard from './components/SetupWizard.jsx';
import TicketPanel from './components/TicketPanel.jsx';
import { Banner, ErrorBanner, Pill, Spinner } from './components/Primitives.jsx';

const TABS = [
  { id: 'tickets', label: 'Tickets' },
  { id: 'search', label: 'Search' },
  { id: 'connector', label: 'Connector' },
  { id: 'settings', label: 'Settings' },
];

export default function App() {
  const [connection, setConnection] = useState(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState('tickets');
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setConnection(await api.connection());
      setError(null);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function reset() {
    if (!window.confirm('Remove the stored Elastic and Jira credentials?')) return;
    try {
      await api.clearConnection();
      await load();
      setTab('tickets');
    } catch (err) {
      setError(err);
    }
  }

  return (
    <div className="app">
      <header className="app-header">
        <div>
          <h1>Probe</h1>
          <p className="tagline">
            Configure Elasticsearch's Jira integrations from here — not from Kibana.
          </p>
        </div>
        {connection?.configured && (
          <div className="header-meta">
            <Pill kind="good">{connection.jira.project_key}</Pill>
            <span className="muted small">{connection.jira.base_url}</span>
          </div>
        )}
      </header>

      <main>
        <ErrorBanner error={error} onDismiss={() => setError(null)} />

        {loading && <Spinner label="Loading…" />}

        {!loading && !connection?.configured && (
          <>
            <Banner kind="info" title="One-time setup">
              Enter your Elasticsearch and Jira details once. They provision the
              Elasticsearch content connector and, where the licence allows it,
              the Kibana Jira action connector.
            </Banner>
            <SetupWizard onComplete={(saved) => setConnection(saved)} />
          </>
        )}

        {!loading && connection?.configured && (
          <>
            <nav className="tabs">
              {TABS.map((t) => (
                <button
                  key={t.id}
                  className={tab === t.id ? 'tab active' : 'tab'}
                  onClick={() => setTab(t.id)}
                >
                  {t.label}
                </button>
              ))}
            </nav>

            {tab === 'tickets' && <TicketPanel connection={connection} />}
            {tab === 'search' && <SearchPanel />}
            {tab === 'connector' && <ConnectorPanel />}
            {tab === 'settings' && (
              <section className="card">
                <h3>Stored connection</h3>
                <p className="muted">
                  Secrets are encrypted at rest and only ever shown masked.
                </p>
                <dl className="kv">
                  <dt>Elasticsearch</dt>
                  <dd><code>{connection.elastic.es_url}</code></dd>
                  <dt>Kibana</dt>
                  <dd><code>{connection.elastic.kibana_url}</code></dd>
                  <dt>Elastic API key</dt>
                  <dd><code>{connection.elastic.api_key || '—'}</code></dd>
                  <dt>Jira site</dt>
                  <dd><code>{connection.jira.base_url}</code></dd>
                  <dt>Jira account</dt>
                  <dd><code>{connection.jira.account_email}</code></dd>
                  <dt>Jira API token</dt>
                  <dd><code>{connection.jira.api_token}</code></dd>
                  <dt>Project</dt>
                  <dd><code>{connection.jira.project_key}</code></dd>
                  <dt>Index</dt>
                  <dd><code>{connection.connector?.index_name}</code></dd>
                  <dt>Updated</dt>
                  <dd>{connection.updated_at || '—'}</dd>
                </dl>
                <div className="actions">
                  <button className="danger" onClick={reset}>
                    Clear credentials
                  </button>
                </div>
              </section>
            )}
          </>
        )}
      </main>

      <footer className="app-footer">
        <span className="muted small">
          The Kibana Jira action connector requires a Gold licence. On Basic,
          tickets are created directly through the Jira REST API — see{' '}
          <code>docs/RESEARCH.md</code>.
        </span>
      </footer>
    </div>
  );
}
