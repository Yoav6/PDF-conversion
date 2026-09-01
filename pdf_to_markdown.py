#!/usr/bin/env python3
"""
Convert a PDF to JSON (and images) via the Datalab API, then turn that JSON into Markdown.
Or open an existing Datalab JSON file and convert it to Markdown only.

API docs: https://documentation.datalab.to/docs/welcome/api
Reads the API key from datalab_api_key.txt (or DATALAB_API_KEY env var).

Run the GUI:  python3 pdf_to_markdown.py
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple
from tkinter import messagebox, simpledialog, ttk
from types import SimpleNamespace

import requests
from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

# ========================= CONFIGURATION =========================
API_URL = "https://www.datalab.to/api/v1/convert"
MODE = "balanced"  # matches Datalab playground: Fast | Balanced | Accurate
DISABLE_IMAGE_CAPTIONS = True
DISABLE_IMAGE_EXTRACTION = False
PAGINATE = False
SKIP_CACHE = False
POLL_INTERVAL_SEC = 2
MAX_POLLS = 300
IMAGES_DIR_NAME = "images"
# Cover extraction: render one PDF page (1-indexed) separately from Datalab.
# Every Markdown file gets YAML frontmatter; cover-image is filled when
# extract_cover is on. The cover file is saved to images/ only when
# download_images is on. Requires either download_images or embed-as-base64;
# otherwise the UI disables the option.
EXTRACT_COVER = True
COVER_PAGE_DEFAULT = 1
COVER_IMAGE_STEM = "cover"
COVER_IMAGE_DPI = 200
# Default markdown image style when not overridden by the UI.
# False → relative links (images/foo.jpg); True → data:image/...;base64,...
EMBED_IMAGES_AS_BASE64 = True
# Indented-paragraph detection.
# Datalab gives every block a bbox in PDF points. A text block whose left edge is
# indented past the page's dominant body-text margin is rendered with "> " in Markdown.
DETECT_INDENTED_QUOTES = True
# Minimum indent (relative to the body margin) to count as indented, expressed as
# a fraction of the page width and as an absolute floor in points (larger wins).
# 0.0194 catches ~25pt indents on typical 1288pt-wide pages (just under 0.02).
QUOTE_INDENT_MIN_FRACTION = 0.0194
QUOTE_INDENT_MIN_POINTS = 12.0
# Datalab marks centered titles/verse with style="text-align: center;" (or align=)
# in block HTML. Preserve that as a raw HTML wrapper; markdownify would drop it.
PRESERVE_CENTERED_TEXT = True
API_KEY_FILE = Path(__file__).resolve().parent / "datalab_api_key.txt"
API_KEY_PLACEHOLDER = "YOUR_API_KEY_HERE"
API_KEY_FILE_TEMPLATE = (
    "# Paste your Datalab API key on the next line (replace the placeholder).\n"
    "# Get a key at: https://www.datalab.to/app/keys\n"
    f"{API_KEY_PLACEHOLDER}\n"
)
# Empty YAML keys written at the top of every Markdown file.
# cover-image is filled first when a cover was extracted.
YAML_FRONTMATTER_KEYS = (
    "cover-image",
    "isbn",
    "title",
    "subtitle",
    "author",
    "identifier",
    "language",
    "publisher",
    "pubdate",
    "description",
    "series",
    "series_index",
)
# ================================================================

ProgressFn = Callable[[float, str], None]


def report_progress(progress: ProgressFn | None, percent: float, message: str) -> None:
    if progress is not None:
        progress(max(0.0, min(100.0, percent)), message)


def select_file_zenity() -> Path | None:
    """Open a native Linux file picker using zenity and return the selected path."""
    if not shutil.which("zenity"):
        return None

    result = subprocess.run(
        [
            "zenity",
            "--file-selection",
            "--title=Select PDF or JSON File",
            "--file-filter=PDF or JSON | *.pdf *.json",
            "--file-filter=PDF files | *.pdf",
            "--file-filter=JSON files | *.json",
            f"--filename={Path.cwd()}/",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode != 0 or not result.stdout.strip():
        return None

    return Path(result.stdout.strip())


def ensure_api_key_file() -> None:
    """Create datalab_api_key.txt with a placeholder template if it's missing."""
    if not API_KEY_FILE.exists():
        API_KEY_FILE.write_text(API_KEY_FILE_TEMPLATE, encoding="utf-8")
        print(f"📝 Created API key file: {API_KEY_FILE}")


def read_stored_api_key() -> str:
    """Return the saved API key (from file), or '' if none is set. Never raises."""
    if API_KEY_FILE.exists():
        for line in API_KEY_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line in {"YOUR_API_KEY_HERE", "your_api_key_here"}:
                continue
            return line
    return ""


def save_api_key(api_key: str) -> None:
    """Write the given API key to datalab_api_key.txt, preserving the header."""
    api_key = api_key.strip()
    API_KEY_FILE.write_text(
        "# Paste your Datalab API key on the next line (replace the placeholder).\n"
        "# Get a key at: https://www.datalab.to/app/keys\n"
        f"{api_key or API_KEY_PLACEHOLDER}\n",
        encoding="utf-8",
    )


def get_api_key() -> str:
    """Read API key from datalab_api_key.txt, falling back to DATALAB_API_KEY."""
    if API_KEY_FILE.exists():
        for line in API_KEY_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line in {"YOUR_API_KEY_HERE", "your_api_key_here"}:
                break
            return line

    api_key = os.environ.get("DATALAB_API_KEY", "").strip()
    if api_key:
        return api_key

    raise RuntimeError(
        "No Datalab API key found.\n"
        f"1. Open {API_KEY_FILE.name} and paste your key on its own line\n"
        "2. Or: export DATALAB_API_KEY='your_key_here'\n"
        "Create a key at: https://www.datalab.to/app/keys"
    )


_PAGE_RANGE_RE = re.compile(
    r"^\s*\d+\s*(?:-\s*\d+)?(?:\s*,\s*\d+\s*(?:-\s*\d+)?)*\s*$"
)


def parse_page_range(page_range: str | None) -> str | None:
    """
    Normalize a page-range string and convert from 1-indexed user input
    to the 0-indexed form expected by Datalab.

    Empty/None means all pages. Raises ValueError if the format is invalid.
    Page numbers are treated as 1-indexed and converted to 0-indexed for
    Datalab, except 0 which is left as-is.
    """
    if page_range is None:
        return None
    page_range = page_range.strip()
    if not page_range:
        return None
    if not _PAGE_RANGE_RE.match(page_range):
        raise ValueError(
            f"Invalid page range: {page_range!r}. "
            "Use forms like 1-10 or 1-5,10,15-20 (1-indexed)."
        )

    def to_zero_indexed(match: re.Match[str]) -> str:
        n = int(match.group(0))
        return str(n if n == 0 else n - 1)

    return re.sub(r"\d+", to_zero_indexed, re.sub(r"\s+", "", page_range))


def submit_conversion(
    pdf_path: Path,
    api_key: str,
    *,
    page_range: str | None = None,
    progress: ProgressFn | None = None,
) -> str:
    """Upload the PDF with playground-equivalent settings; return request_check_url."""
    headers = {"X-API-Key": api_key}
    data = {
        "output_format": "json",
        "mode": MODE,
        "disable_image_captions": str(DISABLE_IMAGE_CAPTIONS).lower(),
        "disable_image_extraction": str(DISABLE_IMAGE_EXTRACTION).lower(),
        "paginate": str(PAGINATE).lower(),
        "skip_cache": str(SKIP_CACHE).lower(),
    }
    if page_range:
        data["page_range"] = page_range

    report_progress(progress, 15, "Uploading PDF to Datalab…")
    with pdf_path.open("rb") as f:
        response = requests.post(
            API_URL,
            files={"file": (pdf_path.name, f, "application/pdf")},
            data=data,
            headers=headers,
            timeout=300,
        )

    if response.status_code != 200:
        raise RuntimeError(f"Upload failed ({response.status_code}): {response.text}")

    payload = response.json()
    if not payload.get("success", True) and "request_check_url" not in payload:
        raise RuntimeError(f"Upload rejected: {payload}")

    check_url = payload.get("request_check_url")
    if not check_url:
        raise RuntimeError(f"No request_check_url in response: {payload}")

    request_id = payload.get("request_id", "?")
    report_progress(progress, 20, f"Submitted (request {request_id}). Waiting…")
    print(f"⏳ Submitted. Request ID: {request_id}")
    return check_url


def poll_result(
    check_url: str,
    api_key: str,
    *,
    progress: ProgressFn | None = None,
) -> dict:
    """Poll until conversion completes and return the result payload."""
    headers = {"X-API-Key": api_key}

    for attempt in range(1, MAX_POLLS + 1):
        response = requests.get(check_url, headers=headers, timeout=60)
        if response.status_code != 200:
            raise RuntimeError(f"Poll failed ({response.status_code}): {response.text}")

        result = response.json()
        status = result.get("status")

        if status == "complete":
            if not result.get("success", True):
                raise RuntimeError(f"Conversion failed: {result.get('error', result)}")
            report_progress(progress, 75, "Datalab conversion complete")
            return result

        if status == "failed":
            raise RuntimeError(f"Conversion failed: {result.get('error', result)}")

        # Advance slowly from 20% toward 75% while polling.
        poll_pct = 20 + min(55, (attempt / MAX_POLLS) * 55)
        if attempt == 1 or attempt % 5 == 0:
            msg = f"Processing on Datalab… (poll {attempt}/{MAX_POLLS})"
            report_progress(progress, poll_pct, msg)
            print(f"   … still processing (poll {attempt}/{MAX_POLLS})")
        else:
            report_progress(progress, poll_pct, "Processing on Datalab…")
        time.sleep(POLL_INTERVAL_SEC)

    raise TimeoutError("Timed out waiting for conversion.")


def decode_image_bytes(data: str) -> bytes:
    """Decode a base64 image string, with or without a data-URI prefix."""
    if "," in data and data.lstrip().startswith("data:"):
        data = data.split(",", 1)[1]
    return base64.b64decode(data)


def collect_images(result: dict, document_json) -> dict[str, bytes]:
    """Gather images from the top-level response and nested JSON blocks."""
    images: dict[str, bytes] = {}

    top_level = result.get("images") or {}
    for name, b64 in top_level.items():
        try:
            images[name] = decode_image_bytes(b64)
        except Exception as exc:
            print(f"⚠ Skipping top-level image '{name}': {exc}")

    def walk(node):
        if isinstance(node, dict):
            nested = node.get("images") or {}
            for name, b64 in nested.items():
                if name in images:
                    continue
                # Nested keys are often block ids; invent a filename if needed.
                filename = name if Path(name).suffix else f"{name.replace('/', '_')}.jpg"
                try:
                    images[filename] = decode_image_bytes(b64)
                except Exception as exc:
                    print(f"⚠ Skipping nested image '{name}': {exc}")
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(document_json)
    return images


def save_images(images: dict[str, bytes], images_dir: Path) -> None:
    images_dir.mkdir(parents=True, exist_ok=True)
    for name, data in images.items():
        # Keep filenames relative and safe.
        safe_name = Path(name).name
        (images_dir / safe_name).write_bytes(data)


def cover_image_filename() -> str:
    return f"{COVER_IMAGE_STEM}.jpg"


def render_cover_jpeg(
    pdf_path: Path,
    cover_page: int = COVER_PAGE_DEFAULT,
    *,
    dpi: int = COVER_IMAGE_DPI,
) -> bytes:
    """
    Render a single PDF page to JPEG bytes.

    Uses poppler's pdftoppm (falling back to pdftocairo). ``cover_page`` is
    1-indexed (1 = first page). Raises on failure.
    """
    if cover_page < 1:
        raise ValueError(f"Cover page must be >= 1 (got {cover_page}).")

    candidates = [
        ("pdftoppm", ["-jpeg"]),
        ("pdftocairo", ["-jpeg"]),
    ]
    last_error: str | None = None
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        out_prefix = tmp_dir / COVER_IMAGE_STEM
        out_path = tmp_dir / cover_image_filename()
        for tool, fmt_args in candidates:
            if not shutil.which(tool):
                continue
            cmd = [
                tool,
                "-f", str(cover_page),
                "-l", str(cover_page),
                "-singlefile",
                *fmt_args,
                "-r", str(dpi),
                str(pdf_path),
                str(out_prefix),
            ]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=120
                )
            except Exception as exc:
                last_error = f"{tool}: {exc}"
                continue
            if result.returncode == 0 and out_path.is_file():
                return out_path.read_bytes()
            last_error = (
                f"{tool} exited {result.returncode}: "
                f"{(result.stderr or result.stdout).strip()}"
            )

    raise RuntimeError(
        "Could not extract cover image. Install poppler (pdftoppm/pdftocairo)."
        + (f" Last error: {last_error}" if last_error else "")
    )


def cover_image_yaml_value(
    cover_bytes: bytes,
    *,
    embed_base64: bool,
    images_dir: Path,
    md_path: Path,
) -> str:
    """Return the YAML ``cover-image`` value: a data URI or a relative link."""
    filename = cover_image_filename()
    if embed_base64:
        b64 = base64.b64encode(cover_bytes).decode("ascii")
        mime = image_mime_type(Path(filename))
        return f"data:{mime};base64,{b64}"
    rel_images = Path(os.path.relpath(images_dir, md_path.parent)).as_posix()
    return f"{rel_images}/{filename}"





def prepend_yaml_frontmatter(markdown: str, cover_image: str | None = None) -> str:
    """Put the standard YAML frontmatter block at the top of the file."""
    lines = ["---"]
    for key in YAML_FRONTMATTER_KEYS:
        if key == "cover-image" and cover_image:
            lines.append(f"{key}: {json.dumps(cover_image, ensure_ascii=False)}")
        else:
            lines.append(f"{key}:")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + "\n" + markdown.lstrip()


def as_block(node) -> SimpleNamespace | None:
    """Wrap a JSON block dict so attribute access matches Marker's json_to_html."""
    if node is None:
        return None
    if isinstance(node, SimpleNamespace):
        return node
    if not isinstance(node, dict):
        return None

    children = node.get("children")
    wrapped_children = None
    if children:
        wrapped_children = [
            wrapped
            for child in children
            if child is not None
            for wrapped in [as_block(child)]
            if wrapped is not None
        ] or None

    return SimpleNamespace(
        id=node.get("id"),
        html=node.get("html") or "",
        children=wrapped_children,
        block_type=node.get("block_type"),
    )


