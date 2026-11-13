"""The summary and the lines shown under it.

The search hands over its best passages (Oracle.POOL of them). Each is a line of a
source: one paragraph of a rule, one item of a list. A larger model reads them,
numbered, and writes one to four sentences, each ending with the number of the
line it rests on. The lines it names are the ones shown, grouped by section.

The model sees the question after redaction, the case facts and rule text. It
does not see the case label or anything else in the record. It returns numbers,
not rule text, so what is shown is always the source's own words. Checked in code,
and a failed check discards the whole answer:

  1. every sentence names a line that was offered;
  2. every figure in the sentence (days, amounts, section numbers) is in that line.

With no model, or a failed check, the screen shows what the search found.
"""
import json
import os
import re

import llm
from notes import kind_label

LABELS = {"plan": "Plan type", "what": "What happened", "service": "Service at issue",
          "notice": "Notice received", "still": "Services still in place"}

SYSTEM = """You answer a helpline advocate's question about a Medicaid managed care rule, using numbered lines from the rules.

- Find the lines that answer the question for this kind of plan and this case. Many lines will be about something else: ignore them.
- The first sentence answers the question that was asked, directly: yes or no, which one, who, when, how long. The sentences after it add only what else bears on that answer. A line on the same subject that does not help answer the question is left out.
- Check who each line is about. A line about what an enrollee may ask for does not answer a question about what a plan may do, and the reverse.
- Write one to four short, plain sentences that say what those lines say, in your own words. Do not copy a line out in full. When several items of one list answer, cover them in one sentence and name each: [3][4][5]
- Do not say what the advocate or the client should do.
- End every sentence with the number of the line it rests on, in square brackets, like this: [12]. Put nothing after the bracket. Do not write citations or section numbers in the sentence itself.
- Use only what the lines say. Add nothing from memory.
- Copy every number, time limit, amount and section number exactly as the line writes it.
- Each group of lines is headed with its source and what kind of source it is. When lines from different sources disagree, say so in one sentence and name both lines.
- The case facts give the plan type. A contract or policy written for another kind of plan does not answer: leave its lines out when a line from a statute, a regulation or this plan type's own contract or policy answers.
- Start with the line that states the rule deciding the question (who must act, who pays first, what is required), not a line that only lists or describes a service. The lines are in order of authority: statutes, then regulations, then contracts and policies. Go through the statute and regulation lines first. When one of them bears on the question, give it a sentence, even if a contract line is more specific, then give the contract line its own sentence.
- Write the sentences only: no headings, no source names on their own line, no bold, no lists.
- If no line answers the question, reply with the single word NONE."""

PHRASES = """A helpline advocate asks about a Medicaid managed care rule. Do not answer the question. Write the passages a search should look for.

Write three lines, one sentence each, with no numbering, no section numbers and nothing else:
- the sentence of a federal statute or regulation that would settle the question, in the wording such a law uses;
- the sentence of a New York State regulation or policy that would settle it, in the wording it uses;
- the sentence of a plan's contract with the State that would settle it, in the wording a contract uses.

Each sentence states the general legal principle the question turns on, not only the service it mentions."""

RANK = {"uscode": 0, "ecfr": 1, "nycrr": 2}     # the order lines are offered in: statutes, federal then State regulations, then contracts and policies
EFFORT = os.environ.get("PINPOINT_EFFORT") or None    # how long the model reasons before the summary; unset is the model's default
WHOLE_LIST = 2500     # characters; a list shorter than this is offered whole, so the model sees every item
MAX_PROMPT = 100000
SENTENCE = re.compile(r"\s*(.+?)\s*((?:\[\d+(?:\s*,\s*\d+)*\]\s*)+)[.;]?\s*", re.S)   # "… words [3]", "… words [3][4].", "… words [3, 4]"
FIGURE = re.compile(r"\d[\d,]*(?:\.\d+)?")
ROW = ("SELECT p.id, p.parent_id, p.source_id, p.section, p.heading, p.cite, p.url, p.text, s.name AS source, s.kind AS kind "
       "FROM passages p JOIN sources s ON s.id = p.source_id ")


def phrases(question, facts):
    """Up to three sentences a rule might contain that would settle the question, or [] with no model.
    They are searched for, to find rules that share no words with the question. They are never shown."""
    if not llm.model_id():
        return []
    known = [f"{LABELS[k]}: {v}" for k, v in (facts or {}).items() if k in LABELS and v]
    text = llm.complete(PHRASES, "Question: " + question + "\nCase facts: " + ("; ".join(known) or "none given"), max_tokens=2000)   # default reasoning: at "low" the sentences missed the rule's wording half the time, and this call runs beside the search
    lines = [re.sub(r"^[\s\d.)*-]+", "", l).strip() for l in (text or "").split("\n")]
    return [l[:400] for l in lines if len(l.split()) >= 5 and not l.startswith("General legal principle")][:3]


