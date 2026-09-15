import { useState } from 'react';
import { api } from '../lib/api.js';
import { Banner, ErrorBanner, Pill, Spinner } from './Primitives.jsx';

/** Search the tickets the content connector has synced into Elasticsearch. */
export default function SearchPanel() {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function search(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setResults(await api.searchTickets({ query, size: 20 }));
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <form className="card" onSubmit={search}>
        <h3>Search tickets</h3>
        <p className="muted">
          Queries the Elasticsearch index the connector syncs into, and falls
          back to Jira when that index is not ready yet.
        </p>
        <div className="search-row">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="latency, checkout 500, connector sync…"
          />
          <button className="primary" type="submit" disabled={busy}>
            {busy ? <Spinner label="Searching…" /> : 'Search'}
          </button>
        </div>
      </form>

      {results && (
        <section className="card">
          <header className="card-header">
            <h3>{results.total} result{results.total === 1 ? '' : 's'}</h3>
            <Pill kind={results.source === 'elasticsearch' ? 'good' : 'warn'}>
              from {results.source}
            </Pill>
          </header>

          {results.note && <Banner kind="info">{results.note}</Banner>}

          {results.results.length === 0 ? (
            <p className="muted">Nothing matched.</p>
          ) : (
            <ul className="results">
              {results.results.map((t, index) => (
                <li key={`${t.key}-${index}`}>
                  <div className="result-head">
                    {t.url ? (
                      <a href={t.url} target="_blank" rel="noreferrer">
                        {t.key}
                      </a>
                    ) : (
                      <strong>{t.key || '—'}</strong>
                    )}
                    {t.status && <Pill kind="neutral">{t.status}</Pill>}
                    {t.score != null && (
                      <span className="muted small">score {t.score.toFixed(2)}</span>
                    )}
                  </div>
                  <div>{t.summary || <span className="muted">No summary indexed</span>}</div>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  );
}
