"""What is stored with each passage so it can be found. Never shown as the rule.

Two things are added to a passage before it is indexed:

  path   built in code: the source and what kind of source it is, the section
         title, and the opening line of every paragraph the passage sits under.
  note   written once by the larger model (llm.py): one or two sentences on what
         the passage is about, who it applies to, and the words advocates use.

Both go in the keyword index (the `context` column of `fts`) and in the text the
meaning vectors are made from (brain.py). Notes are kept in the `notes` table and
can be cleared and rebuilt without touching the source text.

    python app/notes.py paths              rebuild every path (no model), clear the vectors
    python app/notes.py write mltc         write notes for the managed long-term care sources
    python app/notes.py write all          ... for every approved source that has none
    python app/notes.py write 51 17        ... for these source ids
    python app/notes.py clear              delete all notes

After `paths` or `write`, start the app (or bench.py) once so the vectors are rebuilt.
"""
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import llm  # noqa: E402

KIND = {"ecfr": "federal regulation", "uscode": "federal statute", "nycrr": "New York State regulation"}
STEP = re.compile(r"^(.*\))\([A-Za-z0-9]{1,5}\)$|^(.*[^)])\([A-Za-z0-9]{1,5}\)$")
WINDOW = 9000         # characters of source text per model call
SYSTEM = """You are indexing New York Medicaid managed care rules so that helpline advocates can find them.

You are given numbered passages from one section of one source, in order. For each passage write one or two short sentences that say:
- what the passage is about and who it applies to (which kind of plan or program, the plan, the enrollee, the State);
- the plain words and the abbreviations an advocate would use for it (for example SNF for nursing facility, PCS for personal care services, aid continuing, fair hearing, UAS-NY), when they fit.

Describe only what the passage says, read in its place in the section. Do not add rules it does not state. Do not repeat the passage.
Reply with one line per passage, in order, in this form and nothing else:
[3] note for passage 3"""


def kind_label(kind, name):
    if kind in KIND:
        return KIND[kind]
    low = name.lower()
    if "contract" in low or "agreement" in low:
        return "contract"
    if "policy" in low or "guidance" in low or "guidelines" in low:
        return "State policy"
    if "manual" in low:
        return "manual"
    return "web page" if kind == "page" else "document"


def lead(text):
    """The opening of a paragraph: its first sentence, or its first 200 characters."""
    first = re.split(r"(?<=[.:;])\s", text.strip(), maxsplit=1)[0]
    return first if len(first) <= 200 else first[:200].rsplit(" ", 1)[0] + "…"


def above(cite):
    """The citations of the paragraphs a passage sits under: § 505.14(b)(2)(i)(c) -> (b), (b)(2), (b)(2)(i)."""
    out = []
    while True:
        m = re.match(r"^(.*)\([A-Za-z0-9]{1,5}\)$", cite)
        if not m or not m.group(1).endswith(")"):
            return out[::-1]
        cite = m.group(1)
        out.append(cite)


def build_paths(db, source_ids=None):
    """Write path (and note, when there is one) into the keyword index for every passage. Clears the vectors of those passages."""
    n = 0
    for s in db.execute("SELECT id, kind, name FROM sources").fetchall():
        if source_ids and s["id"] not in source_ids:
            continue
        rows = db.execute("SELECT p.id, p.cite, p.heading, p.text, n.terms AS note FROM passages p "
                          "LEFT JOIN notes n ON n.passage_id = p.id WHERE p.source_id=? ORDER BY p.id", (s["id"],)).fetchall()
        first = {}
        for r in rows:
            first.setdefault(r["cite"], r["text"])
        top = f"{s['name']} ({kind_label(s['kind'], s['name'])})"
        for r in rows:
            parts = [top]
            if r["heading"] and r["heading"].lower() not in s["name"].lower():
                parts.append(r["heading"])
            parts += [lead(first[c]) for c in above(r["cite"]) if c in first]
            if r["note"]:
                parts.append(r["note"])
            db.execute("UPDATE fts SET context=? WHERE rowid=?", ("\n".join(parts), r["id"]))
            n += 1
        db.execute("DELETE FROM vectors WHERE id IN (SELECT id FROM passages WHERE source_id=?)", (s["id"],))
    db.commit()
    return n


