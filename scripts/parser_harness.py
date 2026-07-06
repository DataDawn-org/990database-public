#!/usr/bin/env python3
"""
990 parser-expansion harness + promotion gate.

WHY THIS EXISTS (2026-06-19): the expansion brief (§6) recorded the harness as
"done (the keystone)". It was NOT — no re-runnable checker existed; the
"comp-nullability matrix green" result was a one-time planning-thread computation.
A one-shot green with no standing code is exactly how a true finding rots into a
false "done". This module converts that check into standing, re-runnable code AND
provides the hard promotion gate the staged rollout (brief §8.5) depends on.

THE GATE HAS TEETH IN CODE, NOT IN A TASK TRACKER. `promotion_gate()` raises
SystemExit when the baseline is absent/red — momentum cannot wave a stage through,
because the promotion path cannot continue past a SystemExit. This mirrors the
established build_gated() / GATE_* / sys.exit idiom in
openregs/scripts/build_comment_org_entities.py + 990project/update.sh.

SCOPE TODAY (recorded narrowly — brief §6 correction): the baseline check covers the
comp-nullability CROSS-pattern on `officers` — the exact axis Bug #3 lived on. It
does NOT cover the same-nullability swap axis (rcro<->other, both non-NULL),
value-correctness, or grants/related_orgs. New-field reconciliation invariants plug
in at the marked TODO as parsers land; a stage cannot promote until its invariants
exist and pass.
"""
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "990data.db"

# Required-column coverage floor: guards against a degenerate all-NULL table that
# would pass the "forbidden columns empty" bound vacuously. A not-degenerate
# tripwire, NOT a precision target (observed 2026-06-19: 990 99.8%, EZ/PF 100%).
REQUIRED_COVERAGE_FLOOR = 0.80

# The §64 comp-nullability matrix, per return_type:
#   990    -> benefits / expense_account MUST be NULL ; reportable_comp_filing_org populated
#   990EZ  -> reportable_comp_related_org / other_compensation MUST be NULL ; rcfo populated
#   990PF  -> reportable_comp_related_org / other_compensation MUST be NULL ; rcfo populated
_REQUIRED = "reportable_comp_filing_org"


def baseline_nullability_matrix(conn: sqlite3.Connection) -> dict:
    """Per-return_type {n, forbidden_violations, required_coverage}. Read-only.

    One full scan of officers (~43.6M rows) joined to returns to resolve
    return_type (officers carries no return_type column). Computes both
    forbidden-column patterns in a single pass, then selects the one that applies
    to each form.
    """
    rows = conn.execute(
        """
        SELECT r.return_type AS rt, COUNT(*) AS n,
               SUM(CASE WHEN o.benefits IS NOT NULL OR o.expense_account IS NOT NULL
                        THEN 1 ELSE 0 END) AS viol_990,
               SUM(CASE WHEN o.reportable_comp_related_org IS NOT NULL
                         OR o.other_compensation IS NOT NULL
                        THEN 1 ELSE 0 END) AS viol_ezpf,
               SUM(CASE WHEN o.reportable_comp_filing_org IS NOT NULL
                        THEN 1 ELSE 0 END) AS req_cov
        FROM officers o JOIN returns r USING(object_id)
        WHERE r.return_type IN ('990', '990EZ', '990PF')
        GROUP BY r.return_type
        """
    ).fetchall()
    out = {}
    for rt, n, viol_990, viol_ezpf, req_cov in rows:
        n = n or 0
        viol = (viol_990 if rt == "990" else viol_ezpf) or 0
        out[rt] = {
            "n": n,
            "forbidden_violations": viol,
            "required_coverage": (req_cov / n) if n else 0.0,
        }
    return out


def assert_baseline_green(conn: sqlite3.Connection, log=print) -> bool:
    """Both-bounds gate on the §64 matrix. Returns True iff green.

    GREEN := for every return_type, forbidden_violations == 0 AND
             required_coverage >= REQUIRED_COVERAGE_FLOOR (the second bound rules
             out an all-NULL table passing the first bound vacuously). Emits GATE_*
             markers grep-compatible with the build's existing gate logging.
    """
    matrix = baseline_nullability_matrix(conn)
    if not matrix:
        log("GATE_BASELINE_RED: no 990/990EZ/990PF officer rows found — "
            "missing tables or empty DB")
        return False
    green = True
    for rt, m in matrix.items():
        if m["forbidden_violations"] != 0:
            green = False
            log(f"GATE_BASELINE_RED: {rt} has {m['forbidden_violations']:,} "
                f"comp-nullability violations (must be 0) of {m['n']:,} rows")
        if m["required_coverage"] < REQUIRED_COVERAGE_FLOOR:
            green = False
            log(f"GATE_BASELINE_RED: {rt} required-column coverage "
                f"{m['required_coverage']:.3f} < floor {REQUIRED_COVERAGE_FLOOR} "
                f"(possible degenerate/all-NULL table)")
    if green:
        log("GATE_BASELINE_GREEN: officers comp-nullability cross-pattern clean "
            "(0 violations, required-cov above floor) — "
            + ", ".join(f"{rt} n={m['n']:,}" for rt, m in matrix.items()))
    return green


# ── helpers: fail-closed existence checks ────────────────────────────────────
def _table_exists(conn, name) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn, table) -> set:
    if not _table_exists(conn, table):
        return set()
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _missing(conn, table, cols) -> list:
    have = _columns(conn, table)
    return [c for c in cols if c not in have]


# ── Deliverable-A new-field invariants ───────────────────────────────────────
# Every one FAILS CLOSED: if the column / table / fixtures it needs are absent it
# returns RED — it cannot CERTIFY a field that is not there ("can't evaluate" is a
# refusal, not a pass; mirrors §5 of the build packet). They go green only when the
# parser has landed the field AND it validates.

# MEASURED 2026-06-26 on real 03/04 990s (B+C+D vs Part I L18 proxy, n=2,088,195):
# 99.05% reconcile within 1% — the HEALTHY rate is ~99%, NOT the brief's ~94% (that
# figure spanned ALL 990s incl. the non-c3/c4 form-rule class #81; scoping to 03/04
# removes it). 0.90 was too loose: a parse bug breaking ~9% of filers would still pass.
# 0.97 REDs a >~2pp collapse while tolerating the ~1% filer-allocation residual.
# ⚠ CONFIRM against the col-A-specific COMBINED rate at parser-land (this proxy is
# B+C+D≈L18; the live invariant ALSO checks B+C+D≈colA), and the reviewer sets the final
# risk-tolerance (how large a break must RED). [#7 review target 2]
RECONCILE_RATE_FLOOR = 0.999  # FLIPPED BY MAINTAINER 2026-07-03 (was 0.97 proxy). POPULATION (legibility
# receipt, maintainer condition): the GATE evaluates the FULL reconcile population — return_type='990' JOIN
# bmf ON ein WITH subsection IN ('03','04') AND colA>0 = 2,117,991 filers at land (75.6% of the 2.8M
# corpus; c3/c4-ONLY because Form 990 Part IX columns B-D functional allocation is REQUIRED only of
# 501(c)(3)/(4) — unscoped, the check false-fires on everyone else). The audit re-measures on a FRESH
# random 20,000-row sample of that population per run (2026-07-03: 19,819 evaluable after numeric-guard
# drops — hence "19,819" in the land receipts; ~0.7% of corpus because it is a SAMPLE, not a
# subpopulation). Measured at land: agreement 19,819/19,819 = 100%; rule-of-three disagreement bound
# <=1.5e-4 -> floor 0.999 keeps ~6x headroom vs legit noise, 30x tighter teeth than the proxy 0.97.
_TIE_TOL = 0.01
# bmf-join coverage floor — MEASURED 2026-06-26: 94.59% of 990 filers are classifiable via
# bmf (~5.4% drop, ein not in bmf). WARN below this on EROSION — the green-over-a-shrinking-
# base pattern. Surfaced every run, never silent. [#7 finding 2]
COVERAGE_FLOOR = 0.90
# Rate-floor recalibration HARD-GATE — 0.97 is a PROXY (B+C+D≈L18); the live check ALSO
# tests B+C+D≈colA, so the combined rate may differ. Reconciliation REDs once col-A lands
# until a human re-measures the real combined rate, sets the floor, and flips this flag.
# Required gate step before stage promotion, NOT an optional confirm. [#7 finding 3]
RECONCILE_FLOOR_RECALIBRATED = True  # FLIPPED BY MAINTAINER 2026-07-03 — the recalibration = flips 2-5 of
# the land (contractor ceilings measured+verified on built corpus; RECONCILE_RATE_FLOOR re-measured on
# the built col-A column, no longer the B+C+D≈L18 proxy).

# ── Suite-enforced floor derivation (2026-06-27) ─────────────────────────────────────────────
# THE STRUCTURAL FIX (adversarial review, consequence 4): no gate threshold ships as a round
# literal. Every floor below is COMPUTED from a recorded measurement via a named rule; a floor
# that drifts from its derivation REDs test_parser_harness (case FD). This moves threshold-
# derivation from "the reviewer catches it round" to "the suite refuses it round." Add a floor →
# add its measurement here, or the test fails closed.
def _rule_of_three_floor(absent, n):
    """Coverage/agreement floor = 1 − (95% upper bound on the miss rate). Rule of three when
    absent==0: UB ≈ 3/n. A floor TIGHTER than this false-positive-blocks genuine low-freq misses
    the sample was too small to see — so the floor IS the bound the sample earns, not a round wish."""
    return round(1 - (absent + 3) / n, 5)


def _collapse_floor(legit_min, rel_margin=0.10):
    """COLLAPSE tripwire: a fraction below the worst LEGITIMATE cohort rate. A real break drives the
    cohort to ~0, so the value is insensitive across (0, legit_min) — what matters is staying robustly
    BELOW legit variation (incl. small-cohort sampling + the partial-year tail) and far above a break.
    Derived from a CONSERVATIVE legit-min (below the worst observed); audit_floor_measurements.py
    re-checks floor < live legit_min every run, which is what caught the earlier 5σ-below-sample-min
    floor false-positiving when a fresh sample's min landed below it. NOT σ-precise on purpose."""
    return round(legit_min * (1 - rel_margin), 3)


