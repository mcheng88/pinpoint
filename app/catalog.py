"""Read CSS's source list out of the Track 1 document into data/catalog.json.

    python3 -I app/catalog.py "track1/Track 1_ ... .md" data/catalog.json

The appendix of the document is a nested list of links under headings:
area (##), kind (###: Statutes, Regulations, Guidance) and level (####:
Federal, State). Each link becomes one catalog entry.
"""
import json
import re
import sys

LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def main(md_path, out_path):
    rows, area, kind, level = [], None, None, None
    started = False
    for line in open(md_path, encoding="utf-8"):
        if "Appendix" in line and "Dataset" in line:
            started = True
            continue
        if not started:
            continue
        h = re.match(r"^(#{2,4})\s+\**(.+?)\**\s*$", line)
        if h:
            name = h.group(2).strip()
            if len(h.group(1)) == 2:
                area, kind, level = name, None, None
            elif len(h.group(1)) == 3:
                kind, level = name, None
            else:
                level = name
            continue
        for text, url in LINK.findall(line):
            host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
            rows.append({
                "area": area, "kind": kind, "level": level or ("Federal" if "cornell" in host or "ecfr" in host or "cms.gov" in host or "medicaid.gov" in host or "hhs.gov" in host else None),
                "name": text.strip(), "url": url, "host": host,
                "pdf": url.lower().endswith(".pdf"),
                "loader": "ecfr" if "ecfr.gov" in host else ("pdf" if url.lower().endswith(".pdf") else "page"),
            })
    json.dump(rows, open(out_path, "w"), indent=1)
    print(f"{len(rows)} links in {len({r['area'] for r in rows})} areas -> {out_path}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
