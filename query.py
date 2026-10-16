"""Tolkning av söktext. Ord inom citattecken söks exakt, som hela ord:
    "135"            → hittar 135, 135-1, 135.pdf men inte 135a eller 1350
    "svets procedur" → exakt den frasen
Övriga ord matchar var som helst i texten (delord).
"""
import re

_TOKEN = re.compile(r'"([^"]*)"?|(\S+)')


def parse(text):
    """Returnerar [(term_i_gemener, exakt)]."""
    out = []
    for m in _TOKEN.finditer(text):
        if m.group(1) is not None:
            term = " ".join(m.group(1).lower().split())
            if term:
                out.append((term, True))
        else:
            out.append((m.group(2).lower(), False))
    return out


def word_pattern(term):
    """Regex-mönster som hittar term som hela ord (inte omgiven av bokstäver eller siffror)."""
    body = r"\s+".join(re.escape(p) for p in term.split())
    return r"(?<![^\W_])" + body + r"(?![^\W_])"


def word_re(term):
    return re.compile(word_pattern(term), re.IGNORECASE)


def single(text):
    """För ett sökfält med en enda sökning (t.ex. i förhandsvisningen): (text, exakt)."""
    s = text.strip()
    terms = parse(s)
    if len(terms) == 1 and terms[0][1]:
        return terms[0][0], True
    return s.replace('"', "").strip(), False