def _collapse_count_floor(collapse_target, margin):
    """Collapse tripwire for a COUNT of distinct buckets (vs _collapse_floor's RATE). A fully-collapsed
    grouping key has exactly `collapse_target` (=1) distinct value; floor = target + margin REDs any
    collapse to ≤ that. LOW by design (review round 2): anchored to the collapse target, NOT corpus
    richness (~31 distinct returnVersions) — a richness-anchored floor would false-RED a benign
    early-season dip on the UNATTENDED monthly. margin is the risk-tolerance (mirrors SBASE's >0.5)."""
    return collapse_target + margin


def _rule_of_three_ceil(observed, n):
    """Near-zero UPPER bound (the ceiling mirror of _rule_of_three_floor): the 95% upper bound on a rate
    that is ~0 on clean data = (observed + 3) / n. The +3 is the rule-of-three margin a sample of size n
    earns — a ceiling TIGHTER than this false-positive-reds genuine low-frequency events the sample was
    too small to see, so the ceiling IS the bound the sample earns. ⚠ ONLY valid for a GENUINELY near-zero
    floor: the +3 is a SAMPLING-confidence margin that VANISHES as n grows ((obs+3)/n → obs/n at corpus
    scale), so on an ENDEMIC-nonzero floor it collapses to a near-exact bound that reds on normal variation
    (the DROP/VIOLATION reclassification, 2026-06-28 — those moved to _drift_ceil). Used now for
    return_version malformed ONLY — genuinely ~0 (0/8,000), no endemic floor, so the vanishing margin is fine.
    ⚠ round() must not annihilate corpus-scale values: round(…,5) zeroed (0+3)/2,802,059 at the 2026-07-03
    land flip, silently producing the HARD-0 ceiling the registry note forbids (one future malformed filing
    would false-red the unattended monthly). 12 places preserves any realistic n; display rounds elsewhere."""
    return round((observed + 3) / n, 12)


def _drift_ceil(baseline_rate, rel_margin):
    """DRIFT ceiling on a NONZERO floor — the bound ALL THREE contractor buckets need (endemic-nonzero
    filer floors). Unlike _rule_of_three_ceil (whose 3/n margin VANISHES at corpus scale → collapses to
    the exact floor → reds on normal variation), this bounds a rate whose CLEAN value is a real nonzero
    floor: ceiling = baseline floor × (1+rel_margin), bounding a *change* above the floor, n-INDEPENDENT.
    ⚠ BOTH args are PENDING per-bucket until land (review pass 3, 2026-06-28): baseline_rate from the
    worst gated version, AND rel_margin set PER-BUCKET from that bucket's measured cross-version variance
    — NOT a uniform default. Uniform-margin-across-buckets is the exact failure (uniform near-zero) this
    whole thread killed; the scratch first-read already shows the three differ (DROP spread 1.8×,
    INDETERMINATE 4.2×, VIOLATION sparse/single-filer-anchor). rel_margin trades blind-spot size
    [floor, floor×(1+m)] against false-positive headroom; sub-(1+m) SHAPE-conditional bugs are caught (only)
    by the stratified row_count/value witnesses, so the witness stratification must cover those shapes.
    audit_floor_measurements.py asserts the result sits ABOVE the worst legit cohort AND BELOW teeth.
    The variance→margin RULE (UCL at a uniform false-positive α; estimator per bucket-shape) + the
    baseline filer-verify precondition are SPECIFIED in DECISION_contractor_tie_classification_2026-06-28
    → FLIP-TIME §A/§B — instantiate them on corpus data at flip, do NOT improvise the margin here."""
    return round(baseline_rate * (1 + rel_margin), 5)


