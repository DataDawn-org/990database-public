#!/usr/bin/env python3
"""
render_smoke.py — post-deploy RENDER smoke for the ER-QC recipient-disclosure
templates served by the 990 Datasette (grant / charity_grant / daf; org added
post-June-1). COMPLEMENTS update.sh's row-count smoke, which catches "prod DB !=
local DB". This catches a DIFFERENT class: a served PAGE renders wrong, or
references a file the deploy forgot to ship.

  L1  BRANCH INVARIANTS — derive one live id per disclosure branch AT RUNTIME
      (grants.id / schedule_i_*.id are AUTOINCREMENT -> never hardcoded; a baked
      id silently rots across a rebuild), fetch the served page, and assert the
      invariants the Design-B fix guarantees:
        * HTTP 200, no server-error tell in the body. A missing
          `{% from "_recip_copy.html" %}` renders a 500 (TemplateNotFound) — the
          exact trap that bit us 2026-05-29; caught here (the template-include
          layer of the deploy-coverage check).
        * resolve-XOR-disclose: a recipient is EITHER confidently linked WITH a
          "matched by <basis>" provenance label, OR disclosed (gen|cluster|none)
          with no link — never an arbitrary bare link, never link AND disclosure.
        * a NAME-derived link carries the verify-before-citing nudge
          ("Name-derived match … verify against the source filing"); an EIN link
          does NOT (an exact match needs no caveat).
        * a not-in-BMF recipient links to nothing and discloses nothing.
  L2  DEPLOY COVERAGE (asset layer) — for one page of each type, parse the
      rendered HTML for same-origin static asset refs (<link href>, <script src>,
      /static/…) and assert each resolves (not 404). Catches the api/-freeze
      class: a referenced shipped-artifact the deploy dropped.

Read-only on the DB (mode=ro). Infra noise (timeout / 502 / 503 / 504 / reset) is
retried and distinguished from a real failure; a 500 is REAL (the page errored)
and is never retried away. Exit 0 = all real checks pass; 1 = a real failure.

  python3 render_smoke.py --base https://data.datadawn.org --db 990data_public.db
  python3 render_smoke.py ... --surfaces grant,charity_grant,daf,org   # default (org = resolve-or-disclose, #61)
  python3 render_smoke.py ... --json
"""
import argparse, json, re, sqlite3, sys, time, urllib.parse, urllib.request, urllib.error

# ---- assertable strings: these MUST track templates/_recip_copy.html ----------
NAME_NUDGE = "Name-derived match"                       # verify_inline() — NAME links only
CAVEAT     = "matched by name and may be incomplete"    # completeness_caveat()
DISC = {                                                # disclosure(branch, …) bodies
    "gen":     "file under a single IRS group exemption",
    "cluster": "keeps them separate and does not aggregate",
    "none":    "so the recipient is not linked",
}
# A confident link prints `matched by <basis>` (matched_label / "(matched by …)").
# The completeness caveat ALSO contains "matched by name …", so a bare substring
# test would mis-flag a disclosure page that has a name-matched list. Match the
# LABEL forms only, excluding the caveat via the " and may" negative lookahead.
PROV_RE = re.compile(r"matched by (?:EIN|name \+ state \+ city|name \+ state|name)\b(?! and may)")
ERROR_TELLS = ("TemplateNotFound", "Traceback (most recent call last)",
               "OperationalError", "no such column", "jinja2.exceptions",
               "Internal Server Error")
# org/{ein} resolve-or-disclose markers (followup_queue #61). These MUST track org/{ein}.html.
ORG_DISCLOSE_MARK = "cannot be attributed to this organization"   # shared-name disclosure body
ORG_SEARCH_MARK   = "/explore.html?tab=grants"                    # the disclosure's grant-search link
ASSET_RE = re.compile(r'(?:href|src)=["\']([^"\']+)["\']')
ASSET_EXT = re.compile(r"\.(?:css|js|ico|png|svg|jpe?g|gif|woff2?|ttf|json|map)(?:\?|#|$)")