def windows(rows):
    """Consecutive passages of one section, in pieces small enough for one call."""
    out, cur, size = [], [], 0
    for r in rows:
        if cur and (size + len(r["text"]) > WINDOW or len(cur) >= 40):
            out.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += len(r["text"])
    if cur:
        out.append(cur)
    return out


def write_window(source, rows, context):
    """Notes for one window: {passage id: note}."""
    body = [f"Source: {source['name']} ({kind_label(source['kind'], source['name'])})", f"Section: {rows[0]['heading']}"]
    if context:
        body.append("The passages sit under: " + " > ".join(context))
    body.append("")
    body += [f"[{n}] {r['cite']}: {r['text'][:3000]}" for n, r in enumerate(rows, 1)]
    text = llm.complete(SYSTEM, "\n".join(body), max_tokens=8000) or ""
    out = {}
    for m in re.finditer(r"^\[(\d+)\]\s*(.+)$", text, re.M):
        n = int(m.group(1))
        if 1 <= n <= len(rows) and len(m.group(2)) > 15:
            out[rows[n - 1]["id"]] = m.group(2).strip()[:500]
    return out


def write_notes(db, source_ids, workers=8):
    """Notes for every passage of these sources that has none. Returns (passages noted, calls made)."""
    jobs = []
    for sid in source_ids:
        s = db.execute("SELECT id, kind, name FROM sources WHERE id=?", (sid,)).fetchone()
        rows = db.execute("SELECT p.id, p.section, p.cite, p.heading, p.text FROM passages p "
                          "WHERE p.source_id=? AND length(p.text) >= 40 ORDER BY p.id", (sid,)).fetchall()
        done = {r[0] for r in db.execute("SELECT passage_id FROM notes")}
        first, by_section = {}, {}
        for r in rows:
            first.setdefault(r["cite"], r["text"])
            by_section.setdefault(r["section"], []).append(r)
        for sec in by_section.values():
            for w in windows(sec):
                if all(r["id"] in done for r in w):
                    continue
                jobs.append((s, w, [lead(first[c]) for c in above(w[0]["cite"]) if c in first]))
    noted = 0
    with ThreadPoolExecutor(workers) as pool:
        for found in pool.map(lambda j: write_window(*j), jobs):
            for pid, note in found.items():
                db.execute("INSERT OR REPLACE INTO notes(passage_id, terms, questions, model, created) VALUES(?,?,?,?,?)",
                           (pid, note, "", llm.model_id(), datetime.now().isoformat(timespec="seconds")))
            noted += len(found)
            db.commit()
    return noted, len(jobs)


if __name__ == "__main__":
    import ingest
    db = ingest.open_db(str(HERE.parent / "data" / "oracle.db"))
    db.execute("CREATE TABLE IF NOT EXISTS vectors(id INTEGER PRIMARY KEY, vec BLOB)")
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    if cmd == "paths":
        print(build_paths(db), "passages given a path; vectors cleared")
    elif cmd == "clear":
        db.execute("DELETE FROM notes")
        db.commit()
        print(build_paths(db), "passages; notes deleted, vectors cleared")
    elif cmd == "write" and args:
        ok = [r[0] for r in db.execute("SELECT id FROM sources WHERE status='approved' ORDER BY id")]
        if args == ["mltc"]:
            ids = [r[0] for r in db.execute("SELECT id FROM sources WHERE status='approved' AND (name LIKE '%MLTC%' OR name LIKE '%long term%')")]
        else:
            ids = ok if args == ["all"] else [int(a) for a in args]
        t = time.time()
        noted, calls = write_notes(db, ids)
        build_paths(db, set(ids))
        print(f"{noted} passages noted in {calls} calls, {time.time() - t:.0f} s. Tokens in {llm.tokens_in}, out {llm.tokens_out}.")
    else:
        sys.exit(__doc__)
