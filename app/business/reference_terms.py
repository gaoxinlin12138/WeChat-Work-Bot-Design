"""Shared lexical markers for product-reference continuation.

These words are not product attribute values.  They are resolved against a
trusted conversation focus by the canonical request boundary; deterministic
field extraction must never turn them into filters such as ``material=他``.
"""

REFERENCE_PRONOUNS: tuple[str, ...] = (
    "该产品",
    "这个产品",
    "那个产品",
    "这款产品",
    "那款产品",
    "这个",
    "那个",
    "这款",
    "那款",
    "上面",
    "它",
    "他",
    "这",
    "那",
)

REFERENCE_PRONOUN_SET = frozenset(REFERENCE_PRONOUNS)