def candidates(db, pool):
    """The lines the model may choose from: each passage the search found, with the rest of its list when short."""
    lines, seen, size = [], set(), 0
    for pid in pool:
        p = db.execute(ROW + "WHERE p.id = ?", (pid,)).fetchone()
        if not p or p["id"] in seen:
            continue
        top = p["parent_id"] or p["id"]
        group = db.execute(ROW + "WHERE p.id = ? OR p.parent_id = ? ORDER BY p.id", (top, top)).fetchall()
        if sum(len(m["text"]) for m in group) > WHOLE_LIST:
            group = [m for m in group if m["id"] in (top, p["id"])]
        group = [dict(m) for m in group if m["id"] not in seen]
        size += sum(len(m["text"]) + 60 for m in group)
        if size > MAX_PROMPT:
            break
        seen.update(m["id"] for m in group)
        lines.append(group)
    return [m for group in sorted(lines, key=lambda g: (RANK.get(g[0]["kind"], 3), g[0]["id"])) for m in group]   # statutes, regulations, the rest; each in the source's order


def prompt(question, facts, lines, strangers=()):
    known = [f"{LABELS[k]}: {v}" for k, v in (facts or {}).items() if k in LABELS and v]
    parts = ["Question: " + question, "Case facts: " + ("; ".join(known) if known else "none given")]
    if strangers:
        parts.append("Words in the question that no approved source uses: " + ", ".join(strangers) +
                     ". If the question is about one of them (another state, a program the sources do not cover), reply NONE. "
                     "If it is only a detail of the case, ignore it.")
    parts.append("")
    place = None
    for n, m in enumerate(lines, 1):
        here = (m["source_id"], m["section"])
        if here != place:
            parts += ["", f"From {m['source']} ({kind_label(m['kind'], m['source'])}): {m['heading']}"]
            place = here
        parts.append(f"[{n}] {m['cite']}: {m['text']}")
    return "\n".join(parts)


def check(text, lines):
    """The sentences, each tied to its line, or (None, reason)."""
    by_id = {m["id"]: m for m in lines}
    out, end = [], 0
    for m in SENTENCE.finditer(text):
        if m.start() != end:
            break
        end = m.end()
        words = m.group(1).strip()
        if words and words[-1] not in ".?!":
            words += "."
        named = []
        for n in map(int, re.findall(r"\d+", m.group(2))):
            if not 1 <= n <= len(lines):
                return None, f"sentence cites line {n}, which was not offered"
            if lines[n - 1] not in named:
                named.append(lines[n - 1])
        have = set()
        for line in named:
            parent = by_id.get(line["parent_id"], {}).get("text", "")
            have |= {f.replace(",", "").rstrip(".") for f in FIGURE.findall(parent + " " + line["text"] + " " + line["cite"])}
        for f in FIGURE.findall(words):
            if f.replace(",", "").rstrip(".") not in have:
                return None, f"'{f}' is not in the line the sentence cites"
        out.append({"text": words, "cites": [{"id": l["id"], "cite": l["cite"]} for l in named]})
    if end != len(text) or not out:
        return None, "a sentence has no line number"
    if len(out) > 5:
        return None, "too many sentences"
    return out, None


def title(m):
    if m["section"].lower().startswith("page"):
        return f"{m['source']}, {m['section']}"
    name = f"{m['section']} {m['heading']}"
    return name if "§" in name else "§ " + name


def cards(sentences, lines):
    """One card per section of a source, holding the lines the summary names, in the source's order."""
    by_id = {m["id"]: m for m in lines}
    out = {}
    for s in sentences:
        for c in s["cites"]:
            m = by_id[c["id"]]
            card = out.setdefault((m["source_id"], m["section"]), {"title": title(m), "source": m["source"], "ids": set()})
            card["ids"].add(m["id"])
    for card in out.values():
        rows = []
        for i in sorted(card.pop("ids")):
            m = by_id[i]
            lead = by_id.get(m["parent_id"]) if m["parent_id"] and m["parent_id"] not in {r["id"] for r in rows} else None
            rows.append({"id": m["id"], "cite": m["cite"], "text": m["text"], "url": m["url"], "heading": m["heading"],
                         "lead": lead["text"] if lead and len(lead["text"]) < 420 else None})
        card["lines"] = rows
    return list(out.values())


def build(question, facts, lines, strangers=()):
    """What to add to the search's reply: a summary with its cards, or 'none', or {} to leave the reply as it is."""
    if not lines or not llm.model_id():
        return {}
    asked = prompt(question, facts, lines, strangers)
    sentences = why = None
    for attempt in range(2):                 # a summary that fails the checks is asked for once more, with the reason
        text = llm.complete(SYSTEM, asked if not why else
                            f"{asked}\n\nYour last answer was rejected: {why}. Write it again, following every rule.", effort=EFFORT)
        if text is None:
            return {"summary_failed": llm.error}
        text = "\n".join(l for l in text.strip().split("\n") if not re.fullmatch(r"\s*(\*\*.*\*\*|#+ .*)\s*", l)).strip()
        if text.upper().rstrip(".") == "NONE":
            return {"kind": "none", "why": "No passage in the approved sources answers this.", "by": llm.model_id(),
                    "passages": [], "short": None}
        sentences, why = check(text, lines)
        if sentences:
            break
    if not sentences:
        return {"summary_failed": why}
    return {"kind": "answer", "summary": sentences, "cards": cards(sentences, lines), "by": llm.model_id()}


