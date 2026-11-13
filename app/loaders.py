"""Loaders for the kinds of source on CSS's list that are not on ecfr.gov.

    uscode   US Code sections on law.cornell.edu (structured: (a)(1)(A) ...)
    nycrr    New York regulations on law.cornell.edu (structured). The list links
             to Westlaw, which blocks programs; Cornell carries the same text.
    pdf      PDFs (DOH guidance, model contracts). Text only; scanned PDFs need OCR.
    page     Any other web page: paragraphs, lists and headings of its main content.
    blocked  New York statutes on FindLaw: behind a bot wall. Needs a free NY Senate
             API key or a saved copy. Recorded, not loaded.
    skip     Private links (SharePoint) and index pages with no rule text.

Each loader returns (name, blocks) where a block is (section, heading, path, text):
section groups passages for the side panel, path is the (a)(1) designator list
or None, text is the source's own words.
"""
import gzip
import io
import re
import urllib.request
from html import unescape
from html.parser import HTMLParser

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36", "Accept-Encoding": "gzip"}
CORNELL = "https://www.law.cornell.edu"


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90) as r:
        body = r.read()
        return gzip.decompress(body) if r.headers.get("Content-Encoding") == "gzip" else body


def clean(text):
    return re.sub(r"\s+", " ", unescape(text)).strip()


def strip_tags(html):
    return clean(re.sub(r"<[^>]+>", " ", html))


# ------------------------------------------------------------ which loader --
def classify(url, name=""):
    u = url.lower()
    if "ecfr.gov" in u:
        return "ecfr"
    if "sharepoint.com" in u:
        return "skip"
    if u.endswith(".pdf"):
        return "pdf"
    if "law.cornell.edu/uscode/text/" in u:
        return "uscode" if re.search(r"/uscode/text/\d+/[\w-]+$", u) else "skip"   # part-level index pages
    if "law.cornell.edu/regulations/new-york/" in u:
        return "nycrr"
    if "westlaw.com" in u:
        return "nycrr" if cornell_nycrr_url(name) else "blocked"
    if "findlaw.com" in u:
        return "blocked"
    return "page"


def cornell_nycrr_url(name):
    """Map a Westlaw link's name ('18 NYCRR § 505.14 ...', '§ 98-1.13') to Cornell."""
    m = re.search(r"§\s*(98-1\.\d+|505\.\d+|358-\d+\.\d+)", name or "")
    if not m:
        return None
    sec = m.group(1)
    title = 10 if sec.startswith("98-") else 18
    return f"{CORNELL}/regulations/new-york/{title}-NYCRR-{sec}"


# ----------------------------------------------------------------- Cornell --
LEVELS_USC = {"subsection": 1, "paragraph": 2, "subparagraph": 3, "clause": 4, "subclause": 5, "item": 6, "subitem": 7}
DESIG = re.compile(r"^\(([A-Za-z0-9]{1,5})\)\s*")


def _tab(html):
    """The first tab of a Cornell page holds the text."""
    i = html.find('id="tab_default_1"')
    if i < 0:
        return html
    j = html.find('id="tab_default_2"', i)
    return html[i: j if j > 0 else None]


def _blocks_by_class(html, pattern, level_of):
    """Split the HTML at each block-opening tag; each piece's own text is one block."""
    parts = re.split(r'(<div class="(?:' + pattern + r')[^"]*"[^>]*>)', html)
    out, cur_level = [], None
    for piece in parts:
        m = re.match(r'<div class="([^"]*)"', piece)
        if m and re.match(pattern, m.group(1)):
            cur_level = level_of(m.group(1))
            continue
        if cur_level is None:
            continue
        text = strip_tags(piece)
        if text:
            out.append((cur_level, text))
        cur_level = None
    return out


def _paths(blocks):
    """Turn (level, text) into (path, text) using the designators in the text."""
    path, out = [], []
    for level, text in blocks:
        m = DESIG.match(text)
        if m:
            path = path[: level - 1]
            while len(path) < level - 1:
                path.append("")
            path.append(m.group(1))
            out.append((list(path), text))
        else:
            out.append((None, text))
    return out


def load_uscode(url):
    html = get(url).decode("utf-8", "ignore")
    title = clean(re.search(r"<title>(.*?)</title>", html, re.S).group(1)).split("|")[0]
    title = title.replace(" - ", " – ")
    sec = re.search(r"/uscode/text/(\d+)/([\w-]+)", url)
    section = f"{sec.group(1)} U.S.C. § {sec.group(2)}"
    heading = title.split("–", 1)[1].strip() if "–" in title else title
    blocks = _blocks_by_class(_tab(html), "|".join(LEVELS_USC), lambda c: LEVELS_USC[c.split()[0]])
    if not blocks:
        blocks = [(1, t) for t in re.findall(r"<p[^>]*>(.*?)</p>", _tab(html), re.S) if strip_tags(t)]
        blocks = [(l, strip_tags(t)) for l, t in blocks]
    return section, [(section, heading, path, text) for path, text in _paths(blocks)]


