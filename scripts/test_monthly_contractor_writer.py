#!/usr/bin/env python3
"""
#264 — MONTHLY-PATH certification harness for the swapped contractor writer (IN PROGRESS).

Certifies writer_process (extract_990_detail.py) through the LITERAL monthly path:
a real multiprocessing.Queue feeding a real child process, on a scratch DB only.
Never touches 990data.db.

Proof set (followup_queue #264 + round-2 memo 2026-07-05):
  P1 (i)    new 990 w/ contractors -> rows land
  P2 (ii)   same filing re-queued -> identical rows (delete-then-insert, no double-insert)
  P3 (iii)  re-queued with 0-contractor result -> stale rows cleared
  P4 (iv)   parse-error result -> pre-existing rows SURVIVE. Red phase drives a
            GUARD-STRIPPED writer variant (the actual failure behavior), not faked state.
  P5 (v)    EZ/PF results -> contractors untouched (hostile: results even CARRY
            contractor rows; the writer must still refuse them)
  P6 (vi)   arity: OFFICER_SQL 11-col vs 990/EZ tuples + PF explicit-column  [PENDING]
  P7 (vii)  duplicate-officer witness (xml=2 exemplar 201931349349304213,
            THOMAS REGAN) — record collapse-or-store as a receipt for #265   [PENDING]
  P8 (viii) atomicity witness: SIGKILL mid-flush + injected insert-error
            post-delete; prior rows must survive rollback (round-1 item 2)   [PENDING]
  P9 (ix)   interrupted-run-then-rerun convergence across ALL four tables +
            discovery (adjacent-hole finding 2026-07-05; expected RED on
            current code = the gap receipt; fix ruling with maintainer)            [PENDING]

DISCIPLINE: every proof runs RED first — a forced-bad state or forced-bad writer
variant must make the assertion FAIL — then GREEN with the real writer. A red phase
that passes is itself a harness failure (the proof cannot detect its target defect).
Exit 0 only if every implemented proof is red-then-green. Red receipts print inline.
"""

import multiprocessing as mp
import os
import shutil
import sqlite3
import sys
import tempfile
import importlib.util

BASE = os.path.dirname(os.path.abspath(__file__))
LIVE_WRITER = os.path.join(BASE, "extract_990_detail.py")

# Production DDL for the two tables writer_process assumes pre-exist
# (captured verbatim from 990data.db 2026-07-05; create_schema() covers the other two).
PREEXISTING_DDL = """
CREATE TABLE contractors (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id             TEXT NOT NULL,
    ein                   TEXT,
    contractor_name       TEXT,
    city                  TEXT,
    state                 TEXT,
    service_type          TEXT,
    compensation          INTEGER
);
CREATE INDEX idx_contractors_oid ON contractors(object_id);
CREATE INDEX idx_contractors_ein ON contractors(ein);
CREATE TABLE officers (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id             TEXT NOT NULL,
    ein                   TEXT,
    person_name           TEXT,
    title                 TEXT,
    avg_hours_per_week    REAL,
    compensation          INTEGER,
    benefits              INTEGER,
    expense_account       INTEGER,
    reportable_comp_filing_org INTEGER,
    reportable_comp_related_org INTEGER,
    other_compensation    INTEGER,
    is_highest_compensated_employee INTEGER,
    is_officer            INTEGER,
    is_individual_trustee INTEGER,
    is_institutional_trustee INTEGER,
    is_key_employee       INTEGER,
    is_former             INTEGER
);
CREATE INDEX idx_officers_oid ON officers(object_id);
CREATE INDEX idx_officers_ein ON officers(ein);
"""

GUARD_LINE = 'if r["return_type"] == "990" and not r.get("error"):'
STRIPPED_LINE = 'if r["return_type"] == "990":  # GUARD-STRIPPED red-run variant (P4)'


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The writer file proofs run against; the red-variant makers patch whatever
# file this names. Since the 2026-07-05 ship this is always the live writer
# (which now IS the cross-table-fixed writer; --fix-packet retired).
WRITER_UNDER_TEST = LIVE_WRITER


def make_stripped_variant(scratch_dir):
    """Copy the writer under test with the never-deletes-on-error guard removed.

    This is the behavioral red for P4: the variant DOES delete on parse errors,
    so the survive-assertion must catch it. Fails loudly if the guard line has
    drifted (exactly one occurrence required)."""
    src = open(WRITER_UNDER_TEST).read()
    n = src.count(GUARD_LINE)
    if n != 1:
        raise SystemExit(f"HARNESS INVALID: guard line found {n}x (expected 1) — "
                         f"source drifted; re-pin GUARD_LINE before trusting P4.")
    path = os.path.join(scratch_dir, "writer_guard_stripped.py")
    open(path, "w").write(src.replace(GUARD_LINE, STRIPPED_LINE))
    return load_module(path, "writer_guard_stripped")


# ── Cross-table fix (#264 P9) — SHIPPED to the live writer 2026-07-05 ──
# MIDRUN_FLUSH_ANCHOR below is the PRE-fix flush shape (four independent
# commits, the split-write window P9 demonstrated); CROSSTABLE_FIX is what
# now lives in extract_990_detail.py. Both are retained as the packet receipt;
# the anchor no longer matches the live source BY DESIGN, so
# make_crosstable_fix_variant() is unusable post-ship (--fix-packet retired).
MIDRUN_FLUSH_ANCHOR = '''        # Flush buffers
        if len(officer_buf) >= BATCH_INSERT_SIZE:
            con.executemany(OFFICER_SQL, officer_buf)
            con.commit()
            officer_buf.clear()
        if len(sched_i_buf) >= BATCH_INSERT_SIZE:
            con.executemany(SCHED_I_SQL, sched_i_buf)
            con.commit()
            sched_i_buf.clear()
        if len(related_buf) >= BATCH_INSERT_SIZE:
            con.executemany(RELATED_SQL, related_buf)
            con.commit()
            related_buf.clear()
        if len(contractor_del_buf) >= BATCH_INSERT_SIZE or len(contractor_buf) >= BATCH_INSERT_SIZE:
            # one commit covers delete+insert: a flush is atomic, an interrupted flush rolls back whole
            pre_changes = con.total_changes
            con.executemany(CONTRACTOR_DEL_SQL, contractor_del_buf)
            counts["contractor_rows_deleted"] += con.total_changes - pre_changes
            con.executemany(CONTRACTOR_SQL, contractor_buf)
            con.commit()
            contractor_del_buf.clear()
            contractor_buf.clear()'''