def json_to_html(block: SimpleNamespace) -> str:
    """Resolve content-ref tags into full HTML (from Marker's marker/output.py)."""
    if not getattr(block, "children", None):
        return block.html or ""

    child_html = [json_to_html(child) for child in block.children]
    child_ids = [child.id for child in block.children]

    soup = BeautifulSoup(block.html or "", "html.parser")
    for ref in soup.find_all("content-ref"):
        src_id = ref.attrs.get("src")
        if src_id in child_ids:
            child_soup = BeautifulSoup(child_html[child_ids.index(src_id)], "html.parser")
            ref.replace_with(child_soup)
    return str(soup)


def document_json_to_html(document_json) -> str:
    """Convert API JSON (Document object or list of pages) into a single HTML string."""
    if isinstance(document_json, list):
        pages = [as_block(page) for page in document_json]
        return "\n".join(json_to_html(page) for page in pages if page)

    if isinstance(document_json, dict):
        # API usually returns a Document-like object with children = pages.
        if document_json.get("children") is not None or document_json.get("html"):
            return json_to_html(as_block(document_json))
        # Or a wrapper like {"pages": [...]} / {"blocks": [...]}
        for key in ("pages", "blocks", "children"):
            if isinstance(document_json.get(key), list):
                return document_json_to_html(document_json[key])

    raise ValueError(f"Unrecognized JSON structure: {type(document_json)}")


def iter_pages(document_json) -> list:
    """Return the list of Page blocks from a Document-like JSON structure."""
    if isinstance(document_json, list):
        return [p for p in document_json if isinstance(p, dict)]

    if isinstance(document_json, dict):
        if document_json.get("block_type") == "Page":
            return [document_json]
        for key in ("children", "pages", "blocks"):
            value = document_json.get(key)
            if isinstance(value, list) and value:
                if all(
                    isinstance(item, dict) and item.get("block_type") == "Page"
                    for item in value
                ):
                    return value
                # Document children might mix types; still treat as pages if any Page exists.
                pages = [
                    item for item in value
                    if isinstance(item, dict) and item.get("block_type") == "Page"
                ]
                if pages:
                    return pages
        # Fallback: treat top-level children as the block stream (single "page").
        if isinstance(document_json.get("children"), list):
            return [document_json]

    raise ValueError(f"Unrecognized JSON structure: {type(document_json)}")


_FOOTNOTE_MARKER_HTML = re.compile(
    r"^\s*<sup>\s*(\d+)\s*</sup>",
    re.IGNORECASE,
)
_FOOTNOTE_STAR_PLAIN = re.compile(r"^\s*(\*{1,2})(?!\*)")
_FOOTNOTE_DOTTED_PLAIN = re.compile(r"^\s*(\d+)\s*[.)]")
_FOOTNOTE_BARE_PLAIN = re.compile(r"^\s*(\d+)\s+\S")
_FOOTNOTE_CITE_PLAIN = re.compile(r"^\s*\[\^(\d+)\]")
_LEADING_NUMBER_STRIP = (
    r"<sup>\s*\d+\s*</sup>|"
    r"\d+\s*[.)]|"
    r"\[\^\d+\]\s*:?|"
    r"\d+(?=\s+\S)"
)
_LEADING_MARKER_STRIP_HTML = re.compile(
    r"^\s*(?:\*{1,2}(?!\*)|" + _LEADING_NUMBER_STRIP + r")\s*",
    re.IGNORECASE,
)
# Markdown pass: never strip '*' — that would eat italic *Ibid.*
_LEADING_MARKER_STRIP_MD = re.compile(
    r"^\s*(?:" + _LEADING_NUMBER_STRIP + r")\s*",
    re.IGNORECASE,
)
_BODY_SUP_MARKER = re.compile(r"<sup>\s*(\d+)\s*</sup>", re.IGNORECASE)
_FN_PLACEHOLDER = re.compile(r"%%FNREF(\d+)%%")
_STAR_PLACEHOLDER = re.compile(r"%%STARREF(\d+)(?:P(\d+))?%%")
_MARK_PLACEHOLDER = re.compile(r"%%(NREF|STARREF)(\d+)(?:P(\d+))?%%")
_DATALAB_PAGE_ID = re.compile(r"/page/(\d+)")
# In-text * / ** footnote markers sit after a word or punctuation (Harrison,* / justified.*).
# Leading * at the start of a paragraph is the note itself, not a body ref.
_BODY_STAR_MARKER = re.compile(
    r"(?<=[\w.,;:!?\"'”’\)\]\}])(\*{1,2})(?!\*)"
)
_BR_TAG = re.compile(r"<br\s*/?>", re.IGNORECASE)
_NOTES_HEADING = re.compile(
    r"^(notes|endnotes|end[\s-]?notes|footnotes)(?:\s+to\b.*)?$",
    re.IGNORECASE,
)
_HEBREW_NOTES_HEADINGS = frozenset({"הערות", "הערות שוליים", "הערות סוף"})
_ENDNOTE_BLOCK_TYPES = frozenset({"Text", "TextInlineMath", "ListGroup"})
_YEARISH_NOTE_NUMBER = range(1000, 2100)
_FOOTNOTE_GAP_LOG_LIMIT = 20
# After a chapter whose last marker is at least this, a body 1 is a new chapter
# (do not reuse that chapter's [^1]). A 1 after only a handful of notes may be
# a repeated marker (1, 2, then 1, 2, 3…).
_CHAPTER_RESTART_AFTER = 8
# Page-bottom notes whose labels stay this small and restart at 1 on more than
# one page are paired only with markers on the same PDF page (Life of HG).
_PAGE_RESTART_MAX_ORIG = 12


class NumberedFootnote(NamedTuple):
    """Original OCR label, definition text, 0-based Datalab page, and source."""

    orig: int | None
    text: str
    page: int | None = None
    # "page" = page-bottom Footnote (or Text that starts with <sup>n</sup>).
    # "endnote" = collected after a Notes heading; pair globally with body refs.
    source: str = "page"


class StarFootnote(NamedTuple):
    text: str
    page: int | None = None


def is_leading_sup_footnote_html(html: str) -> bool:
    """True if this block starts with <sup>n</sup> (a definition, not a body ref)."""
    kind, _number = parse_leading_footnote_marker(html)
    return kind == "sup"


def datalab_page_index(node: dict | None) -> int | None:
    """0-based PDF page index from a Datalab block (`page` or `/page/N/…` id)."""
    if not isinstance(node, dict):
        return None
    page = node.get("page")
    if isinstance(page, int):
        return page
    ident = str(node.get("id") or "")
    match = _DATALAB_PAGE_ID.search(ident)
    return int(match.group(1)) if match else None


def pdf_page_1based(page: int | None) -> str:
    if page is None:
        return "?"
    return str(page + 1)


def format_unreferenced_footnote(text: str, page: int | None) -> str:
    return (
        f"(Unreferenced footnote; Defined on page {pdf_page_1based(page)}) {text}"
    )


def format_undefined_footnote(marker: str, page: int | None) -> str:
    return (
        f"(Undefined footnote; Referenced as {marker} on page {pdf_page_1based(page)})"
    )


def nref_placeholder(orig: int, page: int | None) -> str:
    if page is None:
        return f"%%NREF{orig}%%"
    return f"%%NREF{orig}P{page}%%"


def starref_placeholder(k: int, page: int | None) -> str:
    if page is None:
        return f"%%STARREF{k}%%"
    return f"%%STARREF{k}P{page}%%"


def html_to_plain_text(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)


def _sup_is_leading_marker(soup: BeautifulSoup) -> re.Match | None:
    """Return a digit match if the first <sup> is a leading footnote marker."""
    first_sup = soup.find("sup")
    if first_sup is None:
        return None
    num_match = re.fullmatch(r"\s*(\d+)\s*", first_sup.get_text())
    if not num_match:
        return None
    before = ""
    for el in first_sup.previous_elements:
        if getattr(el, "name", None) in {None, "p", "div", "span"}:
            if getattr(el, "name", None):
                continue
            before = str(el) + before
        else:
            break
    if before.strip():
        return None
    return num_match


def parse_leading_footnote_marker(html: str) -> tuple[str | None, int | None]:
    """
    Classify a leading footnote marker.

    Returns (kind, number). kind is 'sup', 'dotted', 'bare', 'star', or None.
    Stars have no number. 'dotted' covers '1.' / '1)' / '[^1]'.
    'bare' is a digit followed by whitespace then text ('1 ברור').
    """
    if not html or not str(html).strip():
        return None, None

    html_match = _FOOTNOTE_MARKER_HTML.match(html)
    if html_match:
        return "sup", int(html_match.group(1))

    soup = BeautifulSoup(html, "html.parser")
    sup_match = _sup_is_leading_marker(soup)
    if sup_match:
        return "sup", int(sup_match.group(1))

    plain = soup.get_text(" ", strip=False).lstrip()
    star = _FOOTNOTE_STAR_PLAIN.match(plain)
    if star:
        return "star", None
    dotted = _FOOTNOTE_DOTTED_PLAIN.match(plain)
    if dotted:
        return "dotted", int(dotted.group(1))
    cite = _FOOTNOTE_CITE_PLAIN.match(plain)
    if cite:
        return "dotted", int(cite.group(1))
    bare = _FOOTNOTE_BARE_PLAIN.match(plain)
    if bare:
        return "bare", int(bare.group(1))
    return None, None


def _number_looks_like_year(number: int | None) -> bool:
    return number is not None and number in _YEARISH_NOTE_NUMBER


def _is_plausible_new_note_number(number: int | None, previous: int | None) -> bool:
    """False for 4-digit years that are not the next note, or a restart at 1."""
    if number is None:
        return False
    if not _number_looks_like_year(number):
        return True
    if previous is None:
        return True
    return number == previous + 1 or number == 1


def is_new_footnote(
    html: str,
    previous_number: int | None,
    have_footnotes: bool,
) -> bool:
    """True if this block starts a new note rather than a page-split continuation."""
    if not have_footnotes:
        return True
    kind, number = parse_leading_footnote_marker(html)
    if kind is None:
        return False
    if kind in {"sup", "star"}:
        return True
    # Dotted '1983)' / '1983.' is usually a year in a continuation, same as bare
    # '1998 Something', unless it continues the note sequence or restarts at 1.
    if kind in {"dotted", "bare"}:
        return _is_plausible_new_note_number(number, previous_number)
    return False


def strip_leading_footnote_marker(text: str) -> str:
    text = _LEADING_MARKER_STRIP_MD.sub("", text, count=1)
    text = re.sub(r"^\s*\d+\s*[.)]\s*", "", text, count=1)
    return text.strip()


def strip_leading_marker_from_html(html: str) -> str:
    """Remove a leading <sup>n</sup>, n./n), bare n, or * / ** from footnote HTML."""
    kind, _number = parse_leading_footnote_marker(html)
    soup = BeautifulSoup(html or "", "html.parser")
    if kind == "sup":
        first_sup = soup.find("sup")
        if first_sup is not None and _sup_is_leading_marker(soup):
            first_sup.decompose()
            return str(soup)
    for el in soup.find_all(string=True):
        if str(el).strip():
            stripped = _LEADING_MARKER_STRIP_HTML.sub("", str(el), count=1)
            el.replace_with(stripped)
            break
    return str(soup)


def join_footnote_parts(previous: str, continuation: str) -> str:
    """Recombine a footnote split across pages (inspired by fix_markdown.py)."""
    prev = previous.rstrip()
    cont = continuation.lstrip()
    if not cont:
        return prev
    if not prev:
        return cont

    # Hyphenated line break across pages.
    if re.search(r"-\s*$", prev) and re.match(r"[A-Za-z]", cont):
        return re.sub(r"-\s*$", "", prev) + cont

    # Soft paragraph break mid-sentence.
    if re.search(r"[A-Za-z,:;*()\[\]]$", prev) and re.match(r"[A-Za-z]", cont):
        return prev + " " + cont

    return prev + " " + cont


def footnote_html(block: dict) -> str:
    """Resolve a Footnote block to HTML."""
    return json_to_html(as_block(block))


def footnote_html_to_text(html: str) -> str:
    """Convert footnote HTML to a single-line markdown string."""
    md = html_to_markdown(html).strip()
    return re.sub(r"\n+", " ", md).strip()


def _fragment_note_number(html: str) -> int | None:
    _kind, number = parse_leading_footnote_marker(html)
    return number


def _should_split_note_fragment(html: str, last_number: int | None) -> bool:
    """True if this <br>-separated chunk starts another note, not a line wrap."""
    kind, number = parse_leading_footnote_marker(html)
    if kind is None:
        return False
    if kind == "star":
        return True
    if number is None:
        return False
    if last_number is None:
        if _number_looks_like_year(number) and kind != "sup":
            return False
        return kind in {"sup", "dotted"}
    if number == last_number + 1 or number == 1:
        return True
    # '1983)' after note 25 is a citation year, not footnote 1983.
    if _number_looks_like_year(number) and kind != "sup":
        return False
    # Skipped a note in a <sup>/<n.> sequence (227 then 229).
    if kind in {"sup", "dotted"} and number > last_number:
        return True
    return False


def split_footnote_html(html: str) -> list[str]:
    """
    Split a Footnote block that already contains several notes.

    DataLab sometimes emits one <p> with <sup>227</sup>…<br/><sup>228</sup>…,
    or several <p> tags each starting a note. Line-break wraps inside a single
    note are left joined.
    """
    if not html or not str(html).strip():
        return []

    soup = BeautifulSoup(html, "html.parser")
    paragraphs = soup.find_all("p")
    marked_ps = [p for p in paragraphs if parse_leading_footnote_marker(str(p))[0]]
    if len(paragraphs) >= 2 and len(marked_ps) >= 2:
        return [str(p) for p in paragraphs]

    pieces = _BR_TAG.split(html)
    if len(pieces) < 2:
        return [html]

    last_number = _fragment_note_number(pieces[0])
    grouped = [pieces[0]]
    for piece in pieces[1:]:
        if _should_split_note_fragment(piece, last_number):
            grouped.append(piece)
            number = _fragment_note_number(piece)
            if number is not None:
                last_number = number
        else:
            grouped[-1] = grouped[-1] + "<br/>" + piece

    if len(grouped) < 2:
        return [html]
    return grouped


