/** Small shared presentational pieces. */

export function Field({ label, hint, children, required }) {
  return (
    <label className="field">
      <span className="field-label">
        {label}
        {required && <span className="required"> *</span>}
      </span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

export function Banner({ kind = 'info', title, children, onDismiss }) {
  return (
    <div className={`banner banner-${kind}`}>
      <div className="banner-body">
        {title && <strong>{title}</strong>}
        {children && <div>{children}</div>}
      </div>
      {onDismiss && (
        <button type="button" className="banner-close" onClick={onDismiss}>
          ×
        </button>
      )}
    </div>
  );
}

export function ErrorBanner({ error, onDismiss }) {
  if (!error) return null;
  return (
    <Banner kind="error" title={error.message} onDismiss={onDismiss}>
      {error.hint && <p className="hint">{error.hint}</p>}
    </Banner>
  );
}

export function CheckList({ checks }) {
  if (!checks?.length) return null;
  return (
    <ul className="checklist">
      {checks.map((check) => {
        // An advisory check that did not pass is information, not a failure:
        // the licence gate is the expected state on a free tier, and a red ✕
        // would read as something the operator has to go and fix.
        const kind = check.ok
          ? 'ok'
          : check.severity === 'advisory'
            ? 'info'
            : 'fail';
        const icon = { ok: '✓', info: 'ⓘ', fail: '✕' }[kind];
        return (
          <li key={check.name} className={`check-${kind}`}>
            <span className="check-icon">{icon}</span>
            <span>
              <code>{check.name}</code> — {check.message}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

export function Spinner({ label = 'Working…' }) {
  return (
    <span className="spinner" role="status">
      <span className="spinner-dot" /> {label}
    </span>
  );
}

export function Pill({ kind = 'neutral', children }) {
  return <span className={`pill pill-${kind}`}>{children}</span>;
}