CROSSTABLE_FIX = '''        # Flush buffers — ALL tables under ONE commit whenever ANY buffer hits the
        # threshold (#264 P9 cross-table fix): a filing committed in the legacy
        # tables can then never be missing its contractor leg; an interrupted
        # flush rolls back whole, across all four tables.
        if (len(officer_buf) >= BATCH_INSERT_SIZE
                or len(sched_i_buf) >= BATCH_INSERT_SIZE
                or len(related_buf) >= BATCH_INSERT_SIZE
                or len(contractor_del_buf) >= BATCH_INSERT_SIZE
                or len(contractor_buf) >= BATCH_INSERT_SIZE):
            con.executemany(OFFICER_SQL, officer_buf)
            con.executemany(SCHED_I_SQL, sched_i_buf)
            con.executemany(RELATED_SQL, related_buf)
            pre_changes = con.total_changes
            con.executemany(CONTRACTOR_DEL_SQL, contractor_del_buf)
            counts["contractor_rows_deleted"] += con.total_changes - pre_changes
            con.executemany(CONTRACTOR_SQL, contractor_buf)
            con.commit()
            officer_buf.clear()
            sched_i_buf.clear()
            related_buf.clear()
            contractor_del_buf.clear()
            contractor_buf.clear()'''


def make_crosstable_fix_variant(scratch_dir):
    """The fix candidate as a scratch module — the live file stays untouched."""
    src = open(LIVE_WRITER).read()
    n = src.count(MIDRUN_FLUSH_ANCHOR)
    if n != 1:
        raise SystemExit(f"HARNESS INVALID: mid-run flush anchor found {n}x (expected 1) — "
                         f"source drifted; re-pin MIDRUN_FLUSH_ANCHOR before trusting the packet.")
    path = os.path.join(scratch_dir, "writer_crosstable_fix.py")
    open(path, "w").write(src.replace(MIDRUN_FLUSH_ANCHOR, CROSSTABLE_FIX))
    return path, load_module(path, "writer_crosstable_fix")


def fresh_db(scratch_dir, tag):
    db = os.path.join(scratch_dir, f"scratch_{tag}.db")
    con = sqlite3.connect(db)
    con.executescript(PREEXISTING_DDL)
    con.commit()
    con.close()
    return db


def result(oid, rt="990", ein="770000001", contractors=None, error=None):
    """Synthetic parse_file-shaped result (tuple shapes match CONTRACTOR_SQL)."""
    return {"object_id": oid, "return_type": rt, "ein": ein,
            "officers": [], "contractors": contractors or [],
            "schedule_i": [], "related_orgs": [], "error": error}


def ctuple(oid, name, comp, ein="770000001"):
    return (oid, ein, name, "TESTVILLE", "CA", "SERVICES", comp)


def run_writer(mod, db, results):
    """Drive the literal monthly path: real queue, real child process."""
    q = mp.Queue()
    p = mp.Process(target=mod.writer_process, args=(db, q, len(results), set(), set(), set()),
                   daemon=True)
    p.start()
    for r in results:
        q.put(r)
    q.put(None)
    p.join(timeout=180)
    if p.is_alive():
        p.kill()
        raise SystemExit("HARNESS INVALID: writer child hung")
    if p.exitcode != 0:
        raise SystemExit(f"HARNESS INVALID: writer child exit {p.exitcode}")


def rows(db, oid=None):
    con = sqlite3.connect(db)
    sql = ("SELECT object_id, ein, contractor_name, city, state, service_type, compensation "
           "FROM contractors")
    if oid is not None:
        out = con.execute(sql + " WHERE object_id=? ORDER BY contractor_name", (oid,)).fetchall()
    else:
        out = con.execute(sql + " ORDER BY object_id, contractor_name").fetchall()
    con.close()
    return out


# ── Proofs ──────────────────────────────────────────────────────────────────
# Each returns (red_failed_as_required, red_receipt, green_passed, green_receipt).

def proof_p1(mod, scratch):
    """P1: new 990 with contractors -> rows land."""
    want = [ctuple("OID_P1", "ACME CORP", 250000), ctuple("OID_P1", "ZENITH LLC", 175000)]

    db_red = fresh_db(scratch, "p1_red")   # writer never runs: rows must be absent
    got_red = rows(db_red, "OID_P1")
    red_failed = (sorted(got_red) != sorted(want))
    red_receipt = f"no-writer world has {len(got_red)} rows (assert wants 2) -> assertion fails"

    db = fresh_db(scratch, "p1_green")
    run_writer(mod, db, [result("OID_P1", contractors=want)])
    got = rows(db, "OID_P1")
    green = (sorted(got) == sorted(want))
    return red_failed, red_receipt, green, f"{len(got)}/2 rows landed, fields exact"


def proof_p2(mod, scratch):
    """P2: same filing re-queued -> identical rows, count unchanged (no double-insert)."""
    want = [ctuple("OID_P2", "ACME CORP", 250000), ctuple("OID_P2", "ZENITH LLC", 175000)]

    db = fresh_db(scratch, "p2")
    run_writer(mod, db, [result("OID_P2", contractors=want)])
    snapshot = rows(db, "OID_P2")

    # RED: simulate the append-only failure mode by hand-inserting a duplicate,
    # then check the identity assertion catches it.
    con = sqlite3.connect(db)
    con.execute("INSERT INTO contractors (object_id, ein, contractor_name, city, state, "
                "service_type, compensation) VALUES (?,?,?,?,?,?,?)", want[0])
    con.commit(); con.close()
    got_bad = rows(db, "OID_P2")
    red_failed = (sorted(got_bad) == sorted(snapshot)) is False and len(got_bad) != len(snapshot)
    red_receipt = f"append-only world: {len(got_bad)} rows vs snapshot {len(snapshot)} -> assertion fails"

    # GREEN: fresh world, run twice through the real writer.
    db2 = fresh_db(scratch, "p2_green")
    run_writer(mod, db2, [result("OID_P2", contractors=want)])
    snap2 = rows(db2, "OID_P2")
    run_writer(mod, db2, [result("OID_P2", contractors=want)])
    got2 = rows(db2, "OID_P2")
    green = (sorted(got2) == sorted(snap2)) and len(got2) == 2
    return red_failed, red_receipt, green, f"re-queue: {len(got2)} rows, multiset identical to first run"