def fetch(url, timeout, tries=3):
    """(status:int|str, body:str, infra:bool). 502/503/504/timeout/reset => infra
    (retried). 500/404 are REAL (returned, not retried). 200 returns the body."""
    last = None
    for i in range(tries):
        try:
            r = urllib.request.urlopen(url, timeout=timeout)
            return r.status, r.read().decode("utf-8", "replace"), False
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if e.code in (502, 503, 504):
                last = (e.code, body, True); time.sleep(2 * (i + 1)); continue
            return e.code, body, False                 # 500 / 404 / etc = real, no retry
        except Exception as e:
            last = ("ERR:" + type(e).__name__, "", True); time.sleep(2 * (i + 1)); continue
    return last if last else ("ERR:unknown", "", True)


def derive_targets(con, surfaces):
    """One live id per branch, chosen by the template's OWN criteria, prominence-
    ordered (ORDER BY $ DESC) for a stable, citation-relevant example. Returns
    list of {surface, id, path, intended}. Branches with no example are skipped
    and reported (no silent caps)."""
    con.executescript("""
    CREATE TEMP TABLE nc AS               -- name -> #EINs + the template's GEN-certified flag
      SELECT UPPER(name) un, COUNT(DISTINCT ein) ne,
             CASE WHEN COUNT(DISTINCT grp)=1 AND MIN(grp) NOT IN ('0000','0','') THEN 1 ELSE 0 END gen
      FROM bmf WHERE name IS NOT NULL AND TRIM(name)!='' GROUP BY UPPER(name);
    CREATE INDEX ix_nc ON nc(un);
    CREATE TEMP TABLE ncs AS              -- (name,state) -> #EINs (drives state/none branches)
      SELECT UPPER(name) un, UPPER(COALESCE(state,'')) st, COUNT(DISTINCT ein) ne
      FROM bmf WHERE name IS NOT NULL AND TRIM(name)!='' GROUP BY 1,2;
    CREATE INDEX ix_ncs ON ncs(un,st);
    """)
    con.commit()
    one = lambda q: (con.execute(q).fetchone() or [None])[0]
    G = {
      # grant (grants has NO recipient_ein -> name-only cascade)
      ("grant", "link:name(unique)"):
        "SELECT g.id FROM grants g JOIN nc ON nc.un=UPPER(g.recipient_name) "
        "WHERE nc.ne=1 AND g.amount>0 ORDER BY g.amount DESC LIMIT 1",
      ("grant", "disclosure:gen"):
        "SELECT g.id FROM grants g JOIN nc ON nc.un=UPPER(g.recipient_name) "
        "LEFT JOIN ncs ON ncs.un=nc.un AND ncs.st=UPPER(COALESCE(g.recipient_state,'')) "
        "WHERE nc.ne>1 AND nc.gen=1 AND COALESCE(ncs.ne,0)<>1 AND g.amount>0 ORDER BY g.amount DESC LIMIT 1",
      ("grant", "disclosure:cluster"):
        "SELECT g.id FROM grants g JOIN nc ON nc.un=UPPER(g.recipient_name) "
        "JOIN ncs ON ncs.un=nc.un AND ncs.st=UPPER(COALESCE(g.recipient_state,'')) "
        "WHERE nc.ne>1 AND nc.gen=0 AND ncs.ne>=2 AND COALESCE(TRIM(g.recipient_city),'')='' "
        "AND g.amount>0 ORDER BY g.amount DESC LIMIT 1",
      ("grant", "disclosure:none"):
        "SELECT g.id FROM grants g JOIN nc ON nc.un=UPPER(g.recipient_name) "
        "LEFT JOIN ncs ON ncs.un=nc.un AND ncs.st=UPPER(g.recipient_state) "
        "WHERE nc.ne>1 AND nc.gen=0 AND ncs.un IS NULL AND TRIM(COALESCE(g.recipient_state,''))<>'' "
        "AND g.amount>0 ORDER BY g.amount DESC LIMIT 1",
      ("grant", "no_match"):
        "SELECT g.id FROM grants g LEFT JOIN nc ON nc.un=UPPER(g.recipient_name) "
        "WHERE nc.un IS NULL AND TRIM(COALESCE(g.recipient_name,''))<>'' AND g.amount>0 "
        "ORDER BY g.amount DESC LIMIT 1",
      # charity_grant (schedule_i_990: EIN-first, name fallback)
      ("charity_grant", "link:EIN"):
        "SELECT id FROM schedule_i_990 WHERE recipient_ein IS NOT NULL AND LENGTH(recipient_ein)=9 "
        "AND recipient_ein GLOB '[0-9]*' ORDER BY cash_grant_amt DESC LIMIT 1",
      ("charity_grant", "name-fallback"):
        "SELECT si.id FROM schedule_i_990 si JOIN nc ON nc.un=UPPER(si.recipient_name) "
        "WHERE (si.recipient_ein IS NULL OR LENGTH(si.recipient_ein)<>9) AND nc.ne>1 "
        "ORDER BY si.cash_grant_amt DESC LIMIT 1",
      # daf (schedule_i_grants: EIN-first, name fallback)
      ("daf", "link:EIN"):
        "SELECT id FROM schedule_i_grants WHERE recipient_ein IS NOT NULL AND LENGTH(recipient_ein)=9 "
        "AND recipient_ein GLOB '[0-9]*' ORDER BY amount DESC LIMIT 1",
      ("daf", "name-fallback"):
        "SELECT s.id FROM schedule_i_grants s JOIN nc ON nc.un=UPPER(s.recipient_name) "
        "WHERE (s.recipient_ein IS NULL OR LENGTH(s.recipient_ein)<>9) AND nc.ne>1 "
        "ORDER BY s.amount DESC LIMIT 1",
    }
    out, missing = [], []
    for (surface, intended), q in G.items():
        if surface not in surfaces:
            continue
        rid = one(q)
        if rid is None:
            missing.append(f"{surface}:{intended}")
            continue
        out.append({"surface": surface, "id": rid, "path": f"/{surface}/{rid}", "intended": intended})

    # org/{ein}: EIN-keyed + multi-grant, mixed provenance BY SECTION (not per-page), so it can't use the
    # single-branch G map / check_invariants. Resolve-or-disclose (followup_queue #61): PF grants carry no
    # recipient EIN, so the "Foundation Grants Received" section ATTRIBUTES a name-matched list only when the
    # org's exact name is globally UNIQUE in the BMF; a shared name (federation or namesake) is DISCLOSED with
    # a grant-search link, never aggregated. Three deterministic targets, each ORDER BY $-total DESC, ein ASC
    # so the WORST case is pinned and the tiebreak is stable:
    #   org:disclose — the SHARED-name org with the LARGEST name-only PF-grant total (the worst over-merge).
    #                  MUST disclose, MUST NOT attribute a list. This is the bounded-value gate — the page that
    #                  would balloon to the §518B / $7.1B-Gates-namesake total if the fix regressed.
    #   org:unique   — the UNIQUE-name org with the largest name-matched PF total. MUST render the attributed
    #                  list with the name-match label + verify nudge (the resolve/show path still works).
    #   org:both     — the org with the most EIN-exact schedule_i_990 received grants; asserts the exact
    #                  "Schedule I Grants Received" section (the half name-matching never touches).
    # NOTE: the prior org:ambiguous ordered by a name-keyed COUNT identical for every EIN of a shared name, so
    # LIMIT 1 was ARBITRARY — a gate that "existed" but never deterministically fired on the over-merge it
    # guarded (it picked a sibling, e.g. "SALVATION ARMY" OK, not the reported "THE SALVATION ARMY" NY).
    if "org" in surfaces:
        con.executescript(                        # name -> total PAID PF-grant $ (built once; ~10s over the grants table)
            "CREATE TEMP TABLE IF NOT EXISTS gt AS "
            "  SELECT UPPER(recipient_name) un, SUM(amount) tot FROM grants "
            "  WHERE grant_type='paid' AND recipient_name IS NOT NULL GROUP BY UPPER(recipient_name);"
            "CREATE INDEX IF NOT EXISTS ix_gt ON gt(un);")
        con.commit()
        org_q = {
            "org:disclose":
                "SELECT b.ein FROM bmf b JOIN nc ON nc.un=UPPER(b.name) AND nc.ne>1 "
                "JOIN gt ON gt.un=nc.un ORDER BY gt.tot DESC, b.ein ASC LIMIT 1",
            "org:unique":
                "SELECT b.ein FROM bmf b JOIN nc ON nc.un=UPPER(b.name) AND nc.ne=1 "
                "JOIN gt ON gt.un=nc.un ORDER BY gt.tot DESC, b.ein ASC LIMIT 1",
            "org:both":
                "SELECT s.recipient_ein FROM schedule_i_990 s JOIN bmf b ON b.ein=s.recipient_ein "
                "WHERE LENGTH(s.recipient_ein)=9 AND s.recipient_ein GLOB '[0-9]*' "
                "GROUP BY s.recipient_ein ORDER BY COUNT(*) DESC, s.recipient_ein ASC LIMIT 1",
        }
        for intended, q in org_q.items():
            ein = one(q)
            if ein is None:
                missing.append(intended); continue
            out.append({"surface": "org", "id": ein, "path": f"/org/{ein}", "intended": intended})
    return out, missing


