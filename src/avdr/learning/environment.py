"""Controlled stochastic environment for the local mock resolvers.

Everything here is [CONTROLLED INJECTION]. The distributions below were chosen
by us to make the estimation pipeline observable; they are NOT measurements of
any DID resolver and no distributional claim about real infrastructure follows
from them.

Two properties matter for this milestone.

1. STOCHASTIC, NOT A FIXED +200 ms SCENARIO. Delays and failures are sampled
   per trial from seeded RNGs, so the estimator faces genuine uncertainty
   rather than a lookup table.

2. CORRELATED DEGRADATION EXISTS. `SHARED_DEGRADATION` slows every provider at
   once. Under it, adding providers does NOT multiply independent failure
   probabilities together -- which is exactly why the pipeline must never
   assume q(S) = 1 - prod(1 - p_i).

The hidden episode state is recorded for audit and ground-truth analysis, and
is deliberately NOT available to the estimator as a feature.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Iterator

from ..provenance import config_hash

PROVIDERS = ("local-a", "local-b", "local-c")

# ---- state names -----------------------------------------------------------
NORMAL = "NORMAL"
PROVIDER_A_CONGESTED = "PROVIDER_A_CONGESTED"
PROVIDER_B_UNSTABLE = "PROVIDER_B_UNSTABLE"
INVALID_RESPONSE_RISK = "INVALID_RESPONSE_RISK"
SHARED_DEGRADATION = "SHARED_DEGRADATION"

STATES = (
    NORMAL,
    PROVIDER_A_CONGESTED,
    PROVIDER_B_UNSTABLE,
    INVALID_RESPONSE_RISK,
    SHARED_DEGRADATION,
)


@dataclass(frozen=True)
class ProviderInjection:
    """Per-provider sampling parameters. [CONTROLLED INJECTION]."""

    delay_lo_ms: int = 5
    delay_hi_ms: int = 40
    error_probability: float = 0.0
    invalid_probability: float = 0.0

    def sample(self, rng: random.Random) -> dict:
        return {
            "artificial_delay_ms": rng.randint(self.delay_lo_ms, self.delay_hi_ms),
            "force_error": rng.random() < self.error_probability,
            "force_invalid": rng.random() < self.invalid_probability,
        }


HEALTHY = ProviderInjection()

# [CONTROLLED INJECTION] state -> per-provider parameters.
STATE_TABLE: dict[str, dict[str, ProviderInjection]] = {
    NORMAL: {p: HEALTHY for p in PROVIDERS},
    PROVIDER_A_CONGESTED: {
        "local-a": ProviderInjection(delay_lo_ms=260, delay_hi_ms=620),
        "local-b": HEALTHY,
        "local-c": HEALTHY,
    },
    PROVIDER_B_UNSTABLE: {
        "local-a": HEALTHY,
        "local-b": ProviderInjection(
            delay_lo_ms=5, delay_hi_ms=60, error_probability=0.45
        ),
        "local-c": HEALTHY,
    },
    INVALID_RESPONSE_RISK: {
        # Fast but frequently unacceptable: the fastest response is often the
        # wrong one, which is precisely what acceptance must catch.
        "local-a": ProviderInjection(
            delay_lo_ms=5, delay_hi_ms=25, invalid_probability=0.55
        ),
        "local-b": HEALTHY,
        "local-c": HEALTHY,
    },
    SHARED_DEGRADATION: {
        # Correlated: every provider is slow at the same time. Redundancy does
        # not help here, and an independence model would badly overestimate
        # the benefit of adding providers.
        p: ProviderInjection(delay_lo_ms=210, delay_hi_ms=520) for p in PROVIDERS
    },
}


@dataclass
class EpisodePlan:
    """A sequence of hidden states for one episode."""

    episode_id: str
    seed: int
    split: str
    trial_count: int
    states: list[str] = field(default_factory=list)

    def state_at(self, trial_index: int) -> str:
        return self.states[trial_index]

    def manifest(self) -> dict:
        return {
            "episode_id": self.episode_id,
            "seed": self.seed,
            "split": self.split,
            "trial_count": self.trial_count,
            # Recorded for audit/ground truth ONLY. Never a model feature.
            "states": list(self.states),
        }


def build_episode_plan(
    episode_id: str, seed: int, split: str, trial_count: int
) -> EpisodePlan:
    """Build a piecewise-constant state sequence with seeded transitions.

    Episodes are sequential rather than an IID shuffled bag: a state persists
    for a run of trials, so recent history is genuinely informative and the
    pre-request features have something to learn from.
    """
    rng = random.Random(f"plan:{seed}:{episode_id}")
    states: list[str] = []
    while len(states) < trial_count:
        state = rng.choice(STATES)
        # Runs of 4-9 trials keep the state observable in a 10-trial window.
        run = rng.randint(4, 9)
        states.extend([state] * run)
    return EpisodePlan(
        episode_id=episode_id,
        seed=seed,
        split=split,
        trial_count=trial_count,
        states=states[:trial_count],
    )


def plan_episodes(
    split: str, count: int, base_seed: int, trial_count: int
) -> list[EpisodePlan]:
    """Episodes for one split. Seeds are disjoint across splits by construction."""
    return [
        build_episode_plan(f"{split}-{i:03d}", base_seed + i, split, trial_count)
        for i in range(count)
    ]


def sample_behaviors(state: str, rng: random.Random) -> dict[str, dict]:
    """Sample one trial's injected behaviour for every provider."""
    table = STATE_TABLE[state]
    return {provider: table[provider].sample(rng) for provider in PROVIDERS}


def injection_config_payload() -> dict:
    """Canonical description of the injected environment, for hashing."""
    return {
        "providers": list(PROVIDERS),
        "states": {
            state: {
                provider: {
                    "delay_lo_ms": inj.delay_lo_ms,
                    "delay_hi_ms": inj.delay_hi_ms,
                    "error_probability": inj.error_probability,
                    "invalid_probability": inj.invalid_probability,
                }
                for provider, inj in table.items()
            }
            for state, table in STATE_TABLE.items()
        },
        "note": (
            "CONTROLLED INJECTION. Chosen distributions, not measurements. "
            "SHARED_DEGRADATION deliberately correlates all providers."
        ),
    }


def injection_config_hash() -> str:
    return config_hash(injection_config_payload())


def iter_trials(plan: EpisodePlan) -> Iterator[tuple[int, str, random.Random]]:
    """Yield (trial_index, hidden_state, rng) for each trial in an episode.

    A per-trial RNG derived from the episode seed keeps every trial
    independently reproducible.
    """
    for trial_index in range(plan.trial_count):
        rng = random.Random(f"trial:{plan.seed}:{plan.episode_id}:{trial_index}")
        yield trial_index, plan.state_at(trial_index), rng
