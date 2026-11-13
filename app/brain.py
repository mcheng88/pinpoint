"""The small models that read and pick. Nothing here writes text.

Three open-weight models, all small, all on CPU:

  embedder   BAAI/bge-small-en-v1.5           33M parameters, MIT licence
             turns a question and each passage into vectors, so "member
             dropped by her plan" can find "disenrollment of an enrollee"
  reranker   cross-encoder/ms-marco-MiniLM-L-6-v2   22M parameters, Apache 2.0
             reads the question next to each candidate passage and scores
             how well the passage answers it; a low top score means "no answer"
  reader     deepset/tinyroberta-squad2            82M parameters, MIT licence
             points at the words inside the best passage that answer the
             question ("may not request disenrollment", "sixty (60) days"),
             so the screen can show one sentence of the source as the short
             answer. It can only point; it cannot write.

All download once (about 460 MB together) into the user's cache. If the
packages are not installed, `Brain.available` is False and search.py falls
back to keyword rules.
"""
import threading

EMBEDDER = "BAAI/bge-small-en-v1.5"
RERANKER = "cross-encoder/ms-marco-MiniLM-L-6-v2"
READER = "deepset/tinyroberta-squad2"
SPAN_MAX_TOKENS = 40
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "   # what bge expects

try:
    import numpy as np
    import torch
    from sentence_transformers import CrossEncoder, SentenceTransformer
    from transformers import AutoModelForQuestionAnswering, AutoTokenizer
    available = True
except ImportError:              # the app still runs, on keyword rules only
    available = False


class Brain:
    def __init__(self, db):
        self.db = db
        self.available = available
        self.lock = threading.Lock()
        if not available:
            return
        self.ids = np.zeros(0, dtype=np.int64)
        self.vecs = np.zeros((0, 384), dtype=np.float32)
        self.embedder = SentenceTransformer(EMBEDDER, device="cpu")
        self.reranker = CrossEncoder(RERANKER, device="cpu", max_length=320)   # enough for a paragraph; keeps CPU time low
        self.reader_tok = AutoTokenizer.from_pretrained(READER)
        self.reader = AutoModelForQuestionAnswering.from_pretrained(READER).eval()
        db.execute("CREATE TABLE IF NOT EXISTS vectors(id INTEGER PRIMARY KEY, vec BLOB)")
        db.commit()

    # ---- the index ----
    def index(self, ok_ids_sql):
        """Embed any passage that has no vector yet, then load vectors for the approved sources."""
        if not self.available:
            return
        with self.lock:
            todo = self.db.execute(                      # only approved sources; the rest wait until approval
                f"SELECT p.id, p.text, p.heading, f.context FROM passages p JOIN fts f ON f.rowid = p.id "
                f"WHERE p.id NOT IN (SELECT id FROM vectors) AND p.source_id IN {ok_ids_sql}").fetchall()
            if todo:      # the passage with its path and note (notes.py), so a bare "(c) …" line carries what it is about
                texts = [f"{r['context'] or r['heading']}\n{r['text']}" for r in todo]
                for start in range(0, len(texts), 256):
                    chunk = texts[start:start + 256]
                    vecs = self.embedder.encode(chunk, normalize_embeddings=True, batch_size=32, show_progress_bar=False)
                    self.db.executemany("INSERT INTO vectors(id, vec) VALUES(?, ?)",
                                        [(todo[start + i]["id"], vecs[i].astype(np.float32).tobytes()) for i in range(len(chunk))])
                    self.db.commit()
            rows = self.db.execute(
                f"SELECT v.id, v.vec FROM vectors v JOIN passages p ON p.id = v.id WHERE p.source_id IN {ok_ids_sql}").fetchall()
            self.ids = np.array([r["id"] for r in rows], dtype=np.int64)
            self.vecs = np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float32).reshape(len(rows), -1) if rows else np.zeros((0, 384), dtype=np.float32)

    # ---- the two steps ----
    def nearest(self, question, k=40):
        """Passages whose meaning is closest to the question. Returns [(id, similarity)]."""
        if not self.available or not len(self.ids):
            return []
        q = self.embedder.encode(QUERY_PREFIX + question, normalize_embeddings=True, show_progress_bar=False)
        sims = self.vecs @ q
        top = np.argsort(-sims)[:k]
        return [(int(self.ids[i]), float(sims[i])) for i in top]

    def rerank(self, question, candidates):
        """candidates: [(id, text)]. Returns [(id, score)] best first. Score is a logit:
        above 0 the model thinks the passage answers the question; well below, it does not."""
        if not self.available or not candidates:
            return []
        scores = self.reranker.predict([(question, text) for _, text in candidates], batch_size=16, show_progress_bar=False)
        order = np.argsort(-scores)
        return [(candidates[i][0], float(scores[i])) for i in order]

    # ---- the third step: point at the answer ----
    def read(self, question, texts):
        """For each text, the best answer span: [(score, start, end)] by character offset.
        Score is the span's logit minus the model's "no answer here" logit, so below 0
        the model would rather say nothing."""
        if not self.available or not texts:
            return []
        enc = self.reader_tok([question] * len(texts), texts, truncation="only_second", max_length=384,
                              padding=True, return_offsets_mapping=True, return_tensors="pt")
        offsets = enc.pop("offset_mapping")
        with torch.no_grad():
            out = self.reader(**enc)
        res = []
        for i, text in enumerate(texts):
            s, e = out.start_logits[i], out.end_logits[i]
            ctx = torch.tensor([sid == 1 for sid in enc.sequence_ids(i)])
            n = len(s)
            grid = s[:, None] + e[None, :]                                     # score of every (start, end)
            allowed = ctx[:, None] & ctx[None, :] & (torch.arange(n)[None, :] >= torch.arange(n)[:, None]) \
                & (torch.arange(n)[None, :] - torch.arange(n)[:, None] < SPAN_MAX_TOKENS)
            grid = grid.masked_fill(~allowed, -1e9)
            flat = int(grid.argmax())
            a, b = divmod(flat, n)
            null = float(s[0] + e[0])
            res.append((float(grid[a, b]) - null, int(offsets[i][a][0]), int(offsets[i][b][1])))
        return res