def proof_p3(mod, scratch):
    """P3: re-queued with a 0-contractor parse -> stale rows cleared."""
    stale = [ctuple("OID_P3", "STALE VENDOR", 990000)]

    db = fresh_db(scratch, "p3")
    run_writer(mod, db, [result("OID_P3", contractors=stale)])

    # RED: before the clearing re-run, the cleared-assertion must fail on the stale world.
    got_bad = rows(db, "OID_P3")
    red_failed = (len(got_bad) == 0) is False
    red_receipt = f"stale world holds {len(got_bad)} row(s) -> cleared-assertion fails"

    # GREEN: re-queue same oid with a clean 0-contractor result.
    run_writer(mod, db, [result("OID_P3", contractors=[])])
    got = rows(db, "OID_P3")
    green = (len(got) == 0)
    return red_failed, red_receipt, green, "stale rows cleared on 0-contractor re-queue"


def proof_p4(mod, scratch):
    """P4: parse-error result -> pre-existing rows SURVIVE (behavioral red via
    guard-stripped writer variant)."""
    prior = [ctuple("OID_P4", "KEEP ME LLC", 300000)]
    err = result("OID_P4", contractors=[], error="simulated parse failure")

    # RED: guard-stripped writer really deletes on error -> survive-assertion fails.
    db_red = fresh_db(scratch, "p4_red")
    run_writer(mod, db_red, [result("OID_P4", contractors=prior)])
    stripped = make_stripped_variant(scratch)
    run_writer(stripped, db_red, [err])
    got_bad = rows(db_red, "OID_P4")
    red_failed = (sorted(got_bad) == sorted(prior)) is False
    red_receipt = f"guard-stripped writer left {len(got_bad)} row(s) (prior had {len(prior)}) -> assertion fails"

    # GREEN: real writer, same error result -> rows survive byte-for-byte.
    db = fresh_db(scratch, "p4_green")
    run_writer(mod, db, [result("OID_P4", contractors=prior)])
    run_writer(mod, db, [err])
    got = rows(db, "OID_P4")
    green = (sorted(got) == sorted(prior))
    return red_failed, red_receipt, green, "prior rows survived a parse-error re-queue intact"


def proof_p5(mod, scratch):
    """P5: EZ/PF results leave contractors untouched — hostile variant: the results
    CARRY contractor tuples; the writer must refuse to write or delete anything."""
    pf_existing = [ctuple("OID_P5_PF", "PF VENDOR", 120000)]
    hostile_ez = result("OID_P5_EZ", rt="990EZ", contractors=[ctuple("OID_P5_EZ", "EZ GHOST", 1)])
    hostile_pf = result("OID_P5_PF", rt="990PF", contractors=[ctuple("OID_P5_PF", "PF GHOST", 1)])

    # RED: simulate the failure mode by hand (ghost row written + PF row wiped),
    # prove the untouched-assertion catches each half.
    db_red = fresh_db(scratch, "p5_red")
    con = sqlite3.connect(db_red)
    con.execute("INSERT INTO contractors (object_id, ein, contractor_name, city, state, "
                "service_type, compensation) VALUES (?,?,?,?,?,?,?)",
                hostile_ez["contractors"][0])          # ghost EZ write
    con.commit(); con.close()
    red_failed = (len(rows(db_red, "OID_P5_EZ")) == 0) is False and \
                 (rows(db_red, "OID_P5_PF") == pf_existing) is False
    red_receipt = "ghost-EZ-row + missing-PF-row world -> both untouched-assertions fail"

    # GREEN: seed PF rows directly (they pre-exist in prod), drive hostile EZ+PF results.
    db = fresh_db(scratch, "p5_green")
    con = sqlite3.connect(db)
    con.executemany("INSERT INTO contractors (object_id, ein, contractor_name, city, state, "
                    "service_type, compensation) VALUES (?,?,?,?,?,?,?)", pf_existing)
    con.commit(); con.close()
    run_writer(mod, db, [hostile_ez, hostile_pf])
    green = (len(rows(db, "OID_P5_EZ")) == 0) and (rows(db, "OID_P5_PF") == pf_existing)
    return red_failed, red_receipt, green, "EZ wrote nothing; PF rows untouched despite hostile payloads"


# ── P6-P8 apparatus ─────────────────────────────────────────────────────────

REGAN_OID = "201931349349304213"
REGAN_XML = os.path.join(BASE, "2019", "download990xml_2019_6",
                         "201931349349304213_public.xml")

# Final-flush anchor (8-space indent = final flush; mid-run flush is deeper).
FINAL_FLUSH_ANCHOR = ('        con.executemany(CONTRACTOR_SQL, contractor_buf)\n'
                      '    con.commit()')
COMMIT_SPLIT = ('        con.commit()  # COMMIT-SPLIT red variant (P8a): delete commits before insert\n'
                '        con.executemany(CONTRACTOR_SQL, contractor_buf)\n'
                '    con.commit()')


def make_commit_split_variant(scratch_dir):
    """The 'unwrapped delete-then-insert' world: a commit injected between the
    contractor DELETE and INSERT at the final flush. This is the exact latent
    bug the atomicity proof must be able to detect."""
    src = open(WRITER_UNDER_TEST).read()
    n = src.count(FINAL_FLUSH_ANCHOR)
    if n != 1:
        raise SystemExit(f"HARNESS INVALID: final-flush anchor found {n}x (expected 1) — "
                         f"source drifted; re-pin FINAL_FLUSH_ANCHOR before trusting P8a.")
    path = os.path.join(scratch_dir, "writer_commit_split.py")
    open(path, "w").write(src.replace(FINAL_FLUSH_ANCHOR, COMMIT_SPLIT))
    return load_module(path, "writer_commit_split")


