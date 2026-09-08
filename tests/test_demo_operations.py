"""Static operational contract for the presenter launcher and runbook."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_presenter_scripts_define_safe_startup_and_pid_scoped_shutdown():
    start = (REPO_ROOT / "scripts" / "start_demo.ps1").read_text(encoding="utf-8")
    stop = (REPO_ROOT / "scripts" / "stop_demo.ps1").read_text(encoding="utf-8")

    for port in ("8080", "8001", "8002", "8003"):
        assert port in start
    for module in ("avdr.resolver.app:app", "avdr.real_router.app:app"):
        assert module in start
    for endpoint in ("/health", "/dashboard/", "/demo/scenarios"):
        assert endpoint in start
    assert "Get-NetTCPConnection" in start
    assert "Refusing to stop or reuse it" in start
    assert "session.json" in start

    assert "Get-Process -Id $entry.pid" in stop
    assert "PID identity did not match" in stop
    assert "RESIDUAL_SERVICE_LISTENERS=" in stop
    banned = ("taskkill", "/IM python.exe", "Get-Process python")
    assert not any(token.lower() in stop.lower() for token in banned)


def test_runbook_contains_complete_presenter_flow_and_honest_claims():
    runbook = (
        REPO_ROOT / "docs" / "HACKATHON_DEMO_RUNBOOK.md"
    ).read_text(encoding="utf-8")
    required = (
        "Before presentation",
        "Start service",
        "Open dashboard",
        "Explain the real resolution area",
        "Scenario 1 — Normal",
        "Scenario 2 — Slow / Failure",
        "Scenario 3 — Fast but Unacceptable",
        "Recovery if something fails",
        "Stop service",
        "CONTROLLED DEMO",
        "w3c-basic-v1",
        "RESIDUAL_SERVICE_LISTENERS=0",
    )
    assert all(text in runbook for text in required)
    forbidden = (
        "blockchain verified",
        "cryptographically verified",
        "Byzantine tolerance",
        "3f+1 correctness",
    )
    assert not any(claim.lower() in runbook.lower() for claim in forbidden)


def test_launcher_state_is_ignored():
    ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".avdr-demo/" in ignore


def test_dependency_free_browser_qa_script_has_expected_scenarios():
    script = (
        REPO_ROOT / "scripts" / "browser_demo_qa.mjs"
    ).read_text(encoding="utf-8")
    assert "Page.captureScreenshot" in script
    assert "Emulation.setDeviceMetricsOverride" in script
    for scenario in ("normal", "slow_failure", "fast_unacceptable"):
        assert scenario in script
    assert "node:" in script
    assert "playwright" not in script.lower()