def check_invariants(intended, body):
    """Return list of invariant-violation strings ([] = clean). Caller has already
    confirmed HTTP 200."""
    bad = []
    tell = [t for t in ERROR_TELLS if t in body]
    if tell:
        bad.append("server-error tell in body: " + ",".join(tell))
    has_prov = PROV_RE.search(body) is not None
    is_ein = "matched by EIN" in body
    nudge = NAME_NUDGE in body
    disc = [k for k, v in DISC.items() if v in body]
    has_disc = bool(disc)

    if has_prov and has_disc:
        bad.append("resolve-XOR-disclose violated: link provenance AND disclosure both present")
    if has_prov and not is_ein and not nudge:
        bad.append("name-derived link missing the verify-before-citing nudge")
    if is_ein and nudge:
        bad.append("EIN-exact link wrongly carries a name-derived nudge")
    if has_disc and nudge:
        bad.append("disclosure page wrongly carries a name-derived link nudge")
    if intended == "no_match" and (has_prov or has_disc):
        bad.append("not-in-BMF recipient should neither link nor disclose")

    branch = (("disclosure:" + disc[0]) if has_disc else
              ("link:EIN" if is_ein else ("link:name" if has_prov else
               ("no_match" if intended == "no_match" else "plain/unlinked"))))
    return bad, branch, has_prov