def load_nycrr(url):
    html = get(url).decode("utf-8", "ignore")
    title = clean(re.search(r"<title>(.*?)</title>", html, re.S).group(1)).split("|")[0]
    m = re.search(r"Tit\.\s*(\d+)\s*§+\s*([\d\-.]+)\s*-\s*(.*)", title)
    section = f"{m.group(1)} NYCRR § {m.group(2)}" if m else title
    heading = m.group(3).strip() if m else title
    blocks = _blocks_by_class(_tab(html), r"subsect indent\d", lambda c: int(re.search(r"indent(\d)", c).group(1)) + 1)
    return section, [(section, heading, path, text) for path, text in _paths(blocks)]


# ------------------------------------------------------------ plain pages --
class _Text(HTMLParser):
    KEEP = {"p", "li", "h1", "h2", "h3", "h4", "td", "dd", "dt"}
    DROP = {"script", "style", "nav", "header", "footer", "noscript", "form", "button"}

    def __init__(self):
        super().__init__()
        self.blocks, self.buf, self.tag, self.drop, self.title, self.in_title = [], [], None, 0, "", False
        self.in_a, self.linked = 0, 0          # how much of the block is link text

    def handle_starttag(self, tag, attrs):
        if tag in self.DROP:
            self.drop += 1
        if tag == "title":
            self.in_title = True
        if tag in self.KEEP and not self.drop:
            self._flush()
            self.tag = tag
        if tag == "a":
            self.in_a += 1

    def handle_endtag(self, tag):
        if tag in self.DROP and self.drop:
            self.drop -= 1
        if tag == "title":
            self.in_title = False
        if tag == "a" and self.in_a:
            self.in_a -= 1
        if tag in self.KEEP and tag == self.tag:
            self._flush()

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif self.tag and not self.drop:
            self.buf.append(data)
            if self.in_a:
                self.linked += len(data.strip())

    def _flush(self):
        text = clean("".join(self.buf))
        if text and self.linked < 0.8 * len(text):       # a block that is nearly all link text is a menu, not a rule
            self.blocks.append((self.tag, text))
        self.buf, self.tag, self.linked = [], None, 0


def main_part(html):
    """The page's own content, when the page marks it (main, article, #content, #main)."""
    for pat in (r"<main[^>]*>", r"<article[^>]*>", r'<div id="content"[^>]*>', r'<div id="main[^"]*"[^>]*>'):
        m = re.search(pat, html)
        if m:
            end = html.find("<footer", m.end())
            return html[m.end(): end if end > 0 else None]
    return html


def load_page(url, name=None):
    html = get(url).decode("utf-8", "ignore")
    title_html = html[: html.find("</head>")] if "</head>" in html else html
    p = _Text()
    p.feed(title_html)
    p.feed(main_part(html))                 # the page's own content only; feeding the whole page too doubled every paragraph
    title = clean(p.title).split("|")[0].split(" - ")[0] or name or url
    name = name or title
    blocks, heading = [], title
    for tag, text in p.blocks:
        if tag in ("h1", "h2", "h3", "h4"):
            heading = text
            continue
        if len(text) < 60 or not re.search(r"[a-z]{3,} [a-z]{2,} [a-z]{2,}", text):   # a title or a label, not a sentence
            continue
        blocks.append((name, heading, None, text))
    return name, blocks


def load_pdf(url, name=None):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(get(url)))
    name = name or (reader.metadata.title if reader.metadata and reader.metadata.title else url.rsplit("/", 1)[-1])
    name = clean(str(name))
    blocks = []
    for n, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        paras = [clean(x) for x in re.split(r"\n\s*\n", text)]
        if len(paras) <= 1 and text.strip():          # no blank lines: cut at sentence ends, ~600 chars
            paras, cur = [], ""
            for sent in re.split(r"(?<=[.;:])\s+", clean(text)):
                if len(cur) + len(sent) > 600 and cur:
                    paras.append(cur)
                    cur = ""
                cur = (cur + " " + sent).strip()
            if cur:
                paras.append(cur)
        for para in paras:
            if len(para) >= 60:
                blocks.append((f"page {n}", f"Page {n}", None, para))
    return name, blocks


def load(kind, url, name=None):
    if kind == "uscode":
        return load_uscode(url)
    if kind == "nycrr":
        return load_nycrr(cornell_nycrr_url(name) if "westlaw" in url else url)
    if kind == "pdf":
        return load_pdf(url, name)
    if kind == "page":
        return load_page(url, name)
    raise ValueError(f"No loader for {kind}")
