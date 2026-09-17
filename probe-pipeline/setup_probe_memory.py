"""Creates the `probe-memory` index with the mapping remediator.py and
writer.py actually assume exists -- nothing else in this repo creates
it. Same pattern as ../catalog/upload_to_es.py's `fault-catalog` index:
idempotent (safe to re-run), semantic_text field backed by the
preconfigured ELSER endpoint.

Usage:
    export ES_URL="https://<your-project>.es.<region>...elastic.cloud"
    export ES_API_KEY="<api key>"
    python setup_probe_memory.py

Two document `kind`s share this one index (kept in probe-memory rather
than split into two indices because the Remediator's stage 0/1 queries
and the ruled-out query both filter by `kind` anyway -- see
remediator.py's three ES|QL tools):

  kind: "runbook"    -- signature.*, occurrences, failed_reuses, status,
                         verified_by, root_cause, steps, symptoms,
                         semantic (ELSER), ruled_out_before, runs
  kind: "ruled_out"   -- proposed.{fault_class,service}, incident,
                         symptom, resolved_by, verified_by
"""
import json
import os
import urllib.error
import urllib.request

ES_URL = os.environ.get("ES_URL", "http://localhost:9200")
INDEX_NAME = "probe-memory"
INFERENCE_ID = os.environ.get("ES_INFERENCE_ID", ".elser-2-elasticsearch")


def _auth_header() -> str:
    import base64

    api_key = os.environ.get("ES_API_KEY")
    if api_key:
        return f"ApiKey {api_key}"
    username = os.environ.get("ES_USERNAME", "elastic")
    password = os.environ.get("ES_PASSWORD")
    if not password:
        raise RuntimeError("Set ES_API_KEY, or ES_USERNAME + ES_PASSWORD")
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


def _request(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{ES_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json", "Authorization": _auth_header()},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()}") from e


def ensure_inference_endpoint() -> None:
    try:
        _request("GET", f"/_inference/{INFERENCE_ID}")
        print(f"Inference endpoint {INFERENCE_ID} already exists.")
    except RuntimeError as e:
        if "404" not in str(e):
            raise
        print(f"Creating inference endpoint {INFERENCE_ID} ...")
        _request(
            "PUT",
            f"/_inference/sparse_embedding/{INFERENCE_ID}",
            {"service": "elasticsearch", "service_settings": {"num_allocations": 1, "num_threads": 1}},
        )


MAPPING = {
    "mappings": {
        "properties": {
            "kind": {"type": "keyword"},  # "runbook" | "ruled_out"
            # -- runbook fields --
            # fault_class/service are also baked into the doc id
            # (writer.py's `f"{fault_class}__{service}"`), but stored
            # as their own fields too so a caller with just the doc
            # (e.g. a memory-hit's `runbook`) can read them back
            # without parsing the id string.
            "fault_class": {"type": "keyword"},
            "service": {"type": "keyword"},
            "status": {"type": "keyword"},  # candidate | verified | demoted
            "occurrences": {"type": "integer"},
            "failed_reuses": {"type": "integer"},
            "verified_by": {"type": "keyword"},  # ["human"] | ["recovery"] | []
            "root_cause": {"type": "text"},
            "steps": {"type": "text"},
            "symptoms": {"type": "text"},
            "semantic": {"type": "semantic_text", "inference_id": INFERENCE_ID},
            "signature": {
                "properties": {
                    "change_point": {"type": "keyword"},
                    "metric": {"type": "keyword"},
                    "loudest_service": {"type": "keyword"},
                    "dependency": {"type": "keyword"},
                }
            },
            "ruled_out_before": {"type": "keyword"},
            "runs": {"type": "keyword"},
            # -- ruled_out fields --
            "proposed": {
                "properties": {
                    "fault_class": {"type": "keyword"},
                    "service": {"type": "keyword"},
                }
            },
            "incident": {"type": "keyword"},
            "symptom": {"type": "text"},  # rb_ruled_out's MATCH(symptom, ?symptom)
            "resolved_by": {"type": "keyword"},
        }
    }
}


def ensure_index() -> None:
    existing = _request("GET", "/_cat/indices?format=json")
    if any(idx.get("index") == INDEX_NAME for idx in existing):
        print(f"Index {INDEX_NAME} already exists, leaving it as-is.")
        return
    _request("PUT", f"/{INDEX_NAME}", MAPPING)
    print(f"Created index {INDEX_NAME}.")


if __name__ == "__main__":
    ensure_inference_endpoint()
    ensure_index()
