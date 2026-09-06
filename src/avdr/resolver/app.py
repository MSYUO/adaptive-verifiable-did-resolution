"""Mock DID resolver service.

Exposes a Universal-Resolver-shaped read path plus an admin surface that lets
an experiment driver switch injected behaviour at runtime without recreating
the container. The admin surface is deliberately resolver-side: the router
holds no scenario knowledge.

The documents served are synthetic and deterministic. They are NOT resolved
from any real DID registry, ledger or network.
"""

from __future__ import annotations

import asyncio
import threading

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from ..acceptance import DID_CONTEXT_V1, parse_did_method
from .settings import ResolverBehavior, ResolverSettings, load_resolver_settings


def build_did_document(did: str, resolver_id: str) -> dict:
    """Deterministic synthetic DID document that passes structural checks."""
    return {
        "@context": [DID_CONTEXT_V1],
        "id": did,
        "verificationMethod": [
            {
                "id": f"{did}#key-1",
                "type": "Ed25519VerificationKey2018",
                "controller": did,
                # Deterministic placeholder. NOT a real key and NOT verified.
                "publicKeyMultibase": f"z-mock-{resolver_id}",
            }
        ],
        "authentication": [f"{did}#key-1"],
    }


def build_invalid_did_document(did: str) -> dict:
    """Structurally unacceptable document (Scenario I). CONTROLLED INJECTION.

    Violates two acceptance rules at once: the id does not match the request
    and verificationMethod is absent.
    """
    return {
        "@context": [DID_CONTEXT_V1],
        "id": "did:example:wrong-subject",
    }


class RequestCounter:
    """Thread-safe monotonic counter backing the deterministic failure mode."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0

    def next(self) -> int:
        with self._lock:
            self._value += 1
            return self._value

    @property
    def value(self) -> int:
        with self._lock:
            return self._value

    def reset(self) -> None:
        with self._lock:
            self._value = 0


def create_app(settings: ResolverSettings | None = None) -> FastAPI:
    settings = settings or load_resolver_settings()
    state = {"behavior": settings.behavior}
    counter = RequestCounter()

    app = FastAPI(title=f"AVDR mock resolver {settings.resolver_id}")
    app.state.resolver_id = settings.resolver_id
    app.state.counter = counter

    def behavior() -> ResolverBehavior:
        return state["behavior"]

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "resolver_id": settings.resolver_id,
            "served_requests": counter.value,
            "behavior": behavior().model_dump(),
        }

    @app.get("/admin/behavior")
    async def get_behavior() -> dict:
        return {
            "resolver_id": settings.resolver_id,
            "behavior": behavior().model_dump(),
            "served_requests": counter.value,
        }

    @app.post("/admin/behavior")
    async def set_behavior(update: ResolverBehavior) -> dict:
        state["behavior"] = update
        return {
            "resolver_id": settings.resolver_id,
            "behavior": update.model_dump(),
        }

    @app.post("/admin/reset")
    async def reset() -> dict:
        state["behavior"] = ResolverBehavior()
        counter.reset()
        return {
            "resolver_id": settings.resolver_id,
            "behavior": state["behavior"].model_dump(),
            "served_requests": 0,
        }

    @app.get("/1.0/identifiers/{did}")
    async def resolve(did: str, response: Response):
        current = behavior()
        sequence_number = counter.next()

        if parse_did_method(did) is None:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalidDid",
                    "detail": f"malformed DID: {did!r}",
                    "resolver_id": settings.resolver_id,
                },
            )

        # Injected timeout: hold the connection open past any router deadline.
        if current.force_timeout:
            await asyncio.sleep(current.timeout_sleep_ms / 1000.0)

        if current.artificial_delay_ms:
            await asyncio.sleep(current.artificial_delay_ms / 1000.0)

        deterministic_failure = (
            current.deterministic_failure_every_n > 0
            and sequence_number % current.deterministic_failure_every_n == 0
        )

        if current.force_error or deterministic_failure:
            return JSONResponse(
                status_code=current.force_error_status,
                content={
                    "error": "resolverUnavailable",
                    "detail": "CONTROLLED INJECTION: forced resolver error",
                    "resolver_id": settings.resolver_id,
                    "sequence_number": sequence_number,
                },
            )

        document = (
            build_invalid_did_document(did)
            if current.force_invalid
            else build_did_document(did, settings.resolver_id)
        )

        return {
            "didDocument": document,
            "didResolutionMetadata": {
                "contentType": "application/did+ld+json",
                "resolver_id": settings.resolver_id,
                "sequence_number": sequence_number,
                # Truthful marker: this response is synthetic, not resolved.
                "synthetic": True,
                "injected_invalid": current.force_invalid,
                "injected_delay_ms": current.artificial_delay_ms,
            },
            "didDocumentMetadata": {},
        }

    return app


app = create_app()
