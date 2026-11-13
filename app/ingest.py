"""The knowledge base: load sources, add new ones, check them for changes.

First build (from the files in data/ecfr):
    python3 -I app/ingest.py data/ecfr data/oracle.db

Today every loaded source is a part (or subpart) of the Code of Federal
Regulations from ecfr.gov, which has a clean API. Each paragraph of each
section becomes one passage with the source's own text, a citation such as
"42 CFR § 438.56(b)(2)", and a link to that paragraph. The other kinds of
source on CSS's list (US Code pages, New York statutes and rules, DOH
guidance pages and PDFs) need their own loaders; see README.md.

A source has a status:
    review    added, not yet approved; the Oracle does not search it
    approved  searched
    changed   ecfr.gov has a newer version than the one loaded; needs a reload
    retired   kept for the record, not searched
"""
import gzip
import hashlib
import json
import re
import sqlite3
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

API = "https://www.ecfr.gov/api/versioner/v1"
UA = {"User-Agent": "health-equity-sprint oracle (research prototype)", "Accept-Encoding": "gzip"}

ROMAN = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii",
         "xiii", "xiv", "xv", "xvi", "xvii", "xviii", "xix", "xx"]
LEAD = re.compile(r"^((?:\(\s*[A-Za-z0-9]{1,5}\s*\)\s*)+)")
TOKEN = re.compile(r"\(\s*([A-Za-z0-9]{1,5})\s*\)")
DASH = re.compile(r"^[^.;:()]{0,160}?—\s*\(\s*([A-Za-z0-9]{1,5})\s*\)\s*")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
    id INTEGER PRIMARY KEY, kind TEXT, name TEXT, url TEXT, listed_url TEXT, title INTEGER, part INTEGER, subpart TEXT,
    as_of TEXT, checked TEXT, sections INTEGER DEFAULT 0, status TEXT, hash TEXT, pending BLOB,
    approved_by TEXT, approved_on TEXT, added_on TEXT);