def check_org_invariants(intended, body):
    """org/{ein} is EIN-keyed + multi-grant with mixed provenance BY SECTION (followup_queue #61). PF grants
    carry no recipient EIN, so the "Foundation Grants Received" section ATTRIBUTES a name-matched list only when
    the org's exact name is globally unique in the BMF (RESOLVE/show path → "matched by name" label + verify
    nudge); a shared name (federation or namesake) is DISCLOSED with a grant-search link and NO attributed list
    (DISCLOSE path). The "Schedule I Grants Received" section is EIN-exact. Asserts per intended target; the
    org:disclose case is the bounded-value gate (the worst over-merge must NOT render as an attributed total).
    Returns (violations, branch, has_prov) like check_invariants."""
    bad = []
    tell = [t for t in ERROR_TELLS if t in body]
    if tell:
        bad.append("server-error tell in body: " + ",".join(tell))
    if "Organization Not Found" in body:
        bad.append("'Organization Not Found' for an EIN derived from live data (stale target / routing broke)")
    has_name_attr = "matched by name" in body            # attributed-list label — resolve/show path ONLY
    disclosed     = ORG_DISCLOSE_MARK in body            # shared-name disclosure body
    search_link   = ORG_SEARCH_MARK in body              # the disclosure's grant-search link
    nudge         = "verify against the source" in body.lower()

    if intended == "org:disclose":
        # WORST over-merge (largest shared-name PF total). Must disclose, must NOT attribute a name total.
        if has_name_attr:
            bad.append("shared-name org rendered an attributed name-matched Foundation list — over-merge NOT "
                       "suppressed (#61 value regression: a name total is asserted under this EIN)")
        if not disclosed:
            bad.append("shared-name org did not render the resolve-or-disclose disclosure")
        if not search_link:
            bad.append("disclosure missing the grant-search link (bare suppression is a dead-end, not transparency)")
    elif intended == "org:unique":
        if not has_name_attr:
            bad.append("unique-name org did not render its name-matched Foundation list (resolve/show path broke)")
        if not nudge:
            bad.append("unique-name Foundation list missing the verify-before-citing nudge")
        if disclosed:
            bad.append("unique-name org wrongly rendered the shared-name disclosure")
    elif intended == "org:both":
        if "Schedule I Grants Received" not in body:
            bad.append("EIN-exact 'Schedule I Grants Received' section absent (the exact-match half broke)")

    branch = ("org:disclose" if disclosed else
              ("org:show(matched-by-name)" if has_name_attr else "org:schedI/other"))
    return bad, branch, has_name_attr


