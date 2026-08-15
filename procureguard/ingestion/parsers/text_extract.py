"""Text extraction and chunking for engineering documents.

Handles the formats that actually turn up attached to a requisition: PDF
drawings and datasheets, Word specs, Excel BOMs and parts lists, CSV, and plain
text. PDF and Office parsing use optional dependencies; when one is missing the
extractor says so explicitly rather than returning empty text that would look
like "the specification contains no requirements".

Chunking is structure-aware: it splits on headings and clause numbers first, so
a requirement and its tolerance stay in the same chunk.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field

from procureguard.observability import logger

log = logger(__name__)

MAX_CHUNK_CHARS = 1800
CHUNK_OVERLAP_CHARS = 200
MIN_CHUNK_CHARS = 60


@dataclass(slots=True)
class ExtractedText:
    text: str
    method: str
    page_count: int = 0
    char_count: int = 0
    warnings: list[str] = field(default_factory=list)
    tables: list[list[list[str]]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


@dataclass(slots=True)
class Chunk:
    content: str
    chunk_index: int
    page_number: int = 0
    section_path: str = ""
    token_estimate: int = 0


class TextExtractor:
    """Format-dispatching text extraction."""

    def extract(
        self, content: bytes, *, media_type: str = "", filename: str = ""
    ) -> ExtractedText:
        kind = self._detect(content, media_type, filename)
        try:
            match kind:
                case "pdf":
                    return self._extract_pdf(content)
                case "docx":
                    return self._extract_docx(content)
                case "xlsx":
                    return self._extract_xlsx(content)
                case "csv":
                    return self._extract_csv(content)
                case "html":
                    return self._extract_html(content)
                case _:
                    return self._extract_plain(content)
        except Exception as exc:
            log.error("text_extraction_failed", kind=kind, detail=str(exc)[:300])
            return ExtractedText(
                text="",
                method=f"{kind}:failed",
                warnings=[f"Extraction failed for {filename or kind}: {exc}"],
            )

    @staticmethod
    def _detect(content: bytes, media_type: str, filename: str) -> str:
        suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        lowered = (media_type or "").lower()
        if content[:5] == b"%PDF-" or "pdf" in lowered or suffix == "pdf":
            return "pdf"
        if content[:2] == b"PK":
            # OOXML containers are zips; the part names disambiguate them.
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    names = set(archive.namelist())
                if "word/document.xml" in names:
                    return "docx"
                if "xl/workbook.xml" in names:
                    return "xlsx"
            except zipfile.BadZipFile:
                pass
        if suffix in ("docx", "doc") or "wordprocessing" in lowered:
            return "docx"
        if suffix in ("xlsx", "xls") or "spreadsheet" in lowered or "excel" in lowered:
            return "xlsx"
        if suffix in ("csv", "tsv") or "csv" in lowered:
            return "csv"
        if suffix in ("html", "htm") or "html" in lowered:
            return "html"
        return "text"

    # -------------------------------------------------------------------- PDF
    def _extract_pdf(self, content: bytes) -> ExtractedText:
        try:
            from pypdf import PdfReader  # type: ignore[import-not-found]
        except ImportError:
            return ExtractedText(
                text="",
                method="pdf:unavailable",
                warnings=[
                    "PDF text extraction requires pypdf (pip install 'procureguard[documents]'). "
                    "The document was stored but not indexed; requirements must be entered "
                    "manually or the file re-supplied in a text format."
                ],
            )
        reader = PdfReader(io.BytesIO(content))
        pages: list[str] = []
        warnings: list[str] = []
        for index, page in enumerate(reader.pages, start=1):
            try:
                pages.append(page.extract_text() or "")
            except Exception as exc:
                warnings.append(f"Page {index} could not be extracted: {exc}")
                pages.append("")
        text = "\n\n".join(f"[page {i}]\n{p}" for i, p in enumerate(pages, start=1) if p.strip())
        if not text.strip():
            warnings.append(
                "No selectable text found. This is likely a scanned drawing; OCR or an "
                "engineering-supplied text specification is required."
            )
        return ExtractedText(
            text=text,
            method="pdf:pypdf",
            page_count=len(reader.pages),
            char_count=len(text),
            warnings=warnings,
        )

    # ------------------------------------------------------------------- DOCX
    def _extract_docx(self, content: bytes) -> ExtractedText:
        """Read OOXML directly; no third-party dependency needed."""
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            try:
                xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
            except KeyError:
                return ExtractedText(
                    text="", method="docx:invalid", warnings=["Not a valid .docx container"]
                )
        # Paragraph and row boundaries become newlines; runs concatenate.
        xml = re.sub(r"</w:p>", "\n", xml)
        xml = re.sub(r"</w:tr>", "\n", xml)
        xml = re.sub(r"</w:tc>", "\t", xml)
        xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
        xml = re.sub(r"<w:br[^>]*/>", "\n", xml)
        text = re.sub(r"<[^>]+>", "", xml)
        text = _unescape_xml(text)
        text = "\n".join(line.rstrip() for line in text.splitlines())
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return ExtractedText(text=text, method="docx:ooxml", char_count=len(text))

    # ------------------------------------------------------------------- XLSX
    def _extract_xlsx(self, content: bytes) -> ExtractedText:
        """Read sheets directly from OOXML, resolving the shared-string table."""
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                shared_xml = archive.read("xl/sharedStrings.xml").decode("utf-8", errors="replace")
                shared = [
                    _unescape_xml(re.sub(r"<[^>]+>", "", block))
                    for block in re.findall(r"<si>(.*?)</si>", shared_xml, re.DOTALL)
                ]

            sheet_names = sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
            blocks: list[str] = []
            tables: list[list[list[str]]] = []
            for sheet_name in sheet_names:
                sheet_xml = archive.read(sheet_name).decode("utf-8", errors="replace")
                rows: list[list[str]] = []
                for row_xml in re.findall(r"<row[^>]*>(.*?)</row>", sheet_xml, re.DOTALL):
                    cells: list[str] = []
                    for cell_xml in re.findall(r"<c\b(.*?)(?:/>|</c>)", row_xml, re.DOTALL):
                        is_shared = 't="s"' in cell_xml
                        is_inline = 't="inlineStr"' in cell_xml
                        value_match = re.search(r"<v>(.*?)</v>", cell_xml, re.DOTALL)
                        if is_inline:
                            inline = re.search(r"<t[^>]*>(.*?)</t>", cell_xml, re.DOTALL)
                            cells.append(_unescape_xml(inline.group(1)) if inline else "")
                            continue
                        if not value_match:
                            cells.append("")
                            continue
                        raw = value_match.group(1)
                        if is_shared:
                            index = int(raw) if raw.isdigit() else -1
                            cells.append(shared[index] if 0 <= index < len(shared) else "")
                        else:
                            cells.append(raw)
                    if any(c.strip() for c in cells):
                        rows.append(cells)
                if rows:
                    tables.append(rows)
                    label = sheet_name.rsplit("/", 1)[-1].replace(".xml", "")
                    blocks.append(
                        f"[sheet {label}]\n"
                        + "\n".join("\t".join(cell for cell in row) for row in rows)
                    )
        text = "\n\n".join(blocks)
        return ExtractedText(
            text=text, method="xlsx:ooxml", char_count=len(text), tables=tables
        )

    # -------------------------------------------------------------- CSV / text
    @staticmethod
    def _extract_csv(content: bytes) -> ExtractedText:
        decoded = content.decode("utf-8-sig", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(decoded[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = [row for row in csv.reader(io.StringIO(decoded), dialect) if any(row)]
        text = "\n".join("\t".join(cell.strip() for cell in row) for row in rows)
        return ExtractedText(
            text=text, method="csv:builtin", char_count=len(text), tables=[rows] if rows else []
        )

    @staticmethod
    def _extract_html(content: bytes) -> ExtractedText:
        from procureguard.infrastructure.email.receiver import strip_html

        text = strip_html(content.decode("utf-8", errors="replace"))
        return ExtractedText(text=text, method="html:builtin", char_count=len(text))

    @staticmethod
    def _extract_plain(content: bytes) -> ExtractedText:
        for encoding in ("utf-8", "utf-16", "latin-1"):
            try:
                text = content.decode(encoding)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            text = content.decode("utf-8", errors="replace")
        return ExtractedText(text=text, method="text:decode", char_count=len(text))


# Clause and heading patterns common in engineering specifications.
_SECTION = re.compile(
    r"^\s*(?:"
    r"(?P<numbered>\d+(?:\.\d+){0,3})[.)]?\s+(?P<numbered_title>[A-Z][^\n]{2,90})"
    r"|(?P<upper>[A-Z][A-Z0-9 ,/&()\-]{5,80})"
    r"|(?:section|clause|appendix|annex)\s+(?P<labelled>[\w.\-]+)[.:\s]*(?P<labelled_title>[^\n]{0,80})"
    r")\s*$",
    re.MULTILINE,
)
_PAGE_MARKER = re.compile(r"^\[page (\d+)\]$", re.MULTILINE)


def chunk_text(
    text: str,
    *,
    max_chars: int = MAX_CHUNK_CHARS,
    overlap: int = CHUNK_OVERLAP_CHARS,
) -> list[Chunk]:
    """Split text into retrievable chunks that respect document structure."""
    if not text or not text.strip():
        return []

    chunks: list[Chunk] = []
    for page_number, page_text in _split_pages(text):
        for section_path, body in _split_sections(page_text):
            for piece in _split_to_size(body, max_chars, overlap):
                cleaned = piece.strip()
                if len(cleaned) < MIN_CHUNK_CHARS and chunks:
                    # Fold a stub into the previous chunk instead of emitting a
                    # fragment that retrieves poorly and reads worse.
                    previous = chunks[-1]
                    merged = f"{previous.content}\n{cleaned}"
                    if len(merged) <= max_chars + overlap:
                        chunks[-1] = Chunk(
                            content=merged,
                            chunk_index=previous.chunk_index,
                            page_number=previous.page_number,
                            section_path=previous.section_path,
                            token_estimate=len(merged) // 4,
                        )
                        continue
                if not cleaned:
                    continue
                chunks.append(
                    Chunk(
                        content=cleaned,
                        chunk_index=len(chunks),
                        page_number=page_number,
                        section_path=section_path,
                        token_estimate=len(cleaned) // 4,
                    )
                )
    return chunks


def _split_pages(text: str) -> list[tuple[int, str]]:
    markers = list(_PAGE_MARKER.finditer(text))
    if not markers:
        return [(0, text)]
    pages: list[tuple[int, str]] = []
    for index, match in enumerate(markers):
        start = match.end()
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        body = text[start:end].strip()
        if body:
            pages.append((int(match.group(1)), body))
    return pages or [(0, text)]


def _split_sections(text: str) -> list[tuple[str, str]]:
    matches = list(_SECTION.finditer(text))
    if not matches:
        return [("", text)]
    sections: list[tuple[str, str]] = []
    preamble = text[: matches[0].start()].strip()
    if preamble:
        sections.append(("", preamble))
    for index, match in enumerate(matches):
        heading = _heading_label(match)
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            sections.append((heading, f"{heading}\n{body}" if heading else body))
    return sections or [("", text)]


def _heading_label(match: re.Match[str]) -> str:
    if match.group("numbered"):
        return f"{match.group('numbered')} {match.group('numbered_title') or ''}".strip()[:200]
    if match.group("labelled"):
        return f"{match.group('labelled')} {match.group('labelled_title') or ''}".strip()[:200]
    return (match.group("upper") or "").strip()[:200]


def _split_to_size(text: str, max_chars: int, overlap: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    # Prefer paragraph boundaries, then sentence boundaries, then a hard cut.
    paragraphs = re.split(r"\n\s*\n", text)
    buffer = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if buffer:
                pieces.append(buffer)
                buffer = ""
            pieces.extend(_split_sentences(paragraph, max_chars, overlap))
            continue
        if len(buffer) + len(paragraph) + 2 <= max_chars:
            buffer = f"{buffer}\n\n{paragraph}" if buffer else paragraph
        else:
            if buffer:
                pieces.append(buffer)
            buffer = paragraph
    if buffer:
        pieces.append(buffer)
    return _apply_overlap(pieces, overlap)


def _split_sentences(text: str, max_chars: int, overlap: int) -> list[str]:
    sentences = re.split(r"(?<=[.;:!?])\s+", text)
    pieces: list[str] = []
    buffer = ""
    for sentence in sentences:
        while len(sentence) > max_chars:
            pieces.append(sentence[:max_chars])
            sentence = sentence[max_chars - overlap :]
        if len(buffer) + len(sentence) + 1 <= max_chars:
            buffer = f"{buffer} {sentence}".strip()
        else:
            if buffer:
                pieces.append(buffer)
            buffer = sentence
    if buffer:
        pieces.append(buffer)
    return pieces


def _apply_overlap(pieces: list[str], overlap: int) -> list[str]:
    if overlap <= 0 or len(pieces) < 2:
        return pieces
    out = [pieces[0]]
    for previous, current in zip(pieces, pieces[1:], strict=False):
        tail = previous[-overlap:]
        # Start the carried context at a word boundary so chunks read cleanly.
        space = tail.find(" ")
        if space > 0:
            tail = tail[space + 1 :]
        out.append(f"{tail}\n{current}" if tail else current)
    return out


def _unescape_xml(text: str) -> str:
    replacements = {
        "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'",
        "&#10;": "\n", "&#9;": "\t",
    }
    for entity, char in replacements.items():
        text = text.replace(entity, char)
    return text