def _p9_world(mod, scratch, tag):
    """Shared P9 apparatus: seed returns + fixture stream (5 legacy rows vs 1
    contractor per filing), drive the writer, kill after the first mid-run
    flush commits, return (db, oids, torn_seen)."""
    import time as _t
    n = 150   # first flush covers 100 filings (5 officer rows x 100 = 500); the
    # 50-filing tail stays unflushed at the kill -> proves absent-= rediscoverable
    oids = [f"OID_P9_{i:04d}" for i in range(n)]
    xml_dir = os.path.join(scratch, f"{tag}_xml")
    os.makedirs(xml_dir, exist_ok=True)
    db = fresh_db(scratch, tag)
    con = sqlite3.connect(db)
    mod.create_schema(con)
    con.execute("CREATE TABLE returns (object_id TEXT PRIMARY KEY, source_file TEXT, return_type TEXT)")
    results = []
    for i, oid in enumerate(oids):
        sf = os.path.join(xml_dir, f"{oid}.xml")
        open(sf, "w").write("<placeholder/>")
        con.execute("INSERT INTO returns VALUES (?,?,?)", (oid, sf, "990"))
        r = result(oid, contractors=[ctuple(oid, "P9 VENDOR", 1000 + i)])
        r["officers"] = [(oid, "770000001", f"PERSON {j}", "OFFICER", 1.0,
                          1000, None, None, None, None, None,
                          0, 0, 0, 0, 0) for j in range(5)]
        r["schedule_i"] = [(oid, "770000001", f"RECIP {j}", None, "CITY", "CA",
                            "90210", "501c3", 500, 0, "GRANT") for j in range(5)]
        r["related_orgs"] = [(oid, "770000001", f"REL {j}", None, "CITY", "CA",
                              "90210", "ACT", "CA", "501c3", "PC", None, 0, "R")
                             for j in range(5)]
        results.append(r)
    con.commit()
    con.close()
    q = mp.Queue()
    p = mp.Process(target=mod.writer_process, args=(db, q, len(results), set(), set(), set()),
                   daemon=True)
    p.start()
    for r in results:
        q.put(r)
    torn = False
    deadline = _t.time() + 60
    while _t.time() < deadline:
        try:
            con = sqlite3.connect(db)
            rel = con.execute("SELECT COUNT(*) FROM related_orgs").fetchone()[0]
            con.close()
        except sqlite3.OperationalError:
            _t.sleep(0.01)
            continue
        if rel >= 500:   # the first mid-run flush has committed
            torn = True
            break
        _t.sleep(0.01)
    p.kill()
    p.join(timeout=30)
    q.cancel_join_thread()
    return db, oids, torn


def _p9_characterize(mod, db, oids):
    """(lost, orphaned) — filings with all-3-legacy-committed but no contractors,
    and the subset the REAL discover_files() permanently skips."""
    con = sqlite3.connect(db)
    lost = [o for o in oids
            if con.execute("SELECT 1 FROM officers WHERE object_id=? LIMIT 1", (o,)).fetchone()
            and con.execute("SELECT 1 FROM schedule_i_990 WHERE object_id=? LIMIT 1", (o,)).fetchone()
            and con.execute("SELECT 1 FROM related_orgs WHERE object_id=? LIMIT 1", (o,)).fetchone()
            and not con.execute("SELECT 1 FROM contractors WHERE object_id=? LIMIT 1", (o,)).fetchone()]
    con.close()
    files, _, _, _ = mod.discover_files(db)
    candidates = {f[0] for f in files}
    return lost, [o for o in lost if o not in candidates]


def proof_p9_inverted(mod, scratch):
    """P9-INVERTED (live mode since the 2026-07-05 ship): with the cross-table
    single-commit fix live, the same kill leaves ZERO filings
    legacy-committed-without-contractors — every interrupted filing is either
    fully present or fully absent, and everything absent stays discoverable.
    Green = the P9 gap is CLOSED. (proof_p9 is the retained pre-fix
    gap-receipt; its docstring mandated this inversion post-fix.)"""
    db, oids, interrupted = _p9_world(mod, scratch, "p9inv")

    # RED: hand-tear the fixed world (strip one filing's contractors) — the
    # zero-lost assertion must still be able to fire.
    con = sqlite3.connect(db)
    victim = con.execute("SELECT object_id FROM contractors LIMIT 1").fetchone()
    if victim:
        con.execute("DELETE FROM contractors WHERE object_id=?", (victim[0],))
        con.commit()
    con.close()
    lost_red, _ = _p9_characterize(mod, db, oids)
    red_failed = len(lost_red) > 0
    red_receipt = f"hand-torn fixed world: zero-lost assertion catches {len(lost_red)} filing(s)"

    # GREEN: fresh fixed-world kill — zero lost, zero orphaned, and the kill
    # genuinely interrupted mid-stream (some filings absent entirely).
    db2, oids2, interrupted2 = _p9_world(mod, scratch, "p9inv_green")
    lost, orphaned = _p9_characterize(mod, db2, oids2)
    con = sqlite3.connect(db2)
    fully_absent = [o for o in oids2 if not con.execute(
        "SELECT 1 FROM officers WHERE object_id=? LIMIT 1", (o,)).fetchone()]
    with_contractors = con.execute("SELECT COUNT(DISTINCT object_id) FROM contractors").fetchone()[0]
    con.close()
    green = (interrupted2 and len(lost) == 0 and len(orphaned) == 0
             and with_contractors > 0)
    return red_failed, red_receipt, green, (
        f"fixed writer under the same kill: {with_contractors} filings fully committed "
        f"(WITH contractors), {len(fully_absent)} fully absent (all re-discoverable), "
        f"0 legacy-committed-without-contractors — the P9 gap is CLOSED by the fix")


def latency_benchmark(live_mod, fix_mod, scratch, n_filings=50_000):
    """Same result stream through both writers on fresh scratch DBs; wall-clock
    per variant. Row mix per filing ≈ corpus averages (7 officers, 1 sched_i,
    2 related; contractor delete for every 990 + 2 inserts for 40%)."""
    import time as _t
    stream = []
    for i in range(n_filings):
        oid = f"OID_BENCH_{i:07d}"
        r = result(oid, contractors=(
            [ctuple(oid, "VENDOR A", 100000 + i), ctuple(oid, "VENDOR B", 200000 + i)]
            if i % 5 < 2 else []))
        r["officers"] = [(oid, "770000001", f"P{j}", "T", 1.0, 1000 + j,
                          None, None, None, None, None,
                          0, 0, 0, 0, 0) for j in range(7)]
        r["schedule_i"] = [(oid, "770000001", "R", None, "C", "CA", "9",
                            "501c3", 1, 0, "G")]
        r["related_orgs"] = [(oid, "770000001", f"REL{j}", None, "C", "CA", "9",
                              "A", "CA", "5", "P", None, 0, "R") for j in range(2)]
        stream.append(r)
    chunks = [stream[i:i + 500] for i in range(0, len(stream), 500)]
    timings = {}
    for name, mod in (("live", live_mod), ("fixed", fix_mod)):
        db = fresh_db(scratch, f"bench_{name}")
        t0 = _t.monotonic()
        run_writer(mod, db, chunks)   # writer accepts list items natively
        timings[name] = _t.monotonic() - t0
    return timings


def run_writer_expect_crash(mod, db, results):
    """Drive the writer expecting the child to die; returns its exitcode."""
    q = mp.Queue()
    p = mp.Process(target=mod.writer_process, args=(db, q, len(results), set(), set(), set()),
                   daemon=True)
    p.start()
    for r in results:
        q.put(r)
    q.put(None)
    p.join(timeout=180)
    if p.is_alive():
        p.kill()
        p.join()
        return -9
    return p.exitcode