# Each floor + the measurement it derives from (source cited, re-confirm on the built column at land):
_FLOOR_MEASUREMENTS = {
    # PRE-LANDING XML scan: 0 genuine col-A-absent in 20,000 on-S filers (TotalAmt is the most-
    # stable element — 100% across 15,000 filings + all 31 returnVersions 2015→2025). Rule of three
    # on n=20,000 → coverage floor 0.99985, NOT a round 0.9999 (which 20K does not earn and which
    # would false-positive-block a monthly carrying genuine sub-0.015% absence).
    # Derived from a CONSERVATIVE reference n=10,000 (BELOW any production scan), not the 20,000
    # actually scanned — so the floor (0.9997) sits with headroom UNDER the rule-of-three bound a real
    # ≥10K scan earns, instead of AT the 20K bound where any smaller re-measurement reds it (the
    # at-the-bound brittleness the review flagged; the audit caught 0.99985 redding an 18K rescan).
    # Still has full teeth: a TotalAmt-drop sends coverage → ~0, far below 0.9997.
    "ONS_COVERAGE_FLOOR":  {"fn": _rule_of_three_floor, "args": (0, 10000),
        "src": "0 col-A-absent / 20,000 on-S scanned; floor from conservative ref n=10,000 for headroom (2026-06-27)"},
    # NOTE: the on-S disagreement rate is NOT 0 — a fresh audit measured ~6e-5 (legit filer Part-IX
    # internal inconsistency, distinct from the L18 band-edge). The conservative ref n=10,000 (floor
    # 0.9997) already sits below the measured-disagreement bound (~0.99994) at SCAN scale, so it does
    # not false-positive on a full run. SMALL-COHORT false-positive (a ≥1-disagreement cohort below
    # 1/(1−floor)≈3,334) is a SEPARATE, deferred fix: ONS_AGREEMENT_MIN_COHORT_N = ceil(1/(1−floor)),
    # gating agreement only on cohorts large enough to tolerate one legit disagreement (folds in at land).
    "ONS_AGREEMENT_FLOOR": {"fn": _rule_of_three_floor, "args": (0, 10000),
        "src": "on-S disagreement ~6e-5 (measured); conservative ref n=10,000 → 0.9997 (scan-safe) (2026-06-27)"},
    # per-returnVersion |S|/base, cohorts n≥200: worst LEGIT cohort observed 0.945–0.961 ACROSS
    # samples (the sample-min itself varies — which is why a σ-below-sample-min floor was unsafe; the
    # audit caught 0.9475 false-positiving when a fresh min came in at 0.9457). Conservative legit-min
    # 0.93 (below every observed) × 0.90 margin = 0.837 — a COLLAPSE tripwire (break → ~0), robustly
    # below legit variation. audit_floor_measurements.py enforces floor < live legit_min every run.
    "ONS_VERSION_SBASE_FLOOR": {"fn": _collapse_floor, "args": (0.93, 0.10),
        "src": "per-returnVersion |S|/base n≥200: worst legit ≥0.945 across samples; conservative 0.93×0.90 (2026-06-27)"},
    # Contractor coverage: among 990 filers self-reporting CntrctRcvdGreaterThan100KCnt>0, the fraction
    # listing 0 contractor rows. ⚠ RECLASSIFIED 2026-06-28 (review item, 2nd correction): the original
    # premise ("must be ~0") is EMPIRICALLY FALSE — filers DO report cnt>0 and list nothing (~1.4% on the
    # 8,000-filer scratch; all 10 verified at the XML: 0 ContractorCompensationGrp, one with cnt=34 —
    # endemic FILER behaviour, NOT a parse drop). My first correction kept rule-of-three "because (exc+3)/n
    # absorbs a nonzero floor" — that was WRONG: the 3/n margin is a SAMPLING-confidence margin that
    # VANISHES as n grows (at n=2.77M, ceil = floor + 0.0000011 ≈ the EXACT floor), so a rule-of-three
    # ceiling on an ENDEMIC-nonzero floor becomes a near-exact bound that REDs on normal run/version
    # variation — failure-mode #2, one bucket over from where we were watching. So DROP is the SAME
    # category as INDETERMINATE (endemic-nonzero filer floor) and gets the SAME DRIFT treatment: an
    # n-INDEPENDENT multiplicative margin above the worst-gated-version floor. Teeth survive: a
    # ContractorName-wrapper DROP is a MASS event (~100%), far above floor×(1+margin). ⚠ DETECTION FLOOR
    # (review item b): this leg only reds a VERSION-level drop rate above floor×(1+margin); a
    # SLOT-ASYMMETRIC / sub-shape PARTIAL drop below that is covered by the row_count WITNESSES on the
    # witnessed filings, NOT this leg (a known, bounded gap — see the slot-asymmetric note in (2b) below).
    # PENDING (baseline None) until measured on built 990 contractors at land; the leg fail-closes.
    # ── GATING CRITERION (all three contractor ceilings; documented per maintainer condition 2026-07-03) ──
    # Cohort = returnVersion; a cohort is ENFORCED only at n ≥ ONS_MIN_COHORT_N (200), keyed on the
    # bucket's OWN denominator: VIOLATION/INDETERMINATE gate on base_n, DROP on cnt_pos. Sub-threshold
    # cohorts are SKIPPED but never silently (GATE_RECON_WARN when skipped share >5% of base). THE TRADE
    # (corrected per maintainer 2026-07-03 — the first statement here OVERSTATED coverage): a real regression
    # confined to a sub-200 new-version cohort is SUBSTANTIVELY UNCOVERED until #267 lands — the
    # version-pinned witnesses cannot cover a not-yet-existing version (that IS the window), and the §5
    # deploy assertion is GLOBAL mass-zero (a new-version-confined break at small corpus share cannot
    # trip it). Exposure is bounded only by new-version RAMP RATES (~1-2 monthlies to cross n=200).
    # In-window instruments, both INFORMATIONAL (no gate, no false-red surface): the HOT-COHORT WARN
    # below (a skipped cohort whose measured rate already exceeds its bucket's ceiling logs a WARN,
    # any share — red-first proven on a planted hot sub-200 cohort 2026-07-03) and the corpus
    # indeterminate trend line in the monthly Summary (uniform-creep detector). Substantive closure =
    # exact-binomial-per-cohort (#267): every cohort evaluated, red iff P(X≥x|n,ceil)<α — kills the
    # MIN entirely, both edges; post-land leg change with red-first re-proof, NOT mid-land surgery.
    # ⚠ FIT-POPULATION == LEG-POPULATION: ceilings below are derived on cohorts gated at the SAME
    # ONS_MIN_COHORT_N=200 the leg enforces (2026-07-03 correction: a first fit at MIN=1000 mis-anchored
    # DROP — 2025v4.1 (2.671%, cnt_pos=936) is the true worst gated cohort, not 2018v3.3).
    "CONTRACTOR_DROP_CEIL": {"fn": _drift_ceil, "args": (0.02671, 0.52816),
        "src": "MEASURED AT LAND 2026-07-03 (built 990data_public.db, cohorts gated cnt_pos>=200): worst "
               "gated ver 2025v4.1 = 25/936 = 0.02671, rule-B verified filer-side 8/8 raw-XML (cnt up to "
               "10 reported, ZERO ContractorCompensationGrp present; 2018v3.3 38/1,556 + 2025v4.0 18/726 "
               "also verified 8/8 each). Estimator: one-sided binomial UCB at alpha=0.005 on the verified-"
               "worst cohort (dense bucket; margin is the OUTPUT = UCB/worst-1 = +52.8%) -> ceil 0.04082. "
               "Band: >= worst-legit 0.02671, <= teeth 0.05. FLIPPED BY MAINTAINER 2026-07-03 (rule approved; "
               "value corrected from 0.0349 by the MIN=200 population alignment, flagged in-report)"},
    # ── §4 classify-not-gate (2026-06-28): the contractor `listed>cnt` tie was a CATEGORY ERROR —
    # `cnt` (a reported scalar) and the detail rows (a top-list filers populate loosely: NULL comps,
    # sub-threshold rows, literal `<PersonNm>NONE</PersonNm>`) are two INDEPENDENT filer-controlled
    # disclosures with no schema-enforced relationship. Per [[feedback_cross_field_gate_classify_before
    # _gating]] the per-record gate is demoted to classify + rate-monitor. Two NEW ceilings (DROP above
    # pre-existed), both DRIFT-bounded like DROP — all three contractor floors are endemic-nonzero — both
    # PENDING (baseline None → fail-closed) until measured on built 990 contractors at land; (c) hand-
    # verified filer-side on the 8,000-filer scratch (2026-06-28) so the baselines are NOT parse-contaminated.
    # CONTRACTOR_VIOLATION_CEIL — bounds the per-version RATE of GENUINE filer self-contradictions:
    #   clean_cnt > cnt, where clean_cnt = #contractor rows with comp > 100000 (rows we can AFFIRM are
    #   >$100k contractors). ⚠ ALSO RECLASSIFIED to DRIFT 2026-06-28 (systemic, same as DROP): originally
    #   filed "near-zero, rule-of-three" — but filer MISCOUNTING is endemic at a small NONZERO floor
    #   (scratch worst gated version 1/316 ≈ 0.3%, (c)-verified filer-side: 202200219349300490 lists 5
    #   ≥$100k contractors with cnt=1), and rule-of-three's 3/n margin VANISHES at corpus scale (n=500K →
    #   ceil = floor + 0.33% relative ≈ exact floor) → reds on normal variation = failure-mode #2. So
    #   VIOLATION is endemic-nonzero like DROP/INDETERMINATE → the SAME drift treatment (n-independent
    #   margin above the worst-gated-version floor). A cnt-misread / row-fabrication regression spikes it
    #   far above floor×(1+margin) — teeth survive (band-check teeth ≤0.02 confirm it).
    "CONTRACTOR_VIOLATION_CEIL": {"fn": _drift_ceil, "args": (0.00541, 0.53370),
        "src": "MEASURED AT LAND 2026-07-03 (built, base_n>=200 gating): worst gated ver 2017v2.0 = "
               "25/4,622 = 0.00541, rule-B verified filer-side 8/8 raw-XML (XML lists up to 5 comp>100k "
               "groups vs filer-stated cnt 0-3; stored cnt == XML element exactly). ⚠ scratch's SPARSE "
               "profile FALSIFIED at corpus scale (30/31 gated cohorts nonzero) -> same dense-bucket "
               "estimator as DROP/INDET, not Poisson: binomial UCB alpha=0.005 on verified-worst -> ceil "
               "0.00830 (margin +53.4%). Band: >= 0.00541, <= teeth 0.02. FLIPPED BY MAINTAINER 2026-07-03"},
    # CONTRACTOR_INDETERMINATE_CEIL — bounds the per-version RATE of filers with ≥1 NULL-comp contractor
    #   row. NULL-comp is ORDINARY filer omission at a NONZERO baseline ((c)-verified the CompensationAmt
    #   element is genuinely ABSENT in the XML — MILLS CONSTRUCTION; the 2 no-comp rows on the Lorossta
    #   cnt=3 filer — NOT a parse drop). DRIFT bound (baseline × (1+margin)), NOT just-above-zero — set
    #   just-above-zero it REDs every monthly. Measure the baseline at the WORST GATED VERSION (conservative
    #   reference, like ONS_VERSION_SBASE_FLOOR's legit-min). ⚠ margin per-bucket (review pass 3): this is
    #   the WIDEST-spread bucket on scratch (cross-version 4.2×), so its margin needs the MOST headroom of
    #   the three — do NOT inherit DROP/VIOLATION's. A mass-NULL comp-extraction regression → teeth.
    "CONTRACTOR_INDETERMINATE_CEIL": {"fn": _drift_ceil, "args": (0.06912, 0.09841),
        "src": "MEASURED AT LAND 2026-07-03 (built, base_n>=200 gating): worst gated ver 2016v3.1 = "
               "648/9,375 = 0.06912, rule-B verified filer-side 8/8 raw-XML: <PersonNm>NONE</PersonNm> "
               "placeholder blocks with NO CompensationAmt element at all (2016-era vendor pattern; "
               "2018v3.3 5.42% same shape) — NOT a parse bug; unconditional-append mirrors the XML "
               "faithfully. Estimator: binomial UCB alpha=0.005 on verified-worst -> ceil 0.07592 (margin "
               "+9.8%). ⚠ verified-legit worst FALSIFIED the old 0.05 teeth premise -> teeth raised to "
               "0.10 (maintainer 2026-07-03; audit_floor_measurements._TEETH_MAX_CEIL). NONE-placeholder "
               "re-scope = the better long-term taxonomy: ticketed #266 (pre-Deliverable-B, with the "
               "public-display curation call). FLIPPED BY MAINTAINER 2026-07-03"},
    # return_version column-integrity ceil (adversarial review rounds 1–3, 2026-06-28): the max
    # tolerated NULL/malformed rate on built return_type='990' rows. MEASURE-THEN-FLIP — PENDING
    # (None) until measured at land, so return_version_integrity fail-closes (that malformed rate is
    # one of the two Phase-1-checkpoint numbers owed back). 200/200 universality in the pre-land
    # sample ⇒ the live rate is ~0; a ceil (not a hard 0) so a single malformed new filing can't
    # block the unattended monthly. Prefix-only format by design — see return_version_integrity.
    "RETURN_VERSION_MALFORMED_CEIL": {"fn": _rule_of_three_ceil, "args": (0, 2802059),
        "src": "MEASURED AT LAND 2026-07-03 on built 990data_public.db: NULL=0 + ^\\d{4}v-violation=0 of "
               "2,802,059 return_type='990' rows, 34 distinct versions; per-version classification "
               "NEAR-ZERO (degenerate — zero malformed, nothing to cluster). ceil=(0+3)/2,802,059"
               "≈1.07e-6. FLIPPED BY MAINTAINER (conditional go 2026-07-03: classifier verified FORMAT-based "
               "GLOB, not version-enumeration — new IRS versions pass as new distinct buckets)"},
    # return_version distinct-count COLLAPSE floor (review round 2, 2026-06-28): NULL + format pass but a
    # read mapping every filing to ONE valid-format version (a truncation that still matches ^\d{4}v, or
    # a constant) collapses the grouping key → per-version silently reverts to cumulative. The DISTINCT
    # count catches it. floor = collapse-target 1 + margin 2 = 3; legit is ~20-31 distinct returnVersions
    # (2015→2025), so 3 sits far below even an early-season dip. NOT pending — anchored to the structural
    # collapse target, not measured legit data; the audit confirms the live distinct count is >> floor.
    "RETURN_VERSION_MIN_DISTINCT": {"fn": _collapse_count_floor, "args": (1, 2),
        "src": "collapse target 1 distinct + margin 2; legit ~20-31 distinct returnVersions 2015→2025 (2026-06-28)"},
}


def _derive(name):
    m = _FLOOR_MEASUREMENTS[name]
    if any(a is None for a in m["args"]):
        return None  # measurement PENDING (e.g. land-time) → floor not derivable yet → leg fail-closes
    return m["fn"](*m["args"])


