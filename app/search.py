"""Find passages and decide what to show.

Two parts:

1. Real: keyword search over the loaded sources. The quote and the link come
   from the database, never from a model.

2. Two small open-weight models (brain.py): an embedder so everyday words
   find the rule's words, and a reranker that scores how well each candidate
   passage answers the question. A low top score means "no answer".

3. Plain rules for the rest: redaction, "which plan?", "I don't give advice",
   and a keyword fallback when the models are not installed.
"""
import html
import math
import os
import re
import sqlite3

from brain import Brain

# ---------------------------------------------------------------- STAND-IN --
# Words that carry no meaning for search.
STOP = set("""
a an and are as at be been but by can could did do does for from had has have her hers him his how i if in
into is it its may me might my of on or our she should so than that the their them then there these they
this those to was we were what when where which who whom why will with would you your about any all also
get gets got just kind kinds supposed followed following follow correct appropriate right proper happen
happens need needs want say says tell someone something anyone thing things one out up over under again
judy aura client clients s t not no medicaid born name date long does during because more
""".split())

# Everyday words mapped to the words the rules use. A model will do this
# job properly; this short list only covers the demo topic.
SAME = {
    "member": ["enrollee", "beneficiary", "member"],
    "plan": ["MCO", "PIHP", "managed care", "plan"],
    "drop": ["disenrollment", "disenroll"], "dropped": ["disenrollment", "disenroll"],
    "kick": ["disenrollment", "disenroll"], "kicked": ["disenrollment", "disenroll"],
    "disenroll": ["disenrollment", "disenroll"], "disenrolled": ["disenrollment", "disenroll"],
    "disenrollment": ["disenrollment", "disenroll"], "disenrollments": ["disenrollment", "disenroll"],
    "reason": ["reasons", "cause"], "reasons": ["reasons", "cause"],
    "process": ["procedures", "process"], "procedure": ["procedures", "process"],
    "appeal": ["appeal", "grievance", "fair hearing"], "appeals": ["appeal", "grievance", "fair hearing"],
    "complaint": ["grievance", "complaint"], "complain": ["grievance", "complaint"],
    "hearing": ["fair hearing", "hearing"],
    "error": ["appeal", "grievance"], "rectify": ["appeal", "grievance"], "fix": ["appeal", "grievance"],
    "deadline": ["timeframe", "timeframes", "days", "deadline"],
    "notice": ["notice"], "letter": ["notice"],
    "stop": ["terminate", "termination", "suspend", "reduction"],
    "cut": ["reduction", "reduce", "terminate"],
    "responsible": ["responsible", "must"], "entity": ["State", "agency", "MCO"],
    "decide": ["resolve", "resolution", "decision"], "decision": ["resolve", "resolution", "decision"],
    "timeline": ["timeframe", "timeframes", "days"], "time": ["timeframe", "timeframes", "days"],
    "send": ["notice", "mail", "send"],
    "keep": ["continuation", "continue"], "continue": ["continuation", "continue"],
    "services": ["services", "benefits"], "care": ["services", "benefits"],
    # the acronyms advocates use; the rules spell them out
    "snf": ["nursing facility", "nursing home", "skilled nursing"], "snfs": ["nursing facility", "nursing home", "skilled nursing"],
    "rehab": ["rehabilitation facility", "nursing facility", "rehabilitation"],
    "mltc": ["managed long term care", "MLTC", "MLTCP"], "mltcp": ["managed long term care", "MLTC", "MLTCP"],
    "mmc": ["mainstream", "managed care"], "mco": ["MCO", "plan", "contractor"],
    "dme": ["durable medical equipment", "DME", "medical equipment"],
    "pcs": ["personal care services", "PCS", "personal care"], "cdpas": ["consumer directed personal assistance", "CDPAS", "CDPAP"],
    "cdpap": ["consumer directed personal assistance", "CDPAS", "CDPAP"],
    "uas": ["assessment", "UAS", "uniform assessment"], "uas-ny": ["assessment", "UAS", "uniform assessment"],
    "nyiap": ["independent assessor", "NYIAP", "independent assessment"], "nyia": ["independent assessor", "NYIAP", "independent assessment"],
    "assessment": ["assessment", "reassessment"], "reassessment": ["reassessment", "assessment"],
    "network": ["network", "provider network"], "out-of-network": ["out of network", "outside the network"],
    "dual": ["Medicare", "dually eligible", "dual"], "duals": ["Medicare", "dually eligible", "dual"],
    "cover": ["cover", "covered", "furnish", "authorize"], "covered": ["cover", "covered", "furnish", "authorize"],
    "legacy": ["legacy", "Service Legacy"], "fhp": ["fair hearing"], "ltc": ["long term care"], "ltss": ["long term services and supports"],
    "ican": ["ombudsman", "independent consumer advocacy network"],
    "abd": ["adverse benefit determination"], "iad": ["initial adverse determination"], "fad": ["final adverse determination"],
    "doh": ["Department of Health", "Department"], "ldss": ["local department of social services", "LDSS", "local district"],
    "hra": ["HRA", "local district"], "nh": ["nursing home", "nursing facility"],
}