def is_notes_section_header(block: dict) -> bool:
    """True for a chapter 'Notes' / 'Endnotes' / 'הערות' heading."""
    if block.get("block_type") != "SectionHeader":
        return False
    text = re.sub(r"\s+", " ", html_to_plain_text(block.get("html") or "")).strip()
    text = text.rstrip(".:")
    if not text:
        return False
    if text in _HEBREW_NOTES_HEADINGS:
        return True
    return bool(_NOTES_HEADING.match(text))


class FootnoteAccumulator:
    """Collect footnote definitions, merging page-split continuations."""

    def __init__(self) -> None:
        self.footnotes: list[NumberedFootnote] = []
        self.last_number: int | None = None
        self.merged_continuations = 0
        self.split_extra = 0
        self.endnote_count = 0
        self.notes_section_count = 0
        # Asterisk notes are a separate marker system from <sup>n</sup>.
        # Keep them off the numbered list so body refs pair with endnotes.
        self._star_notes: list[StarFootnote] = []
        self._pending_star_continuation = False
        self.star_count = 0
        self.star_ref_next = 1
        self.star_markers_replaced = 0
        self.text_sup_footnote_count = 0

    def add_html(self, html: str, page: int | None = None, source: str = "page") -> None:
        parts = split_footnote_html(html)
        if len(parts) > 1:
            self.split_extra += len(parts) - 1
        for part in parts:
            self._add_one(part, page, source)

    def add_list_html(self, html: str, page: int | None = None, source: str = "page") -> None:
        soup = BeautifulSoup(html or "", "html.parser")
        items = soup.find_all("li")
        if items:
            for li in items:
                inner = "".join(str(child) for child in li.contents).strip()
                self.add_html(inner or str(li), page, source)
        else:
            self.add_html(html, page, source)

    def finalize(self) -> tuple[list[NumberedFootnote], list[StarFootnote]]:
        """Return numbered definitions (label, text, page) and asterisk notes."""
        self.star_count = len(self._star_notes)
        return self.footnotes, list(self._star_notes)

    def _have_any_notes(self) -> bool:
        return bool(self.footnotes) or bool(self._star_notes)

    def _add_one(self, html: str, page: int | None = None, source: str = "page") -> None:
        html = (html or "").strip()
        if not html:
            return
        kind, number = parse_leading_footnote_marker(html)
        if not is_new_footnote(html, self.last_number, self._have_any_notes()):
            cont = footnote_html_to_text(html)
            if self._pending_star_continuation and self._star_notes:
                prev = self._star_notes[-1]
                self._star_notes[-1] = StarFootnote(
                    join_footnote_parts(prev.text, cont), prev.page
                )
                self.merged_continuations += 1
                return
            if not self.footnotes:
                self.footnotes.append(NumberedFootnote(None, cont, page, source))
            else:
                prev = self.footnotes[-1]
                self.footnotes[-1] = NumberedFootnote(
                    prev.orig,
                    join_footnote_parts(prev.text, cont),
                    prev.page,
                    prev.source,
                )
                self.merged_continuations += 1
            return
        stripped_html = strip_leading_marker_from_html(html)
        text = footnote_html_to_text(stripped_html)
        text = strip_leading_footnote_marker(text)
        if not text:
            return
        if kind == "star":
            self._star_notes.append(StarFootnote(text, page))
            self._pending_star_continuation = True
            return
        self._pending_star_continuation = False
        if number is not None:
            self.last_number = number
        self.footnotes.append(NumberedFootnote(number, text, page, source))


# Block types that can be halves of a paragraph split across a page.
_MERGEABLE_TEXT_TYPES = frozenset({"Text", "TextInlineMath"})
_MERGEABLE_LIST_TYPES = frozenset({"ListGroup"})
# Unrelated blocks that may sit between the two halves (footnotes are handled separately).
_SKIPPABLE_BETWEEN_PARAS = frozenset({
    "Picture",
    "Figure",
    "FigureGroup",
    "PictureGroup",
    "Caption",
    "PageHeader",
    "PageFooter",
})
_LIST_INDENT_CLASS = re.compile(r"list-indent-(\d+)", re.IGNORECASE)

# Image block types (leaf images, and the groups that pair an image with a caption).
_IMAGE_LEAF_TYPES = frozenset({"Picture", "Figure", "Diagram"})
_IMAGE_GROUP_TYPES = frozenset({"PictureGroup", "FigureGroup"})
_CAPTION_TYPE = "Caption"

# Block types whose left edge is compared against the page margin to spot quotes.
_QUOTE_CANDIDATE_TYPES = frozenset({"Text", "TextInlineMath"})
# Block types whose HTML may carry Datalab's text-align:center (not tables/figures).
_CENTERED_CANDIDATE_TYPES = frozenset({"Text", "TextInlineMath", "SectionHeader"})
# Sentinel block type for indented paragraphs (kept out of the merge types above
# so cross-page paragraph merging leaves standalone quotes untouched).
_BLOCKQUOTE_TYPE = "BlockQuote"


def block_bbox(node) -> tuple[float, float, float, float] | None:
    """Return (x0, y0, x1, y1) for a Datalab block, from its bbox or polygon."""
    if not isinstance(node, dict):
        return None

    bbox = node.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        try:
            x0, y0, x1, y1 = (float(v) for v in bbox)
            return (x0, y0, x1, y1)
        except (TypeError, ValueError):
            pass

    polygon = node.get("polygon")
    if isinstance(polygon, (list, tuple)) and polygon:
        try:
            xs = [float(point[0]) for point in polygon]
            ys = [float(point[1]) for point in polygon]
            return (min(xs), min(ys), max(xs), max(ys))
        except (TypeError, ValueError, IndexError):
            pass

    return None


def dominant_left_margin(children) -> float | None:
    """
    Estimate the body-text left margin for a page.

    Groups the left edges of candidate text blocks into 3-point buckets and
    returns the most common one (ties broken toward the smallest edge). Regular
    paragraphs share a margin, so indented quotes — a minority — fall to its right.
    """
    buckets: dict[int, int] = {}
    for child in children:
        if not isinstance(child, dict):
            continue
        if child.get("block_type") not in _QUOTE_CANDIDATE_TYPES:
            continue
        bbox = block_bbox(child)
        if bbox is None:
            continue
        key = int(round(bbox[0] / 3.0))
        buckets[key] = buckets.get(key, 0) + 1

    if not buckets:
        return None

    max_count = max(buckets.values())
    best_key = min(key for key, count in buckets.items() if count == max_count)
    return best_key * 3.0


def is_indented_quote(
    child: dict,
    block_type: str,
    margin: float | None,
    threshold: float,
) -> bool:
    """True if a text block is indented past the page body margin by >= threshold."""
    if margin is None or block_type not in _QUOTE_CANDIDATE_TYPES:
        return False
    bbox = block_bbox(child)
    if bbox is None:
        return False
    return (bbox[0] - margin) >= threshold


def page_indent_context(page: dict) -> tuple[float | None, float]:
    """Return (dominant left margin, indent threshold) for a page."""
    children = page.get("children") or []
    margin = dominant_left_margin(children)
    page_bbox = block_bbox(page)
    page_width = (page_bbox[2] - page_bbox[0]) if page_bbox else None
    threshold = (
        max(QUOTE_INDENT_MIN_POINTS, QUOTE_INDENT_MIN_FRACTION * page_width)
        if page_width
        else QUOTE_INDENT_MIN_POINTS
    )
    return margin, threshold


def is_last_quote_candidate(children: list, index: int) -> bool:
    """True if no later sibling is a body-text quote candidate."""
    for sibling in children[index + 1 :]:
        if (
            isinstance(sibling, dict)
            and sibling.get("block_type") in _QUOTE_CANDIDATE_TYPES
        ):
            return False
    return True


def first_quote_candidate(page: dict | None) -> dict | None:
    """First body-text block on a page, skipping headers/images/footnotes."""
    if not page:
        return None
    for child in page.get("children") or []:
        if not isinstance(child, dict):
            continue
        if child.get("block_type") in _QUOTE_CANDIDATE_TYPES:
            return child
    return None


def is_first_line_page_wrap(html: str, next_page: dict | None) -> bool:
    """
    True if an indented last-of-page block is the first line of a normal
    paragraph that continues on the next page.

    In books, only the first line of a body paragraph is indented; later lines
    sit at the body margin. A real indented paragraph keeps that indent on
    every line, so a continuation on the next page would still be indented.
    """
    nxt = first_quote_candidate(next_page)
    if nxt is None:
        return False
    next_type = nxt.get("block_type") or "Text"
    next_margin, next_threshold = page_indent_context(next_page)
    if is_indented_quote(nxt, next_type, next_margin, next_threshold):
        return False
    next_html = json_to_html(as_block(nxt))
    return should_merge_text_blocks(html, next_html)


def wrap_blockquote(html: str) -> str:
    return f"<blockquote>{html}</blockquote>"


_CENTER_STYLE_RE = re.compile(r"text-align\s*:\s*center", re.IGNORECASE)
_CENTER_SENTINEL_START = "<!--md-center-->"
_CENTER_SENTINEL_END = "<!--/md-center-->"


def element_is_centered(tag) -> bool:
    """True if a tag is explicitly center-aligned (style or align=)."""
    if not getattr(tag, "name", None):
        return False
    align = str(tag.get("align") or "").strip().lower()
    if align == "center":
        return True
    style = str(tag.get("style") or "")
    return bool(_CENTER_STYLE_RE.search(style))


def html_is_centered(html: str) -> bool:
    """
    True if this block's HTML is marked centered by Datalab.

    Table cell alignment is ignored — that is column layout, not a centered block.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup.find_all(True):
        if tag.find_parent(["table", "td", "th"]) is not None:
            continue
        if element_is_centered(tag):
            return True
    return False


def wrap_centered_html(html: str) -> str:
    """Mark a block so html_to_markdown can emit a center-aligned HTML wrapper."""
    return f"{_CENTER_SENTINEL_START}{html}{_CENTER_SENTINEL_END}"


def emit_centered_html(inner_html: str) -> str:
    """
    Wrap original HTML in a center-aligned element.

    Inner markup stays HTML (<br>, <i>, headings) so Obsidian / Readest / pandoc
    render it without needing to parse markdown inside the wrapper. Footnote
    placeholders become <sup>n</sup> because [^n] would show as literal text.
    """
    inner = _FN_PLACEHOLDER.sub(r"<sup>\1</sup>", (inner_html or "").strip())
    return f'<div align="center" style="text-align: center;">{inner}</div>'


def unwrap_outer_paragraph(html: str) -> str:
    """If html is a single outer <p>, return its inner HTML; otherwise return as-is."""
    soup = BeautifulSoup(html or "", "html.parser")
    contents = [c for c in soup.contents if str(c).strip() != ""]
    if len(contents) == 1 and getattr(contents[0], "name", None) == "p":
        return contents[0].decode_contents().strip()
    return (html or "").strip()


def html_ends_incomplete(html: str) -> bool:
    """True if this block looks cut off mid-flow (hyphen, soft end, or Marker flag)."""
    if has_continuation_marker(html):
        return True
    if html_ends_with_hyphen(html):
        return True
    return html_ends_soft(html)


def html_ends_with_hyphen(html: str) -> bool:
    plain = html_to_plain_text(html)
    return bool(re.search(r"(?:-|—|¬)\s*$", plain))


def html_ends_soft(html: str) -> bool:
    """Ends with a letter/comma/etc. and not with sentence-final punctuation."""
    plain = html_to_plain_text(html)
    if re.search(r'[.!?]"?\s*$', plain):
        return False
    return bool(re.search(r"[A-Za-z,:;*()\[\]]\s*$", plain))


def has_continuation_marker(html: str) -> bool:
    """Marker sometimes tags the first half with class='has-continuation'."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup.find_all(True):
        classes = tag.get("class") or []
        if isinstance(classes, str):
            classes = classes.split()
        if "has-continuation" in classes:
            return True
    return False


def html_starts_like_continuation(html: str) -> bool:
    """True if the block begins with a letter (optionally after inline tags)."""
    inner = unwrap_outer_paragraph(html)
    return bool(re.match(r"^(?:\s|<[^>]+>)*[A-Za-z]", inner or ""))


def html_starts_with_lowercase(html: str) -> bool:
    """Strong signal that text continues a previous sentence across a page break."""
    plain = html_to_plain_text(html)
    return bool(re.match(r"[a-z]", plain or ""))


def should_merge_text_blocks(first_html: str, second_html: str) -> bool:
    """
    Decide whether two text blocks across a page boundary should be joined.

    - Marker has-continuation or hyphenated end → merge if next starts with a letter
    - Soft mid-sentence end → merge only if next starts with a lowercase letter
      (avoids gluing a period-less paragraph to a new capitalized paragraph)
    """
    if not html_starts_like_continuation(second_html):
        return False
    if has_continuation_marker(first_html) or html_ends_with_hyphen(first_html):
        return True
    if html_ends_soft(first_html):
        return html_starts_with_lowercase(second_html)
    return False


def merge_paragraph_html(first_html: str, second_html: str) -> str:
    """Join two paragraph halves using the fix_markdown.py ruleset."""
    a = unwrap_outer_paragraph(first_html).rstrip()
    b = unwrap_outer_paragraph(second_html).lstrip()
    if not a:
        return f"<p>{b}</p>" if b else ""
    if not b:
        return f"<p>{a}</p>"

    # Hyphenated break: drop the hyphen and concatenate with no space.
    hyphen_match = re.search(
        r"(?:-|—|¬)\s*(<sup>\s*\d+\s*</sup>\s*)?$",
        a,
        re.IGNORECASE,
    )
    if hyphen_match and re.match(r"^(?:\s|<[^>]+>)*[A-Za-z]", b):
        trailing_sup = hyphen_match.group(1) or ""
        a = a[: hyphen_match.start()] + trailing_sup
        return f"<p>{a}{b}</p>"

    # Soft break: insert a single space (footnote marker may sit at the join).
    return f"<p>{a} {b}</p>"


def strip_blockquote(html: str) -> str:
    """If html is a single outer <blockquote>, return its inner HTML."""
    soup = BeautifulSoup(html or "", "html.parser")
    contents = [c for c in soup.contents if str(c).strip() != ""]
    if len(contents) == 1 and getattr(contents[0], "name", None) == "blockquote":
        return contents[0].decode_contents().strip()
    return (html or "").strip()


def merge_blockquote_html(first_html: str, second_html: str) -> str:
    """Join two blockquote halves split across a page, back into one blockquote."""
    inner = merge_paragraph_html(
        strip_blockquote(first_html),
        strip_blockquote(second_html),
    )
    return wrap_blockquote(inner)