# ── on-S leg (adversarial-review hardening, 2026-06-27) ──────────────────────────────────────
# S = the filer-self-consistent set: 03/04 990s where the PRE-EXISTING B/C/D columns reconcile
# to the PRE-EXISTING L18 within _TIE_TOL — defined WITHOUT the new col-A read, so S-membership
# cannot be moved by a col-A bug. On S, a correct Part IX TotalAmt is structurally pinned to
# ≈ B+C+D ≈ L18, which is what makes these legs meaningful.
#
# DIVISION OF LABOR (consequence 2 — these are NOT independent guarantees):
#   • COVERAGE (PRIMARY): col-A non-null on a cohort's S filers. Catches a TotalAmt-drop that
#     leaves B/C/D intact (S-independent; loudest on the likely code-typo all-drop).
#   • AGREEMENT (CROSS-CHECK): within-_TIE_TOL on S. Reads the SAME _TIE_TOL as the reconciliation
#     (coupling in CODE — test S3). Mostly redundant with test_colA_source.py's element-source rail;
#     its NON-redundant reach is value-corruption >_TIE_TOL on S (int-parse/transform bugs). Bound:
#     it does NOT see sub-tolerance corruption, nor repeating-group row-selection (col-A line-25
#     TotalAmt is a singular total, not a row in the repeating expense group).
#   • PER-COHORT (consequence 3, REBUILT): the off-S-correlated failure is a returnVersion/namespace
#     break that corrupts col-A AND B/C/D together → that VERSION's filers fall off S. A CUMULATIVE
#     |S|/base is structurally BLIND to it (one version is <1% of base — confirmed: biggest tax_year
#     is 13% of cumulative, a version a fraction of that). So coverage/agreement/|S|-fraction are
#     checked PER returnVersion: a broken version reds undiluted; the cumulative ratio is retired.
# Floors are DERIVED (above) from a PRE-LANDING XML proxy; RE-CONFIRM on the built column at land.
ONS_COVERAGE_FLOOR = _derive("ONS_COVERAGE_FLOOR")         # 0.99985 (rule of three, n=20,000)
ONS_AGREEMENT_FLOOR = _derive("ONS_AGREEMENT_FLOOR")       # 0.99985
ONS_VERSION_SBASE_FLOOR = _derive("ONS_VERSION_SBASE_FLOOR")  # 0.837 (collapse tripwire: conservative legit-min×0.9)
CONTRACTOR_DROP_CEIL = _derive("CONTRACTOR_DROP_CEIL")     # None until measured at land (pending)
CONTRACTOR_VIOLATION_CEIL = _derive("CONTRACTOR_VIOLATION_CEIL")          # None until measured at land (§4)
CONTRACTOR_INDETERMINATE_CEIL = _derive("CONTRACTOR_INDETERMINATE_CEIL")  # None until measured at land (§4, drift)
RETURN_VERSION_MALFORMED_CEIL = _derive("RETURN_VERSION_MALFORMED_CEIL")  # None until measured at land (pending)
RETURN_VERSION_MIN_DISTINCT = _derive("RETURN_VERSION_MIN_DISTINCT")  # 3 (collapse tripwire, not pending)
# population floor below which the distinct-count collapse check is SKIPPED — a small sample is not a
# collapse. The real corpus / a full public rebuild is ~2.77M 990 rows spanning ~31 versions; tests lower this.
_COLLAPSE_MIN_POP = 5000
# Gating eligibility: below this per-cohort n the |S|/base estimate's SE (~0.7pt at 99% for n=200)
# is too large to gate without false-positives on sampling noise. Sub-threshold cohorts are SKIPPED
# but their filing share is logged (never silently ungated). Not a precision floor — a noise gate.
ONS_MIN_COHORT_N = 200


# ── return_version column-integrity gate (adversarial review rounds 1–3, 2026-06-28) ─────────
# The on-S leg, the contractor coverage leg, and audit_floor_measurements.py ALL GROUP BY
# returns.return_version. A wrong-attribute read (column PRESENT, every value NULL → one giant NULL
# cohort) silently degrades the per-version defense back to CUMULATIVE — round-3's denominator
# collapse reappearing in the GROUPING KEY itself, one layer below the code-path fix that addressed
# it. NULL + a PREFIX-format check catch the gross modes; this gate is the PRECONDITION that
# recalibration (audit_floor_measurements) and the staged-rollout gate (promotion_gate) refuse to
# measure without, and that update.sh dies on post-build — the unattended monthly, where a masked
# collapse would currently ship silently (nothing on that path inspects return_version).
#   • PREFIX-ONLY ^\d{4}v is DELIBERATE: year/major is the field-shape grouping axis (the
#     cumulative-blindness scar pooled across YEARS, never minors). A tight ^\d{4}v\d+\.\d+$ would
#     false-RED a benign future IRS format (2027v5; a 3-part minor) on the unattended monthly — the
#     symmetric sin to a brittle floor. Minor-version pooling is low-harm and is left to the
#     BOTH-ENDS mapping witnesses (Brenda 2017v2.2 / Acumen 2017v2.3 — same year+major, adjacent
#     minor, already in witness_filings/), not a machine guard.
#   • SEPARATE from the per-version legs on purpose: a leg treats a NULL version as one skipped
#     sub-threshold cohort (legit for a rare/new version); THIS gate catches SYSTEMATIC NULL/garbage.


def return_version_integrity(conn, log=print) -> bool:
    """Population health of returns.return_version BEFORE any per-version measurement trusts it. Three
    modes: systematic NULL, malformed (prefix-format), and COLLAPSE (too few distinct buckets — the
    valid-but-all-one-value read NULL+format MISS). Fail-closed on absent column / pending ceil / zero
    rows. Tolerance is MEASURE-THEN-FLIP (RETURN_VERSION_MALFORMED_CEIL None until land). SQL-aggregated
    (GLOB '[0-9][0-9][0-9][0-9]v*' == ^\\d{4}v) so it scans the corpus without materialising ~2.77M strings."""
    if "return_version" not in _columns(conn, "returns"):
        log("GATE_RVI_RED: returns.return_version absent (fail-closed; capture returnVersion at col-A land)")
        return False
    n, nulls, malformed, distinct = conn.execute(
        """SELECT COUNT(*),
                  SUM(CASE WHEN return_version IS NULL OR TRIM(return_version)='' THEN 1 ELSE 0 END),
                  SUM(CASE WHEN return_version IS NOT NULL AND TRIM(return_version)<>''
                            AND return_version NOT GLOB '[0-9][0-9][0-9][0-9]v*' THEN 1 ELSE 0 END),
                  COUNT(DISTINCT CASE WHEN return_version GLOB '[0-9][0-9][0-9][0-9]v*'
                                      THEN return_version END)
           FROM returns WHERE return_type='990'""").fetchone()
    n, nulls, malformed, distinct = (n or 0), (nulls or 0), (malformed or 0), (distinct or 0)
    if n == 0:
        log("GATE_RVI_RED: 0 return_type='990' rows — can't certify return_version (fail-closed)")
        return False
    bad = nulls + malformed
    if RETURN_VERSION_MALFORMED_CEIL is None:
        log(f"GATE_RVI_RED: RETURN_VERSION_MALFORMED_CEIL PENDING — measure the return_version "
            f"NULL/malformed rate on built data ({bad:,}/{n:,} here), set the ceil, flip (fail-closed "
            "until then; that rate is one of the two Phase-1-checkpoint numbers)")
        return False
    rate = bad / n
    if rate > RETURN_VERSION_MALFORMED_CEIL:
        log(f"GATE_RVI_RED: return_version NULL/malformed {bad:,}/{n:,} ({rate:.5f}) > ceil "
            f"{RETURN_VERSION_MALFORMED_CEIL} — garbage read; the per-version grouping key is "
            "untrustworthy and every per-version floor measured against it is poisoned")
        return False
    if n >= _COLLAPSE_MIN_POP and distinct < RETURN_VERSION_MIN_DISTINCT:
        log(f"GATE_RVI_RED: return_version COLLAPSE — only {distinct} distinct well-formed version(s) "
            f"over {n:,} 990 rows (< {RETURN_VERSION_MIN_DISTINCT}); the grouping key collapsed and the "
            "per-version defense has silently degraded to CUMULATIVE (NULL+format alone miss this)")
        return False
    log(f"GATE_RVI_OK: return_version {n-bad:,}/{n:,} well-formed, {distinct} distinct version(s) "
        f"(NULL/malformed {rate:.5f} ≤ ceil {RETURN_VERSION_MALFORMED_CEIL}; ≥ {RETURN_VERSION_MIN_DISTINCT} distinct)")
    return True


def contractor_bucket_stats(conn):
    """Per-returnVersion contractor bucket counts (§4 classify-not-gate) — the ONE measurement the gate
    (newfields_reconciliation) and the audit (audit_floor_measurements) SHARE, so the rate the audit
    band-checks is byte-for-byte the rate the gate enforces (no drift between enforce and verify).
    Returns rows (ver, base_n, cnt_pos, dropped, violations, indeterminate) over the denominator
    return_type='990' AND contractors_over_100k_cnt IS NOT NULL:
      • base_n        — denominator filers (cnt-not-NULL 990) for this version
      • clean_cnt     — #contractor rows with comp>100000 (STRICTLY >, IRS Line 2 basis — review item 1; the rows we can AFFIRM are >$100k contractors)
      • violations    — clean_cnt > cnt  (genuine filer self-contradiction; DRIFT leg, endemic ~0.3% floor)
      • indeterminate — ≥1 contractor row with comp IS NULL (ordinary filer omission; DRIFT leg)
      • dropped       — cnt>0 AND 0 listed rows (the parse-correctness coverage leg; cnt_pos denominator)
    Caller scopes gating by ONS_MIN_COHORT_N and the per-ceil PENDING check."""
    return conn.execute(
        """
        WITH cs AS (
          SELECT object_id,
                 COUNT(*) AS listed,
                 -- STRICTLY > 100000 (CORRECTED 2026-06-28, review item 1): IRS Line 2
                 -- (CntrctRcvdGreaterThan100KCnt) counts contractors paid MORE THAN $100,000 — strictly
                 -- exclusive (confirmed at the form text + instructions). The spec said ">=" colloquially;
                 -- matching the filer's OWN count basis (>) is load-bearing — a >= would let a $100,000.00-
                 -- exact row (filer-excluded from cnt) push clean_cnt above cnt and INJECT a parser-side
                 -- artifact into VIOLATION, the one bucket defined as "genuine filer self-contradiction"
                 -- (the §4 category error inside §4's own ruler). With > a boundary row is correctly
                 -- excluded regardless of how filer software treats it → no false violation either way.
                 SUM(CASE WHEN compensation > 100000 THEN 1 ELSE 0 END) AS clean_cnt,
                 SUM(CASE WHEN compensation IS NULL THEN 1 ELSE 0 END) AS null_cnt
          FROM contractors GROUP BY object_id
        )
        SELECT r.return_version AS ver,
               COUNT(*) AS base_n,
               SUM(CASE WHEN r.contractors_over_100k_cnt>0 THEN 1 ELSE 0 END) AS cnt_pos,
               SUM(CASE WHEN r.contractors_over_100k_cnt>0
                         AND COALESCE(cs.listed,0)=0 THEN 1 ELSE 0 END) AS dropped,
               SUM(CASE WHEN COALESCE(cs.clean_cnt,0) > r.contractors_over_100k_cnt
                        THEN 1 ELSE 0 END) AS violations,
               SUM(CASE WHEN COALESCE(cs.null_cnt,0) > 0 THEN 1 ELSE 0 END) AS indeterminate
        FROM returns r LEFT JOIN cs ON cs.object_id = r.object_id
        WHERE r.return_type='990' AND r.contractors_over_100k_cnt IS NOT NULL
        GROUP BY r.return_version
        """
    ).fetchall()


