"""
Takes personal details out of a question before it is searched.

    from redact import redact
    clean, removed = redact(question, known_words)

Same call and same result as redact() in search.py: the clean text carries
[name], [date], [phone], [email], [id number], [address]; `removed` lists
the labels, one per detail taken out.

Two modes, chosen by what is installed:

  tool   Microsoft Presidio (presidio-analyzer) with the small spaCy English
         model finds people's names, dates with a year, phone numbers, emails
         and Social Security numbers. The hand-written rules below then run as
         well, so anything the rules caught before is still caught.
  rules  The hand-written rules alone, exactly as in search.py. Used when
         Presidio or the spaCy model is not installed, or when the
         environment variable ORACLE_REDACT is set to "rules".

mode() says which one is in use. warm() loads the tool ahead of the first
question (about two seconds, once).

Install for tool mode: see README, or
    uv pip install -p .venv/bin/python presidio-analyzer
    uv pip install -p .venv/bin/python "en_core_web_sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
"""
import os
import re
import threading

# ------------------------------------------------------------------ RULES --
# Copied from search.py so this file stands alone. STOP is the search stop
# list; the name rule uses it to leave everyday words alone.
STOP = set("""
a an and are as at be been but by can could did do does for from had has have her hers him his how i if in
into is it its may me might my of on or our she should so than that the their them then there these they
this those to was we were what when where which who whom why will with would you your about any all also
get gets got just kind kinds supposed followed following follow correct appropriate right proper happen
happens need needs want say says tell someone something anyone thing things one out up over under again
judy aura client clients s t not no medicaid born name date long does during because more
""".split())

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
    ("[date]", re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? \d{1,2}(?:st|nd|rd|th)?,? \d{4}\b", re.I)),   # only next to a birth word, see _keep
    ("[id number]", re.compile(r"\b[A-Z]{2}\d{5}[A-Z]\b")),
    ("[id number]", re.compile(r"\b\d{7,}\b")),
    ("[address]", re.compile(r"\b\d{1,5}(?:-\d{1,4})? (?:\d{1,3}(?:st|nd|rd|th)|[A-Z][a-z]+)(?: (?:\d{1,3}(?:st|nd|rd|th)|[A-Z][a-z]+)){0,2} "
                             r"(?:St|Street|street|st|Ave|Avenue|avenue|ave|Rd|Road|road|Blvd|Boulevard|Dr|Drive|Pl|Place|Ln|Lane|Concourse|Pkwy|Parkway|Ct|Court)\b\.?")),
    ("[address]", re.compile(r"\b(?:NY|NJ|CT|PA|New York),? \d{5}(?:-\d{4})?\b")),
]
# A date written with a month name ("September 1, 2025") is usually part of the rule being asked about. It is taken
# out only when it follows a birth word; a date written in digits (3/14/1948) is always taken out.
BIRTH = re.compile(r"\b(?:dob|d\.o\.b\.?|born|birth ?date|birthday|date of birth)\b[^.;\n]{0,12}$", re.I)
MONTH_DATE = PATTERNS[4][1]


def _keep(pat, text, start):
    """False for a match that should stay in the question."""
    return pat is not MONTH_DATE or bool(BIRTH.search(text[max(0, start - 30):start]))
NAME = re.compile(r"\b(?:(?:Mr|Mrs|Ms|Miss|Dr)\.? )?[A-Z][a-z]{2,}(?: [A-Z]\.| [A-Z][a-z]+)?")


def _names_by_rule(text, known_words, removed):
    def name(m):
        words = re.sub(r"^(?:Mr|Mrs|Ms|Miss|Dr)\.? ", "", m.group(0)).split()
        titled = m.group(0) != " ".join(words)
        first = words[0]
        if not titled and (first in NOT_NAMES or first.lower() in known_words or first.lower() in STOP - {"judy", "aura"}):
            return m.group(0)
        removed.append("name")
        return "[name]"

    return NAME.sub(name, text)


def redact_rules(text, known_words):
    """The hand-written rules alone. Returns (clean text, what was removed)."""
    removed = []
    for label, pat in PATTERNS:
        def swap(m, label=label, pat=pat):
            if not _keep(pat, m.string, m.start()):
                return m.group(0)
            removed.append(label.strip("[]"))
            return label
        text = pat.sub(swap, text)
    return _names_by_rule(text, known_words, removed), removed


# ------------------------------------------------------------------- TOOL --
# What Presidio calls a detail -> the label the app shows.
LABELS = {"PERSON": "name", "DATE_TIME": "date", "PHONE_NUMBER": "phone", "EMAIL_ADDRESS": "email", "US_SSN": "id number"}
MIN_SCORE = 0.4

