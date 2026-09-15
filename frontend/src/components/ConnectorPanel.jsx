import { useCallback, useEffect, useState } from 'react';
import { api } from '../lib/api.js';
import CapabilityMatrix from './CapabilityMatrix.jsx';
import { Banner, ErrorBanner, Pill, Spinner } from './Primitives.jsx';

const STATUS_KIND = {
  ok: 'good',
  skipped: 'warn',
  failed: 'bad',
};

/** Provision and monitor both Elastic-side integrations. */
export default function ConnectorPanel() {
  const [capabilities, setCapabilities] = useState(null);
  const [status, setStatus] = useState(null);
  const [result, setResult] = useState(null);
  const [serviceConfig, setServiceConfig] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    setError(null);
    const [caps, stat] = await Promise.allSettled([
      api.capabilities(),
      api.connectorStatus(),
    ]);
    if (caps.status === 'fulfilled') setCapabilities(caps.value);
    else setError(caps.reason);
    if (stat.status === 'fulfilled') setStatus(stat.value);
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Poll while a sync is in flight so the document count moves without the
  // operator having to click anything.
  useEffect(() => {
    if (status?.last_sync_status !== 'in_progress') return undefined;
    const timer = setInterval(() => {
      api.connectorStatus().then(setStatus).catch(() => {});
    }, 5000);
    return () => clearInterval(timer);
  }, [status?.last_sync_status]);

  async function provision() {
    setBusy(true);
    setError(null);
    try {
      const provisioned = await api.provision({
        create_content_connector: true,
        create_action_connector: true,
        trigger_sync: true,
        generate_connector_api_key: true,
      });
      setResult(provisioned);
      await refresh();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  async function sync() {
    setBusy(true);
    try {
      await api.triggerSync('full');
      await refresh();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  async function showServiceConfig() {
    try {
      setServiceConfig(await api.serviceConfig());
    } catch (err) {
      setError(err);
    }
  }

  return (
    <div className="stack">
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <CapabilityMatrix report={capabilities} />

      <section className="card">
        <header className="card-header">
          <h3>Provisioning</h3>
          <div className="actions inline">
            <button onClick={refresh} disabled={busy}>
              Refresh
            </button>
            <button className="primary" onClick={provision} disabled={busy}>
              {busy ? <Spinner /> : 'Provision connectors'}
            </button>
          </div>
        </header>

        <p className="muted">
          Creates the Elasticsearch content connector, pushes your Jira
          credentials into its configuration, schedules a sync, and creates the
          Kibana Jira action connector if the licence allows it.
        </p>

        {result && (
          <>
            <ul className="steps-result">
              {result.steps.map((step) => (
                <li key={step.step}>
                  <Pill kind={STATUS_KIND[step.status]}>{step.status}</Pill>
                  <span>
                    <code>{step.step}</code> — {step.message}
                  </span>
                </li>
              ))}
            </ul>

            {result.connector_service_api_key && (
              <Banner kind="warn" title="Copy this API key now">
                <p>
                  Elasticsearch returns an API key's secret only once. Paste it
                  into <code>connectors-config/config.yml</code> before starting
                  the connector service.
                </p>
                <pre className="code">{result.connector_config_yaml}</pre>
              </Banner>
            )}
          </>
        )}
      </section>

      <section className="card">
        <header className="card-header">
          <h3>Sync status</h3>
          <div className="actions inline">
            <button onClick={showServiceConfig}>Show service config</button>
            <button onClick={sync} disabled={busy || status?.status === 'not_created'}>
              Run full sync
            </button>
          </div>
        </header>

        {!status && <Spinner label="Loading…" />}

        {status && status.status === 'not_created' && (
          <Banner kind="info" title="No connector yet">
            Provision the connectors above to create{' '}
            <code>{status.connector_id}</code>.
          </Banner>
        )}

        {status && status.status !== 'not_created' && (
          <>
            <dl className="kv">
              <dt>Connector</dt>
              <dd>
                <code>{status.connector_id}</code>
              </dd>
              <dt>Index</dt>
              <dd>
                <code>{status.index_name}</code>
              </dd>
              <dt>State</dt>
              <dd>
                <Pill kind={status.status === 'connected' ? 'good' : 'warn'}>
                  {status.status || 'unknown'}
                </Pill>
              </dd>
              <dt>Last sync</dt>
              <dd>{status.last_sync_status || '—'}</dd>
              <dt>Last synced at</dt>
              <dd>{status.last_synced || '—'}</dd>
              <dt>Documents in index</dt>
              <dd>{status.document_count ?? '—'}</dd>
            </dl>

            {!status.configured && (
              <Banner kind="warn" title="The connector service has not checked in">
                Elasticsearch is holding the connector record, but the{' '}
                <code>elastic-connectors</code> service has not registered its
                configuration schema. Start it with the config below, then
                provision again — Jira settings cannot be written until it is
                running.
              </Banner>
            )}

            {status.last_sync_error && (
              <Banner kind="error" title="Last sync failed">
                <pre className="code">{status.last_sync_error}</pre>
              </Banner>
            )}
          </>
        )}

        {serviceConfig && (
          <div className="stack">
            <p className="muted">
              Write this to <code>{serviceConfig.filename}</code>:
            </p>
            <pre className="code">{serviceConfig.content}</pre>
          </div>
        )}
      </section>
    </div>
  );
}
