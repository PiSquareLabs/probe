"""Serves dashboard.html + pipeline_events.jsonl (browsers block fetch()
of local files opened via file://), and a small JSON API the dashboard
uses to show/toggle fault flags and launch run_working_fault.py -- the
one combination empirically confirmed (this session, this local stack)
to run the full Detector->Gate->Remediator->Correlator->Grader->Writer
chain and come back correct_at1=True: adHighCpu, with Gate bypassed
(see run_working_fault.py's WORKING_FAULTS docstring for exactly why
real Gate never opened for anything tested, and why that's an honest
limitation of this local stack's traffic level, not of Gate itself).
With the environment (ES_URL/ES_API_KEY/an LLM key) typed into the page.
Stdlib only, no Flask/etc.

    python serve_dashboard.py [port]   # default 8765

Then open http://localhost:8765/dashboard.html .

Security model: binds to 127.0.0.1 only, no auth, no TLS -- this is a
local dev tool, not something to expose beyond localhost. The API keys
you type into the "Environment" panel are POSTed to this local server in
plaintext (loopback only) and passed straight through as subprocess
environment variables for one run -- never written to disk, never
logged, never proxied anywhere else. The dashboard's own localStorage
save (for convenience across page reloads) is entirely client-side; this
server never sees or stores it beyond the single request that launches a
run.

WORKING_FLAGS mirrors run_working_fault.WORKING_FAULTS: only flags
empirically confirmed end-to-end correct are exposed in the UI's flag
buttons/run selector. The other 10 flags in flagd_control.FLAGS are
real and injectable via flagd_control.py directly, but showing all 11
in a "click to test" UI when most don't reliably produce a correct
graded result on this stack's current traffic level just produces
confusing dead-end tests. Update both sets together as more flags get
confirmed (see PROBE-LIVE-TESTING-GUIDE.md sections 6 and 8).

API:
    GET  /api/flags          -> {name: {current, on_variant, target_service, is_on}, ...}
                                 (only WORKING_FLAGS, not all of flagd_control.FLAGS)
    POST /api/flags/<name>   {"variant": "on"} -> sets it, returns the new read_flags() snapshot
    POST /api/flags/reset    -> turns every known flag off (all of them, not just WORKING_FLAGS,
                                 as a safety net against a flag left on from before this UI existed)
    POST /api/run             {"flag": "...", "llm": null,
                                "es_url": "...", "es_api_key": "...",
                                "aws_bearer_token_bedrock": "...", "aws_region": "...",
                                "bedrock_model_id": "...", "openai_api_key": "..."}
                               -> launches run_working_fault.py --flag <flag> as a background
                                  subprocess with those as env vars, returns
                                  {"started": true, "run_id": "..."} immediately
                                  (non-blocking -- watch it happen via pipeline_events.jsonl,
                                  which the dashboard already polls every 3s)
    POST /api/techtest/clear    {"flag": "...", ...same env fields as /api/run}
                                 -> synchronously runs techtest_clear.py, which resolves the
                                    flag's (fault_class, service) and deletes that runbook +
                                    its ruled_out docs from probe-memory (scoped, not a full
                                    wipe), relays its JSON result
    POST /api/techtest/feedback {"verdict": "correct"|"incorrect",
                                  "runbook_id": "...", "root_cause": "...", "steps": [...],
                                  "fault_class": "...", "service": "...",
                                  "incident_id": "...", "symptom": "...",
                                  ...same env fields as /api/run}
                                 -> synchronously runs techtest_feedback.py: "correct" calls
                                    Writer.mark_dev_verified (optionally with an edited
                                    root_cause/steps), which also provisions this service's
                                    Agent Builder candidate-query tool; "incorrect" calls
                                    Writer.mark_jira_rejected. Relays its JSON result.
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import flagd_control
from run_working_fault import WORKING_FAULTS as WORKING_FLAGS

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
HERE = Path(__file__).resolve().parent

# env var name in the POST body -> actual environment variable name passed
# to the subprocess. Only these are ever forwarded -- nothing else in the
# request body leaks into the child process's environment.
_ENV_FIELDS = {
    "es_url": "ES_URL",
    "es_api_key": "ES_API_KEY",
    "aws_bearer_token_bedrock": "AWS_BEARER_TOKEN_BEDROCK",
    "aws_region": "AWS_REGION",
    "bedrock_model_id": "BEDROCK_MODEL_ID",
    "openai_api_key": "OPENAI_API_KEY",
}


def _working_flags() -> dict:
    all_flags = flagd_control.read_flags()
    return {name: info for name, info in all_flags.items() if name in WORKING_FLAGS}


def _build_env(body: dict) -> dict:
    """Same whitelisted-fields-only env-building /api/run always did,
    factored out so /api/techtest/clear and /api/techtest/feedback share
    it: ES_URL/ES_API_KEY typed into the dashboard's Settings tab must
    reach the subprocess this way, never via a direct es_client import in
    this long-running server process (es_client.ES_URL is a module-level
    constant read once at import time -- mutating os.environ afterward
    here would silently do nothing).
    """
    env = dict(os.environ)
    for body_key, env_name in _ENV_FIELDS.items():
        if body.get(body_key):
            env[env_name] = body[body_key]
    return env


def _run_sync(script_args: list[str], env: dict) -> dict:
    """Runs a quick (single/few doc) helper script to completion and
    relays its JSON stdout -- unlike /api/run's fire-and-forget Popen
    (a 2-minute watch loop), techtest_clear.py/techtest_feedback.py are
    a handful of ES calls and can just be waited on synchronously.
    """
    result = subprocess.run(
        [sys.executable, *script_args], cwd=str(HERE), env=env,
        capture_output=True, text=True, timeout=30,
    )
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"ok": False, "error": (result.stderr or result.stdout or "no output").strip()[-2000:]}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(HERE), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format, *args):
        pass  # dashboard polls every 3s; the default per-request log line is just noise

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length))

    def do_GET(self):
        # self.path includes the query string (dashboard.html appends
        # ?t=<timestamp> as a cache-buster on every fetch) -- comparing
        # the raw path against a bare route like "/api/flags" always
        # failed once that cache-buster was added, silently falling
        # through to static-file serving and 404ing. Strip it first.
        path = urlsplit(self.path).path
        if path == "/api/flags":
            try:
                self._json(200, _working_flags())
            except Exception as e:
                self._json(500, {"error": str(e)})
            return
        super().do_GET()

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/api/flags/reset":
            try:
                # resets ALL flags, not just WORKING_FLAGS -- a safety net
                # for anything left on from before this dashboard existed
                changed = flagd_control.reset_all()
                self._json(200, {"reset": changed, "flags": _working_flags()})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if path.startswith("/api/flags/"):
            name = path.removeprefix("/api/flags/")
            if name not in WORKING_FLAGS:
                self._json(400, {"error": f"{name!r} is not in WORKING_FLAGS -- not exposed by this UI"})
                return
            try:
                body = self._read_json_body()
                variant = body.get("variant") or flagd_control.FLAGS[name][0]
                flagd_control.set_flag(name, variant)
                self._json(200, {"flags": _working_flags()})
            except Exception as e:
                self._json(400, {"error": str(e)})
            return

        if path == "/api/run":
            try:
                body = self._read_json_body()
                flag = body.get("flag")
                if flag not in WORKING_FLAGS:
                    raise ValueError(f"{flag!r} is not in WORKING_FLAGS -- not exposed by this UI")

                run_id = uuid.uuid4().hex[:8]
                env = _build_env(body)
                env["PIPELINE_RUN_ID"] = run_id

                cmd = [sys.executable, "run_working_fault.py", "--flag", flag]
                llm = body.get("llm")
                if llm == "bedrock":
                    cmd.append("--use-bedrock")
                elif llm == "openai":
                    cmd.append("--use-openai")

                # Non-blocking: this subprocess can run for up to ~2 minutes
                # (run_working_fault.py's default --minutes) -- the dashboard follows progress via
                # pipeline_events.jsonl (plog.emit calls tagged with run_id),
                # not by waiting on this HTTP response.
                subprocess.Popen(cmd, cwd=str(HERE), env=env,
                                  stdout=open(HERE / f"run_{run_id}.log", "w"),
                                  stderr=subprocess.STDOUT)
                self._json(200, {"started": True, "run_id": run_id, "flag": flag})
            except Exception as e:
                self._json(400, {"error": str(e)})
            return

        if path == "/api/techtest/clear":
            try:
                body = self._read_json_body()
                flag = body.get("flag")
                if flag not in WORKING_FLAGS:
                    raise ValueError(f"{flag!r} is not in WORKING_FLAGS -- not exposed by this UI")
                self._json(200, _run_sync(["techtest_clear.py", "--flag", flag], _build_env(body)))
            except Exception as e:
                self._json(400, {"error": str(e)})
            return

        if path == "/api/techtest/feedback":
            try:
                body = self._read_json_body()
                verdict = body.get("verdict")
                if verdict not in ("correct", "incorrect"):
                    raise ValueError(f"verdict must be 'correct' or 'incorrect', got {verdict!r}")

                cmd = ["techtest_feedback.py", "--verdict", verdict]
                if verdict == "correct":
                    if not body.get("runbook_id"):
                        raise ValueError("verdict 'correct' requires runbook_id")
                    cmd += ["--runbook-id", body["runbook_id"]]
                    if body.get("root_cause"):
                        cmd += ["--root-cause", body["root_cause"]]
                    if body.get("steps") is not None:
                        cmd += ["--steps-json", json.dumps(body["steps"])]
                else:
                    for field in ("fault_class", "service", "incident_id", "symptom"):
                        if not body.get(field):
                            raise ValueError(f"verdict 'incorrect' requires {field}")
                        cmd += [f"--{field.replace('_', '-')}", body[field]]

                self._json(200, _run_sync(cmd, _build_env(body)))
            except Exception as e:
                self._json(400, {"error": str(e)})
            return

        self._json(404, {"error": "not found"})


if __name__ == "__main__":
    with http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler) as httpd:
        print(f"Dashboard: http://localhost:{PORT}/dashboard.html")
        print("Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