def merge_list_group_html(first_html: str, second_html: str) -> str:
    """
    Join two ListGroup blocks split across a page.

    If the last item of the first list looks incomplete, merge it with the first
    item of the second list; otherwise concatenate the item lists.
    """
    first = BeautifulSoup(first_html or "", "html.parser")
    second = BeautifulSoup(second_html or "", "html.parser")
    first_ul = first.find(["ul", "ol"])
    second_ul = second.find(["ul", "ol"])
    if first_ul is None:
        return second_html or first_html or ""
    if second_ul is None:
        return first_html or ""

    # Drop continuation class from the merged list wrapper.
    for tag in first.find_all(True):
        classes = tag.get("class") or []
        if isinstance(classes, str):
            classes = classes.split()
        if "has-continuation" in classes:
            classes = [c for c in classes if c != "has-continuation"]
            if classes:
                tag["class"] = classes
            else:
                tag.attrs.pop("class", None)

    second_items = [
        child for child in list(second_ul.children)
        if getattr(child, "name", None) in {"li", "ul"}
    ]
    if not second_items:
        return str(first)

    first_lis = [child for child in first_ul.children if getattr(child, "name", None) == "li"]
    first_item = second_items[0]
    rest = second_items[1:]

    if first_lis and getattr(first_item, "name", None) == "li":
        last_li = first_lis[-1]
        next_text = first_item.get_text(" ", strip=False).lstrip()
        if html_ends_incomplete(str(last_li)) and re.match(r"[A-Za-z]", next_text or ""):
            # Merge item text using the same hyphen/soft-break rules.
            # Prefer lowercase start for soft (non-hyphen) joins.
            if (
                has_continuation_marker(str(last_li))
                or html_ends_with_hyphen(str(last_li))
                or html_starts_with_lowercase(str(first_item))
            ):
                merged_inner = merge_paragraph_html(
                    f"<p>{last_li.decode_contents()}</p>",
                    f"<p>{first_item.decode_contents()}</p>",
                )
                last_li.clear()
                for node in BeautifulSoup(
                    unwrap_outer_paragraph(merged_inner), "html.parser"
                ).contents:
                    last_li.append(node if not isinstance(node, str) else node)
            else:
                first_ul.append(first_item.extract() if first_item.parent else first_item)
        else:
            first_ul.append(first_item.extract() if first_item.parent else first_item)
    else:
        first_ul.append(first_item.extract() if first_item.parent else first_item)

    for item in rest:
        first_ul.append(item.extract() if item.parent else item)

    return str(first)