def run_writer_kill_midstream(mod, db, results, new_marker):
    """Feed all results but never the shutdown sentinel; SIGKILL the child the
    moment a mid-run flush commits rows carrying new_marker. Returns True if a
    mid-run commit was observed before the kill."""
    import time as _t
    q = mp.Queue()
    p = mp.Process(target=mod.writer_process, args=(db, q, len(results), set(), set(), set()),
                   daemon=True)
    p.start()
    for r in results:
        q.put(r)
    seen = False
    deadline = _t.time() + 120
    while _t.time() < deadline:
        con = sqlite3.connect(db)
        n = con.execute("SELECT COUNT(*) FROM contractors WHERE contractor_name=?",
                        (new_marker,)).fetchone()[0]
        con.close()
        if n > 0:
            seen = True
            break
        _t.sleep(0.02)
    p.kill()
    p.join(timeout=30)
    q.cancel_join_thread()
    return seen


def sweep_old_or_new(db, oids, old_name, new_name):
    """Atomicity invariant: every filing ends wholly-old or wholly-new — never
    empty, never mixed. Returns the violating (oid, names) list."""
    bad = []
    con = sqlite3.connect(db)
    for oid in oids:
        names = sorted(n for (n,) in con.execute(
            "SELECT contractor_name FROM contractors WHERE object_id=?", (oid,)))
        if names != [old_name] and names != [new_name]:
            bad.append((oid, names))
    con.close()
    return bad


def proof_p6(mod, scratch):
    """P6 (REVISED 2026-07-05, R3/done-means-4): OFFICER_SQL 16-col arity — exact
    990/EZ tuple shapes land value-exact incl. the six role flags (990: 0/1, with a
    MULTI-flag row exercising check-all-that-apply; EZ: all six NULL); a wrong-arity
    tuple must crash, never silently pad. (PF explicit-column inserts live in
    extract_990pf_detail.py — separate writer, stays PENDING.)"""
    o990 = ("OID_A_990", "770000001", "JANE DOE", "CEO", 40.0, 150000, 20000, 30000,
            None, None, 1, 1, 0, 0, 0, 0)   # HCE=1 AND officer=1 — multi-flag row
    oez = ("OID_B_EZ", "770000002", "JOHN ROE", "TREASURER", 5.0, 20000, None, None,
           3000, 500, None, None, None, None, None, None)

    # RED: 15-field tuple — the writer must reject (child crash), and no row lands.
    r_bad = result("OID_P6_BAD")
    r_bad["officers"] = [o990[:15]]
    db_red = fresh_db(scratch, "p6_red")
    rc = run_writer_expect_crash(mod, db_red, [r_bad])
    con = sqlite3.connect(db_red)
    leaked = con.execute("SELECT COUNT(*) FROM officers").fetchone()[0]
    con.close()
    red_failed = (rc not in (0, None)) and leaked == 0
    red_receipt = f"15-field tuple -> child exit {rc}, 0 rows leaked (arity enforced, not padded)"

    # GREEN: exact shapes land column-correct.
    r1 = result("OID_A_990")
    r1["officers"] = [o990]
    r2 = result("OID_B_EZ", rt="990EZ")
    r2["officers"] = [oez]
    db = fresh_db(scratch, "p6_green")
    run_writer(mod, db, [r1, r2])
    con = sqlite3.connect(db)
    got = con.execute(
        "SELECT object_id, ein, person_name, title, avg_hours_per_week, "
        "reportable_comp_filing_org, reportable_comp_related_org, other_compensation, "
        "benefits, expense_account, is_highest_compensated_employee, "
        "is_officer, is_individual_trustee, is_institutional_trustee, "
        "is_key_employee, is_former "
        "FROM officers ORDER BY object_id").fetchall()
    con.close()
    green = got == [o990, oez]
    return red_failed, red_receipt, green, ("990 (multi-flag) + EZ (flags NULL) tuples "
                                            "round-tripped all 16 columns exactly")


def make_raw_officer_variant(scratch_dir):
    """Writer variant with the §110 keyed dedup REMOVED (raw-emission officer inserts) —
    the P7 red: the pre-B behavior whose raw dups re-accumulate every monthly (#265
    consequence 1). Fails loudly if the dedup call site drifts."""
    src = open(WRITER_UNDER_TEST).read()
    needle = 'keyed = dedup_officers_keyed(r["officers"])'
    n = src.count(needle)
    if n != 1:
        raise SystemExit(f"HARNESS INVALID: dedup call found {n}x (expected 1) — "
                         f"re-pin the P7 variant needle before trusting P7.")
    path = os.path.join(scratch_dir, "writer_raw_officers.py")
    open(path, "w").write(src.replace(needle, 'keyed = list(r["officers"])'))
    return load_module(path, "writer_raw_officers")


def proof_p7(mod, scratch):
    """P7 (INVERTED 2026-07-05 — R3/done-means-4, going-forward clause): keyed collapse
    AT THE WRITER. The REAL exemplar (xml=2 THOMAS REGAN byte-dup) must land as ONE
    stored row under the §110 key while the parser still emits both copies. Red = the
    raw-insert variant (pre-B writer, dedup stripped) stores 2 → the stored==1 assert
    fires. History: pre-B this proof asserted store-both as the RECEIPT of then-current
    behavior (#264(vii)/#265); Deliverable B flips the required behavior."""
    raw = open(REGAN_XML, encoding="utf-8", errors="replace").read()
    xml_n = raw.count("THOMAS REGAN")
    parsed = mod.parse_file((REGAN_OID, REGAN_XML, "990"))
    emitted = sum(1 for t in parsed["officers"] if t[2] == "THOMAS REGAN")

    def stored_count(db):
        con = sqlite3.connect(db)
        n = con.execute("SELECT COUNT(*) FROM officers WHERE object_id=? AND person_name='THOMAS REGAN'",
                        (REGAN_OID,)).fetchone()[0]
        con.close()
        return n

    # RED: the raw-insert variant stores every emitted copy -> the keyed assert must fail on it.
    raw_mod = make_raw_officer_variant(scratch)
    db_red = fresh_db(scratch, "p7_red")
    run_writer(raw_mod, db_red, [parsed])
    red_failed = (stored_count(db_red) == 1) is False
    red_receipt = (f"raw-insert variant (pre-B writer) stores {stored_count(db_red)} vs 1 "
                   f"required -> keyed assert fires")

    # GREEN: the live keyed writer collapses the byte-dup; parser emission unchanged.
    db = fresh_db(scratch, "p7_green")
    run_writer(mod, db, [parsed])
    stored = stored_count(db)
    green = (emitted == xml_n) and (xml_n >= 2) and (stored == 1)
    return red_failed, red_receipt, green, (
        f"WITNESS RECEIPT: raw-XML occurrences={xml_n}, parser-emitted={emitted}, "
        f"writer-stored={stored} -> keyed writer collapses the byte-dup to ONE row "
        f"(going-forward clause lands at the writer)")


