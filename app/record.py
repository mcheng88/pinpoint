"""The case record: cases, the questions asked in them, the rules kept, the grades.

Kept in the same SQLite file as the sources. No model reads any of it. A case
has the advocate's own label and five case facts; there is no field for a name,
an ID number or an address. Questions are stored as they read after redaction.

For the demo this is the whole record. In real use the case lives in whatever
case system the organisation already has, and this module is where it connects.
"""
import json
import re
from datetime import datetime

FACTS = ("plan", "what", "service", "notice", "still")
GRADES = ("good", "bad", "missing")
OUTCOMES = ("Resolved: the client got what they asked for", "Resolved in part", "Not resolved",
            "Referred to another organization", "Lost contact with the client")


def now():
    return datetime.now().isoformat(timespec="seconds")


def clean_facts(facts):
    return {k: (str((facts or {}).get(k) or "").strip()[:80] or None) for k in FACTS}


def save_case(db, cid, label, facts):
    """Create the case, or update its label and facts. Returns its id."""
    label = str(label or "").strip()[:80]
    facts = json.dumps(clean_facts(facts))
    if cid and db.execute("SELECT 1 FROM cases WHERE id=?", (cid,)).fetchone():
        db.execute("UPDATE cases SET label=?, facts=?, updated=? WHERE id=?", (label, facts, now(), cid))
    else:
        cid = db.execute("INSERT INTO cases(label,facts,created,updated) VALUES(?,?,?,?)", (label, facts, now(), now())).lastrowid
    db.commit()
    return cid


def set_outcome(db, cid, outcome, note):
    """How the case ended, as the advocate records it. An empty outcome means the case is still open."""
    outcome = outcome if outcome in OUTCOMES else None
    db.execute("UPDATE cases SET outcome=?, outcome_note=?, outcome_at=?, updated=? WHERE id=?",
               (outcome, str(note or "").strip()[:500] or None, now() if outcome else None, now(), cid))
    db.commit()


def cases(db):
    return [{"id": r["id"], "label": r["label"] or f"Case {r['id']}", "updated": r["updated"], "asks": r["n"], "outcome": r["outcome"]}
            for r in db.execute("SELECT c.*, (SELECT count(*) FROM asks a WHERE a.case_id=c.id) AS n FROM cases c ORDER BY c.updated DESC LIMIT 100")]


def case(db, cid):
    """One case with everything needed to put it back on the screen."""
    c = db.execute("SELECT * FROM cases WHERE id=?", (cid,)).fetchone()
    if not c:
        return None
    asks = []
    for a in db.execute("SELECT * FROM asks WHERE case_id=? ORDER BY id", (cid,)):
        reply = json.loads(a["reply"])
        reply.update(ask_id=a["id"], grade=a["grade"], grade_note=a["grade_note"])
        asks.append(reply)
    kept = [dict(r) for r in db.execute(
        "SELECT p.id, p.cite, p.heading, p.text, p.url FROM saved s JOIN passages p ON p.id = s.passage_id WHERE s.case_id=? ORDER BY s.rowid", (cid,))]
    return {"id": c["id"], "label": c["label"], "facts": json.loads(c["facts"]), "asks": asks, "saved": kept,
            "outcome": c["outcome"], "outcome_note": c["outcome_note"]}


def log_ask(db, cid, reply, facts, seconds):
    """Write one question and what was shown for it. `reply` is what search returned."""
    keep = {k: v for k, v in reply.items() if k != "searched"}      # the source list is re-read when the case is reopened
    cites = [p["cite"] for p in reply.get("passages", [])]
    aid = db.execute(
        "INSERT INTO asks(case_id,created,question,facts,kind,cites,reply,short_text,short_by,seconds) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (cid, now(), reply["sent"], json.dumps(clean_facts(facts)), reply["kind"], json.dumps(cites), json.dumps(keep),
         (reply.get("short") or {}).get("text"), "reader" if reply.get("short") else None, round(seconds, 2))).lastrowid
    db.execute("UPDATE cases SET updated=? WHERE id=?", (now(), cid))
    db.commit()
    return aid