def _peel_trailing_incomplete(
    blocks: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """
    If the page ends with an incomplete text/list block (plus optional skippable
    blocks after it), peel those off as a pending continuation.
    """
    if not blocks:
        return blocks, []

    # Walk back through trailing skippable blocks.
    end = len(blocks)
    j = end - 1
    while j >= 0 and blocks[j][0] in _SKIPPABLE_BETWEEN_PARAS:
        j -= 1

    if j < 0:
        return blocks, []

    block_type, html = blocks[j]
    should_peel = False
    if block_type in _MERGEABLE_LIST_TYPES and has_continuation_marker(html):
        should_peel = True
    elif block_type in _MERGEABLE_TEXT_TYPES and html_ends_incomplete(html):
        should_peel = True
    elif block_type == _BLOCKQUOTE_TYPE and html_ends_incomplete(html):
        should_peel = True

    if not should_peel:
        return blocks, []

    kept = blocks[:j]
    pending = blocks[j:end]
    return kept, pending


def merge_cross_page_paragraphs(
    pages_body_blocks: list[list[tuple[str, str]]],
) -> list[tuple[str, str]]:
    """
    Merge Text/TextInlineMath/ListGroup/BlockQuote blocks split across page
    boundaries.

    Intervening images/captions/headers between the halves are kept after the
    merged block. Only merges across pages to avoid joining unrelated same-page
    blocks.
    """
    result: list[tuple[str, str]] = []
    pending: list[tuple[str, str]] = []
    merges = 0
    list_merges = 0
    quote_merges = 0

    for page_blocks in pages_body_blocks:
        blocks = list(page_blocks)

        if pending:
            # Skip leading skippable blocks on the new page (headers, images).
            intervening = list(pending[1:])  # skippables after incomplete block
            first_type, first_html = pending[0]
            i = 0
            while i < len(blocks) and blocks[i][0] in _SKIPPABLE_BETWEEN_PARAS:
                intervening.append(blocks[i])
                i += 1

            merged = False
            if i < len(blocks):
                next_type, next_html = blocks[i]
                if (
                    first_type in _MERGEABLE_LIST_TYPES
                    and next_type in _MERGEABLE_LIST_TYPES
                ):
                    merged_html = merge_list_group_html(first_html, next_html)
                    result.append((first_type, merged_html))
                    result.extend(intervening)
                    list_merges += 1
                    blocks = blocks[i + 1 :]
                    pending = []
                    merged = True
                elif (
                    first_type in _MERGEABLE_TEXT_TYPES
                    and next_type in _MERGEABLE_TEXT_TYPES
                    and should_merge_text_blocks(first_html, next_html)
                ):
                    merged_html = merge_paragraph_html(first_html, next_html)
                    result.append((first_type, merged_html))
                    result.extend(intervening)
                    merges += 1
                    blocks = blocks[i + 1 :]
                    pending = []
                    merged = True
                elif (
                    first_type == _BLOCKQUOTE_TYPE
                    and next_type == _BLOCKQUOTE_TYPE
                    and should_merge_text_blocks(first_html, next_html)
                ):
                    merged_html = merge_blockquote_html(first_html, next_html)
                    result.append((first_type, merged_html))
                    result.extend(intervening)
                    quote_merges += 1
                    blocks = blocks[i + 1 :]
                    pending = []
                    merged = True

            if not merged:
                result.append(pending[0])
                result.extend(pending[1:])
                pending = []

        result.extend(blocks)
        result, pending = _peel_trailing_incomplete(result)

    if pending:
        result.extend(pending)

    if merges:
        print(f"🧩 Merged {merges} page-split paragraph(s)")
    if list_merges:
        print(f"📋 Merged {list_merges} page-split list(s)")
    if quote_merges:
        print(f"❝ Merged {quote_merges} page-split blockquote(s)")
    return result


def caption_inner_html(html: str) -> str:
    """Return a caption block's inner content, unwrapping a single block wrapper."""
    soup = BeautifulSoup(html or "", "html.parser")
    top = [
        node
        for node in soup.contents
        if not (isinstance(node, str) and not node.strip())
    ]
    if len(top) == 1 and getattr(top[0], "name", None) in {"p", "div", "span"}:
        return top[0].decode_contents().strip()
    return soup.decode_contents().strip()


def build_figure_html(image_html: str, caption_html: str | None = None) -> str:
    """
    Wrap an image (and optional caption) in a centered <figure>/<figcaption>.

    Emitted as raw HTML so both the image and caption render centered in any
    Markdown viewer that supports embedded HTML. Works the same whether the
    <img> src is a relative link or an inline base64 data URI.
    """
    parts = [
        '<figure align="center" style="text-align: center;">',
        image_html.strip(),
    ]
    if caption_html and caption_html.strip():
        inner = caption_inner_html(caption_html)
        if inner:
            parts.append(f"<figcaption>{inner}</figcaption>")
    parts.append("</figure>")
    return "".join(parts)


def build_figure_from_group(group: dict) -> str:
    """Build a <figure> from a Figure/PictureGroup block (image child + caption child)."""
    image_parts: list[str] = []
    caption_parts: list[str] = []
    for child in group.get("children") or []:
        if not isinstance(child, dict):
            continue
        child_html = json_to_html(as_block(child))
        if child.get("block_type") == _CAPTION_TYPE:
            caption_parts.append(child_html)
        else:
            image_parts.append(child_html)
    image_html = "".join(image_parts)
    caption_html = "".join(caption_parts) or None
    return build_figure_html(image_html, caption_html)


def extract_body_html_and_footnotes(
    document_json,
) -> tuple[str, list[NumberedFootnote], list[StarFootnote]]:
    """
    Build body HTML from non-footnote blocks, and collect footnote definitions.

    Footnote blocks that do not start with a marker are treated as continuations
    of the previous footnote (page-break splits). After a 'Notes' heading,
    following paragraphs and list items are collected as endnotes and omitted
    from the body. In-text * / ** markers on a page are linked to asterisk
    notes collected on that page; numbered body refs are later paired with
    definitions by original superscript. Text blocks split across pages
    (possibly with images between them) are merged back into one paragraph.
    """
    pages_body_blocks: list[list[tuple[str, str]]] = []
    acc = FootnoteAccumulator()
    quote_count = 0
    center_count = 0
    wrap_skips = 0
    in_notes_section = False
    pages = iter_pages(document_json)

    for page_index, page in enumerate(pages):
        page_blocks: list[tuple[str, str]] = []
        children = page.get("children") or []
        stars_at_start = len(acc._star_notes)
        next_page = pages[page_index + 1] if page_index + 1 < len(pages) else None

        # Body margin + indent threshold for this page (used to spot quotes).
        page_margin, indent_threshold = (
            page_indent_context(page)
            if DETECT_INDENTED_QUOTES
            else (None, QUOTE_INDENT_MIN_POINTS)
        )

        i = 0
        n = len(children)
        while i < n:
            child = children[i]
            if not isinstance(child, dict):
                i += 1
                continue
            block_type = child.get("block_type") or "Text"

            if is_notes_section_header(child):
                in_notes_section = True
                acc.notes_section_count += 1
                i += 1
                continue

            if in_notes_section and block_type == "SectionHeader":
                in_notes_section = False
                # Fall through: this header starts the next chapter/section.

            if block_type == "Footnote":
                acc.add_html(
                    footnote_html(child),
                    page=datalab_page_index(child) or datalab_page_index(page),
                    source="endnote" if in_notes_section else "page",
                )
                i += 1
                continue

            if in_notes_section and block_type in _ENDNOTE_BLOCK_TYPES:
                html = json_to_html(as_block(child))
                before = len(acc.footnotes)
                note_page = datalab_page_index(child) or datalab_page_index(page)
                if block_type == "ListGroup":
                    acc.add_list_html(html, note_page, source="endnote")
                else:
                    acc.add_html(html, note_page, source="endnote")
                acc.endnote_count += max(0, len(acc.footnotes) - before)
                i += 1
                continue

            if (
                not in_notes_section
                and block_type in _ENDNOTE_BLOCK_TYPES
            ):
                preview = json_to_html(as_block(child))
                if is_leading_sup_footnote_html(preview):
                    acc.add_html(
                        preview,
                        page=datalab_page_index(child) or datalab_page_index(page),
                        source="page",
                    )
                    acc.text_sup_footnote_count += 1
                    i += 1
                    continue

            if block_type in _IMAGE_GROUP_TYPES:
                page_blocks.append((block_type, build_figure_from_group(child)))
                i += 1
                continue

            if block_type in _IMAGE_LEAF_TYPES:
                image_html = json_to_html(as_block(child))
                caption_html = None
                # A standalone caption often follows the image as its own sibling.
                nxt = children[i + 1] if i + 1 < n else None
                if isinstance(nxt, dict) and nxt.get("block_type") == _CAPTION_TYPE:
                    caption_html = json_to_html(as_block(nxt))
                    i += 1  # consume the caption block
                page_blocks.append(("Figure", build_figure_html(image_html, caption_html)))
                i += 1
                continue

            html = json_to_html(as_block(child))
            centered = (
                PRESERVE_CENTERED_TEXT
                and block_type in _CENTERED_CANDIDATE_TYPES
                and html.strip()
                and html_is_centered(html)
            )
            if centered:
                html = wrap_centered_html(html)
                center_count += 1
            elif (
                DETECT_INDENTED_QUOTES
                and html.strip()
                and is_indented_quote(child, block_type, page_margin, indent_threshold)
            ):
                if (
                    is_last_quote_candidate(children, i)
                    and is_first_line_page_wrap(html, next_page)
                ):
                    wrap_skips += 1
                else:
                    html = wrap_blockquote(html)
                    block_type = _BLOCKQUOTE_TYPE
                    quote_count += 1
            page_blocks.append((block_type, html))
            i += 1

        n_new_stars = len(acc._star_notes) - stars_at_start
        page_num = datalab_page_index(page)
        if n_new_stars:
            page_blocks, n_replaced = replace_page_star_markers(
                page_blocks, n_new_stars, acc.star_ref_next, page_num
            )
            acc.star_ref_next += n_replaced
            acc.star_markers_replaced += n_replaced
            if n_replaced < n_new_stars:
                print(
                    f"⚠ Linked {n_replaced}/{n_new_stars} in-text * marker(s) "
                    f"on a page with asterisk notes"
                )
        page_blocks = [
            (block_type, replace_body_markers_in_html(html, page_num)[0])
            for block_type, html in page_blocks
        ]
        pages_body_blocks.append(page_blocks)

    if acc.merged_continuations:
        print(f"🔗 Recombined {acc.merged_continuations} page-split footnote continuation(s)")
    if acc.split_extra:
        print(f"✂ Split {acc.split_extra} extra note(s) out of multi-note blocks")
    if acc.text_sup_footnote_count:
        print(
            f"📎 Collected {acc.text_sup_footnote_count} footnote(s) from "
            "Text blocks that start with <sup>n</sup>"
        )
    if acc.endnote_count:
        print(
            f"📑 Collected {acc.endnote_count} endnote(s) from "
            f"{acc.notes_section_count} Notes section(s)"
        )
    if quote_count:
        print(f"❝ Detected {quote_count} indented paragraph(s) → blockquote")
    if center_count:
        print(f"⊙ Centered {center_count} block(s) → HTML")
    if wrap_skips:
        print(
            f"↩ Skipped {wrap_skips} page-ending first line(s) "
            "(paragraph wrap, not indented block)"
        )

    body_blocks = merge_cross_page_paragraphs(pages_body_blocks)
    body_html = "\n".join(html for _, html in body_blocks if html)
    numbered, stars = acc.finalize()
    if stars:
        print(
            f"✱ Linked {acc.star_markers_replaced} in-text */** marker(s) "
            f"→ {len(stars)} asterisk note(s)"
        )
    return body_html, numbered, stars


def replace_star_markers_in_html(
    html: str, limit: int, start_k: int, page: int | None = None
) -> tuple[str, int]:
    """Replace up to `limit` footnote-like * / ** in HTML with %%STARREF k%%."""
    if limit <= 0 or not html or "*" not in html:
        return html, 0
    soup = BeautifulSoup(html, "html.parser")
    replaced = 0
    k = start_k

    def repl(_match: re.Match) -> str:
        nonlocal replaced, k
        if replaced >= limit:
            return _match.group(0)
        placeholder = starref_placeholder(k, page)
        replaced += 1
        k += 1
        return placeholder

    for el in soup.find_all(string=True):
        text = str(el)
        if "*" not in text:
            continue
        new = _BODY_STAR_MARKER.sub(repl, text)
        if new != text:
            el.replace_with(new)
        if replaced >= limit:
            break
    return str(soup), replaced


def replace_page_star_markers(
    page_blocks: list[tuple[str, str]],
    n_markers: int,
    start_k: int,
    page: int | None = None,
) -> tuple[list[tuple[str, str]], int]:
    """Replace the first n_markers footnote-like * / ** in this page's body blocks."""
    if n_markers <= 0:
        return page_blocks, 0
    remaining = n_markers
    k = start_k
    out: list[tuple[str, str]] = []
    replaced_total = 0
    for block_type, html in page_blocks:
        if remaining > 0:
            html, n = replace_star_markers_in_html(html, remaining, k, page)
            k += n
            remaining -= n
            replaced_total += n
        out.append((block_type, html))
    return out, replaced_total


def _footnote_preview(text: str, limit: int = 70) -> str:
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _emit_footnote_gap_logs(unused_logs: list[str], missing_logs: list[str]) -> None:
    for line in unused_logs[:_FOOTNOTE_GAP_LOG_LIMIT]:
        print(line)
    extra_unused = len(unused_logs) - _FOOTNOTE_GAP_LOG_LIMIT
    if extra_unused > 0:
        print(f"⚠ … and {extra_unused} more unused definition(s)")
    for line in missing_logs[:_FOOTNOTE_GAP_LOG_LIMIT]:
        print(line)
    extra_missing = len(missing_logs) - _FOOTNOTE_GAP_LOG_LIMIT
    if extra_missing > 0:
        print(f"⚠ … and {extra_missing} more in-text marker(s) with no definition")
    if unused_logs or missing_logs:
        print(
            f"⚠ Footnote pairing: {len(unused_logs)} unused definition(s), "
            f"{len(missing_logs)} in-text marker(s) with no definition "
            f"(unreferenced keep their text; undefined cite marker and PDF page)"
        )


def _page_bottom_restarts_each_page(numbered: list[NumberedFootnote]) -> bool:
    """True when page-bottom labels restart at 1 on most notes pages (not 1…N)."""
    by_page: dict[int | None, list[int]] = {}
    for note in numbered:
        if note.source == "endnote" or note.orig is None:
            continue
        by_page.setdefault(note.page, []).append(note.orig)
    if len(by_page) < 2:
        return False
    pages_with_1 = sum(1 for origs in by_page.values() if 1 in origs)
    max_orig = max(max(origs) for origs in by_page.values())
    # Restart-per-page: labels stay small and most notes pages have a 1.
    # Continuing 1…N across pages has few 1s (chapter starts) even if max is small
    # in a short excerpt.
    return (
        pages_with_1 >= 2
        and max_orig <= _PAGE_RESTART_MAX_ORIG
        and pages_with_1 * 3 >= len(by_page) * 2
    )


def interleave_footnote_streams(
    html: str,
    numbered: list[NumberedFootnote],
    stars: list[StarFootnote],
) -> tuple[str, list[str]]:
    """
    Pair in-text markers with definitions by original number, in document order.

    Page-bottom notes that restart at 1 each page bind only to markers on that
    PDF page, so a leftover 2 cannot steal later pages' 1s. A marker on a page
    with no notes may still take the next/previous page (wrapped paragraph).
    Page-bottom notes whose numbers continue across pages also try the adjacent
    page and reuse an earlier same-chapter definition (repeated 1, 2, …).
    Chapter endnotes still walk a global stream. %%STARREF k%% takes stars[k-1].
    Unused definitions are appended at the end.
    """
    numbered = [
        item if isinstance(item, NumberedFootnote)
        else NumberedFootnote(
            item[0],
            item[1],
            item[2] if len(item) > 2 else None,
            item[3] if len(item) > 3 else "page",
        )
        for item in numbered
    ]
    stars = [
        item if isinstance(item, StarFootnote)
        else (
            StarFootnote(item, None) if isinstance(item, str)
            else StarFootnote(item[0], item[1] if len(item) > 1 else None)
        )
        for item in stars
    ]

    page_unused: dict[int | None, list[NumberedFootnote]] = {}
    endnotes: list[NumberedFootnote] = []
    for note in numbered:
        if note.source == "endnote":
            endnotes.append(note)
        else:
            page_unused.setdefault(note.page, []).append(note)
    page_bottom_pages = set(page_unused)
    restart_per_page = _page_bottom_restarts_each_page(numbered)

    combined: list[str] = []
    def_i = 0
    used_stars: set[int] = set()
    unused_logs: list[str] = []
    missing_logs: list[str] = []
    skipped_numbered: list[str] = []
    last_n_orig: int | None = None
    matched_in_chapter: dict[int, int] = {}
    matched_on_page: dict[tuple[int | None, int], int] = {}

    def take_def(text: str) -> str:
        combined.append(text)
        return f"%%FNREF{len(combined)}%%"

    def bind_endnote(m: int, text: str) -> str:
        nonlocal last_n_orig
        placeholder = take_def(text)
        last_n_orig = m
        matched_in_chapter[m] = len(combined)
        return placeholder

    def log_unused(orig: int | None, text: str, page: int | None) -> None:
        label = orig if orig is not None else "?"
        unused_logs.append(
            f"⚠ No in-text marker for footnote {label} "
            f"(PDF p. {pdf_page_1based(page)}): {_footnote_preview(text)}"
        )

    def skip_unused_endnote() -> None:
        nonlocal def_i
        note = endnotes[def_i]
        log_unused(note.orig, note.text, note.page)
        skipped_numbered.append(format_unreferenced_footnote(note.text, note.page))
        def_i += 1

    def missing_for(m: int, ref_page: int | None, marker: str | None = None) -> str:
        shown = marker if marker is not None else str(m)
        missing_logs.append(
            f"⚠ No definition for in-text marker {shown} "
            f"(PDF p. {pdf_page_1based(ref_page)})"
        )
        return bind_endnote(m, format_undefined_footnote(shown, ref_page))

    def take_from_page(page: int | None, m: int) -> NumberedFootnote | None:
        unused = page_unused.get(page)
        if not unused:
            return None
        for j, note in enumerate(unused):
            if note.orig == m:
                return unused.pop(j)
        return None

    def leftover_before_chapter_one() -> int | None:
        if last_n_orig is None:
            return None
        n = 0
        j = def_i
        while j < len(endnotes) and n <= 10:
            d_orig = endnotes[j].orig
            if d_orig == 1:
                return n
            if d_orig is None or d_orig > last_n_orig:
                n += 1
                j += 1
                continue
            return None
        return None

    def earliest_remaining_page() -> int | None:
        pages = [page for page, unused in page_unused.items() if unused]
        if not pages:
            return None
        numbered_pages = [page for page in pages if page is not None]
        if numbered_pages:
            return min(numbered_pages)
        return None

    def consume_page_note(note: NumberedFootnote, m: int, bind_page: int | None) -> str:
        placeholder = take_def(note.text)
        matched_on_page[(bind_page, m)] = len(combined)
        if not restart_per_page:
            bind_endnote_state(m)
        return placeholder

    def bind_endnote_state(m: int) -> None:
        nonlocal last_n_orig
        last_n_orig = m
        matched_in_chapter[m] = len(combined)

    def try_adjacent_page(m: int, ref_page: int | None) -> str | None:
        if ref_page is None:
            return None
        for adj in (ref_page + 1, ref_page - 1):
            found = take_from_page(adj, m)
            if found is not None:
                placeholder = consume_page_note(found, m, adj)
                matched_on_page[(ref_page, m)] = len(combined)
                return placeholder
        return None

    def bind_page_bottom_local(m: int, ref_page: int | None) -> str:
        found = take_from_page(ref_page, m)
        if found is not None:
            placeholder = take_def(found.text)
            matched_on_page[(ref_page, m)] = len(combined)
            return placeholder
        reuse_at = matched_on_page.get((ref_page, m))
        if reuse_at is not None:
            return f"%%FNREF{reuse_at}%%"
        missing_logs.append(
            f"⚠ No definition for in-text marker {m} "
            f"(PDF p. {pdf_page_1based(ref_page)})"
        )
        placeholder = take_def(format_undefined_footnote(str(m), ref_page))
        matched_on_page[(ref_page, m)] = len(combined)
        return placeholder

    def bind_continuing_page_notes(m: int, ref_page: int | None) -> str | None:
        """Match continuing page-bottom numbers; None if the caller should try endnotes."""
        if m == 1 and last_n_orig is not None and last_n_orig != 1:
            if last_n_orig >= _CHAPTER_RESTART_AFTER:
                matched_in_chapter.clear()

        found = take_from_page(ref_page, m)
        if found is not None:
            return consume_page_note(found, m, ref_page)

        # Def often sits on the next page only when this page already has notes.
        if ref_page in page_bottom_pages:
            adjacent = try_adjacent_page(m, ref_page)
            if adjacent is not None:
                return adjacent

        reuse_at = matched_on_page.get((ref_page, m))
        if reuse_at is not None:
            return f"%%FNREF{reuse_at}%%"
        rem_page = earliest_remaining_page()
        if m in matched_in_chapter and rem_page is not None and (
            ref_page is None or rem_page > ref_page
        ):
            return f"%%FNREF{matched_in_chapter[m]}%%"
        if endnotes:
            return None
        return missing_for(m, ref_page)

    def bind_endnote_stream(m: int, ref_page: int | None) -> str:
        nonlocal def_i, last_n_orig
        if m == 1 and last_n_orig is not None and last_n_orig != 1:
            d_next = endnotes[def_i].orig if def_i < len(endnotes) else None
            if d_next == 1:
                matched_in_chapter.clear()
            else:
                tail = leftover_before_chapter_one()
                if tail is not None:
                    for _ in range(tail):
                        skip_unused_endnote()
                    matched_in_chapter.clear()
                elif last_n_orig >= _CHAPTER_RESTART_AFTER:
                    matched_in_chapter.clear()
        next_orig = endnotes[def_i].orig if def_i < len(endnotes) else None
        if m in matched_in_chapter and next_orig != m:
            return f"%%FNREF{matched_in_chapter[m]}%%"

        while def_i < len(endnotes):
            note = endnotes[def_i]
            if note.orig == m:
                def_i += 1
                return bind_endnote(m, note.text)
            if note.orig is None:
                skip_unused_endnote()
                continue
            if note.orig == 1 and m != 1:
                return missing_for(m, ref_page)
            if _number_looks_like_year(note.orig) and note.orig != m:
                skip_unused_endnote()
                continue
            if note.orig < m:
                skip_unused_endnote()
                continue
            return missing_for(m, ref_page)
        return missing_for(m, ref_page)

    def repl(match: re.Match) -> str:
        kind, idx_s, page_s = match.group(1), match.group(2), match.group(3)
        idx = int(idx_s)
        ref_page = int(page_s) if page_s else None
        if kind == "STARREF":
            if not (1 <= idx <= len(stars)):
                missing_logs.append(
                    f"⚠ No definition for in-text * marker "
                    f"(PDF p. {pdf_page_1based(ref_page)})"
                )
                return take_def(format_undefined_footnote("*", ref_page))
            used_stars.add(idx)
            return take_def(stars[idx - 1].text)

        m = idx
        if restart_per_page and ref_page in page_bottom_pages:
            return bind_page_bottom_local(m, ref_page)
        if not restart_per_page:
            bound = bind_continuing_page_notes(m, ref_page)
            if bound is not None:
                return bound
            return bind_endnote_stream(m, ref_page)
        adjacent = try_adjacent_page(m, ref_page)
        if adjacent is not None:
            return adjacent
        return bind_endnote_stream(m, ref_page)

    html = _MARK_PLACEHOLDER.sub(repl, html)
    while def_i < len(endnotes):
        skip_unused_endnote()
    for _page, unused in page_unused.items():
        for note in unused:
            log_unused(note.orig, note.text, note.page)
            skipped_numbered.append(format_unreferenced_footnote(note.text, note.page))
    combined.extend(skipped_numbered)
    for i, star in enumerate(stars, 1):
        if i not in used_stars:
            unused_logs.append(
                f"⚠ No in-text marker for asterisk note {i} "
                f"(PDF p. {pdf_page_1based(star.page)}): {_footnote_preview(star.text)}"
            )
            combined.append(format_unreferenced_footnote(star.text, star.page))
    _emit_footnote_gap_logs(unused_logs, missing_logs)
    return html, combined


def replace_body_markers_in_html(
    html: str, page: int | None = None
) -> tuple[str, int]:
    """Replace <sup>n</sup> with %%NREFnPpage%%, keeping the original superscript."""
    counter = 0

    def repl(match: re.Match) -> str:
        nonlocal counter
        counter += 1
        orig = int(match.group(1))
        return nref_placeholder(orig, page)

    return _BODY_SUP_MARKER.sub(repl, html), counter


def replace_body_markers_with_placeholders(html: str) -> tuple[str, int]:
    """Replace any remaining <sup>n</sup> after per-page substitution."""
    return replace_body_markers_in_html(html, page=None)


def finalize_footnote_refs(markdown: str) -> str:
    """Turn %%FNREF n%% placeholders into [^n]."""
    return _FN_PLACEHOLDER.sub(r"[^\1]", markdown)


def append_footnotes_section(markdown: str, footnotes: list[str]) -> str:
    """Append a top-level Footnotes heading and [^n]: definitions."""
    if not footnotes:
        return markdown if markdown.endswith("\n") else markdown + "\n"

    parts = [markdown.rstrip(), "", "# Footnotes", ""]
    for i, text in enumerate(footnotes, 1):
        # Keep definition on one line; collapse leftover whitespace.
        clean = re.sub(r"\s+", " ", text).strip()
        parts.append(f"[^{i}]: {clean}")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


_IMAGE_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}


def image_mime_type(path: Path) -> str:
    return _IMAGE_MIME_BY_EXT.get(path.suffix.lower(), "image/jpeg")


