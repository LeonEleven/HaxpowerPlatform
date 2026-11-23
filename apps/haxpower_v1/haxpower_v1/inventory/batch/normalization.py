"""Versioned B03 candidate. Case and width are significant in business text."""

from collections.abc import Mapping
from dataclasses import dataclass
from unicodedata import category, normalize

from .dto import BatchError, BatchKind, BatchSelection, Normalization, NormalizationStatus

CANDIDATE_VERSION = "b03-candidate-1"
MAX_NUMBER_LENGTH = 80
MAX_ORIGINAL_LENGTH = 256
NO_BATCH_KEY = "-"
QUOTES = frozenset("'\"`‘’“”＇＂「」『』")


@dataclass(frozen=True)
class NormalizationPolicy:
    version: str
    max_length: int


POLICIES = {CANDIDATE_VERSION: NormalizationPolicy(CANDIDATE_VERSION, MAX_NUMBER_LENGTH)}


def reserved_name(value):
    if not isinstance(value, str):
        return False
    # Compatibility folding is used only to reserve the system namespace.
    return normalize("NFKC", value).strip().casefold().startswith(("hp-nb-", "hp-rb-"))


def normalize_selection(selection: object, *, policy_version: str = CANDIDATE_VERSION) -> BatchSelection:
    policy = POLICIES.get(policy_version)
    if policy is None:
        raise BatchError("BATCH_POLICY_UNAVAILABLE")
    if not isinstance(selection, Mapping) or "mode" not in selection:
        raise BatchError("BATCH_SELECTION_REQUIRED")
    if selection["mode"] == BatchKind.NO_BATCH and set(selection) == {"mode"}:
        return BatchSelection(BatchKind.NO_BATCH, None)
    if selection["mode"] != BatchKind.BATCH or set(selection) != {"mode", "batchNo"}:
        raise BatchError("INVALID_BATCH_SELECTION")

    original = selection["batchNo"]
    if not isinstance(original, str):
        raise BatchError("INVALID_BATCH_TEXT")
    if len(original) > MAX_ORIGINAL_LENGTH:
        raise BatchError("BATCH_TEXT_TOO_LONG")
    if any((not char.isprintable() and category(char) != "Zs") or char in "<>" for char in original):
        raise BatchError("INVALID_BATCH_TEXT")
    candidate = original.strip()
    if not candidate:
        raise BatchError("BATCH_TEXT_EMPTY")
    if all(char in QUOTES or char.isspace() for char in candidate):
        raise BatchError("BATCH_QUOTES_REQUIRE_REVIEW")
    if reserved_name(candidate):
        raise BatchError("BATCH_TEXT_RESERVED")
    if len(candidate) > policy.max_length:
        raise BatchError("BATCH_TEXT_TOO_LONG")
    status = NormalizationStatus.EXACT if original == candidate else NormalizationStatus.CONFIRMATION_REQUIRED
    return BatchSelection(BatchKind.BATCH, Normalization(original, candidate, policy.version, status))


def selection_key(selection: BatchSelection) -> str:
    if selection.normalization is None:
        return NO_BATCH_KEY
    # Fixed-width ASCII hex encodes exact Unicode code points without losing
    # case, width or spaces under MariaDB's default text collation.
    return "".join(f"{ord(char):06x}" for char in selection.normalization.candidate)
