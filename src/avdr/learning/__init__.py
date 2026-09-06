"""Prospective subset-probability estimation -- CONTROLLED LOCAL QUALIFICATION.

Pipeline: controlled stochastic environment -> fully observed trials ->
subset targets -> pre-request features -> estimators -> freeze -> prospective
holdout -> optimizer integration.

Nothing in this package measures real DID infrastructure. Every distribution
is injected by us, every provider is a local mock on one shared host, and
every result must be labelled CONTROLLED LOCAL QUALIFICATION.
"""
