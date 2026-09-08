"""Offline safety checks for the opt-in P9 real-DID qualification script."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "qualify_real_dids.py"


def _module():
    spec = importlib.util.spec_from_file_location("qualify_real_dids", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_fixed_cases_are_public_external_and_bounded():
    module = _module()
    cases = module.qualification_cases()

    assert [case.method for case in cases] == ["key", "web", "ethr"]
    assert len(cases) == module.MAX_PUBLIC_REQUESTS == 3
    assert {case.provider_id for case in cases} == {"uniresolver-dif-dev"}
    assert {case.provider_adapter for case in cases} == {"universal-resolver-v1"}
    assert all(not case.did.startswith("did:example:") for case in cases)
    assert all(case.provider_endpoint.startswith("https://") for case in cases)


def test_cli_refuses_public_traffic_without_explicit_opt_in():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 2
    assert "LIVE QUALIFICATION DISABLED" in result.stdout
    assert "--execute-live" in result.stdout


def test_failure_classification_preserves_null_metadata_and_rate_limits():
    module = _module()

    null_metadata = {
        "success": False,
        "result": {"didResolutionMetadata": None},
        "attempts": [
            {
                "dispatched": True,
                "transport_outcome": "http_response",
                "http_status": 200,
            }
        ],
    }
    rate_limited = {
        "success": False,
        "result": {"didResolutionMetadata": {"error": "tooManyRequests"}},
        "attempts": [
            {
                "dispatched": True,
                "transport_outcome": "http_response",
                "http_status": 429,
            }
        ],
    }

    assert module._failure_classification(null_metadata) == "acceptance_failure"
    assert module._failure_classification(rate_limited) == "rate_limited"