SPANISH = """Translate each numbered sentence into Spanish for a legal aid advocate to read to a client.
Keep the meaning exact: add nothing, leave nothing out, and do not soften or strengthen any requirement.
Keep citations, program names and abbreviations (such as MLTC, 42 CFR § 438.56) as written.
Reply with the same numbers, one translated sentence per line, and nothing else."""


def spanish(sentences):
    """The summary's sentences in Spanish, in the same order, or None when the model is off or returns a different count."""
    sentences = [str(s)[:1500] for s in sentences[:12]]
    text = llm.complete(SPANISH, "\n".join(f"{i}. {s}" for i, s in enumerate(sentences, 1)), max_tokens=3000, effort="low") if sentences else None
    found = dict(re.findall(r"^\s*(\d+)\.\s*(.+?)\s*$", text or "", re.M))
    out = [found.get(str(i)) for i in range(1, len(sentences) + 1)]
    return out if out and all(out) else None


CHECKER = os.environ.get("PINPOINT_CHECKER") or None     # "provider:model id" of the second reader; unset is the model that wrote the summary
SECOND = """You are the second reader of a summary of Medicaid managed care rules, written for a helpline advocate. You get the question, the numbered lines that were offered from the approved sources, and the summary's sentences, each with the numbers of the lines it cites. Use only the lines. Add nothing from memory.

Part 1. Compare each sentence with the lines it cites, and only those lines:
- "supported": the lines say what the sentence says.
- "overstated": the lines say less than the sentence. It drops a condition or an exception, turns "may" into "must", or widens who the rule applies to. Give "rewrite": the sentence corrected to what the lines say, in plain words, with no line numbers and no citations in it.
- "unsupported": the cited lines do not say it.
Different wording is not overstating. Judge the meaning.

Part 2. List up to three points that the offered lines leave open for this question:
- wording that can be read two ways;
- an exception or a condition in the lines that may or may not apply to this case;
- lines from different sources that disagree with each other;
- something the question asks that no line covers.
List only real ones. An empty list is right when the lines settle the question. Never write a line number in "point" or "check"; name the rule in words. For each: "point", one sentence saying what is open; "lines", the numbers of the lines it rests on (empty when no line covers it); "check", one factual question the advocate can put to the client or look up in the record to settle it. Do not say what the advocate or the client should do.

Reply with JSON only:
{"sentences": [{"n": 1, "verdict": "supported", "rewrite": null}], "open": [{"point": "...", "lines": [3], "check": "...?"}]}"""


def second(question, facts, lines, sentences):
    """A second read of the summary: each sentence against the lines it cites, and what the lines leave open.
    Returns {"summary": the sentences after the read, "second": what was found}, or None when the reader is off or its reply cannot be used."""
    if not (lines and sentences and llm.model_id()):
        return None
    number = {m["id"]: n for n, m in enumerate(lines, 1)}
    listed = "\n".join(f"S{i}. {s['text']} " + "".join(f"[{number[c['id']]}]" for c in s["cites"] if c["id"] in number)
                       for i, s in enumerate(sentences, 1))
    text = llm.complete(SECOND, prompt(question, facts, lines) + "\n\nSummary sentences:\n" + listed, use=CHECKER)
    try:
        found = json.loads(text[text.index("{"):text.rindex("}") + 1])
        verdicts = {int(v["n"]): v for v in found["sentences"]}
    except (TypeError, ValueError, KeyError):
        return None
    kept, read = [], []
    for i, s in enumerate(sentences, 1):
        v = verdicts.get(i) or {}
        verdict = v.get("verdict") if v.get("verdict") in ("supported", "overstated", "unsupported") else "supported"
        row = {"verdict": verdict, "was": s["text"]}
        if verdict == "overstated":
            nums = "".join(f"[{number[c['id']]}]" for c in s["cites"] if c["id"] in number)
            fixed, _ = check(f"{str(v.get('rewrite') or '').strip()} {nums}", lines) if v.get("rewrite") and nums else (None, None)
            if fixed and len(fixed) == 1:           # the reworded sentence passes the same checks as any other
                row["now"] = fixed[0]["text"]
                kept.append({**s, "text": fixed[0]["text"]})
            else:
                kept.append(s)
        elif verdict == "supported":
            kept.append(s)
        read.append(row)
    if not kept:                                    # a read that removes everything is not trusted over the first
        kept, read = sentences, [{"verdict": "supported", "was": s["text"]} for s in sentences]
    left = []
    for o in (found.get("open") or [])[:3]:
        point, ask = str(o.get("point") or "").strip(), str(o.get("check") or "").strip()
        if point and ask:
            rest = [lines[n - 1] for n in o.get("lines") or [] if isinstance(n, int) and 1 <= n <= len(lines)][:4]
            left.append({"point": point[:400], "check": ask[:300], "cites": [{"id": m["id"], "cite": m["cite"], "url": m["url"]} for m in rest]})
    return {"summary": kept, "second": {"read": read, "open": left, "by": llm.setting(CHECKER)[1]}}