# Presidio marks "10 days", "Friday", "last month" and bare numbers as dates. Those are rule words. A date is
# only taken out when it names a year together with a month or a day, or is written in digits (3/14/42).
MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
DATED = re.compile(rf"(?:\b{MONTH}\b.*\b(?:19|20)\d\d\b|\b(?:19|20)\d\d\b.*\b{MONTH}\b|\b\d{{1,4}}[/.-]\d{{1,2}}[/.-]\d{{1,4}}\b)", re.I)
# Words the name finder sometimes sweeps in at the edge of a name.
EDGE = {"client", "clients", "member", "enrollee", "patient", "caller", "the"}

_lock = threading.Lock()
_engine = None      # the Presidio analyzer once loaded; False when it cannot be loaded
_why_not = ""


def _load():
    global _engine, _why_not
    if _engine is not None:
        return _engine
    with _lock:
        if _engine is not None:
            return _engine
        if (os.environ.get("PINPOINT_REDACT") or os.environ.get("ORACLE_REDACT", "")).lower() == "rules":
            _engine, _why_not = False, "PINPOINT_REDACT=rules"
            return _engine
        try:
            import logging
            logging.getLogger("presidio-analyzer").setLevel(logging.ERROR)
            import spacy.util
            if not spacy.util.is_package("en_core_web_sm"):
                raise ImportError("spaCy model en_core_web_sm is not installed")  # without this check Presidio tries to download a model
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider
            nlp = NlpEngineProvider(nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
            }).create_engine()
            _engine = AnalyzerEngine(nlp_engine=nlp, supported_languages=["en"])
        except Exception as e:  # not installed, or would not start: the rules still work
            _engine, _why_not = False, f"{type(e).__name__}: {e}"
    return _engine


def mode():
    """'tool' when Presidio is in use, 'rules' when only the hand-written rules are."""
    return "tool" if _load() else "rules"


def why_rules():
    """Why the tool is not in use ('' when it is)."""
    _load()
    return _why_not


def warm():
    """Load the tool now instead of on the first question."""
    if _load():
        _engine.analyze("Warm up for Jane Roe.", language="en", entities=list(LABELS))
    return mode()


def _person(text, start, end, known_words=()):
    """Trim a name the tool found; None when what is left is rule vocabulary, not a person."""
    while True:
        m = re.match(r"\s*([\w'-]+)[,:]?\s+", text[start:end])
        if m and m.group(1).lower() in EDGE:
            start += m.end()
        else:
            break
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", text[start:end])
    if not words or all(w in NOT_NAMES or w.capitalize() in NOT_NAMES or w.upper() in NOT_NAMES for w in words):
        return None
    if all(w.lower() in known_words for w in words):      # "Legacy Status", "Fidelis Care": words the sources use, not a person
        return None
    return start, end


def _tool_spans(text, known_words=()):
    spans = []
    for r in _engine.analyze(text, language="en", entities=list(LABELS), score_threshold=MIN_SCORE):
        start, end = r.start, r.end
        if r.entity_type == "DATE_TIME":
            if not DATED.search(text[start:end]):
                continue
            if not re.search(r"\d[/.-]\d", text[start:end]) and not BIRTH.search(text[max(0, start - 30):start]):
                continue            # a month-name date away from a birth word: part of the rule, not of the person
            start += len(re.match(r"(?:the |on |in )*", text[start:end], re.I).group(0))
        elif r.entity_type == "PERSON":
            kept = _person(text, start, end, known_words)
            if not kept:
                continue
            start, end = kept
        spans.append((start, end, LABELS[r.entity_type], r.score))
    return spans


def redact_tool(text, known_words):
    """Presidio first, then the hand-written rules on what is left."""
    spans = []   # (start, end, label), never overlapping

    def free(start, end):
        return all(end <= s or start >= e for s, e, _ in spans)

    # 1. the exact patterns (emails, numbers, digit dates, addresses) have first call on the text
    for label, pat in PATTERNS:
        for m in pat.finditer(text):
            if free(m.start(), m.end()) and _keep(pat, text, m.start()):
                spans.append((m.start(), m.end(), label.strip("[]")))
    # 2. what the tool found and the patterns did not; surest and longest first
    for start, end, label, score in sorted(_tool_spans(text, known_words), key=lambda s: (-s[3], s[0] - s[1])):
        if free(start, end):
            spans.append((start, end, label))

    removed, out, at = [], [], 0
    for start, end, label in sorted(spans):
        out.append(text[at:start])
        out.append(f"[{label}]")
        removed.append(label)
        at = end
    out.append(text[at:])
    # 3. the capital-letter name rule on the rest
    return _names_by_rule("".join(out), known_words, removed), removed


def redact(text, known_words):
    """Take personal details out of the question. Returns (clean text, what was removed)."""
    if _load():
        try:
            return redact_tool(text, known_words)
        except Exception:
            pass    # never let the tool stop a question; the rules are the floor
    return redact_rules(text, known_words)
