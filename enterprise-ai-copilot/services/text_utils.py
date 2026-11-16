"""Dependency-free text helpers used for chunking, keywords and extractive summaries."""
import re
from collections import Counter

STOPWORDS = set("""a about above after again against all also am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have having he her here
hers him his how i if in into is it its itself just me more most my no nor not now of off on once only or other our ours
out over own same she should so some such than that the their theirs them then there these they this those through to too
under until up very was we were what when where which while who whom why will with would you your yours may shall must
per etc within without upon via us""".split())

_WORD = re.compile(r"[A-Za-z][A-Za-z\-']{2,}")


def tokens(text):
    return [w.lower() for w in _WORD.findall(text or "")]


def top_keywords(text, n=10):
    counts = Counter(w for w in tokens(text) if w not in STOPWORDS and len(w) > 3)
    return [w for w, _ in counts.most_common(n)]


def split_sentences(text):
    text = re.sub(r"\s+", " ", text or "").strip()
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9“\"'(])", text)
    return [p.strip() for p in parts if p.strip()]


def _ranked_sentences(text, n):
    sentences = [s for s in split_sentences(text) if 40 <= len(s) <= 400]
    if not sentences:
        return []
    freq = Counter(w for w in tokens(text) if w not in STOPWORDS)
    scored = []
    for idx, s in enumerate(sentences):
        words = [w for w in tokens(s) if w not in STOPWORDS]
        if not words:
            continue
        scored.append((sum(freq[w] for w in words) / (len(words) ** 0.8), idx, s))
    top = sorted(scored, reverse=True)[:n]
    return [s for _, _, s in sorted(top, key=lambda t: t[1])]


def extractive_summary(text, n=4):
    picked = _ranked_sentences(text, n)
    return " ".join(picked) if picked else (text or "").strip()[:600]


def extractive_points(text, n=6):
    return _ranked_sentences(text, n)


def split_text(text, size=900, overlap=150):
    """Split text into ~size-character chunks on sentence/line boundaries with overlap."""
    pieces = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text or "") if p and p.strip()]
    chunks, current = [], ""
    for piece in pieces:
        while len(piece) > size:  # very long run without punctuation
            cut = piece.rfind(" ", 0, size) or size
            cut = cut if cut > 0 else size
            if current:
                chunks.append(current)
                current = ""
            chunks.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if not piece:
            continue
        if len(current) + len(piece) + 1 <= size:
            current = f"{current} {piece}".strip()
        else:
            if current:
                chunks.append(current)
                tail = current[-overlap:]
                tail = tail[tail.find(" ") + 1:] if " " in tail else ""
                current = f"{tail} {piece}".strip()
            else:
                current = piece
    if current:
        chunks.append(current)
    return [c for c in chunks if len(c) > 15]
