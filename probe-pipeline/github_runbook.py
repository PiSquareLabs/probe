"""Reads the human-facing runbook mirror -- `runbooks/{fault_class}__{service}.md`
in this GitHub repo -- for display, not for grading.

This is the counterpart to the `probe-memory` reads in remediator.py, but
it is deliberately a separate module that neither remediator.py nor
correlator.py imports. Per PROBE-component-contracts.md #7a's own table,
the only things allowed to read a runbook are the Remediator (on the
graded path) and two display-only/chat-only consumers that are NOT on
the graded path: the Kibana evidence card and the Agent Builder chat
tool. This module is that second kind -- a plain read, for a human or a
UI, that never feeds back into `remediate()` or `correlate()`.

Why it must stay disconnected: the GitHub file can be hand-edited by
whoever reviewed the Writer's PR (that's the whole point of it). If
anything on the graded path read it back, a human's Markdown edit could
silently change what counts as a "hit" on the next incident, and the
hit-rate number would stop being a property of the retrieval logic.

Two ways to read it, pick based on where the caller runs:
  - `read_local()`    -- the harness/CLI has this repo checked out.
  - `read_via_api()`  -- a hosted display service (no local checkout),
                         via GitHub's Contents API.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNBOOKS_DIR = REPO_ROOT / "runbooks"


def _runbook_filename(fault_class: str, service: str) -> str:
    return f"{fault_class}__{service}.md"


@dataclass
class GitRunbook:
    fault_class: str
    service: str
    markdown: str
    steps: list[str]
    source: str  # "local" | "github_api"


def _parse_steps(markdown: str) -> list[str]:
    """Pulls a numbered/bulleted "## Steps" section out of the runbook
    body, if there is one. Best-effort -- this is for display, so a
    parse miss just means an empty list, not an error.
    """
    match = re.search(r"^##\s*Steps\s*$(.*?)(^##\s|\Z)", markdown, re.MULTILINE | re.DOTALL)
    if not match:
        return []
    body = match.group(1)
    return [
        re.sub(r"^\s*(?:\d+[.)]|[-*])\s*", "", line).strip()
        for line in body.splitlines()
        if line.strip()
    ]


def read_local(fault_class: str, service: str, runbooks_dir: Path = RUNBOOKS_DIR) -> GitRunbook | None:
    """Reads runbooks/{fault_class}__{service}.md straight off disk --
    the fast path when the caller already has this repo checked out
    (e.g. a local harness run, or a script sitting next to
    remediator.py). Returns None if no such file exists yet -- this
    fault has never been graded correct, or the Writer hasn't opened
    the PR, or the PR hasn't merged.
    """
    path = runbooks_dir / _runbook_filename(fault_class, service)
    if not path.exists():
        return None
    markdown = path.read_text(encoding="utf-8")
    return GitRunbook(fault_class, service, markdown, _parse_steps(markdown), source="local")


def read_via_api(
    fault_class: str,
    service: str,
    owner: str,
    repo: str,
    ref: str = "main",
    token: str | None = None,
) -> GitRunbook | None:
    """Reads the same file via GitHub's Contents API, for a caller with
    no local checkout (e.g. a hosted evidence-card service, or Agent
    Builder's chat-only lookup tool). `token` is optional for a public
    repo, required for a private one -- reads `GITHUB_TOKEN` from the
    environment if not passed explicitly.
    """
    token = token or os.environ.get("GITHUB_TOKEN")
    filename = _runbook_filename(fault_class, service)
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/runbooks/{filename}?ref={ref}"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    markdown = base64.b64decode(payload["content"]).decode("utf-8")
    return GitRunbook(fault_class, service, markdown, _parse_steps(markdown), source="github_api")