def newfields_reconciliation(conn, log=print) -> bool:
    """In-DB cross-field ties for A's new fields. RED on absent column or rate-collapse."""
    green = True
    # (1) col-A reconciliation BAND, scoped to subsection 03/04 (the #81 guard: cols
    #     B-D are required only of c3/c4 — an unscoped check false-fires on the rest).
    need = _missing(conn, "returns",
                    ["total_functional_expenses", "total_expenses",
                     "program_expenses", "management_expenses", "fundraising_expenses"])
    if need or not _table_exists(conn, "bmf"):
        log(f"GATE_RECON_RED: col-A reconciliation can't evaluate — missing "
            f"{need or ['bmf']} (fail-closed; field not yet landed)")
        green = False
    else:
        n, ok = conn.execute(
            """
            WITH e AS (
              SELECT r.total_functional_expenses AS colA, r.total_expenses AS l18,
                     COALESCE(r.program_expenses,0)+COALESCE(r.management_expenses,0)
                       +COALESCE(r.fundraising_expenses,0) AS bcd
              FROM returns r JOIN bmf b USING(ein)
              WHERE r.return_type='990' AND b.subsection IN ('03','04')
                AND r.total_functional_expenses IS NOT NULL
                AND r.total_functional_expenses > 0
            )
            SELECT COUNT(*),
                   SUM(CASE WHEN ABS(bcd-colA) <= :t*colA
                             AND l18 IS NOT NULL AND ABS(colA-l18) <= :t*l18
                            THEN 1 ELSE 0 END)
            FROM e
            """, {"t": _TIE_TOL}
        ).fetchone()
        n, ok = (n or 0), (ok or 0)
        rate = (ok / n) if n else 0.0
        if n == 0:
            log("GATE_RECON_RED: 0 evaluable 03/04 col-A rows — can't certify (fail-closed)")
            green = False
        elif rate < RECONCILE_RATE_FLOOR:
            log(f"GATE_RECON_RED: col-A reconcile rate {rate:.3f} < floor "
                f"{RECONCILE_RATE_FLOOR} over {n:,} 03/04 filers — RATE COLLAPSE "
                "(parse bug, not filer-allocation error)")
            green = False
        else:
            log(f"GATE_RECON_OK: col-A reconcile {rate:.3f} over {n:,} 03/04 filers")
        # coverage: green-over-a-shrinking-base detector (bmf classifiability). [#7 finding 2]
        tot = conn.execute(
            "SELECT COUNT(*) FROM returns WHERE return_type='990' "
            "AND total_functional_expenses IS NOT NULL AND total_functional_expenses>0"
        ).fetchone()[0]
        joined = conn.execute(
            "SELECT COUNT(*) FROM returns r WHERE r.return_type='990' "
            "AND r.total_functional_expenses IS NOT NULL AND r.total_functional_expenses>0 "
            "AND EXISTS(SELECT 1 FROM bmf b WHERE b.ein=r.ein)"
        ).fetchone()[0]
        cov = (joined / tot) if tot else 0.0
        log(f"GATE_RECON_COVERAGE: {joined:,}/{tot:,} 990-with-colA classifiable via bmf "
            f"({cov:.3f}); 03/04 evaluable={n:,}")
        # ⚠ ROUTING: this WARN is surfaced at manual/rollout invocation, but bmf erosion is a
        # STANDING concern — when the gate runs in ANY non-interactive context it MUST route this
        # through dq_router (#207) -> hc-digest Pushover, NOT the build log (#90 'dies in unread
        # log'). The COVERAGE line above is the labeled-boundary baseline (94.6%), not a defect.
        if cov < COVERAGE_FLOOR:
            log(f"GATE_RECON_WARN: bmf-join coverage {cov:.3f} < {COVERAGE_FLOOR} — reconcile "
                "rate sits over a SHRINKING base (filers dropping unclassified); check bmf freshness")
        # recalibration HARD-GATE: floor is proxy-calibrated until re-measured on col-A. [#7 finding 3]
        if not RECONCILE_FLOOR_RECALIBRATED:
            log("GATE_RECON_RED: col-A landed but RECONCILE_RATE_FLOOR still PROXY-calibrated "
                "(0.97, from B+C+D≈L18) — re-measure the real col-A COMBINED rate, set the floor, "
                "flip RECONCILE_FLOOR_RECALIBRATED. Required gate step before promotion (#7 finding 3)")
            green = False
        # (1b) on-S leg — PER returnVersion (NOT cumulative; a version break is <1% of the cumulative
        #      base, so a cumulative ratio is blind to it). Each qualifying cohort is checked on:
        #      |S|/base (group/namespace break → version falls off S), col-A coverage (TotalAmt drop),
        #      within-_TIE_TOL agreement (value-corruption cross-check). Needs returns.return_version.
        if "return_version" not in _columns(conn, "returns"):
            log("GATE_RECON_RED: on-S per-version leg can't evaluate — returns.return_version absent "
                "(fail-closed; capture returnVersion at col-A land so a per-version break can't dilute)")
            green = False
        else:
            cohorts = conn.execute(
                """
                WITH base AS (
                  SELECT r.return_version AS ver, r.total_functional_expenses AS colA,
                         r.total_expenses AS l18,
                         COALESCE(r.program_expenses,0)+COALESCE(r.management_expenses,0)
                           +COALESCE(r.fundraising_expenses,0) AS bcd
                  FROM returns r JOIN bmf b USING(ein)
                  WHERE r.return_type='990' AND b.subsection IN ('03','04') AND r.total_expenses>0
                )
                SELECT ver, COUNT(*) AS base_n,
                  SUM(CASE WHEN ABS(bcd-l18)<=:t*l18 THEN 1 ELSE 0 END) AS s_n,
                  SUM(CASE WHEN ABS(bcd-l18)<=:t*l18 AND colA IS NOT NULL AND colA>0
                           THEN 1 ELSE 0 END) AS s_cov,
                  SUM(CASE WHEN ABS(bcd-l18)<=:t*l18 AND colA IS NOT NULL AND colA>0
                            AND ABS(bcd-colA)<=:t*colA AND ABS(colA-l18)<=:t*l18
                           THEN 1 ELSE 0 END) AS s_agree
                FROM base GROUP BY ver
                """, {"t": _TIE_TOL}
            ).fetchall()
            if not cohorts:
                log("GATE_RECON_RED: on-S per-version leg — 0 col-A-bearing 03/04 rows (fail-closed)")
                green = False
            gated = skipped = skipped_filers = 0
            for ver, base_n, s_n, s_cov, s_agree in cohorts:
                base_n, s_n, s_cov, s_agree = (base_n or 0), (s_n or 0), (s_cov or 0), (s_agree or 0)
                if base_n < ONS_MIN_COHORT_N:
                    skipped += 1; skipped_filers += base_n
                    continue
                gated += 1
                s_frac = s_n / base_n
                if s_frac < ONS_VERSION_SBASE_FLOOR:
                    log(f"GATE_RECON_RED: returnVersion {ver!r} |S|/base {s_frac:.4f} < "
                        f"{ONS_VERSION_SBASE_FLOOR} ({s_n:,}/{base_n:,}) — B/C/D broke for this "
                        "version, pushing it off S (group/namespace break; undiluted per-version)")
                    green = False
                cov = (s_cov / s_n) if s_n else 0.0
                if cov < ONS_COVERAGE_FLOOR:
                    log(f"GATE_RECON_RED: returnVersion {ver!r} col-A COVERAGE {cov:.5f} < "
                        f"{ONS_COVERAGE_FLOOR} over {s_n:,} S filers — TotalAmt dropped for this "
                        "version while it stays self-consistent (PRIMARY leg)")
                    green = False
                agr = (s_agree / s_cov) if s_cov else 0.0
                if agr < ONS_AGREEMENT_FLOOR:
                    log(f"GATE_RECON_RED: returnVersion {ver!r} col-A AGREEMENT {agr:.5f} < "
                        f"{ONS_AGREEMENT_FLOOR} within _TIE_TOL={_TIE_TOL} over {s_cov:,} — value "
                        "corruption >tol on S (cross-check; element-source railed elsewhere)")
                    green = False
            # never silently ungate: a meaningful filing-share sitting in sub-threshold cohorts WARNs.
            if skipped_filers and skipped_filers > 0.05 * sum((c[1] or 0) for c in cohorts):
                log(f"GATE_RECON_WARN: {skipped_filers:,} filers in {skipped} sub-{ONS_MIN_COHORT_N} "
                    "returnVersion cohorts are UNGATED by the on-S per-version leg (too small to gate "
                    "without sampling-noise false-positives) — new/rare versions; revisit as they grow")
            if green and gated:
                log(f"GATE_RECON_ONS_OK: {gated} returnVersion cohorts gated (≥{ONS_MIN_COHORT_N}); "
                    f"per-version col-A coverage/agreement/|S|-fraction all above derived floors")
    # (2) CONTRACTOR BUCKET-RATE MONITOR (§4 classify-not-gate, 2026-06-28) — REPLACES the old per-record
    #     `listed > cnt` tie, which was a CATEGORY ERROR ([[feedback_cross_field_gate_classify_before_
    #     gating]], instance 1): `cnt` (CntrctRcvdGreaterThan100KCnt, a reported scalar) and the contractor
    #     detail rows (a top-list filers populate LOOSELY — NULL comps, sub-threshold rows, literal
    #     <PersonNm>NONE</PersonNm> placeholders) are two INDEPENDENT filer-controlled disclosures with NO
    #     schema-enforced relationship. The old tie RED'd "structural impossibility (parse bug)" on what is
    #     endemic FILER inconsistency — 32/8,000 on real data, every one (c)-hand-verified filer-side
    #     (cnt + comp both read correctly off the XML; e.g. 202200219349300490 lists 5 ≥$100k contractors
    #     with cnt=1). The build has no standing to RED on filer error. So CLASSIFY into buckets + rate-
    #     monitor each, NEVER per-record. Three PER-returnVersion ceilings (a version whose cnt/comp/child-
    #     path drifts is <1% of the corpus → a CUMULATIVE rate dilutes it = round-3's denominator bug). ALL
    #     THREE are ENDEMIC-nonzero filer-behaviour floors → all three are DRIFT-bounded (`_drift_ceil`,
    #     floor×(1+margin), n-INDEPENDENT) — a rule-of-three (floor+3/n) margin VANISHES at corpus scale
    #     and reds on normal variation (review, 2026-06-28; the near-zero framing was the systematic error):
    #       • DROP  (cnt>0 ⟹ rows>0)        — wrapper/all-slot parse drop above the ~1.4% filer-omission floor.
    #       • VIOLATION (clean_cnt>cnt)      — cnt-misread above the ~0.3% filer-miscount floor.
    #       • INDETERMINATE (≥1 NULL-comp)   — mass comp-extraction regression above the ~0.6-1.8% omission floor.
    #     The MARGIN is PER-BUCKET, set at land from each bucket's cross-version variance (review pass 3) —
    #     NOT a uniform ×2 (uniform-across-buckets was the failure; scratch spreads differ 1.8×/4.2×/sparse).
    #     Each ceiling is a MASS/version-level detector: a sub-(1+margin) SHAPE-conditional bug (e.g. a
    #     double-count on nested groups → VIOLATION, a comp-drop on a sub-shape → INDETERMINATE) sits in the
    #     [floor, floor×(1+margin)] blind spot and is caught (only) by the stratified row_count/value
    #     WITNESSES — so witness stratification MUST cover those shapes (a stated, bounded gap to close at land).
    #     (a) clean_cnt = #contractor rows with comp>100000 (STRICTLY >, matching IRS Line 2's "more than
    #     $100,000" + the filer's own cnt basis — review item 1, 2026-06-28); comp<=100000 excluded;
    #     comp IS NULL → indeterminate (neither counted nor dropped).
    #     Bucket-2 filers (clean_cnt>cnt) carry a NON-gating per-record DQ flag (b) — disclosed, not blocked.
    if "contractors_over_100k_cnt" not in _columns(conn, "returns") \
            or not _table_exists(conn, "contractors"):
        log("GATE_RECON_RED: contractor bucket monitor can't evaluate — "
            "contractors_over_100k_cnt / contractors absent (fail-closed)")
        green = False
    else:
        # (2a/2b/2d) per-version bucket monitor — needs return_version (the un-dilutable grouping key).
        if "return_version" not in _columns(conn, "returns"):
            log("GATE_RECON_RED: contractor bucket monitor can't go per-version — returns.return_version "
                "absent (fail-closed; capture returnVersion at land so a per-version drift can't dilute)")
            green = False
        else:
            # ONE per-version pass (shared with audit_floor_measurements via contractor_bucket_stats).
            cstats = contractor_bucket_stats(conn)
            total_base = sum((row[1] or 0) for row in cstats)
            total_cnt_pos = sum((row[2] or 0) for row in cstats)
            total_viol = sum((row[4] or 0) for row in cstats)
            if total_base == 0:
                # The contractor count column landed but holds 0 cnt-not-NULL 990 rows → can't certify
                # ANY bucket. Fail-closed, NOT a vacuous pass (the LAND headline's zero-evaluable trap).
                log("GATE_RECON_RED: contractor bucket monitor — 0 cnt-not-NULL 990 filers in scope "
                    "(the count column landed but is entirely NULL? fail-closed; not a vacuous pass)")
                green = False
            else:
                # c_green tracks the BUCKET monitor's OWN verdict (independent of other recon legs) so the
                # CONTRACTOR_OK line is green-for-the-right-reason about the buckets, not the whole gate.
                c_green = True
                # PER-LEG fail-closed PENDING: each ceiling independently refuses until measured at land
                # (a floor cannot ship a round guess). DROP only needs its ceil if cnt>0 filers exist.
                v_pending = CONTRACTOR_VIOLATION_CEIL is None
                i_pending = CONTRACTOR_INDETERMINATE_CEIL is None
                d_pending = (total_cnt_pos > 0) and (CONTRACTOR_DROP_CEIL is None)
                if v_pending:
                    log("GATE_RECON_RED: CONTRACTOR_VIOLATION_CEIL PENDING — measure the (clean_cnt>cnt) "
                        "rate on built 990 contractors at the worst gated version, set the args, flip "
                        "(fail-closed; DRIFT bound on the endemic filer-miscount floor — same family as DROP/INDETERMINATE)")
                    green = c_green = False
                if i_pending:
                    log("GATE_RECON_RED: CONTRACTOR_INDETERMINATE_CEIL PENDING — measure the (c)-verified "
                        "baseline NULL-comp omission rate on built 990 contractors, set the args, flip "
                        "(fail-closed; DRIFT bound on a nonzero floor — the (c) hand-verify gates this number)")
                    green = c_green = False
                if d_pending:
                    log("GATE_RECON_RED: CONTRACTOR_DROP_CEIL PENDING — measure the legit (cnt>0, 0-rows) "
                        "exception rate on built 990 contractors, set the args, flip (fail-closed)")
                    green = c_green = False
                if total_cnt_pos == 0:
                    # No filer reports cnt>0 → the DROP leg has nothing to assert (legit empty positive
                    # population). VIOLATION/INDETERMINATE still run on base_n (a cnt=0 filer with a ≥$100k
                    # row IS a violation). Mass all-zero-cnt post-land is caught by the §5 deploy assertion.
                    log("GATE_RECON_NOTE: contractor DROP leg — 0 Form-990 filers with cnt>0 in scope "
                        "(not redding on an empty positive-count population; VIOLATION/INDETERMINATE active)")
                # per-version gating: VIOLATION+INDETERMINATE key on base_n, DROP keys on cnt_pos.
                gated_b = skipped_base = gated_d = 0
                for ver, base_n, cnt_pos, dropped, violations, indeterminate in cstats:
                    base_n = base_n or 0; cnt_pos = cnt_pos or 0; dropped = dropped or 0
                    violations = violations or 0; indeterminate = indeterminate or 0
                    if base_n >= ONS_MIN_COHORT_N:
                        gated_b += 1
                        if not v_pending:
                            vr = violations / base_n
                            if vr > CONTRACTOR_VIOLATION_CEIL:
                                log(f"GATE_RECON_RED: returnVersion {ver!r} contractor VIOLATION rate "
                                    f"{vr:.5f} ({violations:,}/{base_n:,}) > ceiling {CONTRACTOR_VIOLATION_CEIL} "
                                    "— clean_cnt>cnt above the filer-error baseline (cnt-misread / row-fabrication "
                                    "for THIS version, not endemic filer inconsistency)")
                                green = c_green = False
                        if not i_pending:
                            ir = indeterminate / base_n
                            if ir > CONTRACTOR_INDETERMINATE_CEIL:
                                log(f"GATE_RECON_RED: returnVersion {ver!r} contractor INDETERMINATE rate "
                                    f"{ir:.5f} ({indeterminate:,}/{base_n:,}) > ceiling "
                                    f"{CONTRACTOR_INDETERMINATE_CEIL} — NULL-comp rows above the drift floor "
                                    "(mass comp-extraction regression for THIS version, not ordinary omission)")
                                green = c_green = False
                    else:
                        skipped_base += base_n
                        # HOT-COHORT WARN (maintainer 2026-07-03): a sub-threshold cohort whose measured rate
                        # ALREADY exceeds its bucket ceiling logs a WARN regardless of share —
                        # informational only (tiny-n sampling noise is expected; #267 is the real fix).
                        # Closes most of the n<200 blind window without a false-red surface.
                        if base_n > 0 and not v_pending and (violations / base_n) > CONTRACTOR_VIOLATION_CEIL:
                            log(f"GATE_RECON_WARN: HOT sub-{ONS_MIN_COHORT_N} cohort {ver!r} — VIOLATION "
                                f"rate {violations/base_n:.5f} ({violations}/{base_n}) exceeds ceiling "
                                f"{CONTRACTOR_VIOLATION_CEIL} (UNGATED; informational hot-cohort watch, #267)")
                        if base_n > 0 and not i_pending and (indeterminate / base_n) > CONTRACTOR_INDETERMINATE_CEIL:
                            log(f"GATE_RECON_WARN: HOT sub-{ONS_MIN_COHORT_N} cohort {ver!r} — INDETERMINATE "
                                f"rate {indeterminate/base_n:.5f} ({indeterminate}/{base_n}) exceeds ceiling "
                                f"{CONTRACTOR_INDETERMINATE_CEIL} (UNGATED; informational hot-cohort watch, #267)")
                    if total_cnt_pos > 0 and not d_pending and cnt_pos >= ONS_MIN_COHORT_N:
                        gated_d += 1
                        drop_rate = dropped / cnt_pos
                        if drop_rate > CONTRACTOR_DROP_CEIL:
                            log(f"GATE_RECON_RED: returnVersion {ver!r} contractor COVERAGE collapse — "
                                f"{dropped:,}/{cnt_pos:,} ({drop_rate:.5f}) cnt>0 filers list 0 rows > ceiling "
                                f"{CONTRACTOR_DROP_CEIL} (ContractorName-wrapper / child-path drift for THIS version)")
                            green = c_green = False
                    elif total_cnt_pos > 0 and not d_pending and 0 < cnt_pos < ONS_MIN_COHORT_N \
                            and (dropped / cnt_pos) > CONTRACTOR_DROP_CEIL:
                        log(f"GATE_RECON_WARN: HOT sub-{ONS_MIN_COHORT_N} cohort {ver!r} — DROP rate "
                            f"{dropped/cnt_pos:.5f} ({dropped}/{cnt_pos}) exceeds ceiling "
                            f"{CONTRACTOR_DROP_CEIL} (UNGATED; informational hot-cohort watch, #267)")
                # never silently ungate: a meaningful filing-share in sub-threshold cohorts WARNs (on-S leg pattern).
                if skipped_base and skipped_base > 0.05 * total_base:
                    log(f"GATE_RECON_WARN: {skipped_base:,} filers in sub-{ONS_MIN_COHORT_N} returnVersion "
                        "cohorts are UNGATED by the contractor bucket monitor (too small to gate without "
                        "sampling-noise false-positives) — new/rare versions; revisit as they grow")
                # (b) per-record DQ DISCLOSURE — bucket-2 count, NON-gating (the flag travels with the
                #     record; this is the count for transparency, NOT a RED). No review queue (downstream
                #     fact answered: as-filed surface, no consistency contract — DECISION §Downstream).
                if total_viol:
                    log(f"GATE_RECON_NOTE: {total_viol:,} filers carry a contractor-count DQ flag "
                        "(clean_cnt>cnt; self-contradictory AS FILED — disclosed per-record, NON-gating)")
                if c_green and (gated_b or gated_d):
                    log(f"GATE_RECON_CONTRACTOR_OK: {gated_b} cohorts gated on VIOLATION/INDETERMINATE, "
                        f"{gated_d} on DROP (≥{ONS_MIN_COHORT_N}); all bucket rates within derived ceilings")
        # (2c) POPULATION / cross-form ORPHAN — the denominator axis: contractor rows must only exist
        #      for the in-scope forms (990 from this parse, 990PF from extract_990pf_detail). A row for
        #      a 990-EZ (no Part VII Sec B table) or an unknown return_type = the parse emitting outside
        #      its population (selection-predicate leak). Catches out-of-scope contamination corpus-wide.
        orphans = conn.execute(
            """
            SELECT COUNT(*) FROM contractors c
            LEFT JOIN returns r ON r.object_id=c.object_id
            WHERE r.return_type IS NULL OR r.return_type NOT IN ('990','990PF')
            """
        ).fetchone()[0]
        if orphans:
            log(f"GATE_RECON_RED: {orphans:,} contractor rows are ORPHAN/out-of-scope — object_id is "
                "not a 990 or 990PF return (990-EZ has no contractor table; the parse leaked its population)")
            green = False
    # (3) highest-comp flag domain + NULL-for-non-990 (manifest §D).
    if "is_highest_compensated_employee" not in _columns(conn, "officers"):
        log("GATE_RECON_RED: is_highest_compensated_employee absent (fail-closed)")
        green = False
    else:
        dom, nonr = conn.execute(
            """
            SELECT
              SUM(CASE WHEN o.is_highest_compensated_employee NOT IN (0,1)
                        AND o.is_highest_compensated_employee IS NOT NULL THEN 1 ELSE 0 END),
              SUM(CASE WHEN r.return_type!='990'
                        AND o.is_highest_compensated_employee IS NOT NULL THEN 1 ELSE 0 END)
            FROM officers o JOIN returns r USING(object_id)
            """
        ).fetchone()
        dom, nonr = (dom or 0), (nonr or 0)
        if dom or nonr:
            log(f"GATE_RECON_RED: flag domain-violations={dom:,}, non-990-not-NULL={nonr:,}")
            green = False
    if green:
        log("GATE_RECON_GREEN: col-A band + contractor tie + flag domain all clean")
    return green


