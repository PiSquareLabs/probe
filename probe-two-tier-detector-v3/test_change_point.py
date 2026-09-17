"""The 3 tests ../FIX-tier2-changepoint.md asks for. No Elasticsearch
needed -- change_point._esql is mocked in every test.

Run: python -m pytest test_change_point.py -v
  (or: python test_change_point.py)
"""
import io
import urllib.error
from unittest.mock import patch

import change_point
import detector
from detector import _confirm_candidate


def _fake_http_error(body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url="http://x/_query", code=400, msg="Bad Request",
                                   hdrs=None, fp=io.BytesIO(body))


# ---------------------------------------------------------------------
# 1. Insufficient-data test
# ---------------------------------------------------------------------
def test_insufficient_data_reason_counter_and_tier1():
    change_point.INSUFFICIENT_DATA_COUNTS.clear()
    err = _fake_http_error(b"not enough data, only 12 rows for change point analysis")
    with patch.object(change_point, "_esql", side_effect=lambda q: (_ for _ in ()).throw(err)):
        result = change_point.change_point("checkout", "cpu")

    assert result["reason"] == "insufficient_data"
    assert result["rows"] == 12
    assert result["type"] is None
    assert change_point.INSUFFICIENT_DATA_COUNTS[("checkout", "cpu")] == 1

    candidate = _confirm_candidate(
        {"service": "checkout", "signal": "cpu", "z": 5.0, "timestamp": "t0",
         "streak": 2, "recent_count": None},
        result,
    )
    assert candidate["tier"] == 1
    assert candidate["reason"] == "insufficient_data"


# ---------------------------------------------------------------------
# 2. Earliest-break test
# ---------------------------------------------------------------------
def test_earliest_significant_break_wins_not_lowest_pvalue():
    fake_result = {
        "columns": [{"name": "bucket"}, {"name": "type"}, {"name": "pvalue"}],
        "values": [
            [90, "dip", 0.0009],   # lower pvalue, but later -- must NOT win
            [40, "spike", 0.004],  # earlier -- must win
        ],
    }
    with patch.object(change_point, "_esql", return_value=fake_result):
        result = change_point.change_point("ad", "p95_latency")

    assert result["timestamp"] == "40"
    assert result["pvalue"] == 0.004
    assert result["type"] == "spike"
    assert len(result["breaks"]) == 2
    assert [b["timestamp"] for b in result["breaks"]] == ["40", "90"]


# ---------------------------------------------------------------------
# 3. Cascade-batching test
# ---------------------------------------------------------------------
def test_cascade_of_six_same_signal_candidates_issues_one_query():
    candidates = [
        {"service": f"svc{i}", "signal": "p95_latency", "z": 5.0, "timestamp": "t0",
         "streak": 2, "recent_count": None}
        for i in range(6)
    ]
    fake_result = {
        "columns": [{"name": "bucket"}, {"name": "type"}, {"name": "pvalue"}, {"name": "service.name"}],
        "values": [],
    }
    with patch.object(detector, "zscore_scan", return_value=candidates), \
         patch.object(change_point, "_esql", return_value=fake_result) as mock_esql:
        detector.detector_scan({})

    assert mock_esql.call_count == 1


if __name__ == "__main__":
    import sys
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