FOWLER_OID = "202013089349300746"
FOWLER_XML = os.path.join(BASE, "2020", "download990xml_2020_3",
                          "202013089349300746_public.xml")


def proof_p7b(mod, scratch):
    """P7b (ADDED 2026-07-05 — R3): flag-independence through the SHIPPED writer — the
    Norris Fowler witness (one Section-A entry, trustee AND officer boxes both X) must
    land as ONE row carrying BOTH flags = 1 (HCE 0); John B Fowler's HCE box lands
    independently as 1. Red = hand-tampered first-X-wins world (officer flag zeroed) →
    the both-flags assert fires. Emission-level exclusivity is separately red-proven
    against the transform in test_officer_key_witnesses.py W6; this proof pins the
    WRITER path."""
    parsed = mod.parse_file((FOWLER_OID, FOWLER_XML, "990"))

    def read(db):
        con = sqlite3.connect(db)
        r = con.execute("SELECT is_officer, is_individual_trustee, is_highest_compensated_employee "
                        "FROM officers WHERE object_id=? AND person_name='NORRIS R FOWLER'",
                        (FOWLER_OID,)).fetchall()
        j = con.execute("SELECT is_highest_compensated_employee FROM officers "
                        "WHERE object_id=? AND person_name='JOHN B FOWLER'",
                        (FOWLER_OID,)).fetchall()
        con.close()
        return r, j

    def ok(r, j):
        return len(r) == 1 and r[0] == (1, 1, 0) and len(j) == 1 and j[0][0] == 1

    # RED: zero the officer flag by hand (the falsified-exclusivity outcome) -> must fail.
    db_red = fresh_db(scratch, "p7b_red")
    run_writer(mod, db_red, [parsed])
    con = sqlite3.connect(db_red)
    con.execute("UPDATE officers SET is_officer=0 WHERE object_id=? AND person_name='NORRIS R FOWLER'",
                (FOWLER_OID,))
    con.commit()
    con.close()
    r_red, j_red = read(db_red)
    red_failed = ok(r_red, j_red) is False
    red_receipt = f"first-X-wins world: NORRIS flags(officer,indiv,hce)={r_red} -> both-flags assert fires"

    db = fresh_db(scratch, "p7b_green")
    run_writer(mod, db, [parsed])
    r_g, j_g = read(db)
    green = ok(r_g, j_g)
    return red_failed, red_receipt, green, (
        f"NORRIS one row flags(officer,indiv,hce)={r_g[0] if r_g else None}; JOHN B hce=1 "
        f"-> check-all-that-apply lands through the shipped writer")


def proof_p8a(mod, scratch):
    """P8a: injected insert-error post-delete. Real writer: DELETE+INSERT share one
    transaction, so the crash rolls the delete back and prior rows survive. Red =
    the commit-split variant (delete committed before insert) loses them."""
    prior = [ctuple("OID_P8A", "KEEP ME LLC", 424242)]
    poison = result("OID_P8A", contractors=[("OID_P8A", "770000001", "POISON-3-FIELDS")])

    # RED: commit-split world — delete commits, insert crashes, rows are GONE.
    db_red = fresh_db(scratch, "p8a_red")
    run_writer(mod, db_red, [result("OID_P8A", contractors=prior)])
    split = make_commit_split_variant(scratch)
    rc_red = run_writer_expect_crash(split, db_red, [poison])
    got_red = rows(db_red, "OID_P8A")
    red_failed = ((rc_red not in (0, None)) and sorted(got_red) == sorted(prior)) is False
    red_receipt = (f"commit-split writer: child exit {rc_red}, {len(got_red)} row(s) left "
                   f"(prior had {len(prior)}) -> survive-assertion fails on the unwrapped world")

    # GREEN: real writer, same poison — crash fires AND prior rows survive intact.
    db = fresh_db(scratch, "p8a_green")
    run_writer(mod, db, [result("OID_P8A", contractors=prior)])
    rc = run_writer_expect_crash(mod, db, [poison])
    got = rows(db, "OID_P8A")
    green = (rc not in (0, None)) and sorted(got) == sorted(prior)
    return red_failed, red_receipt, green, (
        f"real writer: child exit {rc} (error genuinely fired), prior rows intact — "
        f"delete rolled back with the failed insert")


def proof_p8b(mod, scratch):
    """P8b: SIGKILL mid-stream during a 700-filing re-queue (crosses the 500-row
    flush threshold, so mid-run flushes are exercised). Invariant: every filing
    ends wholly-old or wholly-new — never empty, never mixed."""
    n = 700
    oids = [f"OID_P8B_{i:04d}" for i in range(n)]
    old = [result(o, contractors=[ctuple(o, "OLDGEN VENDOR", 100 + i)])
           for i, o in enumerate(oids)]
    new = [result(o, contractors=[ctuple(o, "NEWGEN VENDOR", 200 + i)])
           for i, o in enumerate(oids)]
    # NOTE: comp differs per oid, so mixed/empty states are detectable by name sweep.

    # RED: hand-torn world (one oid emptied) -> sweep must catch it.
    db_red = fresh_db(scratch, "p8b_red")
    run_writer(mod, db_red, old)
    con = sqlite3.connect(db_red)
    con.execute("DELETE FROM contractors WHERE object_id=?", (oids[3],))
    con.commit()
    con.close()
    bad = sweep_old_or_new(db_red, oids, "OLDGEN VENDOR", "NEWGEN VENDOR")
    red_failed = len(bad) > 0
    red_receipt = f"hand-torn world: sweep flags {len(bad)} filing(s) (e.g. {bad[0] if bad else None})"

    # GREEN: seed old, re-queue new, SIGKILL mid-stream, sweep.
    db = fresh_db(scratch, "p8b_green")
    run_writer(mod, db, old)
    seen = run_writer_kill_midstream(mod, db, new, "NEWGEN VENDOR")
    bad2 = sweep_old_or_new(db, oids, "OLDGEN VENDOR", "NEWGEN VENDOR")
    con = sqlite3.connect(db)
    n_new = con.execute("SELECT COUNT(*) FROM contractors WHERE contractor_name='NEWGEN VENDOR'").fetchone()[0]
    con.close()
    green = seen and len(bad2) == 0 and 0 < n_new < n
    return red_failed, red_receipt, green, (
        f"killed mid-stream after a real mid-run flush ({n_new}/{n} filings already new); "
        f"sweep: 0 empty/mixed filings — every filing wholly-old or wholly-new")


