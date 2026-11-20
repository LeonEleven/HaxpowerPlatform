"""Proposed batch semantics used by the Stage 2B API and legacy preview."""

QUOTES = frozenset("'\"`‘’“”＇＂「」『』")
RESERVED_PREFIXES = ("HP-NB-", "HP-RB-")


class BatchInputError(ValueError):
    pass


def normalize_number(raw):
    if not isinstance(raw, str):
        raise BatchInputError("INVALID_BATCH_TEXT")
    number = raw.strip()
    if not number:
        raise BatchInputError("BATCH_TEXT_EMPTY")
    if all(char in QUOTES or char.isspace() for char in number):
        raise BatchInputError("BATCH_QUOTES_REQUIRE_CONFIRMATION")
    if number.upper().startswith(RESERVED_PREFIXES):
        raise BatchInputError("INTERNAL_BATCH_ID_FORBIDDEN")
    if len(number) > 80 or any(not char.isprintable() or char in "<>" for char in number):
        raise BatchInputError("INVALID_BATCH_TEXT")
    return number


def normalize_selection(selection):
    if not isinstance(selection, dict) or "mode" not in selection:
        raise BatchInputError("BATCH_SELECTION_REQUIRED")
    if selection == {"mode": "NO_BATCH"}:
        return None
    if set(selection) == {"mode", "batchNo"} and selection["mode"] == "BATCH":
        return normalize_number(selection["batchNo"])
    raise BatchInputError("INVALID_BATCH_SELECTION")


def preview_legacy(raw):
    """Classify an import value for review without writing or inferring inventory."""
    if raw is None:
        return {"status": "SOURCE_NULL", "proposed_batch": None}
    try:
        number = normalize_number(raw)
    except BatchInputError as error:
        return {"status": str(error), "requires_review": True}
    return {"status": "UNCHANGED" if raw == number else "TRIMMED", "proposed_batch": number}
