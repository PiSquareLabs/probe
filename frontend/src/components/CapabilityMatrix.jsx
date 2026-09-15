import { Pill } from './Primitives.jsx';

/**
 * Shows what the deployment's Elastic licence permits.
 *
 * This is deliberately prominent rather than buried in a log: "why did my
 * ticket get created by the app instead of by Elastic?" is the first question
 * anyone asks, and the answer is always here.
 */
export default function CapabilityMatrix({ report }) {
  if (!report) return null;

  const viaElastic = report.effective_route === 'elastic_connector';

  return (
    <section className="card">
      <header className="card-header">
        <h3>What this Elastic deployment supports</h3>
        <Pill kind={viaElastic ? 'good' : 'warn'}>
          licence: {report.license_type || 'unknown'}
        </Pill>
      </header>

      <p className="route-summary">
        Tickets are created{' '}
        {viaElastic ? (
          <strong>by Elastic, through the Kibana Jira connector.</strong>
        ) : (
          <strong>by this app, through the Jira REST API.</strong>
        )}{' '}
        {!viaElastic && (
          <span className="muted">
            The Kibana Jira connector needs a Gold licence. Everything still
            works — only the component making the outbound call differs.
          </span>
        )}
      </p>

      <table className="matrix">
        <thead>
          <tr>
            <th>Capability</th>
            <th>Needs</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {report.capabilities.map((cap) => (
            <tr key={cap.id}>
              <td>
                <div>{cap.label}</div>
                <div className="muted small">{cap.reason}</div>
              </td>
              <td>
                <code>{cap.required_license || 'none'}</code>
              </td>
              <td>
                <Pill kind={cap.available ? 'good' : 'warn'}>
                  {cap.available ? 'available' : 'unavailable'}
                </Pill>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
