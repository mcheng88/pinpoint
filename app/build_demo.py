"""Build the recorded demo: the two screens as plain files, for any static host.

    python app/build_demo.py recording.json out_dir

`recording.json` is what app/record_demo.py saved. The build keeps only what the screens show:
the replies, the source text they name, the list of loaded sources and the recorded keyword searches.
It leaves out the organisation's own list of links, the flags typed on the live site and the search internals.
"""
import json
import shutil
import sys
from pathlib import Path

WEB = Path(__file__).parent / "web"
DROP = ("pool", "scores", "phrases", "strangers", "ask_id")       # search internals the screens do not read
SEED = ["What are the reasons that a Medicaid plan may disenroll a member?",
        "Can a plan disenroll someone because she needs more care?",
        "What process is supposed to be followed for these disenrollments?",
        "Should she sue her plan?"]                                 # the case a first-time visitor finds already worked


def main(recording, out):
    raw = json.loads(Path(recording).read_text())
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for a in raw["asks"]:
        a["reply"] = {k: v for k, v in a["reply"].items() if k not in DROP}
    data = {"recorded": raw["recorded"], "facts": raw["facts"], "seed": SEED, "asks": raw["asks"], "sections": raw["sections"],
            "find": raw["find"], "sources": raw["sources"],
            "knowledge": {"sources": raw["knowledge"]["sources"], "catalog": [], "keeper": raw["knowledge"].get("keeper")}}
    (out / "data.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    for name in ("index.html", "sources.html"):
        page = (WEB / name).read_text()
        for old, new in (('href="/theme.css"', 'href="theme.css"'), ('href="/sources"', 'href="sources.html"'), ('href="/"', 'href="./"'),
                         ("<script>", '<script src="demo.js"></script>\n<script>')):
            assert old in page, (name, old)
            page = page.replace(old, new, 1 if old == "<script>" else -1)
        (out / name).write_text(page)
    for name in ("theme.css", "demo.js"):
        shutil.copy(WEB / name, out / name)
    (out / ".nojekyll").write_text("")
    print(f"{len(data['asks'])} questions, {len(data['sections'])} sections, {(out / 'data.json').stat().st_size // 1024} KB -> {out}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