# A question about the case, not about a rule: what is still to be collected from the client.
ASKS_INTAKE = re.compile(
    r"(\bwhat (else |more |other (things|facts|information|details) )?(do|should|must|would) (i|we) (still )?(need|have) to (get|ask|collect|know|find out)\b|"
    r"\bwhat (else )?(should|do|must) (i|we) (still )?ask (the |my |our )?(client|caller|member)\b|"
    r"\bwhat (else )?(do|should|must) (i|we) (still )?(need|get|collect) from (the |my |our )?(client|caller|member)\b)", re.I)
ASKS_ADVICE = re.compile(
    r"(\b(what should|should) (i|we|she|he|they|\[name\]|(my|our|the|your) client)\b|"
    r"\bshould [A-Z][a-z]+ (sue|appeal|file|sign|accept|refuse|switch|enroll|disenroll|complain|request|ask|call|wait)\b|"
    r"\bwhat do (i|we) do|\bwhat would you do|"
    r"\badvise|\bnext steps?\b|\bwhat can (i|we|she|he|\[name\]|(my|our|the|your) client) do\b)", re.I)
PLANS = ["Managed long-term care", "Mainstream managed care", "PACE", "Not sure"]
PLAN_NAMES = {"mltc", "mltcp", "mmc", "pace", "plan", "plans", "medicaid", "managed", "mco", "mcos"}
# Acronyms and shorthand that are spelled out (first entry in SAME) before the models see the question.
SPELL_OUT = {"snf", "snfs", "rehab", "mltc", "mltcp", "mmc", "dme", "pcs", "cdpas", "cdpap", "uas", "uas-ny", "nyiap", "nyia",
             "out-of-network", "dual", "duals", "fhp", "ltc", "ltss", "ican", "abd", "iad", "fad", "doh", "ldss", "hra", "nh"}


# What two of the case facts add to a search, in words the loaded sources use. The question stays
# the main thing: these make a second wording, and a passage keeps its better score of the two.
FACT_TERMS = {
    "what": {
        "Disenrolled from the plan": ["disenrollment"],
        "Service denied": ["adverse benefit determination", "service authorization"],
        "Service reduced or stopped": ["reduction, suspension, or termination", "previously authorized"],
        "Prior authorization denied": ["prior authorization", "adverse benefit determination"],
        "Cannot find a provider": ["out-of-network", "network adequacy"],
    },
    "service": {
        "Home care": ["personal care services", "home health"],
        "Consumer directed personal assistance": ["consumer directed personal assistance"],
        "Nursing home": ["nursing facility"],
        "Equipment or supplies": ["durable medical equipment", "medical supplies"],
        "Prescriptions": ["prescription drug"],
        "Doctor or specialist visits": ["physician", "specialist"],
    },
}


def fact_terms(facts, already=""):
    """The rule words for this case's facts that the question does not already say."""
    out = []
    for k, table in FACT_TERMS.items():
        for term in table.get((facts or {}).get(k) or "", []):
            if term.lower() not in already.lower():
                out.append(term)
    return out


def spell_out(text):
    return re.sub(r"[A-Za-z][A-Za-z'-]+", lambda m: SAME[m.group(0).lower()][0] if m.group(0).lower() in SPELL_OUT else m.group(0), text)

