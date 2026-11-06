"""Quote-to-order handoff assessment.

Reads the five CSV files in data/, runs the checks listed in docs/checks.md,
and writes one row per opportunity to output/handoff_assessment.csv plus a
review queue page at output/queue.html.

Rule checks run in SQLite (built into Python). The only AI input is
ai_cache.json, written by extract_notes.py. No packages to install.

Run:  python3 handoff.py
"""

import csv
import html
import json
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
AI_CACHE = ROOT / "ai_cache.json"

# Settings (see docs/checks.md)
AS_OF_DATE = date(2026, 2, 10)  # latest event date in the data (SIG-004 signal_date)
SOON_DAYS = 14
STALE_CONTACT_DAYS = 365
DISTRICT_LEVEL_TITLE_WORDS = ("superintendent", "chief", "director")
INACTIVE_ORDER_STATUSES = ("cancelled", "canceled", "void", "voided")

FILES = {
    "opportunities": "opportunities.csv",
    "orders": "orders.csv",
    "handoff_notes": "handoff_notes.csv",
    "account_directory": "account_directory.csv",
    "account_signals": "account_signals.csv",
}

STATUS_ORDER = {"Blocked": 0, "Needs Review": 1, "Not Yet Eligible": 2, "Ready": 3}


# ---------------------------------------------------------------- loading

def load_database(data_dir=DATA_DIR):
    """Load every CSV into an in-memory SQLite table. All values are text, trimmed."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    for table, filename in FILES.items():
        with open(data_dir / filename, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        columns = list(rows[0].keys())
        db.execute(f"CREATE TABLE {table} ({', '.join(c + ' TEXT' for c in columns)})")
        db.executemany(
            f"INSERT INTO {table} VALUES ({', '.join('?' for _ in columns)})",
            [[(r[c] or "").strip() for c in columns] for r in rows],
        )
    inactive = ", ".join(f"'{x}'" for x in INACTIVE_ORDER_STATUSES)
    db.execute(f"CREATE VIEW active_orders AS SELECT * FROM orders WHERE lower(order_status) NOT IN ({inactive})")
    db.execute(f"CREATE VIEW inactive_orders AS SELECT * FROM orders WHERE lower(order_status) IN ({inactive})")
    return db


# ---------------------------------------------------------------- rule checks (SQL)

RULE_CHECKS_SQL = """
SELECT
  o.*,
  (SELECT COUNT(*) FROM active_orders x WHERE x.opportunity_id = o.opportunity_id) AS order_count,
  (SELECT GROUP_CONCAT(x.order_id || ' (' || x.order_status || ', ' || x.product_summary || ')', '; ')
     FROM active_orders x WHERE x.opportunity_id = o.opportunity_id)       AS order_list,
  -- E1: stage must be Closed Won
  o.stage <> 'Closed Won'                                                  AS e1_fail,
  -- B1: approval_status must be Approved
  o.approval_status <> 'Approved'                                          AS b1_fail,
  -- B2: quote_expiration must be on or after as_of_date (blank or bad date fails)
  (date(o.quote_expiration) IS NULL OR date(o.quote_expiration) < :as_of) AS b2_fail,
  -- B3: billing_entity must not be blank
  o.billing_entity = ''                                                    AS b3_fail,
  -- B4: implementation_start_date must exist and be on or after as_of_date
  (date(o.implementation_start_date) IS NULL
     OR date(o.implementation_start_date) < :as_of)                        AS b4_fail,
  -- W1: quote still valid but expires within soon_days
  (date(o.quote_expiration) >= :as_of AND date(o.quote_expiration) <= :soon) AS w1_flag,
  -- W2: implementation starts within soon_days and no order exists
  (date(o.implementation_start_date) >= :as_of
     AND date(o.implementation_start_date) <= :soon
     AND NOT EXISTS (SELECT 1 FROM active_orders x WHERE x.opportunity_id = o.opportunity_id)) AS w2_flag,
  -- W3: not Closed Won and close_date already passed
  (o.stage <> 'Closed Won' AND date(o.close_date) < :as_of)                AS w3_flag