CREATE TABLE IF NOT EXISTS passages(
    id INTEGER PRIMARY KEY, source_id INTEGER, section TEXT, heading TEXT,
    cite TEXT, url TEXT, text TEXT, parent_id INTEGER);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(text, heading, context, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS flags(
    id INTEGER PRIMARY KEY, kind TEXT, question TEXT, plan TEXT, cite TEXT, url TEXT,
    created TEXT, status TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS cases(
    id INTEGER PRIMARY KEY, label TEXT, facts TEXT, created TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS asks(
    id INTEGER PRIMARY KEY, case_id INTEGER, created TEXT, question TEXT, facts TEXT, kind TEXT, cites TEXT,
    reply TEXT, short_text TEXT, short_by TEXT, seconds REAL, grade TEXT, grade_note TEXT);
CREATE TABLE IF NOT EXISTS saved(case_id INTEGER, passage_id INTEGER, created TEXT);
CREATE TABLE IF NOT EXISTS notes(
    passage_id INTEGER PRIMARY KEY, terms TEXT, questions TEXT, model TEXT, created TEXT);
"""


# ----------------------------------------------------------------- ecfr.gov --
def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        body = r.read()
        return gzip.decompress(body) if r.headers.get("Content-Encoding") == "gzip" else body


def latest_date(title):
    for t in json.loads(get(f"{API}/titles.json"))["titles"]:
        if t["number"] == int(title):
            return t["up_to_date_as_of"]
    raise ValueError(f"No title {title} on ecfr.gov")


def fetch(title, part, subpart, as_of):
    url = f"{API}/full/{as_of}/title-{title}.xml?part={part}" + (f"&subpart={subpart}" if subpart else "")
    return get(url)


def parse_page_url(url):
    """Read (title, part, subpart) out of any ecfr.gov link, new style or old."""
    url = url or ""
    m = re.search(r"ecfr\.gov/.*?title-(\d+)(?:/.*?)?/part-(\d+)(?:/subpart-([A-Za-z]+))?", url, re.I)
    if m:
        return int(m.group(1)), int(m.group(2)), (m.group(3) or "").upper() or None
    m = re.search(r"node=s[pe](\d+)\.\d+\.(\d+)(?:\.([a-z]+)|_\d+)", url, re.I)      # node=sp42.4.431.e / se42.4.438_156
    if m:
        return int(m.group(1)), int(m.group(2)), (m.group(3) or "").upper() or None
    m = re.search(r"(\d+)cfr(\d+)_main", url, re.I)                                   # 42cfr438_main_02.tpl
    if m:
        return int(m.group(1)), int(m.group(2)), None
    raise ValueError("Paste a link to a part on ecfr.gov, such as https://www.ecfr.gov/current/title-42/part-460")


def page_url(title, part, subpart):
    return f"https://www.ecfr.gov/current/title-{title}/part-{part}" + (f"/subpart-{subpart}" if subpart else "")


# ------------------------------------------------------------------ parsing --
def clean(text):
    return re.sub(r"\s+", " ", text).strip()


def level_of(tok, italic, path):
    """CFR paragraph levels: (a) (1) (i) (A) then italic (1) and italic (i)."""
    if italic:
        return 5 if tok.isdigit() else 6
    if tok.isdigit():
        return 2
    if tok.isupper():
        return 4
    if tok in ROMAN:
        if len(tok) > 1:
            return 3
        if tok == "i":
            return 3 if len(path) >= 2 else 1
        prev = path[2] if len(path) >= 3 else ""
        if prev in ROMAN and ROMAN.index(prev) + 1 == ROMAN.index(tok):
            return 3
    return 1


def paragraphs(section):
    path = []
    for p in section.iter("P"):
        text = clean("".join(p.itertext()))
        if not text or text == "[Reserved]":
            continue
        first = p[0] if len(p) and not (p.text or "").strip() else None
        italic_lead = first is not None and first.tag == "I" and (first.text or "").lstrip().startswith("(")
        m = LEAD.match(text)
        if m:
            toks = TOKEN.findall(m.group(1))
            rest = text[m.end():]
            while True:
                d = DASH.match(rest)
                if not d:
                    break
                toks.append(d.group(1))
                rest = rest[d.end():]
            for n, tok in enumerate(toks):
                lvl = level_of(tok, italic_lead and n == 0, path)
                path = path[: lvl - 1]
                while len(path) < lvl - 1:
                    path.append("")
                path.append(tok)
            yield list(path), text
        else:
            yield None, text


def name_of(root, title, part):
    head = clean(root.findtext("HEAD") or "").replace("—", ": ")
    if root.tag == "DIV6":
        return f"{title} CFR Part {part}, {head}"
    return f"{title} CFR " + re.sub(r"\(([A-Za-z]{2,6})\)", lambda m: "(" + m.group(1).upper() + ")", head.title())


# --------------------------------------------------------------- the store --
def open_db(path):
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    cols = {r[1] for r in db.execute("PRAGMA table_info(sources)")}
    for col in ("kind", "listed_url"):          # databases built before these columns existed
        if col not in cols:
            db.execute(f"ALTER TABLE sources ADD COLUMN {col} TEXT")
    have = {r[1] for r in db.execute("PRAGMA table_info(cases)")}
    for col in ("outcome", "outcome_note", "outcome_at"):      # how the case ended, recorded by the advocate
        if col not in have:
            db.execute(f"ALTER TABLE cases ADD COLUMN {col} TEXT")
    db.execute("UPDATE sources SET kind='ecfr' WHERE kind IS NULL")
    db.commit()
    return db


def load_passages(db, sid, root, title):
    db.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM passages WHERE source_id=?)", (sid,))
    db.execute("DELETE FROM passages WHERE source_id=?", (sid,))
    n_sections = 0
    for sec in root.iter("DIV8"):
        num = sec.get("N")
        heading = re.sub(r"^§\s*[\d.]+\s*", "", clean(sec.findtext("HEAD") or "")).rstrip(".")
        if not heading or "[Reserved]" in heading:
            continue
        n_sections += 1
        base = f"https://www.ecfr.gov/current/title-{title}/section-{num}"
        ids = {}
        for path, text in paragraphs(sec):
            if path and all(path):
                marks = "".join(f"({t})" for t in path)
                cite, link, parent = f"{title} CFR § {num}{marks}", f"{base}#p-{num}{marks}", ids.get(tuple(path[:-1]))
            else:
                cite, link, parent = f"{title} CFR § {num}", base, None
            cur = db.execute(
                "INSERT INTO passages(source_id,section,heading,cite,url,text,parent_id) VALUES(?,?,?,?,?,?,?)",
                (sid, num, heading, cite, link, text, parent))
            pid = cur.lastrowid
            if path and all(path):
                ids[tuple(path)] = pid
            ctx = db.execute("SELECT text FROM passages WHERE id=?", (parent,)).fetchone()[0] if parent else ""
            db.execute("INSERT INTO fts(rowid,text,heading,context) VALUES(?,?,?,?)", (pid, text, heading, ctx))
    db.execute("UPDATE sources SET sections=? WHERE id=?", (n_sections, sid))
    return n_sections


def add_source(db, title, part, subpart, xml, as_of, status, checked, approved_by=None):
    """Insert a source from its XML. Returns the source id."""
    root = ET.fromstring(xml)
    today = date.today().isoformat()
    cur = db.execute(
        "INSERT INTO sources(kind,name,url,listed_url,title,part,subpart,as_of,checked,status,hash,approved_by,approved_on,added_on) "
        "VALUES('ecfr',?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name_of(root, title, part), page_url(title, part, subpart), page_url(title, part, subpart), title, part, subpart,
         as_of, checked, status, hashlib.sha256(xml).hexdigest(), approved_by, today if approved_by else None, today))
    sid = cur.lastrowid
    load_passages(db, sid, root, title)
    db.commit()
    return sid


def load_blocks(db, sid, blocks):
    """Insert passages for a non-eCFR source from loader blocks (section, heading, path, text)."""
    db.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM passages WHERE source_id=?)", (sid,))
    db.execute("DELETE FROM passages WHERE source_id=?", (sid,))
    s = db.execute("SELECT kind, name, url FROM sources WHERE id=?", (sid,)).fetchone()
    ids, n = {}, 0
    for section, heading, path, text in blocks:
        n += 1
        parent = None
        if path and all(path):
            marks = "".join(f"({t})" for t in path)
            cite, parent = f"{section}{marks}", ids.get(tuple(path[:-1]))
            link = s["url"]
        elif s["kind"] == "pdf":
            cite, link = f"{s['name']}, {section}", f"{s['url']}#{section.replace(' ', '=')}"
        else:
            cite, link = f"{s['name']}, ¶ {n}", s["url"]
        cur = db.execute(
            "INSERT INTO passages(source_id,section,heading,cite,url,text,parent_id) VALUES(?,?,?,?,?,?,?)",
            (sid, section, heading, cite, link, text, parent))
        pid = cur.lastrowid
        if path and all(path):
            ids[tuple(path)] = pid
        ctx = db.execute("SELECT text FROM passages WHERE id=?", (parent,)).fetchone()[0] if parent else ""
        db.execute("INSERT INTO fts(rowid,text,heading,context) VALUES(?,?,?,?)", (pid, text, heading, ctx))
    sections = len({b[0] for b in blocks})
    db.execute("UPDATE sources SET sections=? WHERE id=?", (sections, sid))
    return sections


def add_generic(db, kind, url, name=None):
    """Fetch any non-eCFR source with loaders.py and add it, awaiting approval."""
    import loaders
    fetched = loaders.cornell_nycrr_url(name) if kind == "nycrr" and "westlaw" in url else url
    dup = db.execute("SELECT id FROM sources WHERE (url=? OR listed_url=?) AND status!='retired'", (fetched, url)).fetchone()
    if dup:
        raise ValueError("That source is already in the list.")
    found_name, blocks = loaders.load(kind, url, name)
    if not blocks:
        raise ValueError("No text could be read from that link" + (" (a scanned PDF needs OCR)." if kind == "pdf" else "."))
    today = date.today().isoformat()
    digest = hashlib.sha256("\n".join(b[3] for b in blocks).encode()).hexdigest()
    cur = db.execute(
        "INSERT INTO sources(kind,name,url,listed_url,as_of,checked,status,hash,added_on) VALUES(?,?,?,?,?,?,'review',?,?)",
        (kind, name if kind == "pdf" and name else found_name, fetched, url, today, today, digest, today))
    sid = cur.lastrowid
    load_blocks(db, sid, blocks)
    db.commit()
    return sid


def add_from_url(db, url, name=None):
    """Add a source from any link on the list. Returns the source id."""
    import loaders
    kind = loaders.classify(url, name)
    if kind == "blocked":
        raise ValueError("That site blocks programs (New York statutes on FindLaw). It needs a free NY Senate API key or a saved copy.")
    if kind == "skip":
        raise ValueError("That link is private or is an index page with no rule text.")
    if kind != "ecfr":
        return add_generic(db, kind, url, name)
    title, part, subpart = parse_page_url(url)
    dup = db.execute("SELECT id FROM sources WHERE title=? AND part=? AND subpart IS ? AND status!='retired'",
                     (title, part, subpart)).fetchone()
    if dup:
        raise ValueError("That source is already in the list.")
    as_of = latest_date(title)
    xml = fetch(title, part, subpart, as_of)
    if b"<DIV8" not in xml:
        raise ValueError("ecfr.gov returned nothing for that link.")
    return add_source(db, title, part, subpart, xml, as_of, "review", date.today().isoformat())


def check_source(db, sid):
    """Fetch the latest version and compare it with what is loaded."""
    s = db.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
    today = date.today().isoformat()
    if s["kind"] != "ecfr":
        import loaders
        _, blocks = loaders.load(s["kind"], s["listed_url"] or s["url"], s["name"])
        digest = hashlib.sha256("\n".join(b[3] for b in blocks).encode()).hexdigest()
        changed = digest != s["hash"]
        if changed:
            db.execute("UPDATE sources SET checked=?, status='changed', pending=? WHERE id=?",
                       (today, json.dumps(blocks).encode(), sid))
        else:
            db.execute("UPDATE sources SET checked=?, as_of=? WHERE id=?", (today, today, sid))
        db.commit()
        return {"changed": changed, "as_of": today}
    as_of = latest_date(s["title"])
    xml = fetch(s["title"], s["part"], s["subpart"], as_of)
    today = date.today().isoformat()
    changed = hashlib.sha256(xml).hexdigest() != s["hash"]
    if changed:
        db.execute("UPDATE sources SET checked=?, status='changed', pending=? WHERE id=?", (today, xml, sid))
    else:
        db.execute("UPDATE sources SET checked=?, as_of=? WHERE id=?", (today, as_of, sid))
    db.commit()
    return {"changed": changed, "as_of": as_of}


def reload_source(db, sid, approved_by):
    """Swap in the newer version fetched by check_source."""
    s = db.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
    xml = s["pending"]
    if not xml:
        return False
    today = date.today().isoformat()
    if s["kind"] != "ecfr":
        blocks = json.loads(xml)
        load_blocks(db, sid, blocks)
        digest = hashlib.sha256("\n".join(b[3] for b in blocks).encode()).hexdigest()
        db.execute("UPDATE sources SET hash=?, pending=NULL, status='approved', as_of=?, checked=?, approved_by=?, approved_on=? WHERE id=?",
                   (digest, today, today, approved_by, today, sid))
        db.commit()
        return True
    load_passages(db, sid, ET.fromstring(xml), s["title"])
    db.execute("UPDATE sources SET hash=?, pending=NULL, status='approved', as_of=?, checked=?, approved_by=?, approved_on=? WHERE id=?",
               (hashlib.sha256(xml).hexdigest(), latest_date(s["title"]), today, approved_by, today, sid))
    db.commit()
    return True


def set_status(db, sid, status, approved_by=None):
    today = date.today().isoformat()
    if status == "approved":
        db.execute("UPDATE sources SET status='approved', approved_by=?, approved_on=? WHERE id=?", (approved_by, today, sid))
    else:
        db.execute("UPDATE sources SET status=? WHERE id=?", (status, sid))
    db.commit()


def main(src_dir, db_path):
    """First build, from the files downloaded on 2026-11-06 (eCFR version of 2026-10-01)."""
    Path(db_path).unlink(missing_ok=True)
    db = open_db(db_path)
    for f in sorted(Path(src_dir).glob("*.xml")):
        m = re.match(r"title(\d+)-part(\d+)(?:-subpart([A-Z]+))?\.xml", f.name)
        title, part, subpart = int(m.group(1)), int(m.group(2)), m.group(3)
        sid = add_source(db, title, part, subpart, f.read_bytes(), "2026-10-01", "approved", "2026-11-06", "Sprint team")
        s = db.execute("SELECT name, sections FROM sources WHERE id=?", (sid,)).fetchone()
        print(f"Loaded {s['name']}: {s['sections']} sections")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