# Words that start with a capital but are not a person's name.
NOT_NAMES = set("""
Medicaid Medicare PACE MCO PIHP PAHP PCCM State States Federal CHIP HMO MLTC CFR NY NYC New York Jersey
Connecticut Pennsylvania Florida Texas California Massachusetts January February March April May June July
August September October November December Monday Tuesday Wednesday Thursday Friday Saturday Sunday
I What Which Who How Why When Where Can Does Do Is Are Was Were Will Would Should Could May Has Have If
The A An Her His She He They My Our Their Yesterday Today Tomorrow Spanish English Social Security
""".split())

PATTERNS = [
    ("[email]", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("[id number]", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("[phone]", re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")),
    ("[date]", re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")),
    ("[date]", re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? \d{1,2},? \d{4}\b")),
    ("[id number]", re.compile(r"\b[A-Z]{2}\d{5}[A-Z]\b")),
    ("[id number]", re.compile(r"\b\d{7,}\b")),
    ("[address]", re.compile(r"\b\d{1,5} [A-Z][a-z]+(?: [A-Z][a-z]+)* (?:St|Street|Ave|Avenue|Rd|Road|Blvd|Dr|Drive|Pl|Place|Ln|Lane)\b\.?")),
]
NAME = re.compile(r"\b(?:(?:Mr|Mrs|Ms|Miss|Dr)\.? )?[A-Z][a-z]{2,}(?: [A-Z]\.| [A-Z][a-z]+)?")


def redact(text, known_words):
    """Take personal details out of the question. Returns (clean text, what was removed)."""
    removed = []
    for label, pat in PATTERNS:
        text, n = pat.subn(label, text)
        if n:
            removed += [label.strip("[]")] * n

    def name(m):
        words = re.sub(r"^(?:Mr|Mrs|Ms|Miss|Dr)\.? ", "", m.group(0)).split()
        titled = m.group(0) != " ".join(words)
        first = words[0]
        if not titled and (first in NOT_NAMES or first.lower() in known_words or first.lower() in STOP - {"judy", "aura"}):
            return m.group(0)
        removed.append("name")
        return "[name]"

    return NAME.sub(name, text), removed
# ------------------------------------------------------------ END STAND-IN --
from redact import redact  # noqa: E402,F811   Presidio plus the same rules when it is installed (redact.py); the rules alone otherwise


class Oracle:
    NONE_BELOW = -2.0   # reranker logit under which the best passage does not answer. Set by hand on
    MARGIN = 3.0        # 2026-11-06; the benchmark replaces both. Passages within MARGIN of the best are shown.

    def __init__(self, db):
        self.db = db
        self.brain = Brain(db)
        self.refresh()

    def refresh(self):
        """Re-read the source list. Only approved sources are searched."""
        self.sources = [dict(r) for r in self.db.execute(
            "SELECT id,name,url,as_of,checked,sections,status,approved_by,approved_on,added_on FROM sources ORDER BY id")]
        ok = [s["id"] for s in self.sources if s["status"] == "approved"]
        self.ok = "(" + ",".join(map(str, ok)) + ")" if ok else "(0)"
        self.live = [s for s in self.sources if s["status"] == "approved"]
        self.n_sections = sum(s["sections"] for s in self.live)
        self.n_passages = self.db.execute(f"SELECT count(*) FROM passages WHERE source_id IN {self.ok}").fetchone()[0]
        self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.vocab USING fts5vocab(main, fts, row)")
        self.words = {r[0] for r in self.db.execute("SELECT term FROM temp.vocab")}       # stems: "requir", "legaci"
        for r in self.db.execute(f"SELECT text FROM passages WHERE source_id IN {self.ok}"):   # and the words as written, so a
            self.words.update(re.findall(r"[a-z]{3,}", r[0].lower()))                         # rule's own term ("Service Legacy") is not taken for a name
        self.brain.index(self.ok)

    def searched(self):
        return {"sources": self.live, "sections": self.n_sections, "passages": self.n_passages}

    # ---- flags: what advocates send to the keeper of the sources ----
    def flag(self, kind, question, plan=None, cite=None, url=None, note=None):
        from datetime import date
        self.db.execute("INSERT INTO flags(kind,question,plan,cite,url,created,status,note) VALUES(?,?,?,?,?,?,'open',?)",
                        (kind, question, plan, cite, url, date.today().isoformat(), note))
        self.db.commit()

    def flags(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM flags ORDER BY id DESC")]

    def set_flag(self, fid, status):
        self.db.execute("UPDATE flags SET status=? WHERE id=?", (status, fid))
        self.db.commit()

    def recheck_gaps(self):
        """After the sources change, see which flagged gaps now have an answer."""
        now = []
        for f in self.db.execute("SELECT * FROM flags WHERE kind='gap' AND status='open'").fetchall():
            if self.ask(f["question"], f["plan"])["kind"] == "answer":
                self.db.execute("UPDATE flags SET status='answered' WHERE id=?", (f["id"],))
                now.append(f["id"])
        self.db.commit()
        return now

    def section(self, pid):
        """The whole section a passage sits in, so the screen can show it in context."""
        p = self.db.execute(
            "SELECT p.section, p.heading, p.source_id, s.name, s.checked FROM passages p "
            "JOIN sources s ON s.id = p.source_id WHERE p.id=?", (pid,)).fetchone()
        if not p:
            return None
        rows = self.db.execute("SELECT id, cite, text FROM passages WHERE section=? AND source_id=? ORDER BY id",
                               (p["section"], p["source_id"])).fetchall()
        return {
            "section": p["section"], "heading": p["heading"], "source": p["name"], "checked": p["checked"],
            "url": f"https://www.ecfr.gov/current/title-42/section-{p['section']}", "target": pid,
            "paragraphs": [{"id": r["id"], "text": r["text"], "level": max(1, r["cite"].count("("))} for r in rows],
        }

    USE_FACT_TERMS = False   # off: on the practice set (2026-11-11) it found no more citations (5 of 30 either way) and added wait
    POOL = 60             # how many passages go to the larger model, which picks the lines that answer
    PHRASE_KEEP = 6       # passages added for each search phrase the larger model writes
    KEEP = 25             # of those, the best this many from the keyword list and from the meaning list, whatever the merge says
    MERGE = os.environ.get("PINPOINT_POOL", "merge") != "scorer"   # the pool is the keyword and meaning lists merged by rank;
                                                                   # "scorer": the small scoring model's best 20, as before 2026-11-12
    RESCORE = 25          # how many of the best passages are scored again under each further wording
    KEYWORD_BONUS = 3.0   # added to the reranker score for a passage whose own words cover the question

    OWN_PLAN_ONLY = os.environ.get("PINPOINT_OWN_PLAN", "on") != "off"
    PLAN_PENALTY = 6.0       # a Medicare rule should only show for a Medicaid plan when nothing else answers
    PLAN_WORDS = {   # words in a source's name that tie it to one kind of plan
        "PACE": ["pace", "all-inclusive"],
        "Managed long-term care": ["mltc", "managed long term care", "partial capitation"],
        "Mainstream managed care": ["mainstream", "mmc"],
    }
    OTHER = ["medicare advantage", "part 422", "part 423", "prescription drug", "medicaid advantage plus", "map model",
             "§ 1395"]          # 42 U.S.C. 1395 is Medicare (Title XVIII)

    def _other_plan(self, pid, plan):
        name = self.db.execute("SELECT s.name FROM passages p JOIN sources s ON s.id=p.source_id WHERE p.id=?", (pid,)).fetchone()[0].lower()
        mine = any(w in name for w in self.PLAN_WORDS.get(plan, []))
        if mine:
            return False
        theirs = [w for other, words in self.PLAN_WORDS.items() if other != plan for w in words] + self.OTHER
        return any(w in name for w in theirs)

    OTHER_MEDICAID = ["medicaid advantage plus", "map model"]     # a Medicaid plan type the case form does not list

    def _other_medicaid_plan(self, pid, plan):
        """True for a source written for a different kind of Medicaid plan. Medicare rules are kept: a client can have both."""
        name = self.db.execute("SELECT s.name FROM passages p JOIN sources s ON s.id=p.source_id WHERE p.id=?", (pid,)).fetchone()[0].lower()
        if any(w in name for w in self.PLAN_WORDS.get(plan, [])):
            return False
        return any(w in name for other, words in self.PLAN_WORDS.items() if other != plan for w in words) or any(w in name for w in self.OTHER_MEDICAID)

    def _rank_with_models(self, question, groups, weight, plan=None, others=(), phrases=()):
        """Candidates from keywords and from meaning, then the reranker decides.
        Exact rule words still count: a passage gets up to KEYWORD_BONUS for covering them.
        `others` are further wordings of the question (as typed, before acronyms were spelled out;
        with the case facts added) that widen the net. A passage keeps its best score."""
        cands = {}
        if groups:
            rows = self.db.execute(
                f"SELECT fts.rowid AS rowid FROM fts JOIN passages p ON p.id = fts.rowid "
                f"WHERE fts MATCH ? AND p.source_id IN {self.ok} ORDER BY bm25(fts, 1.0, 2.5, 0.6) LIMIT 40",
                (" OR ".join(groups),)).fetchall()
            for r in rows:
                cands[r["rowid"]] = True
        for pid, _ in self.brain.nearest(question, 40):
            cands[pid] = True
        late = set()                              # found only through a further wording
        for other in others:
            for pid, _ in self.brain.nearest(other, 40):
                if pid not in cands:
                    late.add(pid)
                cands[pid] = True
        if not cands:
            return [], {}, []
        texts = []
        for pid in cands:
            p = self.db.execute("SELECT text, heading, parent_id FROM passages WHERE id=?", (pid,)).fetchone()
            if len(p["text"]) < 60 or not re.search(r"[a-z)][.;:!?](\s|$)", p["text"]):    # a letterhead or a title, not a rule
                continue
            lead = self.db.execute("SELECT text FROM passages WHERE id=?", (p["parent_id"],)).fetchone()[0] if p["parent_id"] else ""
            texts.append((pid, f"{p['heading']}. {lead[:200]} {p['text']}"[:1100]))
        ranked = self.brain.rerank(question, texts)
        if others:                                # every wording; a passage keeps its best score
            best = dict(ranked)
            again = late | {pid for pid, _ in ranked[:self.RESCORE]}     # the rest are too far down to matter
            some = [t for t in texts if t[0] in again]
            for other in others:
                for pid, sc in self.brain.rerank(other, some):
                    best[pid] = max(best[pid], sc)
            ranked = sorted(best.items(), key=lambda x: -x[1])
        if plan and plan != "Not sure":            # a source written for another kind of plan counts for less
            wrong = {pid for pid, _ in ranked if self._other_plan(pid, plan)}
            ranked = sorted(((pid, sc - (self.PLAN_PENALTY if pid in wrong else 0)) for pid, sc in ranked), key=lambda x: -x[1])
        if groups:
            ids = [pid for pid, _ in ranked]
            own = {i: 0.0 for i in ids}
            for g in groups:
                for i in self._hits("text : " + g, ids):
                    own[i] += weight[g]
            total = sum(weight.values()) or 1.0
            ranked = sorted(((pid, sc + self.KEYWORD_BONUS * own[pid] / total) for pid, sc in ranked), key=lambda x: -x[1])
        best = ranked[0][1]
        pool = self._merged(question, groups, others, phrases) if self.MERGE else [pid for pid, _ in ranked[:20]]
        if plan and plan != "Not sure":            # the case's plan type narrows what is offered: another Medicaid plan type's contract is left out
            pool = [pid for pid in pool if not self._other_medicaid_plan(pid, plan)]
        if self.OWN_PLAN_ONLY and plan and plan != "Not sure":      # a contract or rule written for another kind of plan is not offered
            pool = [pid for pid in pool if not self._other_plan(pid, plan)]
        if best < self.NONE_BELOW:
            return [], {"best": round(best, 2)}, pool
        good = [pid for pid, sc in ranked if sc >= max(self.NONE_BELOW, best - self.MARGIN)]
        return good, {pid: round(sc, 2) for pid, sc in ranked[:8]}, pool

    def _merged(self, question, groups, others=(), phrases=()):
        """Keyword results and meaning results merged by rank (reciprocal rank fusion): a passage high on either list is kept.
        `phrases` name the question's legal issues in the rules' own words (answer.phrases). Each is searched
        both ways and its best PHRASE_KEEP passages are added, so a rule that shares no words with the question is still offered."""
        lists = []
        if groups:
            lists.append([r[0] for r in self.db.execute(
                f"SELECT fts.rowid FROM fts JOIN passages p ON p.id = fts.rowid "
                f"WHERE fts MATCH ? AND p.source_id IN {self.ok} ORDER BY bm25(fts, 1.0, 2.5, 1.0) LIMIT 100",
                (" OR ".join(groups),))])
        for wording in (question, *others):
            lists.append([pid for pid, _ in self.brain.nearest(wording, 100)])
        score = {}
        for found in lists:
            for k, pid in enumerate(found):
                score[pid] = score.get(pid, 0.0) + 1.0 / (60 + k + 1)
        pool = []                                  # the best of each list is always kept: a passage found only by its
        for found in lists[:2]:                    # words, or only by its meaning, would otherwise lose to ones middling on both
            pool += [pid for pid in found[:self.KEEP] if pid not in pool]
        for pid, _ in sorted(score.items(), key=lambda x: -x[1]):
            if len(pool) >= self.POOL:
                break
            if pid not in pool:
                pool.append(pid)
        for phrase in (phrases.result() if hasattr(phrases, "result") else phrases):    # may still be being written (a Future)
            words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z-]+", phrase) if w.lower() not in STOP and len(w) >= 3]
            by_word = [r[0] for r in self.db.execute(
                f"SELECT fts.rowid FROM fts JOIN passages p ON p.id = fts.rowid "
                f"WHERE fts MATCH ? AND p.source_id IN {self.ok} ORDER BY bm25(fts, 1.0, 2.5, 1.0) LIMIT 50",
                (" OR ".join(f'"{w}"' for w in dict.fromkeys(words)),))] if words else []
            by_meaning = [pid for pid, _ in self.brain.nearest(phrase, 50)]
            score = {}
            for found in (by_word, by_meaning):
                for k, pid in enumerate(found):
                    score[pid] = score.get(pid, 0.0) + 1.0 / (60 + k + 1)
            best = [pid for pid, _ in sorted(score.items(), key=lambda x: -x[1]) if pid not in pool]
            pool += best[:self.PHRASE_KEEP]
        return pool

    def _rank_with_rules(self, groups, weight, words):
        """Keyword-only fallback, used when the models are not installed."""
        rows = self.db.execute(
            f"SELECT fts.rowid AS rowid, bm25(fts, 1.0, 2.5, 0.6) AS score FROM fts JOIN passages p ON p.id = fts.rowid "
            f"WHERE fts MATCH ? AND p.source_id IN {self.ok} ORDER BY score LIMIT 60",
            (" OR ".join(groups),)).fetchall()
        ids = [r["rowid"] for r in rows]
        cover = {i: 0.0 for i in ids}
        own = {i: 0.0 for i in ids}
        head = {i: 0.0 for i in ids}
        for g in groups:
            for i in self._hits(g, ids):
                cover[i] += weight[g]
            for i in self._hits("text : " + g, ids):
                own[i] += weight[g]
            for i in self._hits("heading : " + g, ids):
                head[i] += weight[g]
        total = sum(weight.values())
        need = 0.99 if len(groups) <= 2 else 0.62
        order = {r["rowid"]: n for n, r in enumerate(rows)}
        good = sorted((i for i in ids if cover[i] / total >= need),
                      key=lambda i: (-round(own[i] / total, 1), -round(head[i] / total, 1), order[i]))
        if len(groups) == 1:
            word = next(w.lower() for w in words if w.lower() not in STOP and len(w) >= 3)
            good = [i for i in good if word in self.db.execute(
                "SELECT lower(heading) FROM passages WHERE id=?", (i,)).fetchone()[0]]
        if good:
            best, top = own[good[0]], head[good[0]]
            good = [i for i in good if own[i] >= 0.8 * best and head[i] >= 0.8 * top]
        return good

    def follow_ups(self, passages):
        """Where to go next, read off the text: the other subtopics of the same
        section, and the sections the shown passages point to."""
        out, seen, shown = [], set(), {p["id"] for p in passages}
        top = self.db.execute("SELECT section, source_id FROM passages WHERE id=?", (passages[0]["id"],)).fetchone()
        for r in self.db.execute("SELECT id, cite, text, url FROM passages WHERE section=? AND source_id=? ORDER BY id",
                                 (top["section"], top["source_id"])):
            m = re.match(r"^\(([a-z])\) ([A-Z][^.—:]{3,70})[.—:]", r["text"])
            if m and r["id"] not in shown and r["cite"].count("(") == 1:
                out.append({"label": f"({m.group(1)}) {m.group(2).strip()}", "id": r["id"], "url": r["url"], "cite": r["cite"]})
        for p in passages:
            for ref in re.findall(r"§\s?(\d{3}\.\d+)", p["text"]):
                if ref == top["section"] or ref in seen:
                    continue
                r = self.db.execute(f"SELECT id, heading, url, cite FROM passages WHERE section=? AND source_id IN {self.ok} ORDER BY id LIMIT 1",
                                    (ref,)).fetchone()
                if r:
                    seen.add(ref)
                    out.append({"label": f"§ {ref} {r['heading']}", "id": r["id"], "url": r["url"], "cite": r["cite"]})
        return out[:6]

    def _hits(self, query, ids=None):
        # A JOIN, not `rowid IN (SELECT ...)`: the subquery form makes SQLite scan the
        # whole index for common words (10 s); the join runs in milliseconds.
        sql = f"SELECT fts.rowid FROM fts JOIN passages p ON p.id = fts.rowid WHERE fts MATCH ? AND p.source_id IN {self.ok}"
        if ids:
            sql += f" AND fts.rowid IN ({','.join(map(str, ids))})"
        try:
            return [r[0] for r in self.db.execute(sql, (query,))]
        except sqlite3.OperationalError:
            return []

    def ask(self, question, plan=None, facts=None, phrases=()):
        """Returns a dict the screen can show. `kind` is one of:
        answer, none, clarify, advice. `facts` are the case facts (see FACT_TERMS)."""
        clean, removed = redact(question, self.words)
        out = {"sent": clean, "removed": removed, "plan": plan, "searched": self.searched()}

        # Rules: advice, the PACE gate, words the sources never use, which plan.
        if ASKS_INTAKE.search(clean):               # answered from the case's own missing facts, not from the rules
            return {**out, "kind": "intake"}
        if ASKS_ADVICE.search(clean) or ASKS_ADVICE.search(question):     # also as typed: "Should Judy sue" reads "Should [name] sue" once redacted
            return {**out, "kind": "advice"}
        if plan == "PACE" and not any("pace" in x["name"].lower() for x in self.live):
            return {**out, "kind": "none", "why": "None of the approved sources cover PACE plans."}

        bare = re.sub(r"\[[a-z ]+\]", " ", clean).replace("’", "'")
        words = [w.split("'")[0] for w in re.findall(r"[A-Za-z][A-Za-z'-]+", bare)]
        groups, weight, strangers = [], {}, []
        for n, w in enumerate(words):
            low = w.lower()
            if low in STOP or len(low) < 3 or (plan and low in PLAN_NAMES):   # the plan type is a fact, not a search word
                continue
            g = "(" + " OR ".join(f'"{a}"' for a in SAME.get(low, [low])) + ")"
            if g in weight:
                continue
            found = len(self._hits(g))
            if found:                          # rare words count for more
                groups.append(g)
                weight[g] = math.log(1 + self.n_passages / found)
            elif n > 0 and w[0].isupper():     # a place or program the sources never mention
                strangers.append(w)
        strangers = list(dict.fromkeys(strangers))
        if strangers and not os.environ.get("PINPOINT_LLM"):     # with no larger model to judge, a word the sources never use ends it here
            return {**out, "kind": "none", "why": "None of the approved sources mention " + " or ".join(strangers) + "."}
        out["strangers"] = strangers                # the larger model is told, and says NONE when the question turns on one of them
        if not groups and not self.brain.available:
            return {**out, "kind": "none", "why": "None of the approved sources use the words in this question."}
        # With no plan type the search covers every approved source; each case fact the advocate adds narrows it.

        if self.brain.available:
            # tell the models the rule's words for everyday ones ("kicked off" -> "disenrollment")
            spelled = spell_out(bare)                 # "SNF" -> "nursing facility", "UAS-NY" -> "assessment"
            alt = bare if spelled != bare else None
            hint = " ".join(dict.fromkeys(SAME[w][0] for w in (x.lower() for x in words)
                                          if w in SAME and SAME[w][0].lower() not in spelled.lower()))
            main = spelled + (f" ({hint})" if hint else "")
            others = [alt] if alt else []
            extra = fact_terms(facts, main) if self.USE_FACT_TERMS else []
            if extra:                                 # the same question, read in the light of the case
                others.append(f"{main} ({', '.join(extra)})")
                out["fact_terms"] = extra
            good, scores, pool = self._rank_with_models(main, groups, weight, plan, others, phrases)
            out["phrases"] = list(phrases.result() if hasattr(phrases, "result") else phrases)
            out["scores"] = scores
            out["pool"] = pool
        else:
            good = self._rank_with_rules(groups, weight, words)

        if not good:
            return {**out, "kind": "none", "why": "No passage in the approved sources answers this."}

        passages, seen = [], set()
        mark = " OR ".join(groups)
        for i in good:
            if len(passages) == 3:
                break
            p = None
            if mark:
                p = self.db.execute(
                    "SELECT p.*, highlight(fts, 0, char(1), char(2)) AS marked, s.name AS source, s.checked AS checked "
                    "FROM fts JOIN passages p ON p.id = fts.rowid JOIN sources s ON s.id = p.source_id "
                    "WHERE fts MATCH ? AND fts.rowid = ?", (mark, i)).fetchone()
            if p is None:
                p = self.db.execute(
                    "SELECT p.*, p.text AS marked, s.name AS source, s.checked AS checked FROM passages p "
                    "JOIN sources s ON s.id = p.source_id WHERE p.id = ?", (i,)).fetchone()
            if len(p["text"]) < 60 or p["id"] in seen or p["parent_id"] in seen:
                continue
            seen.add(p["id"])
            lead = None
            if p["parent_id"]:
                lead = self.db.execute("SELECT text FROM passages WHERE id=?", (p["parent_id"],)).fetchone()[0]
            passages.append({
                "id": p["id"], "text": p["text"],
                "cite": p["cite"], "url": p["url"], "heading": p["heading"], "source": p["source"],
                "checked": p["checked"], "lead": lead if lead and len(lead) < 420 else None,
                "html": html.escape(p["marked"]).replace("\x01", "<mark>").replace("\x02", "</mark>"),
            })
        if not passages:
            return {**out, "kind": "none", "why": "No passage in the approved sources answers this."}
        short = self._short(bare, passages)
        if short:                                   # the passage the short answer came from goes first
            passages.sort(key=lambda p: p["id"] != short["id"])
        return {**out, "kind": "answer", "passages": passages, "short": short, "follow": self.follow_ups(passages)}

    SHORT_BELOW = 1.5        # reader score under this: show the passages only, no short answer

    def _short(self, question, passages):
        """One sentence of the source that answers the question, with the answering
        words marked. The reader points at the words; the sentence around them is
        the source's own, trimmed to a readable length."""
        if not self.brain.available:
            return None
        spans = self.brain.read(question, [p["text"] for p in passages])
        for i in sorted(range(len(spans)), key=lambda i: -spans[i][0]):
            score, c0, c1 = spans[i]
            if score < self.SHORT_BELOW:
                return None
            found = self._sentence(passages[i], c0, c1)
            if found and len(found["text"]) >= 60 and len(found["text"].split()) >= 8:
                return {**found, "score": round(score, 2)}
        return None

    @staticmethod
    def _sentence(p, c0, c1, before=160, after=220):
        text = p["text"]
        if c1 - c0 < 3 or not text[c0:c1].strip():
            return None
        ends = [m.end() for m in re.finditer(r"[.;:]\s+(?=[A-Z(\u201c\"])", text)]
        s0 = max([0] + [e for e in ends if e <= c0])
        s1 = min([len(text)] + [e for e in ends if e >= c1])
        if c0 - s0 > before:                                    # too far back: start at a comma or a word
            cut = text.rfind(", ", c0 - before, c0)
            s0 = cut + 2 if cut > 0 else text.rfind(" ", 0, c0 - before) + 1
        if s1 - c1 > after:                                     # too far on: stop at a comma or a word
            cut = text.find(", ", c1, c1 + after)
            s1 = cut if cut > 0 else (text.find(" ", c1 + after) if text.find(" ", c1 + after) > 0 else len(text))
        lead = re.match(r"^\s*(\([a-z0-9]{1,4}\)\s*)+", text[s0:])  # drop a leading "(b)(2)"
        if lead and s0 + lead.end() <= c0:
            s0 += lead.end()
        pre = "… " if s0 > 0 and s0 not in ends else ""
        post = " …" if s1 < len(text) and s1 not in ends else ""
        sentence = text[s0:s1].strip().rstrip(",;")
        marked = (html.escape(text[s0:c0]) + "<b>" + html.escape(text[c0:c1]) + "</b>" + html.escape(text[c1:s1])).strip().rstrip(",;")
        return {"id": p["id"], "cite": p["cite"], "url": p["url"], "source": p["source"], "heading": p["heading"],
                "text": pre + sentence + post, "html": pre + marked + post}
