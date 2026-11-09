"""Local resume text extraction (`.pdf`, `.docx`, `.odt`, `.rtf`, `.txt`, `.md`).

Runs 100% locally with zero network calls and no required third-party packages:
- `.txt` / `.md`: UTF-8 / UTF-16 / Latin-1 decoding.
- `.docx` / `.odt`: Python stdlib ``zipfile`` + ``xml.etree.ElementTree`` paragraph extraction.
- `.pdf`: tries ``pypdf`` if installed, then ``pdftotext`` if on PATH, then a built-in
  pure-Python ``zlib`` FlateDecode + PDF text-operator / ToUnicode CMap parser.
- `.rtf`: uses macOS ``textutil`` if present, falling back to an RTF control-word stripper.
"""

from __future__ import annotations

import base64
import io
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MB raw resume file limit


class ResumeError(ValueError):
    """Raised when an uploaded resume cannot be decoded or yields no readable text."""


def decode_base64_upload(data_base64: str, max_bytes: int = MAX_UPLOAD_BYTES) -> bytes:
    raw = (data_base64 or "").strip()
    if "," in raw and raw.lower().startswith("data:"):
        raw = raw.split(",", 1)[1]
    if not raw:
        raise ResumeError("Empty file upload.")
    try:
        decoded = base64.b64decode(raw, validate=False)
    except Exception as exc:
        raise ResumeError("Uploaded file is not valid base64 data.") from exc
    if not decoded:
        raise ResumeError("Uploaded file is empty.")
    if len(decoded) > max_bytes:
        raise ResumeError(f"Resume file exceeds {max_bytes // (1024 * 1024)} MB limit.")
    return decoded