# Witness ARTIFACT — a SEPARATELY-REVIEWED, SIGNED-OFF unit (NOT just literals dropped in).
# [#7 BLOCKING finding 2026-06-26]: at parser-land, reconciliation passes (self-consistent
# arithmetic) and the witness is RED-because-empty; the pressure is to populate it to clear
# the red. If those literals are transcribed casually (or by whoever watched the parser),
# the witness goes green while HOLLOW and the code cannot tell. So "clearing the red" and
# "the witness is trustworthy" are SPLIT: the witness goes green only if it loads an artifact
# carrying an independent SIGN-OFF — a dated, attributable claim ("these literals are what the
# filings say, transcribed BLIND to parser output") reviewed on its own, separately from the
# suite. Per-fixture provenance (`source` + `transcribed_blind=True`) is mandatory. The sign-
# off is forgeable but ATTRIBUTABLE; the transcription is its OWN reviewed unit, produced
# before/independent of the parser — NOT folded into parser-writing. Absent/unsigned == RED.
WITNESS_ARTIFACT_PATH = Path(__file__).resolve().parent / "witness_fixtures_990.json"


def _load_witness_artifact():
    import json
    if not WITNESS_ARTIFACT_PATH.exists():
        return None
    try:
        return json.loads(WITNESS_ARTIFACT_PATH.read_text())
    except Exception:
        return None


