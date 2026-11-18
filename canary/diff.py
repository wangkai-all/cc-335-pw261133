"""Field-and-row comparison between two rule versions over the same raw pages.

The comparison is deliberately *not* a total-row-count check.  A redesign can
leave the number of rows identical while splitting one row's fields across two
siblings (old rule sees two half-empty rows) or while silently dropping a field
(new rule stops finding `.brand`).  So the report has four layers:

1. **Coverage** -- which page versions have documents under both rules, and the
   switch boundary (highest fetch id at the moment the canary was approved) so
   pages fetched after approval are reported as not-yet-eligible rather than
   missing.
2. **Per-field presence** -- for each field, how many compared pages have a
   non-empty value under each rule.  A field that is present in the old rule
   and empty/absent in the new one is a field-loss finding, regardless of row
   counts.
3. **Per-field value agreement** -- among pages where both rules found a value,
   how often the values agree; disagreements list examples and are what a real
   review reads.
4. **Row structure** -- row count, duplicate row keys, rows whose title or price
   is empty while the other rule has a value for that key, and rows present in
   only one rule's key set.  This is what catches "split rows are two rows each
   missing half the fields" that a total count would call identical.

The result is ranked pass / review / fail.  Approval is a separate human action;
this module never changes state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import state

# Fields expected on an item document.  Order matters for the UI.
ITEM_FIELDS = ["title", "price", "brand", "sku", "stock", "description"]
LIST_ROW_FIELDS = ["title", "price", "url"]

_NUM = __import__("re").compile(r"-?\d+(?:\.\d+)?")


def relation(field: str, old: str, new: str) -> tuple[str, str, str]:
    """Compare one field's value in a field-aware, direction-aware way.

    Returns (verdict, old_norm, new_norm) where verdict is one of:
      equal       -- same value after field-appropriate normalization
      extended    -- new contains everything old had, plus more
      truncated   -- new is a strict prefix/subset of old: real data loss
      different   -- values genuinely conflict
    """
    old, new = (old or "").strip(), (new or "").strip()
    if old == new:
        return "equal", old, new
    if field in ("price", "stock"):
        om, nm = _NUM.search(old), _NUM.search(new)
        if om and nm and float(om.group()) == float(nm.group()):
            return "equal", om.group(), nm.group()
        return "different", old, new
    if field == "description":
        if new.startswith(old) or old in new:
            return "extended", old, new
        if old.startswith(new) or new in old:
            return "truncated", old, new
        return "different", old, new
    return "different", old, new


@dataclass
class FieldStat:
    field: str
    old_present: int = 0
    new_present: int = 0
    both_present: int = 0
    agree: int = 0
    extended: int = 0
    truncated: int = 0
    examples: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        compared = self.both_present
        return {
            "field": self.field,
            "old_present": self.old_present,
            "new_present": self.new_present,
            "both_present": compared,
            "agree": self.agree,
            "extended": self.extended,
            "truncated": self.truncated,
            "disagree": compared - self.agree,
            "old_only_missing_new": self.old_present - self.new_present,
            "new_only": self.new_present - self.old_present,
            "agreement": round(self.agree / compared, 4) if compared else None,
            "examples": self.examples[:5],
        }


class Comparer:
    def __init__(self, conn):
        self.conn = conn
        self.fields: dict[str, FieldStat] = {}
        self.row_issues: list[dict[str, Any]] = []
        self.pages: list[dict[str, Any]] = []
        self.missing_in_new: list[dict[str, Any]] = []
        self.missing_in_old: list[dict[str, Any]] = []

    def stat(self, name: str) -> FieldStat:
        if name not in self.fields:
            self.fields[name] = FieldStat(name)
        return self.fields[name]

    def page(self, old: dict, new: dict) -> None:
        old_fields = old.get("fields") or {}
        new_fields = new.get("fields") or {}
        kind = old.get("kind") or new.get("kind")
        if kind == "item":
            for name in ITEM_FIELDS:
                st = self.stat(name)
                ov = str(old_fields.get(name) or "").strip()
                nv = str(new_fields.get(name) or "").strip()
                if ov:
                    st.old_present += 1
                if nv:
                    st.new_present += 1
                if ov and nv:
                    st.both_present += 1
                    verdict, onorm, nnorm = relation(name, ov, nv)
                    if verdict == "equal":
                        st.agree += 1
                    elif verdict == "extended":
                        # new found additional content: benign, but visible
                        st.agree += 1
                        st.extended += 1
                    elif verdict == "truncated":
                        st.truncated += 1
                        if len(st.examples) < 5:
                            st.examples.append({
                                "url": old.get("url") or new.get("url"),
                                "old": ov, "new": nv, "kind": "truncated_in_new"})
                    elif len(st.examples) < 5:
                        st.examples.append({"url": old.get("url") or new.get("url"),
                                            "old": ov, "new": nv})
                elif ov and not nv and len(st.examples) < 5:
                    st.examples.append({"url": old.get("url") or new.get("url"),
                                        "old": ov, "new": "", "kind": "lost_in_new"})
                elif nv and not ov and len(st.examples) < 5:
                    st.examples.append({"url": old.get("url") or new.get("url"),
                                        "old": "", "new": nv, "kind": "new_found"})
        self.compare_rows(old, new)

    def compare_rows(self, old: dict, new: dict) -> None:
        """Structural comparison of list rows.

        Presence of each row field is counted over *all* rows, because a split
        row is exactly a row whose title exists and whose price does not.  Key
        agreement is matched by key when keys exist, otherwise row order.
        """
        orows = old.get("rows") or []
        nrows = new.get("rows") or []
        count_stat = self.stat("__row_count")
        count_stat.old_present += len(orows)
        count_stat.new_present += len(nrows)

        def keyed(rows):
            out = {}
            for i, r in enumerate(rows):
                k = str(r.get("key") or "")
                out[k or f"#row{i}"] = r
            return out

        old_by_key, new_by_key = keyed(orows), keyed(nrows)
        old_named = [str(r.get("key") or "") for r in orows]
        new_named = [str(r.get("key") or "") for r in nrows]
        old_dupes = len([k for k in old_named if k and old_named.count(k) > 1])
        new_dupes = len([k for k in new_named if k and new_named.count(k) > 1])
        old_missing_keys = sum(1 for k in old_named if not k)
        new_missing_keys = sum(1 for k in new_named if not k)

        def partial(rows):
            n = 0
            for r in rows:
                f = r.get("fields") or {}
                filled = sum(1 for name in LIST_ROW_FIELDS
                             if str(f.get(name) or "").strip())
                if 0 < filled < len(LIST_ROW_FIELDS):
                    n += 1
            return n

        old_partial, new_partial = partial(orows), partial(nrows)

        def row_issue(kind: str, **data) -> None:
            if len(self.row_issues) < 40:
                self.row_issues.append({"kind": kind,
                                        "url": old.get("url") or new.get("url"),
                                        **data})

        if old_partial or new_partial:
            row_issue("split_parts", old_partial=old_partial,
                      new_partial=new_partial, old_rows=len(orows),
                      new_rows=len(nrows),
                      regression=new_partial > old_partial)
        if old_dupes or new_dupes:
            row_issue("duplicate_keys", old_duplicates=old_dupes,
                      new_duplicates=new_dupes, old_rows=len(orows),
                      new_rows=len(nrows),
                      regression=new_dupes > old_dupes)
        if old_missing_keys or new_missing_keys:
            row_issue("keys_missing", old_missing=old_missing_keys,
                      new_missing=new_missing_keys,
                      regression=new_missing_keys > old_missing_keys)
        if len(orows) != len(nrows):
            # Row counts alone are ambiguous (a redesign splits or merges rows),
            # so this is informational unless the new rule also lost filled
            # values -- which the per-field stats below decide.
            row_issue("row_count", old_rows=len(orows), new_rows=len(nrows),
                      regression=False)

        # Presence per row field, counted over every row on each side.
        for fname in LIST_ROW_FIELDS:
            fs = self.stat(f"row.{fname}")
            for side_rows, is_old in ((orows, True), (nrows, False)):
                for r in side_rows:
                    v = str((r.get("fields") or {}).get(fname) or "").strip()
                    if v:
                        if is_old:
                            fs.old_present += 1
                        else:
                            fs.new_present += 1
        # Agreement matched by key where possible, else row position.
        for fname in LIST_ROW_FIELDS:
            fs = self.stat(f"row.{fname}")
            for k, o in old_by_key.items():
                n = new_by_key.get(k)
                ov = str((o.get("fields") or {}).get(fname) or "").strip()
                nv = str(((n or {}).get("fields") or {}).get(fname) or "").strip()
                if ov and nv:
                    fs.both_present += 1
                    verdict, _on, _nn = relation(fname, ov, nv)
                    if verdict in ("equal", "extended"):
                        fs.agree += 1
                        if verdict == "extended":
                            fs.extended += 1
                    elif verdict == "truncated":
                        fs.truncated += 1
                        if len(fs.examples) < 5:
                            fs.examples.append({
                                "url": old.get("url") or new.get("url"),
                                "key": k, "old": ov, "new": nv,
                                "kind": "truncated_in_new"})
                    elif len(fs.examples) < 5:
                        fs.examples.append({"url": old.get("url") or new.get("url"),
                                            "key": k, "old": ov, "new": nv})
                elif ov and not nv and len(fs.examples) < 5:
                    fs.examples.append({"url": old.get("url") or new.get("url"),
                                        "key": k, "old": ov, "new": "",
                                        "kind": "lost_in_new"})
        only_old = sorted(set(old_by_key) - set(new_by_key))
        only_new = sorted(set(new_by_key) - set(old_by_key))
        if only_old or only_new:
            row_issue("key_set", only_old=only_old[:10], only_new=only_new[:10],
                      regression=False)

    # -- report --------------------------------------------------------
    def summary(self, *, sample_size: int, coverage: dict) -> dict:
        field_dicts = [s.as_dict() for s in self.fields.values()]
        field_loss = []
        for f in field_dicts:
            if f["field"].startswith("__"):
                continue
            lost = f["old_present"] - f["new_present"]
            if f.get("truncated"):
                field_loss.append({"field": f["field"],
                                   "pages_lost": f["truncated"],
                                   "kind": "truncated_in_new"})
            elif lost > 0:
                field_loss.append({"field": f["field"], "pages_lost": lost})
        big_row_issues = [r for r in self.row_issues if r.get("regression")]
        structural_notes = [r for r in self.row_issues
                            if r["kind"] in ("duplicate_keys", "key_set",
                                             "split_parts", "keys_missing",
                                             "row_count")]
        agreement_values = [f["agreement"] for f in field_dicts
                            if f["field"] != "__row_count" and f["agreement"] is not None
                            and not f["field"].startswith("__")]
        min_agreement = min(agreement_values) if agreement_values else 1.0
        status = "pass"
        if field_loss or big_row_issues:
            status = "fail"
        elif structural_notes or (agreement_values and min_agreement < 0.999):
            status = "review"
        return {
            "status": status,
            "sample_size": sample_size,
            "field_loss": field_loss,
            "row_issue_count": len(self.row_issues),
            "row_regressions": len(big_row_issues),
            "row_issue_kinds": sorted({r["kind"] for r in self.row_issues}),
            "split_rows_seen": max([r.get("old_partial", 0) + r.get("new_partial", 0)
                                    for r in self.row_issues
                                    if r["kind"] == "split_parts"] or [0]),
            "duplicate_key_pages": len([r for r in self.row_issues
                                        if r["kind"] == "duplicate_keys"]),
            "split_part_pages": len([r for r in self.row_issues
                                     if r["kind"] == "split_parts"]),
            "row_count_mismatch_pages": len(
                [r for r in self.row_issues if r["kind"] == "row_count"]),
            "min_field_agreement": round(min_agreement, 4),
            "truncated_fields": [f["field"] for f in field_dicts
                                 if f.get("truncated")],
            "missing_in_new": len(self.missing_in_new),
            "missing_in_old": len(self.missing_in_old),
            "coverage": coverage,
        }


def _coverage(conn, epoch_row, boundary: int, canary_rule: int,
              baseline_rule: int) -> dict:
    """Which versions are eligible for the comparison and why."""
    rows = conn.execute(
        """SELECT f.id, f.kind FROM page_fetches f
            WHERE f.id <= ?
              AND f.kind = 'item'""",
        (boundary,),
    ).fetchall()
    eligible = [r["id"] for r in rows]
    in_scope_versions = conn.execute(
        """SELECT COUNT(*) AS n FROM publication_assignments a
             JOIN page_fetches f ON f.id=a.fetch_id
            WHERE a.epoch_id=? AND f.id <= ?""",
        (epoch_row["id"], boundary),
    ).fetchone()["n"]
    return {
        "boundary_fetch_id": boundary,
        "item_versions_at_boundary": len(eligible),
        "canary_assigned_versions": in_scope_versions,
        "canary_rule_id": canary_rule,
        "baseline_rule_id": baseline_rule,
        "eligible_fetch_ids": eligible,
    }


def compare(conn, old_rule_id: int, new_rule_id: int, *,
            epoch_id: int | None = None, sample: int = 40,
            only_fetch_ids: list[int] | None = None) -> dict:
    """Compare documents produced by two rules over identical page versions.

    Pages are paired by fetch id, not by URL: the two rules must see exactly the
    same raw bytes.  When `sample` is smaller than the paired set, pages are
    picked deterministically across the whole boundary (every Nth), so a
    100-version corpus is not reviewed by its first 10 pages.
    """
    epoch_row = None
    if epoch_id is not None:
        epoch_row = conn.execute("SELECT * FROM rule_epochs WHERE id=?",
                                 (epoch_id,)).fetchone()
    boundary = int(epoch_row["max_fetch_id_at_close"] or 0) if epoch_row is not None else 0
    if boundary == 0:
        boundary = conn.execute(
            "SELECT COALESCE(MAX(id),0) AS n FROM page_fetches").fetchone()["n"]

    where = ["d1.rule_id=?", "d2.rule_id=?", "f.id <= ?"]
    params: list[Any] = [old_rule_id, new_rule_id, boundary]
    if only_fetch_ids:
        where.append("f.id IN (%s)" % ",".join("?" * len(only_fetch_ids)))
        params.extend(only_fetch_ids)
    sql = (
        "SELECT f.id AS fetch_id, f.url, f.page_id, f.kind, "
        "       d1.doc_json AS old_doc, d2.doc_json AS new_doc "
        "  FROM page_fetches f "
        "  JOIN parsed_documents d1 ON d1.fetch_id=f.id "
        "  JOIN parsed_documents d2 ON d2.fetch_id=f.id "
        " WHERE " + " AND ".join(where) +
        " ORDER BY f.id"
    )
    pairs = conn.execute(sql, params).fetchall()
    total_pairs = len(pairs)
    rows = list(pairs)
    if sample and sample < len(rows):
        step = len(rows) / float(sample)
        rows = [rows[int(i * step)] for i in range(sample)]

    comparer = Comparer(conn)
    compared = []
    for row in rows:
        if row["kind"] != "item" and not row["new_doc"]:
            continue
        old = json.loads(row["old_doc"])
        new = json.loads(row["new_doc"])
        # Restore the URL for examples; documents store it from the fetch.
        if "url" not in old:
            old["url"] = row["url"]
        if "url" not in new:
            new["url"] = row["url"]
        comparer.page(old, new)
        compared.append({
            "fetch_id": row["fetch_id"], "page_id": row["page_id"],
            "kind": row["kind"], "url": row["url"],
            "old_fields": old.get("fields") or {},
            "new_fields": new.get("fields") or {},
            "old_rows": old.get("rows") or [],
            "new_rows": new.get("rows") or [],
            "row_count_eq": len(old.get("rows") or []) == len(new.get("rows") or []),
        })

    # Pages present under one rule only -- coverage holes, distinct from field
    # loss, and never hidden behind a row-count total.
    old_ids = {r["fetch_id"] for r in conn.execute(
        "SELECT fetch_id FROM parsed_documents WHERE rule_id=? AND fetch_id<=?",
        (old_rule_id, boundary))}
    new_ids = {r["fetch_id"] for r in conn.execute(
        "SELECT fetch_id FROM parsed_documents WHERE rule_id=? AND fetch_id<=?",
        (new_rule_id, boundary))}
    comparer.missing_in_new = sorted(old_ids - new_ids)
    comparer.missing_in_old = sorted(new_ids - old_ids)

    coverage = _coverage(conn, epoch_row, boundary, new_rule_id, old_rule_id) \
        if epoch_row is not None else {
            "boundary_fetch_id": boundary,
            "item_versions_at_boundary": total_pairs,
            "canary_assigned_versions": None,
            "canary_rule_id": new_rule_id,
            "baseline_rule_id": old_rule_id,
            "eligible_fetch_ids": [],
        }
    coverage["paired_versions"] = total_pairs
    coverage["compared_versions"] = len(compared)
    summary = comparer.summary(sample_size=len(compared), coverage=coverage)
    summary["total_pairs"] = total_pairs

    report = {
        "summary": summary,
        "fields": [s.as_dict() for s in comparer.fields.values()],
        "row_issues": comparer.row_issues,
        "pages": compared,
        "missing_in_new": comparer.missing_in_new,
        "missing_in_old": comparer.missing_in_old,
        "coverage": coverage,
    }
    return report


def save_report(conn, epoch_id: int, report: dict, *, notes: str = "") -> int:
    report["summary"].setdefault("coverage", report.get("coverage"))
    signature = json.dumps(report["summary"], sort_keys=True, ensure_ascii=False)
    cur = conn.execute(
        "INSERT INTO diff_reports(epoch_id, old_rule_id, new_rule_id, created_at, "
        "status, sample_size, signature, summary_json, field_json, row_json, "
        "coverage_json, pages_json, boundary_fetch_id, notes) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            epoch_id,
            report["coverage"]["baseline_rule_id"],
            report["coverage"]["canary_rule_id"],
            state.now(),
            report["summary"]["status"],
            report["summary"]["sample_size"],
            signature,
            json.dumps(report["summary"], ensure_ascii=False),
            json.dumps(report["fields"], ensure_ascii=False),
            json.dumps(report["row_issues"], ensure_ascii=False),
            json.dumps(report["coverage"], ensure_ascii=False),
            json.dumps(report["pages"], ensure_ascii=False),
            int(report["coverage"]["boundary_fetch_id"]),
            notes,
        ),
    )
    return cur.lastrowid


def load_report(conn, report_id: int) -> dict:
    row = conn.execute("SELECT * FROM diff_reports WHERE id=?", (report_id,)).fetchone()
    if row is None:
        return {}
    return {
        "id": row["id"],
        "epoch_id": row["epoch_id"],
        "created_at": row["created_at"],
        "status": row["status"],
        "boundary_fetch_id": row["boundary_fetch_id"],
        "notes": row["notes"],
        "summary": json.loads(row["summary_json"]),
        "fields": json.loads(row["field_json"]),
        "row_issues": json.loads(row["row_json"]),
        "coverage": json.loads(row["coverage_json"]),
        "pages": json.loads(row["pages_json"]),
    }
