"""Run the Oracle on this machine.

    python3 app/server.py          then open http://localhost:8000
                                   the knowledge base is at http://localhost:8000/sources

No packages to install. The first run loads the sources into data/oracle.db.
Each case is kept in the same file (record.py): its five case facts, the questions
asked as they read after redaction, what was shown, the rules kept and the grades.
Nothing is written to the server log.
"""
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DB = ROOT / "data" / "oracle.db"
CATALOG = ROOT / "data" / "catalog.json"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000

sys.path.insert(0, str(HERE))
import answer  # noqa: E402
import ingest  # noqa: E402
import llm  # noqa: E402
import loaders  # noqa: E402
import record  # noqa: E402
import redact as redactor  # noqa: E402
from search import Oracle, redact  # noqa: E402

if not DB.exists():
    subprocess.run([sys.executable, "-I", str(HERE / "ingest.py"), str(ROOT / "data" / "ecfr"), str(DB)], check=True)
db = ingest.open_db(str(DB))
oracle = Oracle(db)
lock = threading.Lock()
writers = ThreadPoolExecutor(4)      # model calls that run while the search does
writers.submit(redactor.warm)        # load the redaction tool now, not on the first question
KEEPER = "Demo user"   # who approves; a login replaces this
PAGES = {"/": "index.html", "/index.html": "index.html", "/sources": "sources.html", "/theme.css": "theme.css"}


