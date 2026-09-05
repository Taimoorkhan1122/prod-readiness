#!/usr/bin/env python3
"""
severity.py - deterministic severity from a finding's factors.

Lenses no longer choose a severity. Two runs of the same audit used to grade
the same gap P1 one time and P3 the next, because severity was a judgement
call made once per finding, by whichever lens got there first. That judgement
is now split into four closed properties any two readers would code the same
way - exposure, data sensitivity, blast radius, and whether a compensating
control is present - and this module turns that description into P0-P3 by a
single published rubric. The same factors always produce the same severity,
on every run, from every lens.

Rubric (kept as data below, not as nested conditionals, so it can be read and
audited without tracing code):

    exposure                points
    ------------------------------
    internet                 3
    authenticated             2
    internal                  1
    local                      0

    data_class               points
    ------------------------------
    secrets                   3
    pii                       3
    financial                  3
    business                   1
    none                        0

    blast_radius              points
    ------------------------------
    systemic                  3
    multi-tenant               2
    single-tenant                1
    single-user                   0

    compensating_control == "present"  ->  subtract 2 (floor the total at 0)

    total >= 8   ->  P0
    total 6-7    ->  P1
    total 3-5    ->  P2
    total <= 2   ->  P3

Hard cap: a finding whose evidence state is UNVERIFIED can never be P0. If the
rubric produces P0 for an UNVERIFIED finding, `compute_severity` records it as
P1 instead. Uncertainty must never escalate severity.
"""
from __future__ import annotations

# Point value per factor value. Expressed as data, not as if/elif chains, so
# the rubric above and the code below cannot silently disagree.
EXPOSURE_POINTS = {
    "internet": 3,
    "authenticated": 2,
    "internal": 1,
    "local": 0,
}

DATA_CLASS_POINTS = {
    "secrets": 3,
    "pii": 3,
    "financial": 3,
    "business": 1,
    "none": 0,
}

BLAST_RADIUS_POINTS = {
    "systemic": 3,
    "multi-tenant": 2,
    "single-tenant": 1,
    "single-user": 0,
}

# Not a point value on its own - a signed adjustment applied after the three
# scores above are summed.
COMPENSATING_CONTROL_ADJUSTMENT = {
    "present": -2,
    "absent": 0,
}

# The four factors a finding must supply, and the closed enum of values each
# one accepts. validate_findings.py and finding_store.py both read this table,
# so an error message listing "allowed values" can never drift from what
# scoring actually accepts.
FACTORS = {
    "exposure": EXPOSURE_POINTS,
    "data_class": DATA_CLASS_POINTS,
    "blast_radius": BLAST_RADIUS_POINTS,
    "compensating_control": COMPENSATING_CONTROL_ADJUSTMENT,
}
FACTOR_KEYS = tuple(FACTORS)

# Rubric total -> severity. Ordered highest threshold first; the first
# threshold the total meets or exceeds wins. Expressed as data so the
# boundaries in the docstring above are the boundaries the code applies.
SEVERITY_THRESHOLDS = (
    (8, "P0"),
    (6, "P1"),
    (3, "P2"),
    (0, "P3"),
)

UNVERIFIED_STATE = "UNVERIFIED"
UNVERIFIED_SEVERITY_CAP = "P1"


class FactorError(ValueError):
    """`factors` is missing, not an object, or has a value outside its enum."""


def validate_factors(factors) -> list[str]:
    """Return one message per problem with `factors`; [] means it is valid.

    Each message names the offending key and lists the values it accepts, so
    a caller can surface it directly without re-deriving the rubric.
    """
    if not isinstance(factors, dict):
        return [
            "factors is required and must be an object with keys "
            + ", ".join(FACTOR_KEYS)
        ]

    errors = []
    for key, allowed in FACTORS.items():
        if key not in factors or factors[key] in (None, ""):
            errors.append(
                f"factors.{key} is required; allowed values: {', '.join(allowed)}"
            )
            continue
        value = factors[key]
        if value not in allowed:
            errors.append(
                f"factors.{key} must be one of {', '.join(allowed)}, got {value!r}"
            )
    return errors


def score_factors(factors: dict) -> int:
    """Sum the rubric points for an already-valid factors object.

    Assumes `validate_factors(factors) == []`; call that first if the input
    is not already known to be valid, since this indexes the enum tables
    directly and raises KeyError on a bad value.
    """
    total = (
        EXPOSURE_POINTS[factors["exposure"]]
        + DATA_CLASS_POINTS[factors["data_class"]]
        + BLAST_RADIUS_POINTS[factors["blast_radius"]]
        + COMPENSATING_CONTROL_ADJUSTMENT[factors["compensating_control"]]
    )
    return max(total, 0)


def severity_for_score(total: int) -> str:
    """Map a rubric total to P0-P3 using SEVERITY_THRESHOLDS."""
    for minimum, severity in SEVERITY_THRESHOLDS:
        if total >= minimum:
            return severity
    return "P3"  # unreachable: the lowest threshold is 0 and total is floored


def compute_severity(factors: dict, state: str) -> str:
    """Derive severity from factors, applying the UNVERIFIED-cannot-be-P0 cap.

    Raises FactorError if `factors` is missing or has an invalid value. Call
    `validate_factors` first if you want every problem reported at once
    rather than an exception on the first one.
    """
    errors = validate_factors(factors)
    if errors:
        raise FactorError("; ".join(errors))

    severity = severity_for_score(score_factors(factors))
    if severity == "P0" and str(state).strip().upper() == UNVERIFIED_STATE:
        severity = UNVERIFIED_SEVERITY_CAP
    return severity