def check_assets(base, body, timeout):
    """Same-origin static asset refs in the rendered page must resolve (not 404)."""
    refs, bad = set(), []
    for ref in ASSET_RE.findall(body):
        if ref.startswith("data:") or ref.startswith("mailto:"):
            continue
        absu = urllib.parse.urljoin(base, ref)
        if not absu.startswith(base):
            continue                                   # external host — not ours to ship
        if ASSET_RE and (ASSET_EXT.search(ref) or ref.startswith("/static/")):
            refs.add(absu)
    for u in sorted(refs):
        st, _, infra = fetch(u, timeout, tries=2)
        if st != 200:
            bad.append(f"{urllib.parse.urlparse(u).path} -> {st}" + (" (infra)" if infra else ""))
    return sorted(refs), bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://data.datadawn.org")
    ap.add_argument("--db", default="990data_public.db")
    ap.add_argument("--surfaces", default="grant,charity_grant,daf,org")
    ap.add_argument("--timeout", type=float, default=25)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    base = a.base.rstrip("/")
    surfaces = [s.strip() for s in a.surfaces.split(",") if s.strip()]

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    targets, missing = derive_targets(con, surfaces)
    con.close()

    results, real_fails = [], 0
    provenance_seen = 0
    for t in targets:
        st, body, infra = fetch(base + t["path"], a.timeout)
        r = {**t, "status": st, "branch": None, "violations": []}
        if st != 200:
            r["violations"].append(f"HTTP {st}" + (" (infra after retries)" if infra else " (real)"))
            if not infra:
                real_fails += 1
        else:
            if t["surface"] == "org":
                bad, branch, has_prov = check_org_invariants(t["intended"], body)
            else:
                bad, branch, has_prov = check_invariants(t["intended"], body)
            r["branch"] = branch
            r["violations"] = bad
            provenance_seen += 1 if has_prov else 0
            if bad:
                real_fails += 1
        results.append(r)

    # cross-set: at least one page must render a "matched by" provenance label —
    # else the ER-QC disclosure templates aren't deployed (or the macro is broken).
    pages_200 = sum(1 for r in results if r["status"] == 200)
    coverage_fail = (provenance_seen == 0 and pages_200 > 0)
    if coverage_fail:
        real_fails += 1

    # deploy-coverage asset sweep: one page per surface (first 200 of each)
    asset_report, asset_fails = {}, 0
    seen_surface = set()
    for r in results:
        if r["status"] == 200 and r["surface"] not in seen_surface:
            seen_surface.add(r["surface"])
            st, body, infra = fetch(base + r["path"], a.timeout)
            if st == 200:
                refs, bad = check_assets(base, body, a.timeout)
                asset_report[r["surface"]] = {"checked": len(refs), "bad": bad}
                if bad:
                    asset_fails += len(bad)
    real_fails += asset_fails

    ok = real_fails == 0
    summary = {
        "ok": ok, "base": base, "targets": len(targets), "missing_branches": missing,
        "provenance_pages": provenance_seen, "coverage_fail": coverage_fail,
        "asset_fails": asset_fails, "results": results, "assets": asset_report,
    }
    if a.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(f"=== render_smoke vs {base}  ({'PASS' if ok else 'FAIL'}) ===")
        for r in results:
            mark = "ok  " if (r["status"] == 200 and not r["violations"]) else "FAIL"
            print(f"  [{mark}] {r['surface']:13} {r['path']:22} intended={r['intended']:22} "
                  f"-> HTTP {r['status']} branch={r['branch']}")
            for v in r["violations"]:
                print(f"           ! {v}")
        if missing:
            print(f"  (no live example for branches: {', '.join(missing)} — coverage gap, not a failure)")
        if coverage_fail:
            print(f"  ! coverage: 0/{pages_200} pages rendered a 'matched by' "
                  f"provenance label — ER-QC templates not deployed, or macro broken")
        for s, ar in asset_report.items():
            tag = "ok" if not ar["bad"] else "FAIL"
            print(f"  [asset:{tag}] {s}: {ar['checked']} same-origin refs checked"
                  + ("" if not ar["bad"] else "  -> " + "; ".join(ar["bad"])))
        print(f"=== {'PASS' if ok else 'FAIL'}  (real failures: {real_fails}) ===")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
