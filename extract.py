"""Textutvinning ur dokument. Körs i separata processer, så modulen hålls lätt."""
import html
import os
import re
import zipfile

MAX_CHARS = 2_000_000
MAX_FILE = 300 * 1024 * 1024

TEXT_EXT = {
    ".txt", ".md", ".csv", ".tsv", ".log", ".ini", ".cfg", ".conf", ".json", ".xml", ".html", ".htm",
    ".yaml", ".yml", ".sql", ".py", ".js", ".ts", ".cs", ".c", ".cpp", ".h", ".java", ".ps1", ".bat",
    ".cmd", ".vb", ".eml", ".srt",
}
ZIP_PARTS = {
    ".docx": r"word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml",
    ".docm": r"word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml",
    ".dotx": r"word/(document|header\d*|footer\d*)\.xml",
    ".xlsx": r"xl/sharedStrings\.xml",
    ".xlsm": r"xl/sharedStrings\.xml",
    ".pptx": r"ppt/(slides/slide\d+|notesSlides/notesSlide\d+)\.xml",
    ".odt": r"content\.xml",
    ".ods": r"content\.xml",
    ".odp": r"content\.xml",
}
_BREAK = re.compile(r"</(?:w:p|a:p|si|text:p|text:h)>|<w:br/>|<w:tab/>|<a:br/>")
_TAG = re.compile(r"<[^>]+>")


def supported(path):
    ext = os.path.splitext(path)[1].lower()
    return ext in (".pdf", ".rtf") or ext in TEXT_EXT or ext in ZIP_PARTS


def extract(path):
    """Returnerar filens text, eller "" om den inte går att läsa."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            text = _pdf(path)
        elif ext in ZIP_PARTS:
            text = _zip(path, ZIP_PARTS[ext])
        elif ext == ".rtf":
            text = _rtf(path)
        else:
            text = _text(path, ext)
    except Exception:
        return ""
    return text[:MAX_CHARS]


def _pdf(path):
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    fitz.TOOLS.mupdf_display_errors(False)
    parts, total = [], 0
    with fitz.open(path) as doc:
        for page in doc:
            t = page.get_text()
            parts.append(t)
            total += len(t)
            if total > MAX_CHARS:
                break
    return "\n".join(parts)


def _zip(path, pattern):
    parts = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if re.fullmatch(pattern, name):
                xml = _BREAK.sub("\n", z.read(name).decode("utf-8", "replace"))
                parts.append(html.unescape(_TAG.sub("", xml)))
    return "\n".join(parts)


def _read(path):
    with open(path, "rb") as f:
        return f.read(MAX_CHARS * 2)


def _text(path, ext):
    data = _read(path)
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16", "replace")
    elif b"\x00" in data[:4096]:
        return ""
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", "replace")
    if ext in (".html", ".htm", ".xml"):
        text = html.unescape(_TAG.sub(" ", text))
    return text


def _rtf(path):
    s = _read(path).decode("latin-1")
    s = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", "replace"), s)
    s = re.sub(r"\\par[d]?\b ?", "\n", s)
    s = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", s)
    return s.replace("{", "").replace("}", "")
