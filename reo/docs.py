"""Turns documents the user attaches into plain text Reo can read: PDF, Word, Excel, PowerPoint, OpenDocument, RTF, HTML and every text/code/data file.
Pure standard library except PDF, which uses `pypdf` (a small pure-Python package that bootstrap installs). Nothing leaves the computer."""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from xml.etree import ElementTree as ET

MAX_BYTES = 25 * 1024 * 1024
TEXT_EXT = {"txt", "md", "markdown", "csv", "tsv", "json", "xml", "yaml", "yml", "ini", "cfg", "toml", "log", "py", "js", "ts", "tsx", "jsx", "html", "htm",
            "css", "java", "c", "h", "cpp", "hpp", "cs", "go", "rs", "rb", "php", "sql", "sh", "bat", "ps1", "lua", "mcfunction", "mcmeta", "srt", "vtt", "tex", "bib"}
DOC_EXT = {"pdf", "docx", "xlsx", "pptx", "odt", "ods", "odp", "rtf"}
LEGACY = {"doc": "Word 97-2003 (.doc)", "xls": "Excel 97-2003 (.xls)", "ppt": "PowerPoint 97-2003 (.ppt)"}


class DocError(RuntimeError):
    pass


def ext_of(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _zip(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise DocError("This file is damaged or not really that kind of document.") from None


def _xml(z: zipfile.ZipFile, name: str):
    try:
        return ET.fromstring(z.read(name))
    except (KeyError, ET.ParseError):
        return None


# ---------------------------------------------------------------- Word / OpenDocument text
def _docx(data: bytes) -> tuple[str, int | None]:
    z = _zip(data)
    root = _xml(z, "word/document.xml")
    if root is None:
        raise DocError("No Word content found in this file.")
    out = []
    for p in root.iter():
        if _local(p.tag) == "p":
            line = []
            for n in p.iter():
                t = _local(n.tag)
                if t == "t" and n.text:
                    line.append(n.text)
                elif t == "tab":
                    line.append("\t")
                elif t in ("br", "cr"):
                    line.append("\n")
            out.append("".join(line))
        elif _local(p.tag) == "tc":
            out.append("\x1f")                                         # cell boundary marker, tidied below
    text = "\n".join(out)
    text = re.sub(r"\n?\x1f\n?", " | ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip(), None


def _odf(data: bytes) -> tuple[str, int | None]:
    z = _zip(data)
    root = _xml(z, "content.xml")
    if root is None:
        raise DocError("No content found in this file.")
    out = []
    for n in root.iter():
        t = _local(n.tag)
        if t in ("p", "h"):
            out.append("".join(x for x in n.itertext()))
        elif t == "table-row":
            out.append("")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip(), None


# ---------------------------------------------------------------- PowerPoint
def _pptx(data: bytes) -> tuple[str, int | None]:
    z = _zip(data)
    slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=lambda n: int(re.findall(r"\d+", n)[-1]))
    if not slides:
        raise DocError("No slides found in this file.")
    out = []
    for i, name in enumerate(slides, 1):
        root = _xml(z, name)
        paras = []
        if root is not None:
            for p in root.iter():
                if _local(p.tag) == "p":
                    s = "".join(t.text or "" for t in p.iter() if _local(t.tag) == "t").strip()
                    if s:
                        paras.append(s)
        out.append(f"--- Slide {i} ---\n" + "\n".join(paras))
    for name in sorted(n for n in z.namelist() if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", n)):
        root = _xml(z, name)
        notes = " ".join((t.text or "") for t in root.iter() if _local(t.tag) == "t").strip() if root is not None else ""
        notes = re.sub(r"^\d+\s*$", "", notes).strip()
        if notes:
            out.append(f"(speaker notes {re.findall(r'[0-9]+', name)[-1]}) {notes}")
    return "\n\n".join(out), len(slides)


# ---------------------------------------------------------------- Excel
def _col_index(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n


def _xlsx(data: bytes, max_rows: int = 400) -> tuple[str, int | None]:
    z = _zip(data)
    shared = []
    sroot = _xml(z, "xl/sharedStrings.xml")
    if sroot is not None:
        for si in sroot:
            shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
    names = {}
    wb = _xml(z, "xl/workbook.xml")
    rels = _xml(z, "xl/_rels/workbook.xml.rels")
    targets = {r.get("Id"): r.get("Target") for r in (rels if rels is not None else [])}
    if wb is not None:
        for sh in wb.iter():
            if _local(sh.tag) == "sheet":
                rid = next((v for k, v in sh.attrib.items() if k.endswith("}id")), None)
                tgt = targets.get(rid, "")
                path = tgt.lstrip("/") if tgt.startswith("/") else "xl/" + tgt
                names[path] = sh.get("name", "Sheet")
    sheets = sorted((n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)), key=lambda n: int(re.findall(r"\d+", n)[-1]))
    if not sheets:
        raise DocError("No sheets found in this file.")
    out = []
    for path in sheets:
        root = _xml(z, path)
        if root is None:
            continue
        rows = []
        for row in root.iter():
            if _local(row.tag) != "row":
                continue
            cells = {}
            for c in row:
                if _local(c.tag) != "c":
                    continue
                v = next((x for x in c if _local(x.tag) == "v"), None)
                t = c.get("t")
                if t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter() if _local(x.tag) == "t")
                elif v is None or v.text is None:
                    continue
                elif t == "s":
                    try:
                        val = shared[int(v.text)]
                    except (ValueError, IndexError):
                        val = ""
                elif t == "b":
                    val = "TRUE" if v.text == "1" else "FALSE"
                else:
                    val = v.text
                cells[_col_index(c.get("r", "A1"))] = val
            if cells:
                rows.append(" | ".join(cells.get(i, "") for i in range(1, max(cells) + 1)))
            if len(rows) >= max_rows:
                rows.append(f"[... more rows not shown (first {max_rows})]")
                break
        out.append(f"--- Sheet: {names.get(path, path.rsplit('/', 1)[-1])} ---\n" + "\n".join(rows))
    return "\n\n".join(out), len(sheets)


# ---------------------------------------------------------------- RTF / HTML / PDF
def _rtf(data: bytes) -> tuple[str, int | None]:
    s = data.decode("latin-1")
    s = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", "replace"), s)
    s = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), s)
    s = re.sub(r"\{\\\*[^{}]*\}", "", s)
    s = re.sub(r"\\(?:par|line|row)\b ?", "\n", s)
    s = re.sub(r"\\tab\b ?", "\t", s)
    s = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", s)
    s = re.sub(r"[{}]", "", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip(), None


def _pdf(data: bytes) -> tuple[str, int | None]:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise DocError("Reading PDFs needs the 'pypdf' package. Run: pip install pypdf (Reo installs it on start-up when it can).") from None
    try:
        r = PdfReader(io.BytesIO(data))
        if r.is_encrypted:
            try:
                if not r.decrypt(""):
                    raise DocError("This PDF is password-protected.")
            except DocError:
                raise
            except Exception:                                          # noqa: BLE001
                raise DocError("This PDF is password-protected.") from None
        pages = []
        for i, pg in enumerate(r.pages, 1):
            try:
                t = (pg.extract_text() or "").strip()
            except Exception:                                          # noqa: BLE001 - one bad page must not lose the rest
                t = ""
            pages.append(f"--- Page {i} ---\n{t}" if t else "")
        n = len(r.pages)
    except DocError:
        raise
    except Exception:                                                  # noqa: BLE001
        raise DocError("This PDF is damaged or could not be opened.") from None
    if not any(p.strip() for p in pages):
        raise DocError("This PDF has no selectable text (it is probably a scan or images). Reo cannot read pictures of text yet.")
    return "\n\n".join(p for p in pages if p), n


def _text(data: bytes, ext: str) -> tuple[str, int | None]:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encs = ("utf-16", "utf-8-sig", "cp1252")
    else:
        encs = ("utf-8-sig", "cp1252")                                 # utf-16 without a BOM is not guessed: it turns cp1252 text into nonsense
    for enc in encs:
        try:
            text = data.decode(enc)
            break
        except UnicodeError:
            continue
    else:
        text = data.decode("utf-8", errors="replace")
    if "\x00" in text[:2000]:
        raise DocError("This is a binary file, not a text file.")
    if ext in ("html", "htm"):
        from . import web
        title, body = web.page_text(text, limit=10**9)
        return (f"{title}\n\n{body}" if title else body), None
    if ext == "json":
        try:
            return json.dumps(json.loads(text), ensure_ascii=False, indent=1), None
        except ValueError:
            pass
    if ext in ("csv", "tsv"):
        try:
            rows = list(csv.reader(io.StringIO(text), delimiter="\t" if ext == "tsv" else csv.Sniffer().sniff(text[:4000]).delimiter))
            return "\n".join(" | ".join(r) for r in rows), None
        except (csv.Error, ValueError):
            pass
    return text, None


_READERS = {"pdf": _pdf, "docx": _docx, "pptx": _pptx, "xlsx": _xlsx, "odt": _odf, "ods": _odf, "odp": _odf, "rtf": _rtf}


def extract(name: str, data: bytes, max_chars: int = 60000) -> dict:
    """{text, chars, truncated, pages}. Raises DocError with a sentence meant for the user."""
    ext = ext_of(name)
    if len(data) > MAX_BYTES:
        raise DocError(f"{name} is larger than {MAX_BYTES // (1024 * 1024)} MB.")
    if ext in LEGACY:
        raise DocError(f"{LEGACY[ext]} is an old binary format. Open it and save it as {'docx' if ext == 'doc' else 'xlsx' if ext == 'xls' else 'pptx'} (or PDF), then attach that.")
    if ext in _READERS:
        text, pages = _READERS[ext](data)
    elif ext in TEXT_EXT or not ext:
        text, pages = _text(data, ext)
    else:
        text, pages = _text(data, ext)                                 # unknown extension: try as text, refuses real binaries
    text = text.replace("\r\n", "\n").replace("\x00", "").strip()
    if not text:
        raise DocError("No text could be read from this file.")
    total = len(text)
    truncated = total > max_chars
    if truncated:
        text = text[:max_chars].rstrip() + f"\n\n[... Reo only received the first {max_chars:,} of {total:,} characters of this file]"
    return {"text": text, "chars": total, "truncated": truncated, "pages": pages}
