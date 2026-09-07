"""V3: provider-symmetric controlled generator + policy-faithful collection.

WHY V3 EXISTS
-------------
The V1/V2 generator is provably asymmetric. Provider-specific degraded states
per provider: local-a = 2, local-b = 1, local-c = 0. Structurally this makes
local-c never individually degraded and local-a degraded twice, so fixed
subsets containing c are favoured and subsets containing a are penalised by
provider IDENTITY rather than by anything a router could learn.

The reason for V3 is therefore "remove provider-identity asymmetry". It is NOT
"make adaptive win" -- and it very plausibly will not, since the audit shows
every pair already captures ~0.842 while all three capture ~0.859, leaving the
third provider worth only ~1.7 points.

HOW SYMMETRY IS GUARANTEED
--------------------------
States are defined over ROLES (role_0, role_1, role_2), never over provider
names. Each episode draws a seeded permutation of provider labels onto those
roles, so provider identity carries no information by construction. The
permutation is recorded for audit and is NEVER a feature.

All distributions are [CONTROLLED INJECTION], frozen before generation.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from itertools import permutations

from ..provenance import config_hash
from .environment import PROVIDERS, ProviderInjection

ROLES = ("role_0", "role_1", "role_2")

NORMAL = "NORMAL"
SINGLE_PROVIDER_CONGESTED = "SINGLE_PROVIDER_CONGESTED"
SINGLE_PROVIDER_UNSTABLE = "SINGLE_PROVIDER_UNSTABLE"
SINGLE_PROVIDER_INVALID_RISK = "SINGLE_PROVIDER_INVALID_RISK"
PAIR_CORRELATED_DEGRADATION = "PAIR_CORRELATED_DEGRADATION"
SHARED_DEGRADATION_V3 = "SHARED_DEGRADATION"

STATES_V3 = (
    NORMAL,
    SINGLE_PROVIDER_CONGESTED,
    SINGLE_PROVIDER_UNSTABLE,
    SINGLE_PROVIDER_INVALID_RISK,
    PAIR_CORRELATED_DEGRADATION,
    SHARED_DEGRADATION_V3,
)

HEALTHY_V3 = ProviderInjection()
CONGESTED = ProviderInjection(delay_lo_ms=260, delay_hi_ms=620)
UNSTABLE = ProviderInjection(delay_lo_ms=5, delay_hi_ms=60, error_probability=0.45)
INVALID_RISK = ProviderInjection(
    delay_lo_ms=5, delay_hi_ms=25, invalid_probability=0.55
)
CORRELATED_SLOW = ProviderInjection(delay_lo_ms=210, delay_hi_ms=520)

# ROLE table. role_0 is the primary affected role, role_1 the secondary.
# [CONTROLLED INJECTION], frozen before generation.
ROLE_TABLE: dict[str, dict[str, ProviderInjection]] = {
    NORMAL: {r: HEALTHY_V3 for r in ROLES},
    SINGLE_PROVIDER_CONGESTED: {
        "role_0": CONGESTED, "role_1": HEALTHY_V3, "role_2": HEALTHY_V3,
    },
    SINGLE_PROVIDER_UNSTABLE: {
        "role_0": UNSTABLE, "role_1": HEALTHY_V3, "role_2": HEALTHY_V3,
    },
    SINGLE_PROVIDER_INVALID_RISK: {
        "role_0": INVALID_RISK, "role_1": HEALTHY_V3, "role_2": HEALTHY_V3,
    },
    PAIR_CORRELATED_DEGRADATION: {
        "role_0": CORRELATED_SLOW, "role_1": CORRELATED_SLOW, "role_2": HEALTHY_V3,
    },
    SHARED_DEGRADATION_V3: {r: CORRELATED_SLOW for r in ROLES},
}

GENERATOR_VERSION_V3 = "symmetric-role-permuted-v3"


@dataclass
class EpisodePlanV3:
    episode_id: str
    seed: int
    split: str
    trial_count: int
    states: list[str] = field(default_factory=list)
    # role -> provider, drawn per episode. AUDIT ONLY, never a feature.
    permutation: dict[str, str] = field(default_factory=dict)

    def state_at(self, index: int) -> str:
        return self.states[index]

    def manifest(self) -> dict:
        return {
            "episode_id": self.episode_id,
            "seed": self.seed,
            "split": self.split,
            "trial_count": self.trial_count,
            "states": list(self.states),
            "role_permutation_audit_only": dict(self.permutation),
        }


def build_episode_plan_v3(
    episode_id: str, seed: int, split: str, trial_count: int
) -> EpisodePlanV3:
    rng = random.Random(f"v3plan:{seed}:{episode_id}")
    states: list[str] = []
    while len(states) < trial_count:
        state = rng.choice(STATES_V3)
        states.extend([state] * rng.randint(4, 9))
    ordering = rng.choice(list(permutations(sorted(PROVIDERS))))
    return EpisodePlanV3(
        episode_id=episode_id,
        seed=seed,
        split=split,
        trial_count=trial_count,
        states=states[:trial_count],
        permutation=dict(zip(ROLES, ordering)),
    )


def plan_episodes_v3(
    split: str, count: int, base_seed: int, trial_count: int
) -> list[EpisodePlanV3]:
    return [
        build_episode_plan_v3(f"{split}-{i:03d}", base_seed + i, split, trial_count)
        for i in range(count)
    ]


def sample_behaviors_v3(
    state: str, permutation: dict[str, str], rng: random.Random
) -> dict[str, dict]:
    """Sample one trial's injected behaviour, mapped through the permutation."""
    table = ROLE_TABLE[state]
    return {permutation[role]: table[role].sample(rng) for role in ROLES}


def injection_config_payload_v3() -> dict:
    return {
        "generator_version": GENERATOR_VERSION_V3,
        "roles": list(ROLES),
        "states": {
            state: {
                role: {
                    "delay_lo_ms": inj.delay_lo_ms,
                    "delay_hi_ms": inj.delay_hi_ms,
                    "error_probability": inj.error_probability,
                    "invalid_probability": inj.invalid_probability,
                }
                for role, inj in table.items()
            }
            for state, table in ROLE_TABLE.items()
        },
        "symmetry": (
            "states are defined over roles; each episode draws a seeded "
            "permutation of provider labels onto roles, so provider identity "
            "is exchangeable by construction"
        ),
    }


def injection_config_hash_v3() -> str:
    return config_hash(injection_config_payload_v3())


def iter_trials_v3(plan: EpisodePlanV3):
    for index in range(plan.trial_count):
        rng = random.Random(f"v3trial:{plan.seed}:{plan.episode_id}:{index}")
        yield index, plan.state_at(index), rng
