"""Render the ACK 2026 figures from the package's own source CSVs.

Every plotted value is read from figures/source/*.csv, which
recompute_ack_results.py derives from the raw V4 evidence. No numeric constant
is embedded here.

Palette (validated with the dataviz skill's validator, light surface #fcfcfb):

  Fig 2 - categorical, 2 hues, all-pairs mode: ALL CHECKS PASS
      adaptive          #2a78d6 (blue,   slot 1)
      BEST_FIXED_K2     #eb6834 (orange, slot 2)
      other baselines   #52514e - deliberately achromatic CHROME, not a series:
                        they are a reference class, and every point is directly
                        labelled so identity is never carried by colour alone.

  Fig 3 - SEQUENTIAL ramp over an ordinal quantity (fan-out k = 1 < 2 < 3),
      one hue light->dark: #86b6ef -> #2a78d6 -> #104281.
      Lightness is monotone (L 0.727 -> 0.560 -> 0.385) and adjacent CVD
      separation is dE 19.0 (deutan) / 16.7 (tritan). The lightest step sits
      below 3:1 on the light surface, so the RELIEF RULE applies: every bar
      carries a visible direct value label.

Usage:
    python artifacts/ack2026/analysis/make_figures.py
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

PACKAGE = Path(__file__).resolve().parent.parent
SRC = PACKAGE / "figures" / "source"
OUT = PACKAGE / "figures"

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e1e0d9"
ADAPTIVE = "#2a78d6"
FROZEN = "#eb6834"
NEUTRAL = "#52514e"
SEQ = ["#86b6ef", "#2a78d6", "#104281"]

plt.rcParams.update(
    {
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "font.size": 9,
        "axes.labelsize": 9,
        "axes.titlesize": 10,
        "axes.edgecolor": INK_2,
        "axes.linewidth": 0.8,
        "xtick.color": INK_2,
        "ytick.color": INK_2,
        "text.color": INK,
        "axes.labelcolor": INK,
        "legend.frameon": False,
        "pdf.fonttype": 42,   # embed TrueType so the PDF is editable/portable
        "ps.fonttype": 42,
    }
)


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def short(policy: str) -> str:
    """Compact policy label for plotting."""
    return (
        policy.replace("local-", "")
        .replace("fixed{", "{")
        .replace("BEST_FIXED_K2{", "{")
        .replace("all-race{a,b,c}", "all-race {a,b,c}")
        .replace("adaptive-best-effort", "adaptive")
    )


# ---------------------------------------------------------------------------
# Figure 1 - architecture
# ---------------------------------------------------------------------------


def figure1() -> None:
    fig, ax = plt.subplots(figsize=(5.0, 6.6))
    ax.set_xlim(0, 10)
    ax.set_ylim(-1.5, 15.2)
    ax.axis("off")

    stages = [
        ("DID Resolution Request", None, "#ffffff"),
        ("Capability-aware\ncandidate filtering", "eligibility", "#ffffff"),
        ("Pre-request history\nestimator   q̂(S | x)", "ESTIMATOR", "#eaf2fd"),
        ("Adaptive minimum-set\noptimizer   S* = argmin C(S)", "OPTIMIZER", "#eaf2fd"),
        ("Selected resolver subset  S*", None, "#ffffff"),
        ("Concurrent\nfirst-acceptable execution", "EXECUTOR", "#eaf2fd"),
        ("W3C DID Resolution-oriented\nacceptance / normalization", "acceptance", "#ffffff"),
        ("DID Resolution Result", None, "#ffffff"),
    ]

    height, gap = 1.32, 0.52
    y = 14.4
    centres = []
    for label, tag, fill in stages:
        box = FancyBboxPatch(
            (1.15, y - height), 7.7, height,
            boxstyle="round,pad=0.06,rounding_size=0.12",
            linewidth=1.0,
            edgecolor=ADAPTIVE if tag in ("ESTIMATOR", "OPTIMIZER", "EXECUTOR") else INK_2,
            facecolor=fill,
        )
        ax.add_patch(box)
        ax.text(5.0, y - height / 2, label, ha="center", va="center",
                fontsize=8.6, color=INK, linespacing=1.35)
        if tag in ("ESTIMATOR", "OPTIMIZER", "EXECUTOR"):
            ax.text(1.30, y - 0.20, tag, ha="left", va="top", fontsize=6.4,
                    color=ADAPTIVE, fontweight="bold")
        centres.append(y - height / 2)
        y -= height + gap

    for i in range(len(stages) - 1):
        ax.add_patch(
            FancyArrowPatch(
                (5.0, centres[i] - height / 2 - 0.03),
                (5.0, centres[i + 1] + height / 2 + 0.03),
                arrowstyle="-|>", mutation_scale=9,
                linewidth=0.9, color=INK_2, shrinkA=0, shrinkB=0,
            )
        )

    ax.text(
        5.0, -0.85,
        "Three separated layers. No consensus protocol and no 3f+1 rule;\n"
        "DID method semantics are unchanged by the router.",
        ha="center", va="center", fontsize=7.0, color=INK_2, linespacing=1.4,
    )
    fig.tight_layout()
    fig.savefig(OUT / "fig1_architecture.pdf", bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Figure 2 - success vs cost
# ---------------------------------------------------------------------------


def figure2() -> None:
    rows = read_csv(SRC / "fig2_success_vs_calls.csv")
    fig, ax = plt.subplots(figsize=(6.2, 4.1))

    ax.grid(True, color=GRID, linewidth=0.7, zorder=0)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)

    # Label placement only (never data): the three k=2 subsets sit at the same
    # x with y within 0.003 of each other, so their labels need explicit
    # offsets and leader lines or they collide.
    offsets = {
        "{a}": (9, -3, False), "{b}": (9, -3, False), "{c}": (9, -3, False),
        "{a,b}": (16, -16, True), "{a,c}": (16, 1, True),
        "all-race {a,b,c}": (-9, -14, False),
    }

    for row in rows:
        x = float(row["calls_per_request"])
        y = float(row["success_rate"])
        tag = row["highlight"]
        label = short(row["policy"])

        if tag == "adaptive":
            colour, size, z, edge = ADAPTIVE, 110, 6, "#ffffff"
        elif tag == "frozen_comparator":
            colour, size, z, edge = FROZEN, 110, 6, "#ffffff"
        else:
            colour, size, z, edge = NEUTRAL, 32, 3, SURFACE
        ax.scatter(x, y, s=size, color=colour, zorder=z,
                   edgecolors=edge, linewidths=1.2)

        leader = dict(arrowstyle="-", linewidth=0.6, color="#9a9992",
                      shrinkA=1, shrinkB=3)
        if tag == "adaptive":
            ax.annotate(f"adaptive best-effort\n{y:.3f} @ {x:.2f} calls", (x, y),
                        textcoords="offset points", xytext=(-14, 16),
                        ha="right", fontsize=8.2, color=ADAPTIVE,
                        fontweight="bold", linespacing=1.35,
                        arrowprops=leader)
        elif tag == "frozen_comparator":
            ax.annotate(f"BEST_FIXED_K2 {label}\n{y:.3f} @ {x:.2f} calls", (x, y),
                        textcoords="offset points", xytext=(30, 26),
                        ha="left", fontsize=8.2, color=FROZEN,
                        fontweight="bold", linespacing=1.35,
                        arrowprops=leader)
        else:
            dx, dy, use_leader = offsets.get(label, (9, -3, False))
            ax.annotate(label, (x, y), textcoords="offset points",
                        xytext=(dx, dy),
                        ha="right" if dx < 0 else "left",
                        fontsize=7.4, color=INK_2,
                        arrowprops=leader if use_leader else None)

    ax.set_xlabel("Mean resolver calls per logical request")
    ax.set_ylabel("Resolution success rate")
    ax.set_xlim(0.70, 3.35)
    ax.set_ylim(0.55, 0.95)

    handles = [
        Line2D([], [], marker="o", linestyle="", markersize=8,
               markerfacecolor=ADAPTIVE, markeredgecolor="#ffffff",
               label="adaptive best-effort (frozen policy)"),
        Line2D([], [], marker="o", linestyle="", markersize=8,
               markerfacecolor=FROZEN, markeredgecolor="#ffffff",
               label="BEST_FIXED_K2 {b,c} (frozen comparator)"),
        Line2D([], [], marker="o", linestyle="", markersize=6,
               markerfacecolor=NEUTRAL, markeredgecolor=SURFACE,
               label="static baseline subsets"),
    ]
    ax.legend(handles=handles, loc="lower right", fontsize=7.4,
              labelcolor=INK_2, handletextpad=0.5, borderpad=0.6)
    ax.set_title("V4 confirmatory: success vs cost (controlled local environment)",
                 loc="left", color=INK, pad=8)
    fig.tight_layout()
    fig.savefig(OUT / "fig2_success_vs_calls.pdf", bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Figure 3 - selected k by controlled injection state
# ---------------------------------------------------------------------------


def figure3() -> None:
    rows = read_csv(SRC / "fig3_selected_k_by_state.csv")
    states = [r["controlled_injection_state"] for r in rows]
    labels = [s.replace("SINGLE_PROVIDER_", "SINGLE_\n").replace("_DEGRADATION", "_DEGR.")
              .replace("PAIR_CORRELATED_DEGR.", "PAIR_CORRELATED\nDEGRADATION")
              .replace("SHARED_DEGR.", "SHARED\nDEGRADATION")
              for s in states]

    fig, ax = plt.subplots(figsize=(6.6, 3.7))
    ax.grid(True, axis="y", color=GRID, linewidth=0.7, zorder=0)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)

    width = 0.26
    positions = range(len(states))
    for index, key in enumerate(("k1", "k2", "k3")):
        values = [int(r[key]) for r in rows]
        offsets = [p + (index - 1) * (width + 0.02) for p in positions]
        ax.bar(offsets, values, width, color=SEQ[index], zorder=3,
               edgecolor=SURFACE, linewidth=1.2, label=f"k = {index + 1}")
        # RELIEF RULE: the lightest sequential step is below 3:1 on this
        # surface, so every bar carries a visible direct value label.
        for x, v in zip(offsets, values):
            ax.text(x, v + 4, str(v), ha="center", va="bottom",
                    fontsize=6.8, color=INK_2)

    ax.set_xticks(list(positions))
    ax.set_xticklabels(labels, fontsize=6.9, color=INK_2, linespacing=1.3)
    ax.set_ylabel("Trials")
    ax.set_ylim(0, max(int(r[k]) for r in rows for k in ("k1", "k2", "k3")) * 1.18)
    ax.legend(fontsize=7.6, labelcolor=INK_2, ncol=3, loc="upper right")
    ax.set_title(
        "Selected fan-out k by CONTROLLED INJECTION STATE  (mechanism diagnostic)",
        loc="left", color=INK, pad=8,
    )
    fig.text(
        0.012, -0.045,
        "Hidden states are injected by the controlled generator and are never "
        "estimator inputs.\nThis diagnoses the policy mechanism; it is not "
        "evidence of real-world state classification.",
        fontsize=6.6, color=INK_2, linespacing=1.4,
    )
    fig.tight_layout()
    fig.savefig(OUT / "fig3_selected_k_by_state.pdf", bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    figure1()
    figure2()
    figure3()
    for name in ("fig1_architecture.pdf", "fig2_success_vs_calls.pdf",
                 "fig3_selected_k_by_state.pdf"):
        path = OUT / name
        print(f"  {name:34s} {path.stat().st_size:>8d} bytes")
    print("figures rendered from package source CSVs (no embedded constants)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