def catalog():
    """CSS's own list of sources, with the ones we have loaded matched up."""
    rows = json.loads(CATALOG.read_text()) if CATALOG.exists() else []
    loaded, by_url = {}, {}
    for s in oracle.sources:
        if s["status"] != "retired":
            r = db.execute("SELECT kind, title, part, subpart, url, listed_url FROM sources WHERE id=?", (s["id"],)).fetchone()
            if r["kind"] == "ecfr":
                loaded[(r["title"], r["part"], r["subpart"])] = s
                loaded.setdefault((r["title"], r["part"], None), s)
            by_url[r["url"]] = s
            by_url[r["listed_url"]] = s
    by_url.pop(None, None)          # a source with no listed link must not match a link that is not loaded
    for r in rows:
        r["source"] = None
        r["loader"] = loaders.classify(r["url"], r["name"])
        if r["loader"] == "ecfr":
            try:
                key = ingest.parse_page_url(r["url"])
                r["source"] = loaded.get(key) or loaded.get((key[0], key[1], None))
            except ValueError:
                pass
        else:
            r["source"] = by_url.get(r["url"]) or by_url.get(loaders.cornell_nycrr_url(r["name"]) if r["loader"] == "nycrr" else None)
    return rows


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, kind="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in PAGES:
            kind = "text/css" if u.path.endswith(".css") else "text/html; charset=utf-8"
            self._send(200, (HERE / "web" / PAGES[u.path]).read_bytes(), kind)
        elif u.path == "/api/sources":
            self._send(200, oracle.searched())
        elif u.path == "/api/knowledge":
            self._send(200, {"sources": oracle.sources, "catalog": catalog(), "flags": oracle.flags(), "keeper": KEEPER})
        elif u.path == "/api/find":             # words in the text of the approved sources; no model
            with lock:
                self._send(200, record.find(db, q.get("q", [""])[0]))
        elif u.path == "/api/section":
            pid = q.get("id", [""])[0]
            found = oracle.section(int(pid)) if pid.isdigit() else None
            self._send(200, found) if found else self._send(404, {"error": "No such passage."})
        elif u.path == "/api/cases":
            self._send(200, record.cases(db))
        elif u.path == "/api/case":
            cid = q.get("id", [""])[0]
            found = record.case(db, int(cid)) if cid.isdigit() else None
            if found:
                for a in found["asks"]:
                    a["searched"] = oracle.searched()
            self._send(200, found) if found else self._send(404, {"error": "No such case."})
        elif u.path == "/api/record":
            self._send(200, record.record(db))
        else:
            self._send(404, b"Not found", "text/plain")

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path == "/api/answer":
                return self._send(200, self.answer(body))
            if self.path == "/api/second":          # a model call, so made outside the lock
                with lock:
                    kept = record.stored(db, int(body.get("ask_id") or 0))
                    lines = answer.candidates(db, [int(i) for i in (kept or {}).get("pool") or []][:Oracle.POOL + 3 * Oracle.PHRASE_KEEP])
                found = answer.second(kept["sent"], kept["facts"], lines, kept.get("summary")) if kept else None
                if found:
                    with lock:
                        record.set_second(db, int(body["ask_id"]), found)
                return self._send(200, found or {"error": "The second read could not be done. Try again."})
            if self.path == "/api/spanish":         # a model call, so made outside the lock
                found = answer.spanish(body.get("sentences") or [])
                return self._send(200, {"sentences": found} if found else {"error": "The translation could not be written. Try again."})
            if self.path == "/api/ask" and str(body.get("question", "")).strip():    # a model call, so made outside the lock
                facts = record.clean_facts(body.get("facts") or {"plan": body.get("plan")})
                body["phrases"] = writers.submit(answer.phrases, redact(str(body["question"])[:2000], oracle.words)[0], facts)   # written while the search runs
            with lock:
                self._send(200, self.act(self.path, body))
        except ValueError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:  # keep the demo alive; say what broke
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def answer(self, body):
        """The summary for a question already searched. The model call is made outside the lock, so a slow model holds up nothing else."""
        question = redact(str(body.get("question", ""))[:2000], oracle.words)[0]
        facts = record.clean_facts(body.get("facts"))
        with lock:
            lines = answer.candidates(db, [int(i) for i in (body.get("pool") or [])[:Oracle.POOL + 3 * Oracle.PHRASE_KEEP]])
        extra = answer.build(question, facts, lines, [str(w)[:40] for w in (body.get("strangers") or [])[:8]])
        aid = int(body.get("ask_id") or 0)
        if aid and extra.get("kind"):
            with lock:
                record.set_answer(db, aid, extra)
        return extra

    def act(self, path, body):
        sid = int(body.get("id", 0) or 0)
        if path == "/api/ask":
            question = str(body.get("question", "")).strip()[:2000]
            if not question:
                raise ValueError("Type a question.")
            facts = record.clean_facts(body.get("facts") or {"plan": body.get("plan")})
            started = time.time()
            reply = oracle.ask(question, facts["plan"], facts, body.get("phrases") or ())
            reply["need"] = [k for k in record.FACTS if not facts[k]]      # case facts still not known
            reply["writing"] = bool(llm.model_id() and reply.get("pool"))  # the page asks /api/answer next
            cid = int(body.get("case_id") or 0)
            if cid:
                reply["ask_id"] = record.log_ask(db, cid, reply, facts, time.time() - started)
                db.execute("UPDATE cases SET facts=? WHERE id=?", (json.dumps(facts), cid))    # the case keeps the facts it was asked with
                db.commit()
            return reply
        if path == "/api/case":
            return {"id": record.save_case(db, int(body.get("id") or 0), body.get("label"), body.get("facts"))}
        if path == "/api/case/outcome":
            record.set_outcome(db, int(body["id"]), body.get("outcome"), redact(str(body.get("note") or "")[:500], oracle.words)[0])
            return {"ok": True}
        if path == "/api/case/delete":
            record.delete_case(db, int(body["id"]))
            return {"ok": True}
        if path == "/api/grade":
            row = record.grade(db, int(body["ask_id"]), body.get("grade") or None, body.get("note"))
            if row and row["grade"] == "missing":        # a missing source is also a gap for whoever maintains the sources
                oracle.flag("gap", row["question"], json.loads(row["facts"]).get("plan"), note=row["grade_note"])
            return {"ok": True}
        if path == "/api/save":
            record.keep(db, int(body["case_id"]), int(body["passage_id"]), bool(body.get("on")))
            return {"ok": True}
        if path == "/api/flag":
            oracle.flag(body.get("kind", "gap"), str(body.get("question", ""))[:2000], body.get("plan"),
                        body.get("cite"), body.get("url"), str(body.get("note", ""))[:500] or None)
            return {"ok": True}
        if path == "/api/flag/status":
            oracle.set_flag(int(body["id"]), body["status"])
            return {"ok": True}
        if path == "/api/source/add":
            new = ingest.add_from_url(db, body.get("url", ""), body.get("name") or None)
            oracle.refresh()
            return {"ok": True, "id": new}
        if path == "/api/source/approve-all":
            for s in oracle.sources:
                if s["status"] == "review":
                    ingest.set_status(db, s["id"], "approved", KEEPER)
            oracle.refresh()
            return {"ok": True, "now_answered": oracle.recheck_gaps()}
        if path == "/api/source/check":
            ids = [sid] if sid else [s["id"] for s in oracle.sources if s["status"] in ("approved", "changed")]
            results = {i: ingest.check_source(db, i) for i in ids}
            oracle.refresh()
            return {"ok": True, "results": results}
        if path == "/api/source/status":
            status = body.get("status")
            if status == "reload":
                ingest.reload_source(db, sid, KEEPER)
            elif status in ("approved", "retired", "review"):
                ingest.set_status(db, sid, status, KEEPER)
            else:
                raise ValueError("Unknown status.")
            oracle.refresh()
            return {"ok": True, "now_answered": oracle.recheck_gaps() if status in ("approved", "reload") else []}
        raise ValueError("Unknown action.")

    def log_message(self, *args):   # no record of who asked what
        pass


if __name__ == "__main__":
    print(f"Oracle: http://localhost:{PORT}   Sources: http://localhost:{PORT}/sources   (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
