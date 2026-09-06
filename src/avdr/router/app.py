"""Adaptive router service -- baseline milestone.

Executes a policy's plan strictly sequentially, records attempt-level and
logical-request-level telemetry, and returns the first ACCEPTED response.

Explicitly out of scope for this milestone: prediction, adaptive fan-out
sizing, parallel racing, blockchain logging. The router also holds no
scenario/fault knowledge -- injected behaviour belongs to the resolvers.
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from ..acceptance import ACCEPTANCE_RULES, ACCEPTANCE_RULESET_VERSION, parse_did_method
from ..config import RouterConfig, load_router_config
from ..models import LogicalRequestRecord, ResolverAttempt
from ..telemetry import TelemetrySink
from .policies import build_policies
from .transport import dispatch_attempt


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_app(
    config: RouterConfig | None = None,
    sink: TelemetrySink | None = None,
) -> FastAPI:
    config = config or load_router_config()
    sink = sink or TelemetrySink(config.telemetry_dir)
    policies = build_policies(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # One client for the lifetime of the router. The per-attempt timeout
        # is passed explicitly at call time; no library default is relied on.
        async with httpx.AsyncClient() as client:
            app.state.client = client
            yield

    app = FastAPI(title="AVDR adaptive router", lifespan=lifespan)
    app.state.config = config
    app.state.sink = sink
    app.state.policies = policies

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "policy_version": config.policy_version,
            "default_policy": config.default_policy,
            "attempt_timeout_ms": config.attempt_timeout_ms,
            "resolvers": [r.model_dump() for r in config.resolvers],
            "telemetry": sink.counts(),
        }

    @app.get("/policies")
    async def list_policies() -> dict:
        return {
            "available": sorted(policies),
            "default": config.default_policy,
            "single_static_target": config.single_static_target,
            "acceptance_ruleset": {
                "version": ACCEPTANCE_RULESET_VERSION,
                "rules": list(ACCEPTANCE_RULES),
            },
        }

    @app.post("/admin/reset-policies")
    async def reset_policies() -> dict:
        """Reset stateful policy counters (round-robin rotation)."""
        for policy in policies.values():
            policy.reset()
        return {"status": "reset", "policies": sorted(policies)}

    @app.get("/telemetry/requests/{request_id}")
    async def get_request(request_id: str):
        record = sink.get_request(request_id)
        if record is None:
            return JSONResponse(status_code=404, content={"error": "unknown request_id"})
        return record

    @app.get("/telemetry/recent")
    async def recent(limit: int = Query(default=20, ge=1, le=200)):
        return {"records": sink.recent_requests(limit)}

    @app.get("/1.0/identifiers/{did}")
    async def resolve(did: str, policy: str | None = Query(default=None)):
        policy_name = policy or config.default_policy
        if policy_name not in policies:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "unknownPolicy",
                    "detail": f"policy {policy_name!r} is not available",
                    "available": sorted(policies),
                },
            )

        request_id = str(uuid.uuid4())
        timestamp = _utc_now_iso()
        plan = policies[policy_name].plan(did)

        attempts: list[ResolverAttempt] = []
        accepted_payload: dict | None = None
        returned_resolver: str | None = None

        logical_started = time.perf_counter()
        for attempt_index, resolver_id in enumerate(plan.attempt_order()):
            endpoint = config.endpoint(resolver_id)
            result = await dispatch_attempt(
                client=app.state.client,
                endpoint=endpoint,
                did=did,
                request_id=request_id,
                attempt_index=attempt_index,
                timeout_ms=config.attempt_timeout_ms,
            )
            sink.record_attempt(result.attempt)
            attempts.append(result.attempt)
            if result.attempt.accepted:
                accepted_payload = result.payload
                returned_resolver = resolver_id
                break
        logical_latency_ms = round((time.perf_counter() - logical_started) * 1000.0, 3)

        attempted_sequence = [a.resolver_id for a in attempts]
        success = accepted_payload is not None
        final_error = None
        if not success:
            outcomes = [a.outcome.value for a in attempts]
            final_error = (
                f"no resolver returned an acceptable response; outcomes={outcomes}"
            )

        record = LogicalRequestRecord(
            request_id=request_id,
            timestamp=timestamp,
            did=did,
            did_method=parse_did_method(did),
            routing_policy=policy_name,
            policy_version=config.policy_version,
            policy_target=plan.target,
            candidate_sequence=list(plan.candidate_sequence),
            attempted_sequence=attempted_sequence,
            returned_resolver=returned_resolver,
            logical_completion_latency_ms=logical_latency_ms,
            success=success,
            final_error=final_error,
            attempt_count=len(attempts),
            fanout_count=len(set(attempted_sequence)),
            canceled_count=sum(1 for a in attempts if a.canceled),
            attempt_timeout_ms=config.attempt_timeout_ms,
        )
        sink.record_request(record)

        if not success:
            return JSONResponse(
                status_code=502,
                content={
                    "request_id": request_id,
                    "did": did,
                    "routing_policy": policy_name,
                    "error": "noAcceptableResponse",
                    "detail": final_error,
                    "attempted_sequence": attempted_sequence,
                    "attempt_count": len(attempts),
                    "logical_completion_latency_ms": logical_latency_ms,
                    "attempt_outcomes": [
                        {
                            "attempt_index": a.attempt_index,
                            "resolver_id": a.resolver_id,
                            "outcome": a.outcome.value,
                            "http_status": a.http_status,
                            "timeout": a.timeout,
                            "document_valid": a.document_valid,
                            "acceptance_reason": a.acceptance_reason,
                            "latency_ms": a.latency_ms,
                            "error": a.error,
                        }
                        for a in attempts
                    ],
                },
            )

        return {
            "request_id": request_id,
            "did": did,
            "routing_policy": policy_name,
            "returned_resolver": returned_resolver,
            "attempted_sequence": attempted_sequence,
            "attempt_count": len(attempts),
            "logical_completion_latency_ms": logical_latency_ms,
            "didDocument": accepted_payload.get("didDocument"),
            "didResolutionMetadata": accepted_payload.get("didResolutionMetadata", {}),
        }

    return app


app = create_app()