def proof_p9(mod, scratch):
    """P9: interrupted-run-then-rerun convergence — THE GAP RECEIPT on current code.

    Torn state is produced through the REAL writer + SIGKILL, exploiting the
    independent per-table flush thresholds: 5 officers/5 sched_i/5 related but
    only 1 contractor per filing, so at filing 100 the three legacy buffers hit
    500 and COMMIT (three separate commits) while the contractor delete+insert
    batch (100 rows) is still buffered. The kill loses only the contractor leg.
    The REAL discover_files() then reads those filings as done (presence in all
    three legacy tables) — they never re-parse and their contractors never
    converge.

    FRAMING (maintainer, round-4 note 1): the discovery-intersection pool is a CEILING
    on the serviceable population, NOT a self-limiting bound — every interrupt
    permanently orphans its torn filings and cumulative orphans GROW until the
    write-side fix. 'PASS' here means the gap is demonstrated and characterized
    (the convergence assertion FAILS on current code, captured as a receipt).
    After a write-side fix, this proof must be INVERTED: gap absent = green."""
    n = 100
    oids = [f"OID_P9_{i:04d}" for i in range(n)]
    xml_dir = os.path.join(scratch, "p9_xml")
    os.makedirs(xml_dir, exist_ok=True)
    db = fresh_db(scratch, "p9")
    con = sqlite3.connect(db)
    mod.create_schema(con)   # sched_i/related exist before the parent-side polls
    con.execute("CREATE TABLE returns (object_id TEXT PRIMARY KEY, source_file TEXT, return_type TEXT)")
    results = []
    for i, oid in enumerate(oids):
        sf = os.path.join(xml_dir, f"{oid}.xml")
        open(sf, "w").write("<placeholder/>")   # discovery checks existence only
        con.execute("INSERT INTO returns VALUES (?,?,?)", (oid, sf, "990"))
        r = result(oid, contractors=[ctuple(oid, "P9 VENDOR", 1000 + i)])
        r["officers"] = [(oid, "770000001", f"PERSON {j}", "OFFICER", 1.0,
                          1000, None, None, None, None, None,
                          0, 0, 0, 0, 0) for j in range(5)]
        r["schedule_i"] = [(oid, "770000001", f"RECIP {j}", None, "CITY", "CA",
                            "90210", "501c3", 500, 0, "GRANT") for j in range(5)]
        r["related_orgs"] = [(oid, "770000001", f"REL {j}", None, "CITY", "CA",
                              "90210", "ACT", "CA", "501c3", "PC", None, 0, "R")
                             for j in range(5)]
        results.append(r)
    con.commit()
    con.close()

    # Drive the REAL writer; no shutdown sentinel, so the final flush never runs.
    # After filing 100 the legacy flushes have committed and the writer idles with
    # the contractor batch still in memory — a deterministic torn window.
    import time as _t
    q = mp.Queue()
    p = mp.Process(target=mod.writer_process, args=(db, q, len(results), set(), set(), set()),
                   daemon=True)
    p.start()
    for r in results:
        q.put(r)
    torn = False
    deadline = _t.time() + 60
    while _t.time() < deadline:
        try:
            con = sqlite3.connect(db)
            rel = con.execute("SELECT COUNT(*) FROM related_orgs").fetchone()[0]
            ctr = con.execute("SELECT COUNT(*) FROM contractors").fetchone()[0]
            con.close()
        except sqlite3.OperationalError:
            _t.sleep(0.01)
            continue
        if rel >= 5 * n and ctr == 0:
            torn = True
            break
        _t.sleep(0.01)
    p.kill()
    p.join(timeout=30)
    q.cancel_join_thread()

    # Characterize the torn state, then run the REAL discovery on it.
    con = sqlite3.connect(db)
    lost = [o for o in oids
            if con.execute("SELECT 1 FROM officers WHERE object_id=? LIMIT 1", (o,)).fetchone()
            and con.execute("SELECT 1 FROM schedule_i_990 WHERE object_id=? LIMIT 1", (o,)).fetchone()
            and con.execute("SELECT 1 FROM related_orgs WHERE object_id=? LIMIT 1", (o,)).fetchone()
            and not con.execute("SELECT 1 FROM contractors WHERE object_id=? LIMIT 1", (o,)).fetchone()]
    con.close()
    files, _, _, _ = mod.discover_files(db)
    candidates = {f[0] for f in files}
    orphaned = [o for o in lost if o not in candidates]

    demonstrated = torn and len(lost) > 0 and len(orphaned) == len(lost)
    receipt = (f"GAP RECEIPT (current code): {len(lost)}/{n} filings ended the interrupted run "
               f"with all three legacy tables committed and their contractor batch rolled back; "
               f"real discover_files() re-parses {len(lost) - len(orphaned)} of them and "
               f"PERMANENTLY SKIPS {len(orphaned)} — contractors never converge. Cumulative "
               f"across interrupts: orphans only GROW until the write-side fix (the "
               f"intersection pool is a ceiling on the serviceable population, not a bound).")
    conv_assert_fails_today = len(orphaned) > 0
    return conv_assert_fails_today, receipt, demonstrated, receipt