FROM opportunities o
ORDER BY o.opportunity_id
"""


def run_rule_checks(db):
    params = {"as_of": AS_OF_DATE.isoformat(), "soon": (AS_OF_DATE + timedelta(days=SOON_DAYS)).isoformat()}
    return [dict(r) for r in db.execute(RULE_CHECKS_SQL, params)]


# ---------------------------------------------------------------- name matching (rule, no AI)

def clean_name(name):
    """Lowercase, drop punctuation, and read a final 'sd' as 'school district'."""
    cleaned = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return re.sub(r"\bsd$", "school district", cleaned)


def match_names(account_name, other_name):
    """Return (confidence, rationale) or None if the names do not match."""
    if account_name == other_name:
        return "High", "Exact name match."
    if clean_name(account_name) == clean_name(other_name):
        return "Medium", f'"{other_name}" matches "{account_name}" after cleaning the name.'
    return None


# ---------------------------------------------------------------- AI results

def normalize_space(text):
    return re.sub(r"\s+", " ", text or "").strip()


def evidence_is_valid(evidence, note_text):
    """Evidence counts only if it is an exact quote from the note."""
    return bool(normalize_space(evidence)) and normalize_space(evidence) in normalize_space(note_text)


def compact(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def number_in(value, text):
    return any(float(n) == float(value) for n in re.findall(r"\d+(?:\.\d+)?", text or ""))


def validated_ai_facts(raw, note_text):
    """Keep only AI facts whose evidence is quoted from the note AND contains the fact itself
    (the number, product or billing name). Returns (facts, dropped)."""
    facts = {"approved_discount_pct": None, "products": [], "billing_hint": None,
             "asks_to_start_fulfillment": False}
    dropped = []

    pct = raw.get("approved_discount_pct")
    if pct is not None:
        if (evidence_is_valid(raw.get("approved_discount_evidence"), note_text)
                and number_in(pct, raw["approved_discount_evidence"])):
            facts["approved_discount_pct"] = float(pct)
            facts["approved_discount_evidence"] = raw["approved_discount_evidence"]
        else:
            dropped.append("approved_discount_pct")

    for product in raw.get("products") or []:
        if (product.get("name") and evidence_is_valid(product.get("evidence"), note_text)
                and compact(product["name"]) in compact(product["evidence"])):
            facts["products"].append(product)
        else:
            dropped.append(f"product:{product.get('name')}")

    if raw.get("billing_hint"):
        if (evidence_is_valid(raw.get("billing_hint_evidence"), note_text)
                and compact(raw["billing_hint"]) in compact(raw["billing_hint_evidence"])):
            facts["billing_hint"] = raw["billing_hint"]
            facts["billing_hint_evidence"] = raw["billing_hint_evidence"]
        else:
            dropped.append("billing_hint")

    if raw.get("asks_to_start_fulfillment"):
        if evidence_is_valid(raw.get("asks_to_start_fulfillment_evidence"), note_text):
            facts["asks_to_start_fulfillment"] = True
            facts["asks_to_start_fulfillment_evidence"] = raw["asks_to_start_fulfillment_evidence"]
        else:
            dropped.append("asks_to_start_fulfillment")

    return facts, dropped


def load_ai_results(cache_path, notes):
    """Return {opportunity_id: facts} for notes with a valid, current AI result."""
    if not Path(cache_path).exists():
        return {}
    cache = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    results = {}
    for opp_id, entry in cache.get("results", {}).items():
        note_text = notes.get(opp_id)
        if note_text is None or normalize_space(entry.get("note_text")) != normalize_space(note_text):
            continue  # note changed since the AI read it, so the result is out of date
        facts, dropped = validated_ai_facts(entry.get("facts") or {}, note_text)
        facts["dropped"] = dropped
        results[opp_id] = facts
    return results


def product_matches(product_name, product_summary):
    a = re.sub(r"[^a-z0-9 ]", "", product_name.lower())
    b = re.sub(r"[^a-z0-9 ]", "", product_summary.lower())
    return bool(a and b) and (a in b or b in a)


# ---------------------------------------------------------------- assessment

ACTIONS = {
    "E1": "Do not start the order. The rep updates the stage once the contract is signed.",
    "B1": "Confirm which discount was actually approved, then get the quote approved before building the order.",
    "B2": "Confirm the quote is still valid or issue a new quote.",
    "B3": "Confirm the legal billing name and fill in billing_entity.",
    "B4": "Confirm the implementation start date.",
    "R1": "Confirm whether both orders are needed; if not, cancel the duplicate.",
    "R2": "Check the note against the quote with Finance before changing the discount.",
    "R3": "Add the missing product to the order or confirm it ships separately.",
    "R4": "Read the handoff note by hand; the AI result is missing or out of date.",
}


def assess(data_dir=DATA_DIR, cache_path=AI_CACHE):
    db = load_database(data_dir)
    notes = {r["opportunity_id"]: r["note_text"] for r in db.execute("SELECT * FROM handoff_notes")}
    directory = [dict(r) for r in db.execute("SELECT * FROM account_directory")]
    signals = [dict(r) for r in db.execute("SELECT * FROM account_signals")]
    orders = [dict(r) for r in db.execute("SELECT * FROM active_orders")]
    inactive_orders = [dict(r) for r in db.execute("SELECT * FROM inactive_orders")]
    owners = {r["owner"].lower() for r in db.execute("SELECT owner FROM opportunities")}
    ai = load_ai_results(cache_path, notes)

    results = []
    for opp in run_rule_checks(db):
        opp_id = opp["opportunity_id"]
        failed, warnings, signal_matches = [], [], []

        # Eligibility and blocking checks (rules). If E1 fails, B and R checks wait.
        if opp["e1_fail"]:
            failed.append("E1")
        else:
            failed += [c for c in ("B1", "B2", "B3", "B4") if opp[c.lower() + "_fail"]]
            if opp["order_count"] > 1:
                failed.append("R1")

            # AI-read checks: the AI pulls facts from the note, a rule compares them.
            facts = ai.get(opp_id)
            if facts is None:
                failed.append("R4")
            else:
                pct = facts["approved_discount_pct"]
                if pct is not None and pct != float(opp["discount_pct"] or "nan"):
                    failed.append("R2")
                    warnings.append(f'R2: note says {pct:g}% approved ("{facts["approved_discount_evidence"]}"), '
                                    f'quote shows {opp["discount_pct"]}%.')
                opp_orders = [o for o in orders if o["opportunity_id"] == opp_id]
                if opp_orders:
                    missing = [p for p in facts["products"]
                               if not any(product_matches(p["name"], o["product_summary"]) for o in opp_orders)]
                    if missing:
                        failed.append("R3")
                        for p in missing:
                            warnings.append(f'R3: note mentions "{p["name"]}" ("{p["evidence"]}"), not on any order.')

        # Warnings from rules
        for o in inactive_orders:
            if o["opportunity_id"] == opp_id:
                warnings.append(f"W10: order {o['order_id']} is {o['order_status']}; it does not count as an order.")
        if opp["w1_flag"]:
            warnings.append(f"W1: quote expires {opp['quote_expiration']}.")
        if opp["w2_flag"]:
            warnings.append(f"W2: implementation starts {opp['implementation_start_date']} and no order exists.")
        if opp["w3_flag"]:
            warnings.append(f"W3: close_date {opp['close_date']} has passed but stage is {opp['stage']}.")

        contacts = [d for d in directory if match_names(opp["account_name"], d["district_name"])]
        if not contacts:
            warnings.append("W4: district not found in account_directory.")
        for c in contacts:
            verified = c["last_verified_date"]
            try:
                age = (AS_OF_DATE - date.fromisoformat(verified)).days
            except ValueError:
                age = None
            if age is None or age > STALE_CONTACT_DAYS:
                warnings.append(f"W5: {c['contact_name']} last verified {verified or 'never'}; re-verify.")
            if c["school_id"] and any(w in c["title"].lower() for w in DISTRICT_LEVEL_TITLE_WORDS):
                warnings.append(f"W6: {c['title']} {c['contact_name']} is filed under {c['school_name']}, "
                                f"but the title is district-level.")
            if c["contact_name"].lower() in owners:
                warnings.append(f"W7: contact {c['contact_name']} has the same name as a sales rep; confirm the contact.")

        for s in signals:
            match = match_names(opp["account_name"], s["organization_name"])
            if match:
                confidence, rationale = match
                after_close = s["signal_date"] > opp["close_date"]
                signal_matches.append({
                    "signal_id": s["signal_id"], "signal_type": s["signal_type"],
                    "description": s["description"], "signal_date": s["signal_date"],
                    "source": s["source"], "source_quality": s["source_quality"],
                    "match_confidence": confidence, "rationale": rationale,
                    "after_close_date": after_close,
                })
                warnings.append(f"W8: {s['signal_type']} on {s['signal_date']} ({s['source']}, "
                                f"{s['source_quality']} quality, {confidence} match): {s['description']}. "
                                + (f"{rationale} " if confidence != "High" else "")
                                + ("Came after close_date." if after_close else ""))

        # Warnings from AI-read facts (never change handoff_status)
        facts = ai.get(opp_id)
        if facts:
            if facts["asks_to_start_fulfillment"] and opp["stage"] != "Closed Won":
                warnings.append(f'W9: note asks to start work ("{facts["asks_to_start_fulfillment_evidence"]}") '
                                f"but stage is {opp['stage']}.")
            if facts["billing_hint"] and not opp["billing_entity"]:
                warnings.append(f'W9: note suggests billing "{facts["billing_hint"]}" '
                                f'("{facts["billing_hint_evidence"]}"); a person must confirm the legal name.')

        # handoff_status: first match wins (see docs/checks.md)
        if "E1" in failed:
            status = "Not Yet Eligible"
        elif any(c.startswith("B") for c in failed):
            status = "Blocked"
        elif any(c.startswith("R") for c in failed):
            status = "Needs Review"
        else:
            status = "Ready"

        if "R4" in failed:
            confidence = "Low"
        elif status == "Needs Review" and not {"R1"} & set(failed):
            confidence = "Medium"  # an AI finding is the reason for review
        else:
            confidence = "High"

        if failed:
            next_action = " Then: ".join(ACTIONS[c] for c in failed)
        elif opp["order_count"]:
            next_action = f"Order already exists: {opp['order_list']}. Continue normal processing."
        else:
            next_action = "Build the order."

        warning_ids = sorted({w.split(":")[0] for w in warnings if re.match(r"W\d", w)})
        results.append({
            "opportunity_id": opp_id,
            "account_name": opp["account_name"],
            "amount": opp["amount"],
            "handoff_status": status,
            "exception_types": "; ".join(failed + warning_ids),
            "validated_facts": "; ".join([
                f"stage={opp['stage']}", f"approval_status={opp['approval_status']}",
                f"quote_expiration={opp['quote_expiration']}", f"discount_pct={opp['discount_pct']}",
                f"billing_entity={opp['billing_entity'] or '(blank)'}",
                f"implementation_start_date={opp['implementation_start_date']}",
                f"orders={opp['order_list'] or 'none'}",
            ]),
            "confidence": confidence,
            "recommended_next_action": next_action,
            "owner": opp["owner"] if status == "Not Yet Eligible" else "Deal Operations",
            "opportunity_owner": opp["owner"],
            "review_status": "Pending review" if status in ("Blocked", "Needs Review") else "Not needed",
            "warnings": " | ".join(w for w in warnings if not re.match(r"R\d", w)),
            "ai_findings": " | ".join(w for w in warnings if re.match(r"R\d", w)),
            "signal_matches": json.dumps(signal_matches),
        })
    return results


# ---------------------------------------------------------------- output

def write_csv(results, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)


def write_queue_html(results, path):
    rows = sorted(results, key=lambda r: (STATUS_ORDER[r["handoff_status"]], r["opportunity_id"]))
    colors = {"Blocked": "#b42318", "Needs Review": "#b54708", "Not Yet Eligible": "#475467", "Ready": "#067647"}
    cards = []
    for r in rows:
        items = [i for i in (r["ai_findings"] + " | " + r["warnings"]).split(" | ") if i.strip()]
        cards.append(f"""