def strip_image_tags(html: str) -> str:
    """Remove all <img> tags so the markdown ends up free of image links.

    Figures are unwrapped: any caption survives as a plain paragraph and empty
    figures are dropped, so no stray <figure> wrappers are left behind.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    for img in soup.find_all("img"):
        img.decompose()
    for figure in soup.find_all("figure"):
        caption = figure.find("figcaption")
        if caption is not None and caption.get_text(strip=True):
            caption.name = "p"
            figure.replace_with(caption)
        else:
            figure.decompose()
    return str(soup)


def rewrite_image_srcs(
    html: str,
    images_dir: Path,
    *,
    embed_base64: bool = False,
    md_path: Path | None = None,
    images: dict[str, bytes] | None = None,
) -> str:
    """
    Point <img src="..."> at local files or inline them as base64 data URIs.

    For base64 embedding, image bytes are taken from the in-memory ``images``
    mapping when provided, falling back to files under ``images_dir``. For
    relative links, images are expected on disk under ``images_dir``.
    """
    images_by_name = (
        {Path(name).name: data for name, data in images.items()} if images else {}
    )

    if embed_base64:
        def repl(match: re.Match) -> str:
            src = match.group(1)
            if src.startswith(("http://", "https://", "data:")):
                return match.group(0)
            filename = Path(src).name
            data_bytes = images_by_name.get(filename)
            if data_bytes is None:
                image_path = images_dir / filename
                if image_path.is_file():
                    data_bytes = image_path.read_bytes()
            if data_bytes is None:
                print(f"⚠ Image not found for base64 embed: {filename}")
                return match.group(0)
            b64 = base64.b64encode(data_bytes).decode("ascii")
            mime = image_mime_type(Path(filename))
            return f'src="data:{mime};base64,{b64}"'

        return re.sub(r'src="([^"]+)"', repl, html)

    if md_path is not None:
        rel_images = Path(os.path.relpath(images_dir, md_path.parent)).as_posix()
    else:
        rel_images = images_dir.name

    def link_repl(match: re.Match) -> str:
        src = match.group(1)
        if src.startswith(("http://", "https://", "data:", "/")):
            return match.group(0)
        filename = Path(src).name
        return f'src="{rel_images}/{filename}"'

    return re.sub(r'src="([^"]+)"', link_repl, html)


def html_emphasis_tags_to_markdown(text: str) -> str:
    """
    Convert any remaining HTML emphasis tags to markdown:
    <i>/<em> → *…*, <b>/<strong> → **…**, nested → ***…***.
    """
    # Nested bold+italic (either order), possibly with whitespace.
    patterns = [
        (
            re.compile(
                r"<(i|em)>\s*<(b|strong)>(.*?)</\2>\s*</\1>",
                re.IGNORECASE | re.DOTALL,
            ),
            r"***\3***",
        ),
        (
            re.compile(
                r"<(b|strong)>\s*<(i|em)>(.*?)</\2>\s*</\1>",
                re.IGNORECASE | re.DOTALL,
            ),
            r"***\3***",
        ),
        (
            re.compile(r"<(b|strong)>(.*?)</\1>", re.IGNORECASE | re.DOTALL),
            r"**\2**",
        ),
        (
            re.compile(r"<(i|em)>(.*?)</\1>", re.IGNORECASE | re.DOTALL),
            r"*\2*",
        ),
    ]

    prev = None
    while prev != text:
        prev = text
        for pattern, repl in patterns:
            text = pattern.sub(repl, text)

    # Collapse accidental adjacent markers from partial nesting: * **x** * → ***x***
    text = re.sub(r"\*(\s*)\*\*(.*?)\*\*(\s*)\*", r"***\2***", text)
    text = re.sub(r"\*\*(\s*)\*(.*?)\*(\s*)\*\*", r"***\2***", text)
    return text


def _clear_list_indent_classes(li_tag) -> None:
    classes = li_tag.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    new_classes = [c for c in classes if not _LIST_INDENT_CLASS.match(str(c))]
    if new_classes:
        li_tag["class"] = new_classes
    elif "class" in li_tag.attrs:
        del li_tag.attrs["class"]


# Source list-marker cleanup (glyphs / a,b,c / i,ii / 1,2,3 inside <li> text).
_LIST_BULLET_GLYPH_RE = re.compile(
    r"^[\s\u00a0]*[•●○◦▪▫■□‣∙\*∗·･・➢➤►▶▸‣]+[\s\u00a0]*"
)
_LIST_DASH_BULLET_RE = re.compile(r"^[\s\u00a0]*[-–—][\s\u00a0]+")
_LIST_NUMBER_MARKER_RE = re.compile(
    r"^[\s\u00a0]*(?:\(?\d+\)|\d{1,3}[.)])[\s\u00a0]*"
)
_LIST_ALPHA_LOWER_MARKER_RE = re.compile(
    r"^[\s\u00a0]*(?:\(([a-z])\)|([a-z])[.)])[\s\u00a0]*"
)
_LIST_ALPHA_UPPER_MARKER_RE = re.compile(
    r"^[\s\u00a0]*(?:\(([A-Z])\)|([A-Z])[.)])[\s\u00a0]*"
)
_LIST_ROMAN_LOWER_MARKER_RE = re.compile(
    r"^[\s\u00a0]*(?:\(([ivxlcdm]+)\)|([ivxlcdm]+)[.)])[\s\u00a0]*"
)
_LIST_ROMAN_UPPER_MARKER_RE = re.compile(
    r"^[\s\u00a0]*(?:\(([IVXLCDM]+)\)|([IVXLCDM]+)[.)])[\s\u00a0]*"
)

_OL_TYPE_TO_KIND = {
    "1": "number",
    "a": "alpha_lower",
    "A": "alpha_upper",
    "i": "roman_lower",
    "I": "roman_upper",
}


def _int_to_roman(n: int) -> str:
    if n <= 0:
        return ""
    parts = [
        (1000, "m"),
        (900, "cm"),
        (500, "d"),
        (400, "cd"),
        (100, "c"),
        (90, "xc"),
        (50, "l"),
        (40, "xl"),
        (10, "x"),
        (9, "ix"),
        (5, "v"),
        (4, "iv"),
        (1, "i"),
    ]
    out = []
    for value, glyph in parts:
        while n >= value:
            out.append(glyph)
            n -= value
    return "".join(out)


def _roman_to_int(text: str) -> int | None:
    raw = (text or "").strip().lower()
    if not raw or any(ch not in "ivxlcdm" for ch in raw):
        return None
    values = {"m": 1000, "d": 500, "c": 100, "l": 50, "x": 10, "v": 5, "i": 1}
    total = 0
    prev = 0
    for ch in reversed(raw):
        value = values[ch]
        if value < prev:
            total -= value
        else:
            total += value
            prev = value
    if total <= 0 or _int_to_roman(total) != raw:
        return None
    return total


def _int_to_alpha(n: int) -> str:
    """1-based index → a, b, … z, aa, ab, …"""
    if n <= 0:
        return ""
    chars: list[str] = []
    while n > 0:
        n, rem = divmod(n - 1, 26)
        chars.append(chr(ord("a") + rem))
    return "".join(reversed(chars))


def _direct_list_items(list_tag) -> list:
    return [
        child
        for child in list_tag.children
        if getattr(child, "name", None) == "li"
    ]


def _li_first_text_node(li_tag):
    """Return the first non-empty NavigableString inside an <li>, if any."""
    for descendant in li_tag.descendants:
        if isinstance(descendant, str) and descendant.strip():
            return descendant
    return None


def _li_leading_text(li_tag) -> str:
    node = _li_first_text_node(li_tag)
    return node if isinstance(node, str) else ""


def _strip_li_leading(li_tag, pattern: re.Pattern) -> re.Match | None:
    node = _li_first_text_node(li_tag)
    if node is None:
        return None
    match = pattern.match(node)
    if not match:
        return None
    node.replace_with(node[match.end() :])
    return match


def _prepend_li_text(li_tag, prefix: str) -> None:
    for child in li_tag.children:
        if getattr(child, "name", None) == "p":
            for descendant in child.descendants:
                if isinstance(descendant, str):
                    descendant.replace_with(prefix + descendant)
                    return
            child.insert(0, prefix)
            return
    node = _li_first_text_node(li_tag)
    if node is not None:
        node.replace_with(prefix + node)
        return
    li_tag.insert(0, prefix)


def _marker_label(match: re.Match | None) -> str | None:
    if match is None:
        return None
    for group in match.groups():
        if group:
            return group
    return None


def _classify_li_marker(text: str) -> tuple[str, str | None]:
    """
    Classify a leading source marker in list-item text.

    Returns (kind, label) where kind is one of:
    bullet, number, alpha_lower, alpha_upper, roman_lower, roman_upper, none.
    """
    if not text or not text.strip():
        return "none", None

    if _LIST_BULLET_GLYPH_RE.match(text) or _LIST_DASH_BULLET_RE.match(text):
        return "bullet", None

    m = _LIST_NUMBER_MARKER_RE.match(text)
    if m:
        digits = re.search(r"\d+", m.group(0))
        return "number", digits.group(0) if digits else None

    # Prefer multi-character roman before single-letter alpha (ii. vs i.).
    m = _LIST_ROMAN_LOWER_MARKER_RE.match(text)
    if m:
        label = _marker_label(m)
        if label and _roman_to_int(label) is not None and len(label) > 1:
            return "roman_lower", label.lower()

    m = _LIST_ROMAN_UPPER_MARKER_RE.match(text)
    if m:
        label = _marker_label(m)
        if label and _roman_to_int(label) is not None and len(label) > 1:
            return "roman_upper", label.upper()

    m = _LIST_ALPHA_LOWER_MARKER_RE.match(text)
    if m:
        return "alpha_lower", _marker_label(m)

    m = _LIST_ALPHA_UPPER_MARKER_RE.match(text)
    if m:
        return "alpha_upper", _marker_label(m)

    m = _LIST_ROMAN_LOWER_MARKER_RE.match(text)
    if m:
        label = _marker_label(m)
        if label and _roman_to_int(label) is not None:
            return "roman_lower", label.lower()

    m = _LIST_ROMAN_UPPER_MARKER_RE.match(text)
    if m:
        label = _marker_label(m)
        if label and _roman_to_int(label) is not None:
            return "roman_upper", label.upper()

    return "none", None


def _looks_like_roman_sequence(labels: list[str]) -> bool:
    if not labels:
        return False
    values: list[int] = []
    for label in labels:
        value = _roman_to_int(label)
        if value is None:
            return False
        values.append(value)
    if any(len(label) > 1 for label in labels):
        return True
    if len(values) >= 2:
        return all(values[i] + 1 == values[i + 1] for i in range(len(values) - 1))
    return False


def _detect_list_kind(list_tag, items: list) -> str:
    type_attr = (list_tag.get("type") or "").strip()
    if type_attr in _OL_TYPE_TO_KIND:
        return _OL_TYPE_TO_KIND[type_attr]

    kinds: list[str] = []
    labels: list[str] = []
    for li in items:
        kind, label = _classify_li_marker(_li_leading_text(li))
        if kind == "none":
            continue
        kinds.append(kind)
        if label:
            labels.append(label)

    if not kinds:
        return "number" if list_tag.name == "ol" else "bullet"

    # Resolve alpha-vs-roman for ambiguous single-letter markers like "i.".
    if (
        all(k in {"alpha_lower", "roman_lower"} for k in kinds)
        and labels
        and _looks_like_roman_sequence([lab.lower() for lab in labels])
    ):
        return "roman_lower"
    if (
        all(k in {"alpha_upper", "roman_upper"} for k in kinds)
        and labels
        and _looks_like_roman_sequence(labels)
    ):
        return "roman_upper"

    # Majority vote among detected markers.
    counts: dict[str, int] = {}
    for kind in kinds:
        counts[kind] = counts.get(kind, 0) + 1
    return max(counts, key=lambda k: (counts[k], k == "number"))


def _format_alpha_roman_marker(index: int, kind: str) -> str:
    """1-based index → 'a. ' / 'i. ' etc."""
    if kind == "alpha_lower":
        return f"{_int_to_alpha(index)}. "
    if kind == "alpha_upper":
        return f"{_int_to_alpha(index).upper()}. "
    if kind == "roman_lower":
        return f"{_int_to_roman(index)}. "
    if kind == "roman_upper":
        return f"{_int_to_roman(index).upper()}. "
    return ""


def _strip_kind_marker(li_tag, kind: str) -> None:
    if kind == "bullet":
        if _strip_li_leading(li_tag, _LIST_BULLET_GLYPH_RE):
            return
        _strip_li_leading(li_tag, _LIST_DASH_BULLET_RE)
        return
    if kind == "number":
        _strip_li_leading(li_tag, _LIST_NUMBER_MARKER_RE)
        return
    if kind == "alpha_lower":
        _strip_li_leading(li_tag, _LIST_ALPHA_LOWER_MARKER_RE)
        return
    if kind == "alpha_upper":
        _strip_li_leading(li_tag, _LIST_ALPHA_UPPER_MARKER_RE)
        return
    if kind == "roman_lower":
        # Also catches single-letter roman that classified as alpha.
        if not _strip_li_leading(li_tag, _LIST_ROMAN_LOWER_MARKER_RE):
            _strip_li_leading(li_tag, _LIST_ALPHA_LOWER_MARKER_RE)
        return
    if kind == "roman_upper":
        if not _strip_li_leading(li_tag, _LIST_ROMAN_UPPER_MARKER_RE):
            _strip_li_leading(li_tag, _LIST_ALPHA_UPPER_MARKER_RE)


def _li_has_kind_marker(li_tag, kind: str) -> bool:
    found, label = _classify_li_marker(_li_leading_text(li_tag))
    if found == kind:
        return True
    if kind == "roman_lower" and found == "alpha_lower":
        return bool(label and _roman_to_int(label) is not None)
    if kind == "roman_upper" and found == "alpha_upper":
        return bool(label and _roman_to_int(label) is not None)
    return False


def cleanup_list_markers_html(soup: BeautifulSoup) -> None:
    """
    Normalize list marker semantics before markdownify:

    - bullet lists → <ul>, strip source glyphs (markdown uses *)
    - numeric lists → <ol>, strip in-text 1./1) (markdown uses 1. 2. 3.)
    - alphabetic / roman lists → <ul>, keep/add a./i. labels so markdown
      becomes "* a. …" / "* i. …"
    """
    lists = list(soup.find_all(["ul", "ol"]))
    lists.sort(key=lambda tag: len(list(tag.parents)), reverse=True)

    for list_tag in lists:
        items = _direct_list_items(list_tag)
        if not items:
            continue
        kind = _detect_list_kind(list_tag, items)

        if kind == "number":
            list_tag.name = "ol"
            if "type" in list_tag.attrs:
                del list_tag.attrs["type"]
            for li in items:
                _strip_kind_marker(li, "number")
            continue

        if kind in {"alpha_lower", "alpha_upper", "roman_lower", "roman_upper"}:
            list_tag.name = "ul"
            if "type" in list_tag.attrs:
                del list_tag.attrs["type"]
            for index, li in enumerate(items, start=1):
                if _li_has_kind_marker(li, kind):
                    continue
                # No usable source label — synthesize one, then done.
                # If a mismatched leftover bullet/number sits at the front, drop it.
                leading_kind, _ = _classify_li_marker(_li_leading_text(li))
                if leading_kind in {"bullet", "number"}:
                    _strip_kind_marker(li, leading_kind)
                _prepend_li_text(li, _format_alpha_roman_marker(index, kind))
            continue

        # bullet (default)
        list_tag.name = "ul"
        if "type" in list_tag.attrs:
            del list_tag.attrs["type"]
        for li in items:
            _strip_kind_marker(li, "bullet")


def normalize_list_html(html: str) -> str:
    """
    Fix Marker list HTML so markdownify can emit proper nested markdown lists.

    Marker often encodes indented items as sibling <ul><li class="list-indent-N">
    inside the parent <ul>. Nest those under the previous <li> instead.
    Also wrap runs of orphan <li> tags in a <ul>, then normalize source markers.
    """
    soup = BeautifulSoup(html or "", "html.parser")

    # Nest Marker indent wrappers under the preceding list item.
    changed = True
    while changed:
        changed = False
        for parent_list in list(soup.find_all(["ul", "ol"])):
            for child in list(parent_list.children):
                if getattr(child, "name", None) != "ul":
                    continue
                prev_li = None
                sibling = child.previous_sibling
                while sibling is not None:
                    if getattr(sibling, "name", None) == "li":
                        prev_li = sibling
                        break
                    if getattr(sibling, "name", None) is not None:
                        break
                    if isinstance(sibling, str) and sibling.strip():
                        break
                    sibling = sibling.previous_sibling
                if prev_li is None:
                    continue
                child.extract()
                for li in child.find_all("li"):
                    _clear_list_indent_classes(li)
                prev_li.append(child)
                changed = True

    # Wrap consecutive orphan <li> elements (not already inside ul/ol).
    candidates = []
    for node in list(soup.descendants):
        if getattr(node, "name", None) == "li":
            parent_name = getattr(node.parent, "name", None)
            if parent_name not in {"ul", "ol"}:
                candidates.append(node)

    i = 0
    while i < len(candidates):
        li = candidates[i]
        if li.parent is None:
            i += 1
            continue
        parent = li.parent
        run = [li]
        j = i + 1
        while j < len(candidates) and candidates[j].parent is parent:
            prev = run[-1]
            nxt = candidates[j]
            # Only group truly consecutive siblings (ignore whitespace text nodes).
            sibling = prev.next_sibling
            while isinstance(sibling, str) and not sibling.strip():
                sibling = sibling.next_sibling
            if sibling is not nxt:
                break
            run.append(nxt)
            j += 1
        if run:
            wrapper = soup.new_tag("ul")
            run[0].insert_before(wrapper)
            for item in run:
                wrapper.append(item.extract())
        i = j if j > i else i + 1

    cleanup_list_markers_html(soup)
    return str(soup)


def normalize_list_markdown(text: str) -> str:
    """Normalize list markers and tighten spacing around markdown list blocks."""
    # Unordered lists always use asterisks (not - or +).
    text = re.sub(r"(?m)^([ \t]*)[-+] ", r"\1* ", text)
    # Remove blank lines between consecutive list items.
    text = re.sub(
        r"(?m)^([ \t]*(?:\* |\d+\. ).+)\n\n+(?=[ \t]*(?:\* |\d+\. ))",
        r"\1\n",
        text,
    )
    return text


_ATX_HEADING_RE = re.compile(r"^(#+)\s+(.+?)\s*$")


def combine_consecutive_headings(text: str) -> str:
    """
    Merge runs of same-level ATX headings that have only blank lines between them.

    Datalab often splits a multi-line title into adjacent SectionHeader blocks
    (e.g. "## Chapter One" then "## The Beginning"); join those into one heading.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        match = _ATX_HEADING_RE.match(lines[i])
        if not match:
            out.append(lines[i])
            i += 1
            continue

        level = match.group(1)
        titles = [match.group(2).strip()]
        i += 1

        while i < n:
            # Peek past blank lines for another heading at the same level.
            k = i
            while k < n and not lines[k].strip():
                k += 1
            if k >= n:
                break
            nxt = _ATX_HEADING_RE.match(lines[k])
            if not nxt or nxt.group(1) != level:
                break
            titles.append(nxt.group(2).strip())
            i = k + 1  # consume intervening blanks + heading

        out.append(f"{level} {' | '.join(titles)}")

    return "\n".join(out)