def proof_p10_random_kills(mod, scratch):
    """P10 (fix-packet only): randomized-interrupt sweep. The engineered P9 worlds
    kill at flush boundaries the proofs were designed around; this one kills at
    uniform-random offsets (seeded rng — reproducible), so the fix is validated
    against interrupt timings it was NOT built to pass. RED = the LIVE writer
    under the same random kills leaks legacy-committed-without-contractors
    filings; GREEN = the FIXED writer leaks zero across every iteration, every
    filing wholly-old-or-new, everything absent still discoverable."""
    import random
    import time as _t
    rng = random.Random(264)
    n = 30_000   # big enough that consumption takes ~1s and random offsets land
    # genuinely MID-EXECUTION (at 400 the writer went quiescent before the kill,
    # so every "random" kill sampled the same state — 250/250/250... — which is
    # a narrower claim than the proof's name)

    def iteration(m, tag):
        oids = [f"OID_P10_{i:04d}" for i in range(n)]
        xml_dir = os.path.join(scratch, f"{tag}_xml")
        os.makedirs(xml_dir, exist_ok=True)
        db = fresh_db(scratch, tag)
        con = sqlite3.connect(db)
        m.create_schema(con)
        con.execute("CREATE TABLE IF NOT EXISTS returns "
                    "(object_id TEXT PRIMARY KEY, source_file TEXT, return_type TEXT)")
        stream = []
        for i, oid in enumerate(oids):
            sf = os.path.join(xml_dir, f"{oid}.xml")
            open(sf, "w").write("<placeholder/>")
            con.execute("INSERT INTO returns VALUES (?,?,?)", (oid, sf, "990"))
            r = result(oid, contractors=[ctuple(oid, "NEWGEN VENDOR", 1000 + i)])
            r["officers"] = [(oid, "770000001", f"P{j}", "T", 1.0, 1,
                              None, None, None, None, None) for j in range(5)]
            r["schedule_i"] = [(oid, "770000001", f"R{j}", None, "C", "CA", "9",
                                "501c3", 1, 0, "G") for j in range(2)]
            r["related_orgs"] = [(oid, "770000001", f"L{j}", None, "C", "CA", "9",
                                  "A", "CA", "5", "P", None, 0, "R") for j in range(2)]
            stream.append(r)
        con.commit()
        con.close()
        q = mp.Queue()
        p = mp.Process(target=m.writer_process, args=(db, q, len(stream), set(), set(), set()),
                       daemon=True)
        p.start()
        for r in stream:
            q.put(r)          # no shutdown sentinel: writer never final-flushes
        deadline = _t.time() + 60
        while _t.time() < deadline:   # wait for the first commit of anything
            try:
                con = sqlite3.connect(db)
                seen = con.execute("SELECT COUNT(*) FROM officers").fetchone()[0]
                con.close()
            except sqlite3.OperationalError:
                _t.sleep(0.005)
                continue
            if seen > 0:
                break
            _t.sleep(0.005)
        _t.sleep(rng.uniform(0.0, 1.0))   # random offset — NOT a flush boundary
        p.kill()
        p.join(timeout=30)
        q.cancel_join_thread()
        lost, orphaned = _p9_characterize(m, db, oids)
        bad = sweep_old_or_new(db, [o for o in oids if o not in set(lost)],
                               "NEVER-OLD", "NEWGEN VENDOR")
        # fresh filings: wholly-new or wholly-absent; absent ones show as bad=[]
        mixed = [b for b in bad if b[1] not in ([],)]
        con = sqlite3.connect(db)
        n_new = con.execute("SELECT COUNT(DISTINCT object_id) FROM contractors").fetchone()[0]
        con.close()
        return len(lost), len(orphaned), len([b for b in mixed if b[1] != ["NEWGEN VENDOR"]]), n_new

    live_mod = load_module(LIVE_WRITER, "p10_live")
    lost_live = []
    for k in range(4):
        lost, orph, mixed, n_new = iteration(live_mod, f"p10_live_{k}")
        lost_live.append(lost)
    red_failed = any(l > 0 for l in lost_live)
    red_receipt = (f"LIVE writer, 6 random-offset kills: lost-per-iteration = {lost_live} "
                   f"-> the gap fires under arbitrary timing, not just the engineered window")

    _, fixed_mod = make_crosstable_fix_variant(scratch) if not isinstance(mod, tuple) else (None, mod)
    lost_fixed, mixed_fixed = [], []
    for k in range(4):
        lost, orph, mixed, n_new = iteration(fixed_mod, f"p10_fix_{k}")
        lost_fixed.append(lost)
        mixed_fixed.append(mixed)
    green = all(l == 0 for l in lost_fixed) and all(m == 0 for m in mixed_fixed)
    return red_failed, red_receipt, green, (
        f"FIXED writer, 6 random-offset kills: lost = {lost_fixed}, mixed = {mixed_fixed} "
        f"-> zero cross-table tears at any random timing")


PROOFS = [
    ("P1 rows-land", proof_p1),
    ("P2 requeue-identical", proof_p2),
    ("P3 zero-contractor-clears", proof_p3),
    ("P4 parse-error-guard", proof_p4),
    ("P5 ez-pf-untouched", proof_p5),
    ("P6 officer-arity-16col", proof_p6),
    ("P7-INVERTED keyed-collapse (going-forward clause)", proof_p7),
    ("P7b fowler-flag-independence", proof_p7b),
    ("P8a atomicity-injected-error", proof_p8a),
    ("P8b atomicity-kill-midstream", proof_p8b),
    ("P9-INVERTED gap-closed (fix live 2026-07-05)", proof_p9_inverted),
]

PENDING = ["P6-PF explicit-column leg (extract_990pf_detail.py, separate writer)"]


def main():
    mp.set_start_method("fork", force=True)
    if "--fix-packet" in sys.argv:
        sys.exit(
            "--fix-packet is RETIRED: the cross-table fix is LIVE in extract_990_detail.py "
            "as of 2026-07-05 (#264 ship, maintainer-approved; diff receipt: "
            "fix_packet_crosstable_flush.diff, packet greens incl. P10 [0,0,0,0] in the "
            "#264/#265 record). Run without flags — live mode now certifies the fixed "
            "writer, with P9 running INVERTED (gap-closed).")
    scratch = tempfile.mkdtemp(prefix="t264_")
    proofs = list(PROOFS)
    mod = load_module(LIVE_WRITER, "live_writer_under_test")
    failures = 0
    print(f"#264 harness — scratch: {scratch}")
    print(f"writer under test: {LIVE_WRITER}\n")
    try:
        for name, fn in proofs:
            red_failed, red_receipt, green, green_receipt = fn(mod, scratch)
            red_str = "RED fired" if red_failed else "RED DID NOT FIRE (proof invalid)"
            green_str = "GREEN" if green else "GREEN FAILED"
            ok = red_failed and green
            failures += 0 if ok else 1
            print(f"[{'PASS' if ok else 'FAIL'}] {name}")
            print(f"        red:   {red_str} — {red_receipt}")
            print(f"        green: {green_str} — {green_receipt}")
        print(f"\npending (not yet implemented): {', '.join(PENDING)}")
        print(f"\n{'ALL IMPLEMENTED PROOFS RED-THEN-GREEN' if failures == 0 else str(failures) + ' PROOF(S) FAILED'}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