def extract_text(filename: str, data: bytes) -> str:
    """Extract readable plain text from resume bytes based on file extension / magic bytes."""
    if not data:
        raise ResumeError("Uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ResumeError(f"Resume file exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.")

    ext = Path(filename or "resume.txt").suffix.lower()
    if data.startswith(b"%PDF") or ext == ".pdf":
        text = _extract_pdf(data)
    elif ext == ".docx":
        text = _extract_docx(data)
    elif ext == ".odt":
        text = _extract_odt(data)
    elif ext == ".rtf" or data.startswith(b"{\\rtf"):
        text = _extract_rtf(data)
    elif ext in (".txt", ".md", ".markdown", ""):
        text = _decode_plain(data)
    else:
        raise ResumeError(
            f"Unsupported file format '{ext}'. Upload a .pdf, .docx, .txt, or .md resume, or paste text directly."
        )

    cleaned = _clean_whitespace(text)
    if len(cleaned) < 20:
        raise ResumeError(
            "Could not extract enough readable text from this file (it may be a scanned image PDF). "
            "Try exporting as selectable PDF / DOCX or paste your resume text directly."
        )
    return cleaned


def _decode_plain(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "utf-16", "cp874", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _extract_docx(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml_names = [
                name for name in zf.namelist()
                if name == "word/document.xml" or re.match(r"word/(header|footer)\d*\.xml$", name)
            ]
            if not xml_names:
                raise ResumeError("Invalid .docx file (missing word/document.xml).")
            # Put main document first
            xml_names.sort(key=lambda n: 0 if n == "word/document.xml" else 1)
            blocks: list[str] = []
            for name in xml_names:
                root = ET.fromstring(zf.read(name))
                for elem in root.iter():
                    tag = elem.tag.rsplit("}", 1)[-1]
                    if tag == "p":
                        parts: list[str] = []
                        for child in elem.iter():
                            ctag = child.tag.rsplit("}", 1)[-1]
                            if ctag == "t" and child.text:
                                parts.append(child.text)
                            elif ctag == "tab":
                                parts.append("\t")
                            elif ctag in ("br", "cr"):
                                parts.append("\n")
                        line = "".join(parts).strip()
                        if line:
                            blocks.append(line)
            return "\n".join(blocks)
    except zipfile.BadZipFile as exc:
        raise ResumeError("Could not read .docx archive (file appears corrupted).") from exc
    except ET.ParseError as exc:
        raise ResumeError("Could not parse XML inside .docx file.") from exc


def _extract_odt(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            if "content.xml" not in zf.namelist():
                raise ResumeError("Invalid .odt file (missing content.xml).")
            root = ET.fromstring(zf.read("content.xml"))
            lines = []
            for elem in root.iter():
                tag = elem.tag.rsplit("}", 1)[-1]
                if tag in ("p", "h"):
                    txt = "".join(elem.itertext()).strip()
                    if txt:
                        lines.append(txt)
            return "\n".join(lines)
    except (zipfile.BadZipFile, ET.ParseError) as exc:
        raise ResumeError("Could not read .odt file.") from exc


def _extract_rtf(data: bytes) -> str:
    textutil = shutil.which("textutil")
    if textutil:
        try:
            proc = subprocess.run(
                [textutil, "-stdin", "-stdout", "-convert", "txt", "-format", "rtf"],
                input=data, capture_output=True, timeout=10,
            )
            if proc.returncode == 0 and proc.stdout:
                return _decode_plain(proc.stdout)
        except (OSError, subprocess.SubprocessError):
            pass
    raw = _decode_plain(data)
    raw = re.sub(r"\\par[d]?\b", "\n", raw)
    raw = re.sub(r"\\'[0-9a-fA-F]{2}", lambda m: bytes.fromhex(m.group(0)[2:]).decode("latin-1"), raw)
    raw = re.sub(r"\\[a-zA-Z]+-?\d*\s?", "", raw)
    raw = raw.replace("{", "").replace("}", "")
    return raw


def _extract_pdf(data: bytes) -> str:
    # 1. Try pypdf if available in the Python environment
    try:
        import pypdf  # type: ignore
        reader = pypdf.PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        joined = "\n\n".join(p for p in pages if p)
        if len(joined.strip()) >= 20:
            return joined
    except Exception:
        pass

    # 2. Try pdftotext CLI if installed on PATH
    pdftotext = shutil.which("pdftotext")
    if pdftotext:
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
                tmp.write(data)
                tmp.flush()
                proc = subprocess.run(
                    [pdftotext, "-layout", "-enc", "UTF-8", tmp.name, "-"],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
                )
                if proc.returncode == 0 and proc.stdout:
                    out = proc.stdout.decode("utf-8", errors="replace").strip()
                    if len(out) >= 20:
                        return out
        except (OSError, subprocess.SubprocessError):
            pass

    # 3. Pure-Python stdlib fallback: decompress PDF streams and extract text operators
    return _extract_pdf_stdlib(data)


def _extract_pdf_stdlib(data: bytes) -> str:
    """Pure-Python PDF text extractor supporting FlateDecode streams, literal strings, and ToUnicode CMaps."""
    streams: list[bytes] = []
    for match in re.finditer(rb"stream(?:\r\n|\n)(.*?)(?:\r\n|\n)?endstream", data, re.DOTALL):
        chunk = match.group(1)
        decompressed = None
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                decompressed = zlib.decompress(chunk, wbits)
                break
            except zlib.error:
                continue
        streams.append(decompressed if decompressed is not None else chunk)
    if not streams:
        streams = [data]

    cmap: dict[int, str] = {}
    for s in streams:
        if b"beginbfchar" in s or b"beginbfrange" in s:
            _parse_cmap(s.decode("latin-1", errors="ignore"), cmap)

    lines: list[str] = []
    for s in streams:
        if b"BT" not in s:
            continue
        text = s.decode("latin-1", errors="ignore")
        for block in re.findall(r"\bBT\b(.*?)\bET\b", text, re.DOTALL):
            parts: list[str] = []
            for token_match in re.finditer(
                r"\[((?:[^\[\]]|\\\(|\\\))*)\]\s*TJ|\(((?:[^()\\]|\\.)*)\)\s*Tj|<([0-9A-Fa-f\s]+)>\s*Tj|\b(T\*|Td|TD)\b",
                block,
            ):
                tj_arr, tj_lit, tj_hex, newline_op = token_match.groups()
                if newline_op:
                    if parts and parts[-1] != "\n":
                        parts.append("\n")
                elif tj_lit is not None:
                    parts.append(_decode_pdf_literal(tj_lit))
                elif tj_hex is not None:
                    parts.append(_decode_pdf_hex(tj_hex, cmap))
                elif tj_arr is not None:
                    for sub in re.finditer(r"\(((?:[^()\\]|\\.)*)\)|<([0-9A-Fa-f\s]+)>|(-?\d+(?:\.\d+)?)", tj_arr):
                        lit, hx, kern = sub.groups()
                        if lit is not None:
                            parts.append(_decode_pdf_literal(lit))
                        elif hx is not None:
                            parts.append(_decode_pdf_hex(hx, cmap))
                        elif kern is not None:
                            try:
                                if float(kern) < -120:
                                    parts.append(" ")
                            except ValueError:
                                pass
            rendered = "".join(parts).strip()
            if rendered:
                lines.append(rendered)
    return "\n".join(lines)


def _parse_cmap(content: str, cmap: dict[int, str]) -> None:
    for block in re.findall(r"beginbfchar(.*?)endbfchar", content, re.DOTALL):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            try:
                cmap[int(src, 16)] = bytes.fromhex(dst).decode("utf-16-be", errors="ignore")
            except ValueError:
                pass
    for block in re.findall(r"beginbfrange(.*?)endbfrange", content, re.DOTALL):
        for start_h, end_h, dst_h in re.findall(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block
        ):
            try:
                start, end, base = int(start_h, 16), int(end_h, 16), int(dst_h, 16)
                for offset in range(min(end - start + 1, 512)):
                    code_bytes = (base + offset).to_bytes(max(2, len(dst_h) // 2), "big")
                    cmap[start + offset] = code_bytes.decode("utf-16-be", errors="ignore")
            except (ValueError, OverflowError):
                pass


def _decode_pdf_literal(raw: str) -> str:
    out = []
    i = 0
    n = len(raw)
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "(": "(", ")": ")", "\\": "\\"}
    while i < n:
        ch = raw[i]
        if ch == "\\" and i + 1 < n:
            nxt = raw[i + 1]
            if nxt in escapes:
                out.append(escapes[nxt])
                i += 2
                continue
            if nxt.isdigit():
                octal = raw[i + 1 : i + 4]
                m = re.match(r"[0-7]{1,3}", octal)
                if m:
                    out.append(chr(int(m.group(0), 8)))
                    i += 1 + len(m.group(0))
                    continue
            i += 1
            continue
        out.append(ch)
        i += 1
    data = "".join(out).encode("latin-1", errors="ignore")
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", errors="ignore")
    return data.decode("latin-1", errors="ignore")


def _decode_pdf_hex(hex_str: str, cmap: dict[int, str]) -> str:
    cleaned = re.sub(r"\s+", "", hex_str)
    if len(cleaned) % 2 == 1:
        cleaned += "0"
    try:
        raw = bytes.fromhex(cleaned)
    except ValueError:
        return ""
    if cmap:
        step = 2 if len(raw) >= 2 and len(raw) % 2 == 0 and any((raw[i] << 8 | raw[i + 1]) in cmap for i in range(0, len(raw), 2)) else 1
        if step == 2:
            return "".join(cmap.get(raw[i] << 8 | raw[i + 1], "") for i in range(0, len(raw), 2))
        return "".join(cmap.get(b, chr(b)) for b in raw)
    if raw.startswith(b"\xfe\xff") or (len(raw) % 2 == 0 and raw[0:1] == b"\x00"):
        return raw.decode("utf-16-be", errors="ignore")
    return raw.decode("latin-1", errors="ignore")


def _clean_whitespace(text: str) -> str:
    text = (text or "").replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]{2,}", " ", ln).strip() for ln in text.split("\n")]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if not ln:
            blank += 1
            if blank <= 1:
                out.append("")
        else:
            blank = 0
            out.append(ln)
    return "\n".join(out).strip()