def set_answer(db, aid, extra):
    """Add the summary and its cards (or the 'none' the larger model returned) to a question already written down."""
    a = db.execute("SELECT reply FROM asks WHERE id=?", (aid,)).fetchone()
    if not a:
        return
    reply = {**json.loads(a["reply"]), **extra}
    cites = [l["cite"] for c in reply.get("cards", []) for l in c["lines"]] or [p["cite"] for p in reply.get("passages", [])]
    text = " ".join(s["text"] for s in reply.get("summary", [])) or None
    db.execute("UPDATE asks SET reply=?, kind=?, cites=?, short_text=?, short_by=? WHERE id=?",
               (json.dumps(reply), reply["kind"], json.dumps(cites), text, extra.get("by"), aid))
    db.commit()


def stored(db, aid):
    a = db.execute("SELECT reply, facts FROM asks WHERE id=?", (aid,)).fetchone()
    return ({**json.loads(a["reply"]), "facts": json.loads(a["facts"])}) if a else None


def set_second(db, aid, found):
    """Keep the second read with the question: the sentences as they stand after it, and what it found."""
    a = db.execute("SELECT reply FROM asks WHERE id=?", (aid,)).fetchone()
    if not a:
        return
    reply = {**json.loads(a["reply"]), **found}
    db.execute("UPDATE asks SET reply=?, short_text=? WHERE id=?", (json.dumps(reply), " ".join(s["text"] for s in reply["summary"]), aid))
    db.commit()


def grade(db, aid, value, note=None):
    """good / bad / missing, or None to clear. Returns the question row, so a 'missing' can be flagged as a gap."""
    if value not in GRADES + (None,):
        raise ValueError("Unknown grade.")
    db.execute("UPDATE asks SET grade=?, grade_note=? WHERE id=?", (value, (str(note).strip()[:500] or None) if note else None, aid))
    db.commit()
    return db.execute("SELECT * FROM asks WHERE id=?", (aid,)).fetchone()


def keep(db, cid, pid, on):
    db.execute("DELETE FROM saved WHERE case_id=? AND passage_id=?", (cid, pid))
    if on:
        db.execute("INSERT INTO saved(case_id,passage_id,created) VALUES(?,?,?)", (cid, pid, now()))
    db.commit()


def delete_case(db, cid):
    """Remove a case with its questions (and their grades) and its saved rules."""
    db.execute("DELETE FROM saved WHERE case_id=?", (cid,))
    db.execute("DELETE FROM asks WHERE case_id=?", (cid,))
    db.execute("DELETE FROM cases WHERE id=?", (cid,))
    db.commit()


def record(db, limit=300):
    """Every question with what was shown and how it was graded, newest first. For the Sources screen."""
    rows = []
    for a in db.execute("SELECT a.*, c.label, c.outcome, c.outcome_note FROM asks a JOIN cases c ON c.id = a.case_id ORDER BY a.id DESC LIMIT ?", (limit,)):
        rows.append({"id": a["id"], "case": a["label"] or f"Case {a['case_id']}", "created": a["created"], "question": a["question"],
                     "facts": json.loads(a["facts"]), "kind": a["kind"], "cites": json.loads(a["cites"]), "short": a["short_text"],
                     "short_by": a["short_by"], "seconds": a["seconds"], "grade": a["grade"], "note": a["grade_note"],
                     "outcome": a["outcome"], "outcome_note": a["outcome_note"]})
    count = {g: sum(1 for r in rows if r["grade"] == g) for g in GRADES}
    return {"asks": rows, "count": {**count, "none": sum(1 for r in rows if not r["grade"]), "all": len(rows)}}


def find(db, words, limit=40):
    """Passages of the approved sources that contain every word typed. A keyword search: no model reads anything."""
    terms = re.findall(r"[\w§.\-]+", str(words)[:200])
    terms = [t.strip(".-") for t in terms if len(t.strip(".-")) > 1][:8]
    if not terms:
        return {"words": [], "count": 0, "rows": []}
    match = " ".join('"' + t.replace('"', "") + '"' for t in terms)
    sql = ("FROM fts JOIN passages p ON p.id = fts.rowid JOIN sources s ON s.id = p.source_id "
           "WHERE fts MATCH ? AND s.status = 'approved'")
    count = db.execute("SELECT count(*) " + sql, (match,)).fetchone()[0]
    rows = db.execute("SELECT p.id, p.cite, p.url, s.name AS source, s.id AS source_id, "
                      "snippet(fts, 0, '\x01', '\x02', ' … ', 40) AS part " + sql + " ORDER BY rank LIMIT ?", (match, limit)).fetchall()
    return {"words": terms, "count": count, "rows": [dict(r) for r in rows]}