def _witness_artifact_signed(art, log) -> bool:
    """RED unless the artifact carries a complete sign-off AND per-fixture blind-provenance."""
    if not art:
        log("GATE_WITNESS_RED: no witness artifact (witness_fixtures_990.json) — the "
            "transcription is a SEPARATE reviewed unit; absent is fail-closed RED")
        return False
    so = art.get("signoff") or {}
    if not (so.get("reviewer") and so.get("date") and so.get("attestation")):
        log("GATE_WITNESS_RED: artifact present but UNSIGNED — needs an independent sign-off "
            "(reviewer/date/attestation) reviewed separately from the suite. Literals alone do "
            "NOT clear the red (the blocking-finding control).")
        return False
    fx = art.get("fixtures") or []
    if not fx:
        log("GATE_WITNESS_RED: signed artifact has 0 fixtures — certifies nothing (fail-closed)")
        return False
    for w in fx:
        if not w.get("source") or w.get("transcribed_blind") is not True or not w.get("covers"):
            log(f"GATE_WITNESS_RED: fixture {w.get('object_id')!r} lacks source / "
                "transcribed_blind=true / covers — blind-provenance AND a stratification "
                "rationale ('which branch this filing exercises') are mandatory per fixture, "
                "so the sign-off reviewer sees the set covers the failure surface, not 5 easy ones")
            return False
    return True


def newfields_witnesses(conn, log=print, artifact=None) -> bool:
    art = _load_witness_artifact() if artifact is None else artifact
    if not _witness_artifact_signed(art, log):
        return False
    green = True
    for w in art["fixtures"]:
        oid, table, kind = w["object_id"], w["table"], w["kind"]
        if not _table_exists(conn, table):
            log(f"GATE_WITNESS_RED: table {table} absent — can't witness {oid} (fail-closed)")
            green = False
            continue
        # MULTI-ROW kind: row_count asserts the full row-SET size for an object_id. This is how the
        # contractor SLOT-DIFFERENTIAL lives in the harness (the scalar value/absent kinds, which read
        # rows[0][0], cannot): a broken name-slot DROPS rows, so the count reds — no slot column needed,
        # missing rows ARE the signal. (Hendrick=5 all-PersonNm, St Luke's=5 all-BusinessName: break
        # either slot read → that filing's count != expected → RED.)
        if kind == "row_count":
            n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE object_id=?", (oid,)).fetchone()[0]
            if n != w["expected"]:
                log(f"GATE_WITNESS_RED: row_count witness {oid} {table} — parser={n} != hand-read "
                    f"{w['expected']} (source {w['source']}) → rows DROPPED/added on a stratified "
                    "filing (e.g. ContractorName-wrapper slot drop)")
                green = False
            continue
        # scalar / set-membership kinds need a column.
        col = w["column"]
        if col not in _columns(conn, table):
            log(f"GATE_WITNESS_RED: {table}.{col} absent — can't witness {oid} (fail-closed)")
            green = False
            continue
        if kind == "row_contains":
            # value-correctness across a row-SET (order non-deterministic): the hand-read value must
            # appear in SOME row for this object_id (e.g. a specific contractor name/comp was parsed).
            hit = conn.execute(f"SELECT 1 FROM {table} WHERE object_id=? AND {col}=? LIMIT 1",
                               (oid, w["expected"])).fetchone()
            if not hit:
                log(f"GATE_WITNESS_RED: row_contains witness {oid} {table}.{col} — hand-read "
                    f"{w['expected']!r} (source {w['source']}) not found in any row → value not parsed")
                green = False
            continue
        if kind == "keyed_value":
            # per-PERSON / per-keyed-row value: the row identified by (object_id, key_column=key) must
            # have the expected value in `column`. The scalar `value` kind reads rows[0][0] and cannot
            # target a person in a multi-row table — this is how the HCE-flag INDEPENDENCE claim ("HCE
            # read from its own box, NOT suppressed by another role") gets witnessed: assert a specific
            # MULTI-ROLE person (Brenda Morris: trustee+officer+HCE) has is_highest_compensated_employee=1,
            # so an `if officer then not HCE` inference reds. Also per-person comp (Susan Wade col-D).
            got = conn.execute(f"SELECT {col} FROM {table} WHERE object_id=? AND {w['key_column']}=?",
                               (oid, w["key"])).fetchall()
            if not got:
                log(f"GATE_WITNESS_RED: keyed_value witness {oid} {table}.{col} — no row where "
                    f"{w['key_column']}={w['key']!r} (source {w['source']}) → keyed row absent")
                green = False
            elif got[0][0] != w["expected"]:
                log(f"GATE_WITNESS_RED: keyed_value witness {oid} {table}.{col} for "
                    f"{w['key_column']}={w['key']!r} — parser={got[0][0]!r} != hand-read {w['expected']!r} "
                    f"(source {w['source']}) → wrong value (e.g. role-box inference suppressing HCE)")
                green = False
            continue
        rows = conn.execute(f"SELECT {col} FROM {table} WHERE object_id=?", (oid,)).fetchall()
        if not rows:
            log(f"GATE_WITNESS_RED: witness object_id {oid} not in {table} (fail-closed)")
            green = False
            continue
        if kind == "absent":
            if any(r[0] is not None for r in rows):
                log(f"GATE_WITNESS_RED: absence witness {oid} {table}.{col} — expected NULL "
                    f"(element absent per {w['source']}), got non-NULL → parser captured a "
                    "field the filing does not have")
                green = False
        elif kind == "value":
            if rows[0][0] != w["expected"]:
                log(f"GATE_WITNESS_RED: value witness {oid} {table}.{col} — parser={rows[0][0]!r} "
                    f"!= hand-read {w['expected']!r} (source {w['source']}) → wrong element/value")
                green = False
        else:
            log(f"GATE_WITNESS_RED: unknown witness kind {kind!r} for {oid} (fail-closed)")
            green = False
    if green:
        log(f"GATE_WITNESS_GREEN: {len(art['fixtures'])} signed witnesses match primary-filing "
            f"values (signoff: {art['signoff']['reviewer']} {art['signoff']['date']})")
    return green


