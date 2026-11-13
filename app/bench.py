"""Run a test set through the Oracle and write a report to grade.

    .venv/bin/python app/bench.py data/bench/questions.json [report.md]

questions.json is a list of objects:
    {"q": "How long does an enrollee have to file an appeal?",
     "plan": "Mainstream managed care",         optional; one of the plan types, or null
     "expect": "42 CFR § 438.402(c)(2)(ii)",    optional; the citation CSS says is right, or part of it
     "answer": "60 calendar days",              optional; CSS's answer, shown next to ours for grading
     "kind": "answered"}                        optional; answered / none / advice / clarify

For each question the report shows the time taken, what the tool returned (short answer,
top citations) and, when `expect` is given, whether that citation is among the ones shown.
Nothing is graded automatically beyond that match; a person marks the rest.
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import answer  # noqa: E402
import ingest  # noqa: E402
import llm  # noqa: E402
from search import Oracle, redact  # noqa: E402

WRITERS = ThreadPoolExecutor(2)
USE_PHRASES = os.environ.get("PINPOINT_PHRASES", "on") != "off"   # the larger model names the legal issue before the search


JUDGE = os.environ.get("PINPOINT_JUDGE")      # "provider:model id" of a second model that reads our answer against CSS's
JUDGE_SYSTEM = """You compare a tool's answer with the answer a legal aid organisation's staff wrote for the same question.
Work only from the two answers. Do not use your own knowledge of the law.
1. Split the staff answer into its separate points of substance: each rule, condition or conclusion it states. Leave out bare citations and remarks about what the sources do not mention.
2. For each point, decide whether the tool's answer states it. The same meaning in other words counts. A point the tool's answer only implies does not count.
3. List every statement in the tool's answer that contradicts the staff answer. A statement that adds something the staff answer does not mention is not a contradiction.
Reply with JSON only:
{"points": [{"point": "...", "stated": true}], "contradictions": ["..."]}"""


def judge(question, ours, theirs):
    """Points of CSS's answer that ours states, and statements of ours that contradict it. None when no judge is set or it fails."""
    if not (JUDGE and ours and theirs):
        return None
    text = llm.complete(JUDGE_SYSTEM, f"Question: {question}\n\nStaff answer: {theirs}\n\nTool's answer: {ours}", use=JUDGE)
    try:
        found = json.loads(text[text.index("{"):text.rindex("}") + 1])
        return {"points": [{"point": str(p["point"]), "stated": bool(p["stated"])} for p in found["points"]],
                "contradictions": [str(c) for c in found.get("contradictions") or []]}
    except (TypeError, ValueError, KeyError):
        return None


KINDS = {"no answer": "none", "answered": "answer"}   # words a question file may use for the tool's own labels


def run(questions, oracle):
    rows = []
    for item in questions:
        if item.get("kind"):
            item["kind"] = KINDS.get(item["kind"], item["kind"])
        t = time.time()
        said = WRITERS.submit(answer.phrases, redact(item["q"], oracle.words)[0], item.get("facts") or {"plan": item.get("plan")}) if USE_PHRASES else []
        r = oracle.ask(item["q"], item.get("plan"), item.get("facts"), said)
        lines = answer.candidates(oracle.db, r["pool"]) if r.get("pool") else []
        if lines:                               # the larger model picks the lines and writes the summary, when one is set
            r.update(answer.build(r["sent"], item.get("facts") or {"plan": item.get("plan")}, lines, r.get("strangers") or ()))
        dt = time.time() - t
        cites = [l["cite"] for c in r.get("cards", []) for l in c["lines"]] or [p["cite"] for p in r.get("passages", [])]
        if r.get("summary"):
            r["short"] = {"text": " ".join(s["text"] for s in r["summary"]), "cite": r.get("by")}
        expect = item.get("expect")
        hit = offered = None
        if expect:
            wanted = expect if isinstance(expect, list) else [expect]
            hit = any(e.lower() in c.lower() or c.lower() in e.lower() for e in wanted for c in cites)
            offered = any(e.lower() in l["cite"].lower() or l["cite"].lower() in e.lower() for e in wanted for l in lines)
            expect = "; ".join(wanted)
        rows.append({"q": item["q"], "plan": item.get("plan"), "kind": r["kind"], "seconds": round(dt, 1),
                     "short": (r.get("short") or {}).get("text"), "short_cite": (r.get("short") or {}).get("cite"),
                     "cites": cites, "why": r.get("why") or r.get("ask"), "expect": expect, "hit": hit, "offered": offered,
                     "want_kind": item.get("kind"), "phrases": r.get("phrases"), "css_answer": item.get("answer"), "failed": r.get("summary_failed"),
                     "judged": judge(item["q"], (r.get("short") or {}).get("text"), item.get("answer"))})
    return rows


