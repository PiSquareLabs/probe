/**
 * Backend client.
 *
 * Every call funnels through `request` so the error envelope the backend
 * returns ({code, message, hint, detail}) reaches the UI intact instead of
 * being flattened into "Failed to fetch".
 */

const BASE = import.meta.env.VITE_API_BASE_URL || '';
const API_KEY = import.meta.env.VITE_PROBE_API_KEY || '';

export class ApiError extends Error {
  constructor(message, { code, hint, detail, status } = {}) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.hint = hint;
    this.detail = detail;
    this.status = status;
  }
}

async function request(path, { method = 'GET', body } = {}) {
  let response;
  try {
    response = await fetch(`${BASE}${path}`, {
      method,
      headers: {
        'Content-Type': 'application/json',
        ...(API_KEY ? { 'X-Probe-Key': API_KEY } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (cause) {
    throw new ApiError('Could not reach the Probe backend.', {
      code: 'network_error',
      hint: 'Is the FastAPI server running on port 8000?',
    });
  }

  const text = await response.text();
  const payload = text ? safeJson(text) : null;

  if (!response.ok) {
    throw new ApiError(errorMessage(payload, response.status), {
      code: payload?.code,
      hint: payload?.hint,
      detail: payload?.detail,
      status: response.status,
    });
  }
  return payload;
}

function safeJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return { raw: text };
  }
}

function errorMessage(payload, status) {
  if (!payload) return `Request failed (${status}).`;
  if (typeof payload.message === 'string') return payload.message;
  // FastAPI validation errors arrive as {detail: [{loc, msg}, ...]}.
  if (Array.isArray(payload.detail)) {
    return payload.detail
      .map((d) => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`)
      .join('; ');
  }
  if (typeof payload.detail === 'string') return payload.detail;
  return `Request failed (${status}).`;
}

export const api = {
  health: () => request('/api/health'),
  defaults: () => request('/api/setup/defaults'),
  connection: () => request('/api/connection'),
  saveSetup: (payload) => request('/api/setup', { method: 'POST', body: payload }),
  clearConnection: () => request('/api/connection', { method: 'DELETE' }),

  validateJira: (jira) => request('/api/validate/jira', { method: 'POST', body: jira }),
  validateElastic: (elastic) =>
    request('/api/validate/elastic', { method: 'POST', body: elastic }),

  capabilities: () => request('/api/connector/capabilities'),
  provision: (options) =>
    request('/api/connector/provision', { method: 'POST', body: options }),
  connectorStatus: () => request('/api/connector/status'),
  triggerSync: (jobType = 'full') =>
    request(`/api/connector/sync?job_type=${encodeURIComponent(jobType)}`, {
      method: 'POST',
    }),
  serviceConfig: () => request('/api/connector/service-config'),

  projects: (query = '') =>
    request(`/api/jira/projects?query=${encodeURIComponent(query)}`),
  issueTypes: (projectKey) =>
    request(`/api/jira/issue-types${projectKey ? `?project_key=${encodeURIComponent(projectKey)}` : ''}`),

  createTicket: (ticket) => request('/api/tickets', { method: 'POST', body: ticket }),
  searchTickets: (payload) =>
    request('/api/tickets/search', { method: 'POST', body: payload }),
  similarTickets: (payload) =>
    request('/api/tickets/similar', { method: 'POST', body: payload }),
};