# Declaration-boundary defense (manifest §G): every expansion column that EXISTS must
# map to a covering invariant or sit on the short, justified allowlist; an existing
# UNCOVERED column REDs — turns a silent omission into a fail-closed refusal.
_EXPANSION_COLUMNS = {
    "returns": ["total_functional_expenses", "contractors_over_100k_cnt", "return_version"],
    "officers": ["is_highest_compensated_employee"],
    # B's columns (returns_governance/_checklist, the 5 role flags, Part-I summary)
    # get added here when B is decided — each then needs a covering invariant or an
    # allowlist entry, or this guard REDs.
}
_COLUMN_COVERAGE = {  # column -> the registered invariant that validates it
    "total_functional_expenses": "newfields_reconciliation",
    "contractors_over_100k_cnt": "newfields_reconciliation",
    "is_highest_compensated_employee": "newfields_reconciliation",
    "return_version": "return_version_integrity",
}
_DRIFT_ALLOWLIST: set = set()  # intentionally-unguarded columns — KEEP SHORT, justify each

# AFFINITY — the type check, RELOCATED to the schema (green-pass §3, 2026-06-28). The witness value-
# compare must NOT carry a type check (a TEXT-affinity numeric column stores '100' uncoerced and a
# value-blind compare would pass a string-typed regression); the load-bearing place is the column's
# declared affinity. A numeric expansion column declared without INTEGER affinity (or no type → BLOB)
# stores strings uncoerced — this REDs it at the schema boundary, where the bug actually lives.
_EXPANSION_AFFINITY = {
    "returns": {"total_functional_expenses": "INTEGER", "contractors_over_100k_cnt": "INTEGER",
                "return_version": "TEXT"},
    "officers": {"is_highest_compensated_employee": "INTEGER"},
}


def _affinity(decl):
    """SQLite column affinity from a declared type string (the actual affinity rules, not a literal
    match): contains INT → INTEGER; CHAR/CLOB/TEXT → TEXT; BLOB or empty → BLOB; REAL/FLOA/DOUB → REAL;
    else NUMERIC."""
    d = (decl or "").upper()
    if "INT" in d:
        return "INTEGER"
    if any(x in d for x in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if d == "" or "BLOB" in d:
        return "BLOB"
    if any(x in d for x in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def newfields_schema_drift_guard(conn, log=print) -> bool:
    green = True
    for table, cols in _EXPANSION_COLUMNS.items():
        have = _columns(conn, table)
        for col in cols:
            if col not in have or col in _DRIFT_ALLOWLIST:
                continue  # not landed yet, or deliberately unguarded
            if _COLUMN_COVERAGE.get(col) not in _REGISTERED_INVARIANTS:
                log(f"GATE_DRIFT_RED: expansion column {table}.{col} exists but is not covered "
                    f"by a registered invariant (cov={_COLUMN_COVERAGE.get(col)!r}) nor allowlisted")
                green = False
    # AFFINITY assertion (§3): every landed expansion column has the expected affinity.
    for table, affs in _EXPANSION_AFFINITY.items():
        decl = {r[1]: r[2] for r in conn.execute(f"PRAGMA table_info({table})")} if _table_exists(conn, table) else {}
        for col, want in affs.items():
            if col in decl and _affinity(decl[col]) != want:
                log(f"GATE_DRIFT_RED: {table}.{col} affinity {_affinity(decl[col])!r} (declared "
                    f"{decl[col]!r}) != expected {want!r} — a numeric column without INTEGER affinity "
                    "stores strings UNCOERCED (the type check the witness value-compare must not carry)")
                green = False
    if green:
        log("GATE_DRIFT_GREEN: every landed expansion column maps to a registered invariant + has the "
            "expected affinity")
    return green


# ── Invariant registry (the fail-closed core) ───────────────────────────────
# What is IMPLEMENTED and registered: invariant key -> callable(conn, log) -> bool.
_REGISTERED_INVARIANTS = {
    "baseline_comp_nullability": assert_baseline_green,
    "newfields_reconciliation": newfields_reconciliation,
    "newfields_witnesses": newfields_witnesses,
    "newfields_schema_drift_guard": newfields_schema_drift_guard,
    "return_version_integrity": return_version_integrity,
}

# What each stage REQUIRES before it may promote. Stages >= 1 require the new-field
# suite, which is DECLARED here even though it is not yet implemented. THAT is what
# makes the gate fail CLOSED: a required-but-unregistered invariant refuses
# promotion, rather than the gate passing on the subset it happens to know. As task
# #4 implements each new-field invariant it gets added to _REGISTERED_INVARIANTS;
# until then, any stage >= 1 cannot pass.
# newfields_schema_drift_guard defends the DECLARATION boundary (added 2026-06-19):
# _REQUIRED_BY_STAGE is a hand-maintained list, so a needed invariant that is never
# DECLARED would pass silently — the same undefended-human-artifact class this gate
# exists to kill, relocated one level up. This guard (implemented at schema-land time)
# must RED when any expansion-table column exists with no registered invariant and not
# on an explicit intentionally-unguarded allowlist — turning a silent omission into a
# fail-closed refusal. Declared-required-but-unregistered NOW so the gate refuses the
# full-capture expansion until that defense is actually built (not banked in a doc).
_NEWFIELD_INVARIANTS = [
    "newfields_reconciliation",
    "newfields_witnesses",
    "newfields_schema_drift_guard",
    "return_version_integrity",
]
_REQUIRED_BY_STAGE = {
    0: [],  # fixture corpus — dev sandbox, no baseline gate
    1: ["baseline_comp_nullability", *_NEWFIELD_INVARIANTS],
    2: ["baseline_comp_nullability", *_NEWFIELD_INVARIANTS],
    3: ["baseline_comp_nullability", *_NEWFIELD_INVARIANTS],
    4: ["baseline_comp_nullability", *_NEWFIELD_INVARIANTS],
}


def promotion_gate(conn: sqlite3.Connection, stage: int, log=print) -> None:
    """HARD, FAIL-CLOSED precondition on promoting a stage. Raises SystemExit unless
    EVERY invariant that stage REQUIRES is both registered AND green.

    Fail-closed is the whole point: a stage that introduces new fields requires
    invariants that are not yet implemented (_NEWFIELD_INVARIANTS); because they are
    required-but-unregistered, this REFUSES — it does not pass on the baseline it
    happens to know. There is no "wave it through": a SystemExit cannot be overridden
    by momentum, and an empty/partial invariant set refuses rather than greenlights.
    Mirrors the build_gated()/GATE_HOLD idiom.
    """
    required = _REQUIRED_BY_STAGE.get(stage)
    if required is None:
        raise SystemExit(
            f"GATE_HOLD: unknown stage {stage!r} — refusing (fail-closed; declare it "
            f"in _REQUIRED_BY_STAGE before use)."
        )
    for key in required:
        check = _REGISTERED_INVARIANTS.get(key)
        if check is None:
            raise SystemExit(
                f"GATE_HOLD: stage {stage} requires invariant '{key}', which is NOT "
                f"registered/implemented — refusing to promote (fail-closed). Implement "
                f"it and add it to _REGISTERED_INVARIANTS before this stage can pass."
            )
        if not check(conn, log=log):
            raise SystemExit(
                f"GATE_HOLD: invariant '{key}' RED — refusing to promote to stage {stage}."
            )
    log(f"GATE_PROMOTE_OK: stage {stage} — all required invariants registered + green "
        f"({', '.join(required) or 'none'}).")


def main() -> int:
    # argv[1] = optional DB-path override so the build can gate the freshly-built
    # public DB ($PUBLIC_DB); default DB_PATH is the source 990data.db. Wired as
    # the 5th validate-before-deploy gate in update.sh (#232). Backward-compatible:
    # a no-arg invocation still gates DB_PATH (the manual-run behaviour).
    db = sys.argv[1] if len(sys.argv) > 1 else DB_PATH
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return 0 if assert_baseline_green(conn) else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