def judged_lines(rows):
    """Correctness and completeness against CSS's own answers, for the questions that have one."""
    judged = [r["judged"] for r in rows if r.get("judged")]
    if not judged:
        return []
    points = [p for j in judged for p in j["points"]]
    return [f"Correct (no statement contradicts CSS's answer): {sum(1 for j in judged if not j['contradictions'])} of {len(judged)} answers.",
            f"Complete (states every point of CSS's answer): {sum(1 for j in judged if all(p['stated'] for p in j['points']))} of {len(judged)} answers.",
            f"Points of CSS's answers stated: {sum(1 for p in points if p['stated'])} of {len(points)}.",
            f"Read by a second model ({JUDGE.partition(':')[2]}), not by a person."]


def report(rows):
    n = len(rows)
    with_expect = [r for r in rows if r["expect"]]
    hits = sum(1 for r in with_expect if r["hit"])
    kinds_ok = [r for r in rows if r["want_kind"]]
    kinds_hit = sum(1 for r in kinds_ok if r["want_kind"] == r["kind"])
    out = [f"# Benchmark run, {time.strftime('%Y-%m-%d %H:%M')}", "",
           f"{n} questions. Average {sum(r['seconds'] for r in rows) / max(n, 1):.1f} s each, slowest {max((r['seconds'] for r in rows), default=0):.1f} s.",
           f"Expected citation offered to the larger model: {sum(1 for r in with_expect if r['offered'])} of {len(with_expect)}." if with_expect else "",
           f"Expected citation shown: {hits} of {len(with_expect)}." if with_expect else "No expected citations given.",
           f"Right kind of response: {kinds_hit} of {len(kinds_ok)}." if kinds_ok else "",
           *judged_lines(rows),
           f"Larger model ({llm.model_id()}): {llm.tokens_in} tokens read, {llm.tokens_out} written, over {n} questions." if llm.model_id() else "", ""]
    for i, r in enumerate(rows, 1):
        out += [f"## {i}. {r['q']}", f"Plan: {r['plan'] or 'not given'} · {r['seconds']} s · returned **{r['kind']}**"
                + (f" (wanted {r['want_kind']})" if r["want_kind"] else "")]
        if r["short"]:
            out.append(f"\nShort answer: {r['short']} — {r['short_cite']}")
        if r["cites"]:
            out.append("\nShown: " + "; ".join(r["cites"]))
        if r.get("failed"):
            out.append(f"\nSummary dropped: {r['failed']}")
        if r["why"]:
            out.append(f"\nSaid: {r['why']}")
        if r["expect"]:
            out.append(f"\nCSS expects: {r['expect']} → {'offered' if r['offered'] else 'NOT offered'}, {'shown' if r['hit'] else 'NOT shown'}")
        if r["css_answer"]:
            out.append(f"\nCSS answer: {r['css_answer']}")
        if r.get("judged"):
            out.append("\nPoints of CSS's answer: " + "; ".join(("stated: " if p["stated"] else "MISSING: ") + p["point"] for p in r["judged"]["points"]))
            out.append("Contradicts CSS: " + ("; ".join(r["judged"]["contradictions"]) or "nothing"))
        out += ["", "Grade: [ ] right  [ ] partly  [ ] wrong  [ ] should have said no answer", ""]
    return "\n".join(out)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    questions = json.loads(Path(sys.argv[1]).read_text())
    db = ingest.open_db(str(HERE.parent / "data" / "oracle.db"))
    rows = run(questions, Oracle(db))
    text = report(rows)
    dest = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE.parent / "data" / "bench" / "report.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    print(text)