<section class="card">
  <div class="top">
    <span class="status" style="background:{colors[r['handoff_status']]}">{html.escape(r['handoff_status'])}</span>
    <strong>{html.escape(r['opportunity_id'])} · {html.escape(r['account_name'])}</strong>
    <span class="amount">${int(r['amount']):,}</span>
  </div>
  <p><b>Next action ({html.escape(r['owner'])}):</b> {html.escape(r['recommended_next_action'])}</p>
  <p class="meta">Confidence: {html.escape(r['confidence'])} · Review: {html.escape(r['review_status'])} ·
     Checks: {html.escape(r['exception_types'] or 'none')}</p>
  {'<ul>' + ''.join(f'<li>{html.escape(i)}</li>' for i in items) + '</ul>' if items else ''}
</section>""")
    counts = {s: sum(r["handoff_status"] == s for r in results) for s in STATUS_ORDER}
    summary = " · ".join(f"{s}: {n}" for s, n in counts.items())
    Path(path).write_text(f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Handoff review queue</title>
<style>
 body{{font-family:Arial,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#101828}}
 h1{{font-size:22px;margin-bottom:4px}} .sub{{color:#475467;margin-top:0}}
 .card{{border:1px solid #d0d5dd;border-radius:8px;padding:12px 16px;margin:12px 0}}
 .top{{display:flex;gap:10px;align-items:center}} .amount{{margin-left:auto;color:#475467}}
 .status{{color:#fff;border-radius:4px;padding:2px 8px;font-size:13px}}
 .meta{{color:#475467;font-size:13px}} li{{margin:4px 0;font-size:14px}}
</style></head><body>
<h1>Handoff review queue</h1>
<p class="sub">As of {AS_OF_DATE.isoformat()} · {summary} · Synthetic data</p>
{''.join(cards)}
</body></html>
""", encoding="utf-8")


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    results = assess()
    write_csv(results, OUTPUT_DIR / "handoff_assessment.csv")
    write_queue_html(results, OUTPUT_DIR / "queue.html")
    for r in results:
        print(f"{r['opportunity_id']}  {r['handoff_status']:<17} {r['exception_types']}")
    if not AI_CACHE.exists():
        print("\nNote: ai_cache.json not found. Run extract_notes.py first; AI-read checks show as R4.")


if __name__ == "__main__":
    main()