_FIGURE_BLOCK_RE = re.compile(r"<figure\b.*?</figure>", re.IGNORECASE | re.DOTALL)
_FIGURE_PLACEHOLDER_RE = re.compile(r"%%FIGURE(\d+)%%")
_CENTER_BLOCK_RE = re.compile(
    re.escape(_CENTER_SENTINEL_START) + r"(.*?)" + re.escape(_CENTER_SENTINEL_END),
    re.IGNORECASE | re.DOTALL,
)
_CENTER_PLACEHOLDER_RE = re.compile(r"%%CENTER(\d+)%%")


class _PdfMarkdownConverter(MarkdownConverter):
    """markdownify with project-specific tweaks."""

    def convert_hr(self, el, text, parent_tags):
        # Use *** not --- so pandoc yaml_metadata_block won't misparse <hr> output.
        return "\n\n***\n\n"


def _new_markdown_converter() -> MarkdownConverter:
    return _PdfMarkdownConverter(
        heading_style="ATX",
        bullets="*",
        escape_misc=False,
        escape_underscores=True,
        escape_asterisks=True,
        strong_em_symbol="*",  # *italic*, **bold**, ***both***
    )


def html_to_markdown(html: str) -> str:
    # Stash <figure> blocks so markdownify emits them verbatim (it would
    # otherwise drop the <figure>/<figcaption> wrappers as unknown tags).
    figures: list[str] = []

    def _stash_figure(match: re.Match) -> str:
        figures.append(match.group(0))
        return f"\n\n%%FIGURE{len(figures) - 1}%%\n\n"

    html = _FIGURE_BLOCK_RE.sub(_stash_figure, html)

    # Stash Datalab-centered blocks; markdownify would strip text-align.
    centers: list[str] = []

    def _stash_center(match: re.Match) -> str:
        centers.append(match.group(1))
        return f"\n\n%%CENTER{len(centers) - 1}%%\n\n"

    html = _CENTER_BLOCK_RE.sub(_stash_center, html)

    html = normalize_list_html(html)
    markdown = _new_markdown_converter().convert(html)
    markdown = html_emphasis_tags_to_markdown(markdown)
    markdown = normalize_list_markdown(markdown)
    markdown = combine_consecutive_headings(markdown)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)

    if centers:
        markdown = _CENTER_PLACEHOLDER_RE.sub(
            lambda m: emit_centered_html(centers[int(m.group(1))]),
            markdown,
        )
        markdown = re.sub(r"\n{3,}", "\n\n", markdown)

    if figures:
        markdown = _FIGURE_PLACEHOLDER_RE.sub(
            lambda m: figures[int(m.group(1))], markdown
        )
        # Keep each figure as its own block, separated by blank lines.
        markdown = _FIGURE_BLOCK_RE.sub(lambda m: f"\n\n{m.group(0)}\n\n", markdown)
        markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    return markdown.strip() + "\n"


def load_document_json(json_path: Path):
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON file: {exc}") from exc
    except OSError as exc:
        raise OSError(f"Could not read JSON: {exc}") from exc


def convert_json_to_markdown(
    document_json,
    md_path: Path,
    images_dir: Path,
    *,
    newly_saved_images: bool = False,
    embed_images_as_base64: bool = False,
    download_images: bool = True,
    images: dict[str, bytes] | None = None,
    cover_image: str | None = None,
    progress: ProgressFn | None = None,
) -> bool:
    """Write markdown next to the source. Returns True if image links were rewritten."""
    report_progress(progress, 90, "Converting JSON → Markdown…")
    print("📝 Converting JSON → Markdown...")
    html, numbered, stars = extract_body_html_and_footnotes(document_json)

    have_images_source = newly_saved_images or images_dir.is_dir() or bool(images)

    if embed_images_as_base64 and have_images_source:
        print("🖼️  Embedding images as base64")
        html = rewrite_image_srcs(
            html,
            images_dir,
            embed_base64=True,
            md_path=md_path,
            images=images,
        )
        use_images = True
    elif download_images and have_images_source:
        print("🖼️  Embedding images as links")
        html = rewrite_image_srcs(
            html,
            images_dir,
            embed_base64=False,
            md_path=md_path,
        )
        use_images = True
    else:
        # No image output requested (or nothing to embed): keep markdown clean.
        html = strip_image_tags(html)
        use_images = False

    html, _extra = replace_body_markers_with_placeholders(html)
    marker_count = sum(
        1 for match in _MARK_PLACEHOLDER.finditer(html) if match.group(1) == "NREF"
    )
    star_marker_count = len(_STAR_PLACEHOLDER.findall(html))
    html, footnotes = interleave_footnote_streams(html, numbered, stars)
    markdown = html_to_markdown(html)
    markdown = finalize_footnote_refs(markdown)
    markdown = append_footnotes_section(markdown, footnotes)
    markdown = prepend_yaml_frontmatter(markdown, cover_image)
    if cover_image:
        print("🖼️  Wrote cover-image into YAML frontmatter")
    md_path.write_text(markdown, encoding="utf-8")

    total_markers = marker_count + star_marker_count
    if footnotes or total_markers:
        print(
            f"📎 Footnotes: {len(footnotes)} definition(s), "
            f"{marker_count} numbered marker(s), "
            f"{star_marker_count} */** marker(s); "
            "numbered notes paired by original superscript"
        )

    return use_images


def book_dir_for(source_path: Path) -> Path:
    """
    Folder that should hold this book's files, named after the file stem.

    If the source already lives in that folder, return its parent.
    Otherwise the book folder is a sibling named after the stem.

    Example: /books/MyBook.pdf → /books/MyBook/
             /books/MyBook/MyBook.pdf → /books/MyBook/
    """
    if source_path.parent.name == source_path.stem:
        return source_path.parent
    return source_path.parent / source_path.stem


def prepare_book_dir(source_path: Path) -> tuple[Path, Path]:
    """
    Ensure the source file lives in a folder named after its stem.

    If it already does, return that folder unchanged. Otherwise create the
    folder next to the file and move the source into it.

    Returns (book_dir, source_path_in_book_dir).
    """
    book_dir = book_dir_for(source_path)
    if source_path.parent.resolve() == book_dir.resolve():
        return book_dir, source_path

    book_dir.mkdir(parents=True, exist_ok=True)
    dest = book_dir / source_path.name
    if dest.resolve() != source_path.resolve():
        shutil.move(str(source_path), str(dest))
        print(f"📦 Moved {source_path.name} → {book_dir.name}/")
    return book_dir, dest


def resolve_images_dir(book_dir: Path) -> Path:
    """Prefer book_dir/images; fall back to legacy book_dir_images next to it."""
    modern = book_dir / IMAGES_DIR_NAME
    if modern.is_dir():
        return modern
    legacy = book_dir.parent / f"{book_dir.name}_images"
    if legacy.is_dir():
        return legacy
    return modern


