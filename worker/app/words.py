"""Memorable random capture names.

Generated server-side at ``POST /api/captures`` time when the
caller doesn't provide a ``name`` (or provides an empty string).

Pattern: ``<adjective> <color> <noun>`` — e.g. "swift azure otter".
Three lightly-curated word lists, each in the dozens:

    64 adjectives × 32 colors × 64 nouns ≈ 131 000 combinations

which is more than enough collision-tolerance for a single-user
studio (the rare collision is fine — the URL is the capture id,
not the name, and the user can rename via PATCH at any point).

The lists are intentionally narrow — animals + landscape nouns,
warm-leaning adjectives, broadly-recognizable colors. They avoid
anything ambiguous, harsh, or culturally loaded; the goal is a
friendly identifier the user smiles at, not an exhaustive
namespace.
"""
from __future__ import annotations

import random
from typing import Final


_ADJECTIVES: Final[tuple[str, ...]] = (
    "swift", "brave", "calm", "eager", "fancy", "gentle", "happy", "jolly",
    "kind", "lucky", "merry", "nimble", "plucky", "quiet", "tiny", "wise",
    "bright", "clever", "daring", "fierce", "glad", "humble", "lively", "mighty",
    "noble", "patient", "regal", "sturdy", "vivid", "witty", "agile", "bold",
    "charming", "dapper", "fluffy", "graceful", "hardy", "jovial", "keen", "lithe",
    "mellow", "neat", "peppy", "quirky", "radiant", "sleek", "tender", "upbeat",
    "vibrant", "warm", "zesty", "brisk", "cosy", "dashing", "fearless", "gifted",
    "hopeful", "jaunty", "loyal", "modest", "peaceful", "quaint", "rugged", "savvy",
)

_COLORS: Final[tuple[str, ...]] = (
    "azure", "crimson", "golden", "scarlet", "emerald", "ivory", "jade", "sapphire",
    "amber", "ruby", "silver", "violet", "indigo", "copper", "coral", "lavender",
    "magenta", "periwinkle", "plum", "rose", "salmon", "sienna", "teal", "turquoise",
    "mustard", "cobalt", "charcoal", "lime", "marigold", "frost", "tawny", "fuchsia",
)

_NOUNS: Final[tuple[str, ...]] = (
    "otter", "fox", "owl", "panda", "badger", "beaver", "raccoon", "hedgehog",
    "mole", "rabbit", "squirrel", "turtle", "koala", "lemur", "wombat", "sloth",
    "capybara", "fennec", "ocelot", "marmot", "gecko", "finch", "robin", "sparrow",
    "swallow", "heron", "kestrel", "eagle", "hawk", "falcon", "crane", "swan",
    "dolphin", "whale", "seal", "narwhal", "octopus", "axolotl", "frog", "toad",
    "lizard", "salamander", "butterfly", "dragonfly", "firefly", "bee", "ladybug", "moth",
    "river", "mountain", "valley", "forest", "meadow", "comet", "planet", "lake",
    "brook", "peak", "ridge", "dune", "cliff", "oak", "willow", "juniper",
)


# Sanity check: enforce the documented widths so an accidental
# duplicate or trim doesn't silently shrink the namespace.
assert len(_ADJECTIVES) == 64, f"expected 64 adjectives, got {len(_ADJECTIVES)}"
assert len(_COLORS) == 32, f"expected 32 colors, got {len(_COLORS)}"
assert len(_NOUNS) == 64, f"expected 64 nouns, got {len(_NOUNS)}"


def random_name(rng: random.Random | None = None) -> str:
    """Pick a memorable three-word name.

    Pass an explicit [random.Random] for deterministic tests; the
    default uses the module-level RNG (seeded from os.urandom on
    import, like the stdlib `random.choice`).
    """
    r = rng or random
    return f"{r.choice(_ADJECTIVES)} {r.choice(_COLORS)} {r.choice(_NOUNS)}"
