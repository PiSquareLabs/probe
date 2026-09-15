import { useEffect, useRef, useState } from 'react';
import { api } from '../lib/api.js';
import { Banner, ErrorBanner, Field, Pill, Spinner } from './Primitives.jsx';

const BLANK = {
  summary: '',
  description: '',
  issue_type: '',
  priority: '',
  labels: '',
  check_duplicates: true,
};

/** Create tickets, with a live duplicate check against the synced index. */
export default function TicketPanel({ connection }) {
  const [form, setForm] = useState(BLANK);
  const [issueTypes, setIssueTypes] = useState([]);
  const [similar, setSimilar] = useState(null);
  const [created, setCreated] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const debounce = useRef(null);

  useEffect(() => {
    api
      .issueTypes()
      .then((data) => setIssueTypes(data.issue_types || []))
      .catch(() => {
        /* The field falls back to free text if Jira is unreachable. */
      });
  }, []);

  // Look for duplicates as the summary is typed, but only once the user has
  // written enough for the search to mean anything.
  useEffect(() => {
    if (!form.check_duplicates || form.summary.trim().length < 8) {
      setSimilar(null);
      return undefined;
    }
    clearTimeout(debounce.current);
    debounce.current = setTimeout(() => {
      api
        .similarTickets({ query: form.summary, size: 5 })
        .then(setSimilar)
        .catch(() => setSimilar(null));
    }, 500);
    return () => clearTimeout(debounce.current);
  }, [form.summary, form.check_duplicates]);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setCreated(null);
    try {
      const result = await api.createTicket({
        summary: form.summary,
        description: form.description,
        issue_type: form.issue_type || null,
        priority: form.priority || null,
        labels: form.labels
          .split(',')
          .map((l) => l.trim())
          .filter(Boolean),
        check_duplicates: form.check_duplicates,
      });
      setCreated(result);
      setForm(BLANK);
      setSimilar(null);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      {created && (
        <Banner kind="success" title={`Created ${created.key}`} onDismiss={() => setCreated(null)}>
          <p>
            <a href={created.url} target="_blank" rel="noreferrer">
              {created.url}
            </a>
          </p>
          <p className="muted small">
            Created via{' '}
            {created.route_used === 'elastic_connector'
              ? 'the Kibana Jira connector'
              : 'the Jira REST API'}
            .
          </p>
        </Banner>
      )}

      <form className="card" onSubmit={submit}>
        <h3>New ticket</h3>
        <p className="muted">
          Goes into <code>{connection?.jira?.project_key}</code> on{' '}
          <code>{connection?.jira?.base_url}</code>.
        </p>

        <Field label="Summary" required>
          <input
            value={form.summary}
            onChange={(e) => setForm({ ...form, summary: e.target.value })}
            placeholder="Checkout returns 500 for guest users"
            maxLength={255}
            required
          />
        </Field>

        <Field label="Description">
          <textarea
            rows={5}
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
            placeholder="Steps to reproduce, expected vs actual…"
          />
        </Field>

        <div className="row">
          <Field label="Issue type">
            {issueTypes.length > 0 ? (
              <select
                value={form.issue_type}
                onChange={(e) => setForm({ ...form, issue_type: e.target.value })}
              >
                <option value="">
                  Default ({connection?.jira?.default_issue_type || 'Task'})
                </option>
                {issueTypes.map((t) => (
                  <option key={t.id} value={t.name}>
                    {t.name}
                  </option>
                ))}
              </select>
            ) : (
              <input
                value={form.issue_type}
                onChange={(e) => setForm({ ...form, issue_type: e.target.value })}
                placeholder="Task"
              />
            )}
          </Field>

          <Field label="Priority">
            <input
              value={form.priority}
              onChange={(e) => setForm({ ...form, priority: e.target.value })}
              placeholder="High"
            />
          </Field>

          <Field label="Labels" hint="Comma-separated">
            <input
              value={form.labels}
              onChange={(e) => setForm({ ...form, labels: e.target.value })}
              placeholder="from-probe, triage"
            />
          </Field>
        </div>

        <label className="checkbox">
          <input
            type="checkbox"
            checked={form.check_duplicates}
            onChange={(e) =>
              setForm({ ...form, check_duplicates: e.target.checked })
            }
          />
          Check for duplicates before creating
        </label>

        {similar?.results?.length > 0 && (
          <Banner kind="warn" title="Similar tickets already exist">
            <ul className="similar">
              {similar.results.map((t) => (
                <li key={`${t.key}-${t.summary}`}>
                  <a href={t.url} target="_blank" rel="noreferrer">
                    {t.key}
                  </a>{' '}
                  — {t.summary}{' '}
                  {t.status && <Pill kind="neutral">{t.status}</Pill>}
                </li>
              ))}
            </ul>
            <p className="muted small">
              Matched from {similar.source === 'elasticsearch' ? 'the synced Elasticsearch index' : 'Jira directly'}.
            </p>
          </Banner>
        )}

        <div className="actions">
          <button className="primary" type="submit" disabled={busy || !form.summary.trim()}>
            {busy ? <Spinner label="Creating…" /> : 'Create ticket'}
          </button>
        </div>
      </form>
    </div>
  );
}