def process_pdf(
    pdf_path: Path,
    *,
    page_range: str | None = None,
    embed_images_as_base64: bool | None = None,
    download_images: bool = True,
    extract_cover: bool = EXTRACT_COVER,
    cover_page: int = COVER_PAGE_DEFAULT,
    progress: ProgressFn | None = None,
) -> Path:
    """
    Convert a PDF via Datalab and write book folder outputs.

    Returns the output folder path. Raises on failure.
    """
    if embed_images_as_base64 is None:
        embed_images_as_base64 = EMBED_IMAGES_AS_BASE64

    report_progress(progress, 5, "Starting PDF conversion…")
    api_key = get_api_key()
    page_range = parse_page_range(page_range)

    print(
        f"🚀 Uploading to Datalab "
        f"(mode={MODE}, format=json, disable_image_captions={DISABLE_IMAGE_CAPTIONS})"
    )

    check_url = submit_conversion(
        pdf_path, api_key, page_range=page_range, progress=progress
    )
    result = poll_result(check_url, api_key, progress=progress)

    document_json = result.get("json")
    if document_json is None:
        raise RuntimeError("Response did not include JSON output.")

    stem = pdf_path.stem
    out_dir, pdf_path = prepare_book_dir(pdf_path)
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    images_dir = out_dir / IMAGES_DIR_NAME

    report_progress(progress, 80, "Saving JSON and images…")
    print(f"📁 Output folder: {out_dir}")

    json_path.write_text(
        json.dumps(document_json, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"💾 Saved JSON: {json_path.relative_to(out_dir)}")

    # Only extract images if we need them: either to save to disk, or to embed.
    extract_images = download_images or embed_images_as_base64
    images = collect_images(result, document_json) if extract_images else {}
    newly_saved = False
    if download_images and images:
        save_images(images, images_dir)
        newly_saved = True
        print(f"🖼️  Saved {len(images)} image(s) → {IMAGES_DIR_NAME}/")
    elif not download_images:
        print("ℹ️  Image download disabled.")
    elif not images:
        print("ℹ️  No images in response.")

    cover_yaml: str | None = None
    do_cover = extract_cover and (download_images or embed_images_as_base64)
    if do_cover:
        report_progress(progress, 85, f"Extracting cover (page {cover_page})…")
        try:
            cover_bytes = render_cover_jpeg(pdf_path, cover_page)
            if download_images:
                images_dir.mkdir(parents=True, exist_ok=True)
                cover_path = images_dir / cover_image_filename()
                cover_path.write_bytes(cover_bytes)
                print(
                    f"🖼️  Saved cover (page {cover_page}) → "
                    f"{IMAGES_DIR_NAME}/{cover_path.name}"
                )
            cover_yaml = cover_image_yaml_value(
                cover_bytes,
                embed_base64=embed_images_as_base64,
                images_dir=images_dir,
                md_path=md_path,
            )
        except Exception as exc:
            print(f"⚠ Cover extraction failed: {exc}")

    used_images = convert_json_to_markdown(
        document_json,
        md_path,
        images_dir,
        newly_saved_images=newly_saved,
        embed_images_as_base64=embed_images_as_base64,
        download_images=download_images,
        images=images,
        cover_image=cover_yaml,
        progress=progress,
    )

    page_count = result.get("page_count")
    quality = result.get("parse_quality_score")
    extras = []
    if page_count is not None:
        extras.append(f"{page_count} page(s)")
    if quality is not None:
        extras.append(f"quality={quality}")

    report_progress(progress, 100, "Done")
    print("\n✅ Done!")
    print(f"   Folder:   {out_dir}")
    print(f"   JSON:     {json_path}")
    print(f"   Markdown: {md_path}")
    if used_images:
        print(f"   Images:   {images_dir}/")
    if extras:
        print(f"   Stats:    {', '.join(extras)}")
    return out_dir


def process_json(
    json_path: Path,
    *,
    embed_images_as_base64: bool | None = None,
    download_images: bool = True,
    progress: ProgressFn | None = None,
) -> Path:
    """
    Convert an existing Datalab JSON file to markdown.

    Returns the output folder path. Raises on failure.
    """
    if embed_images_as_base64 is None:
        embed_images_as_base64 = EMBED_IMAGES_AS_BASE64

    report_progress(progress, 10, "Loading JSON…")
    print("⏭  Skipping Datalab API (JSON input).")
    document_json = load_document_json(json_path)

    stem = json_path.stem
    out_dir, json_path = prepare_book_dir(json_path)

    md_path = out_dir / f"{stem}.md"
    images_dir = resolve_images_dir(out_dir)

    print(f"📁 Output folder: {out_dir}")

    report_progress(progress, 40, "Extracting images…")
    # Only extract images if we need them: either to save to disk, or to embed.
    extract_images = download_images or embed_images_as_base64
    images = collect_images({}, document_json) if extract_images else {}
    newly_saved = False
    if not download_images:
        print("ℹ️  Image download disabled.")
    elif images:
        images_dir = out_dir / IMAGES_DIR_NAME
        save_images(images, images_dir)
        newly_saved = True
        print(f"🖼️  Saved {len(images)} embedded image(s) → {IMAGES_DIR_NAME}/")
    elif images_dir.is_dir():
        print(f"🖼️  Using existing images folder: {images_dir.name}/")
    else:
        print("ℹ️  No images folder found in the book directory.")

    report_progress(progress, 70, "Converting JSON → Markdown…")
    used_images = convert_json_to_markdown(
        document_json,
        md_path,
        images_dir,
        newly_saved_images=newly_saved,
        embed_images_as_base64=embed_images_as_base64,
        download_images=download_images,
        images=images,
        progress=progress,
    )

    report_progress(progress, 100, "Done")
    print("\n✅ Done!")
    print(f"   Folder:   {out_dir}")
    print(f"   JSON:     {json_path}")
    print(f"   Markdown: {md_path}")
    if used_images:
        print(f"   Images:   {images_dir}/")
    return out_dir


def run_conversion(
    input_path: Path,
    *,
    page_range: str | None = None,
    embed_images_as_base64: bool | None = None,
    download_images: bool = True,
    extract_cover: bool = EXTRACT_COVER,
    cover_page: int = COVER_PAGE_DEFAULT,
    progress: ProgressFn | None = None,
) -> Path:
    """Dispatch PDF or JSON conversion. Returns the output folder path."""
    suffix = input_path.suffix.lower()
    if suffix == ".pdf":
        return process_pdf(
            input_path,
            page_range=page_range,
            embed_images_as_base64=embed_images_as_base64,
            download_images=download_images,
            extract_cover=extract_cover,
            cover_page=cover_page,
            progress=progress,
        )
    if suffix == ".json":
        return process_json(
            input_path,
            embed_images_as_base64=embed_images_as_base64,
            download_images=download_images,
            progress=progress,
        )
    raise ValueError("Please select a PDF (*.pdf) or JSON (*.json) file.")


class ConversionApp(tk.Tk):
    """Simple tkinter UI for PDF/JSON conversion with progress."""

    def __init__(self) -> None:
        super().__init__()
        self.title("PDF → Markdown")
        self.minsize(520, 220)
        self.resizable(True, False)

        self._running = False
        self._file_var = tk.StringVar()
        self._page_range_var = tk.StringVar()
        self._download_images_var = tk.BooleanVar(value=True)
        self._base64_var = tk.BooleanVar(value=EMBED_IMAGES_AS_BASE64)
        self._extract_cover_var = tk.BooleanVar(value=EXTRACT_COVER)
        self._cover_page_var = tk.StringVar(value=str(COVER_PAGE_DEFAULT))
        self._status_var = tk.StringVar()
        self._progress_var = tk.DoubleVar(value=0.0)

        self._build()
        self._sync_page_range_state()
        self._sync_image_options()
        self._sync_cover_state()

    def _build(self) -> None:
        pad = {"padx": 12, "pady": 6}
        root = ttk.Frame(self, padding=12)
        root.grid(row=0, column=0, sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        root.columnconfigure(1, weight=1)

        ttk.Label(root, text="File").grid(row=0, column=0, sticky="w", **pad)
        file_row = ttk.Frame(root)
        file_row.grid(row=0, column=1, sticky="ew", **pad)
        file_row.columnconfigure(0, weight=1)
        ttk.Entry(file_row, textvariable=self._file_var).grid(
            row=0, column=0, sticky="ew", padx=(0, 8)
        )
        ttk.Button(file_row, text="Browse…", command=self._browse).grid(row=0, column=1)

        ttk.Label(root, text="Page range").grid(row=1, column=0, sticky="w", **pad)
        page_row = ttk.Frame(root)
        page_row.grid(row=1, column=1, sticky="ew", **pad)
        page_row.columnconfigure(0, weight=1)
        self._page_entry = ttk.Entry(page_row, textvariable=self._page_range_var)
        self._page_entry.grid(row=0, column=0, sticky="ew")
        ttk.Label(
            page_row,
            text="1-indexed, e.g. 1-10 — leave blank for all (PDF only)",
            foreground="#555",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        ttk.Checkbutton(
            root,
            text="Download images (save to images/ folder)",
            variable=self._download_images_var,
            command=self._sync_image_options,
        ).grid(row=2, column=1, sticky="w", **pad)

        self._base64_check = ttk.Checkbutton(
            root,
            text="Embed images as base64 in markdown",
            variable=self._base64_var,
            command=self._sync_image_options,
        )
        self._base64_check.grid(row=3, column=1, sticky="w", **pad)

        cover_row = ttk.Frame(root)
        cover_row.grid(row=4, column=1, sticky="w", **pad)
        self._cover_check = ttk.Checkbutton(
            cover_row,
            text="Extract cover — page",
            variable=self._extract_cover_var,
            command=self._sync_cover_state,
        )
        self._cover_check.grid(row=0, column=0, sticky="w")
        self._cover_page_entry = ttk.Spinbox(
            cover_row,
            from_=1,
            to=99999,
            width=6,
            textvariable=self._cover_page_var,
        )
        self._cover_page_entry.grid(row=0, column=1, sticky="w", padx=(6, 0))
        ttk.Label(
            cover_row,
            text="(1 = first page; PDF only; needs download or base64)",
            foreground="#555",
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))

        action_row = ttk.Frame(root)
        action_row.grid(row=5, column=0, columnspan=2, sticky="ew", **pad)
        action_row.columnconfigure(0, weight=1)
        ttk.Button(
            action_row, text="API Key…", command=self._edit_api_key
        ).grid(row=0, column=0, sticky="w")
        self._convert_btn = ttk.Button(action_row, text="Convert", command=self._start)
        self._convert_btn.grid(row=0, column=1, sticky="e")

        self._progress = ttk.Progressbar(
            root,
            maximum=100,
            variable=self._progress_var,
            mode="determinate",
        )
        self._progress.grid(
            row=6, column=0, columnspan=2, sticky="ew", padx=12, pady=(12, 4)
        )
        self._progress.grid_remove()

        ttk.Label(root, textvariable=self._status_var, wraplength=480).grid(
            row=7, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 8)
        )

        self._file_var.trace_add("write", lambda *_: self._sync_page_range_state())

    def _edit_api_key(self) -> None:
        """Prompt for a Datalab API key and save it to the key file."""
        current = read_stored_api_key()
        new_key = simpledialog.askstring(
            "Datalab API Key",
            "Enter your Datalab API key\n(get one at https://www.datalab.to/app/keys):",
            initialvalue=current,
            parent=self,
        )
        if new_key is None:
            return
        new_key = new_key.strip()
        if not new_key:
            messagebox.showwarning("Empty key", "No API key was entered.")
            return
        try:
            save_api_key(new_key)
        except OSError as exc:
            messagebox.showerror("Could not save key", str(exc))
            return
        messagebox.showinfo("API key saved", f"Saved to:\n{API_KEY_FILE}")

    def _browse(self) -> None:
        """Open a native zenity file picker."""
        try:
            path = select_file_zenity()
        except Exception as exc:
            messagebox.showerror("File picker error", str(exc))
            return
        if path is not None:
            self._file_var.set(str(path))
            if not self._running:
                self._reset_progress_ui()

    def _sync_page_range_state(self) -> None:
        path = self._file_var.get().strip()
        is_pdf = path.lower().endswith(".pdf")
        state = "normal" if is_pdf else "disabled"
        self._page_entry.configure(state=state)
        if not is_pdf:
            self._page_range_var.set("")
        self._sync_cover_state()

    def _cover_available(self) -> bool:
        path = self._file_var.get().strip()
        is_pdf = path.lower().endswith(".pdf") or path == ""
        has_image_output = bool(
            self._download_images_var.get() or self._base64_var.get()
        )
        return is_pdf and has_image_output

    def _sync_cover_state(self) -> None:
        # Cover needs a PDF plus either a saved file or a base64 embed target.
        available = self._cover_available()
        self._cover_check.configure(state="normal" if available else "disabled")
        entry_state = (
            "normal"
            if available and self._extract_cover_var.get()
            else "disabled"
        )
        self._cover_page_entry.configure(state=entry_state)

    def _sync_image_options(self) -> None:
        # base64 embedding works whether or not images are downloaded to disk.
        self._base64_check.configure(state="normal")
        self._sync_cover_state()

    def _reset_progress_ui(self) -> None:
        """Clear leftover conversion progress when a new file is chosen."""
        self._progress_var.set(0)
        self._progress.grid_remove()
        self._status_var.set("")

    def _set_progress(self, percent: float, message: str) -> None:
        self._progress.grid()
        self._progress_var.set(percent)
        self._status_var.set(message)

    def _ui_progress(self, percent: float, message: str) -> None:
        self.after(0, lambda: self._set_progress(percent, message))

    def _start(self) -> None:
        if self._running:
            return

        raw = self._file_var.get().strip()
        if not raw:
            messagebox.showwarning("Missing file", "Please choose a PDF or JSON file.")
            return

        path = Path(raw).expanduser()
        if not path.exists():
            messagebox.showerror("File not found", f"File not found:\n{path}")
            return

        is_pdf = path.suffix.lower() == ".pdf"
        page_range = self._page_range_var.get()
        if is_pdf:
            try:
                parse_page_range(page_range)
            except ValueError as exc:
                messagebox.showerror("Invalid page range", str(exc))
                return
        else:
            page_range = None

        embed_base64 = bool(self._base64_var.get())
        download_images = bool(self._download_images_var.get())
        extract_cover = (
            is_pdf
            and bool(self._extract_cover_var.get())
            and (embed_base64 or download_images)
        )
        cover_page = COVER_PAGE_DEFAULT
        if extract_cover:
            try:
                cover_page = int(self._cover_page_var.get().strip())
            except ValueError:
                cover_page = 0
            if cover_page < 1:
                messagebox.showerror(
                    "Invalid cover page",
                    "Cover page must be a whole number >= 1 (1 = first page).",
                )
                return

        self._running = True
        self._convert_btn.configure(state="disabled")
        self._set_progress(0, "Starting…")
        thread = threading.Thread(
            target=self._run_worker,
            args=(
                path,
                page_range,
                embed_base64,
                download_images,
                extract_cover,
                cover_page,
            ),
            daemon=True,
        )
        thread.start()

    def _run_worker(
        self,
        path: Path,
        page_range: str | None,
        embed_images_as_base64: bool,
        download_images: bool,
        extract_cover: bool,
        cover_page: int,
    ) -> None:
        try:
            out_dir = run_conversion(
                path,
                page_range=page_range,
                embed_images_as_base64=embed_images_as_base64,
                download_images=download_images,
                extract_cover=extract_cover,
                cover_page=cover_page,
                progress=self._ui_progress,
            )
        except Exception as exc:
            self.after(0, lambda e=exc, p=path: self._on_error(e, p))
            return
        self.after(0, lambda d=out_dir, p=path: self._on_success(d, p))

    def _point_file_at_book_dir(self, original: Path, out_dir: Path | None = None) -> None:
        """Keep the file field in sync if the source was moved into a book folder."""
        candidates: list[Path] = []
        if out_dir is not None:
            candidates.append(out_dir / original.name)
        candidates.append(original.parent / original.stem / original.name)
        for cand in candidates:
            if cand.is_file():
                self._file_var.set(str(cand))
                return

    def _on_success(self, out_dir: Path, original: Path) -> None:
        self._running = False
        self._convert_btn.configure(state="normal")
        self._point_file_at_book_dir(original, out_dir)
        self._set_progress(100, f"Done — saved to {out_dir}")
        messagebox.showinfo("Conversion complete", f"Output folder:\n{out_dir}")

    def _on_error(self, exc: BaseException, original: Path) -> None:
        self._running = False
        self._convert_btn.configure(state="normal")
        if not original.exists():
            self._point_file_at_book_dir(original)
        self._status_var.set(f"Error: {exc}")
        messagebox.showerror("Conversion failed", str(exc))


def main() -> None:
    ensure_api_key_file()
    app = ConversionApp()
    app.mainloop()


if __name__ == "__main__":
    main()
