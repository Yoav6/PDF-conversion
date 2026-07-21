#!/usr/bin/env python3
"""
Convert a PDF to JSON (and images) via the Datalab API, then turn that JSON into Markdown.
Or open an existing Datalab JSON file and convert it to Markdown only.

API docs: https://documentation.datalab.to/docs/welcome/api
Reads the API key from datalab_api_key.txt (or DATALAB_API_KEY env var).

Run the GUI (default):  python3 pdf_to_markdown.py
Run the CLI:            python3 pdf_to_markdown.py --cli
CLI base64 images:      python3 pdf_to_markdown.py --cli --base64-images
CLI without images:     python3 pdf_to_markdown.py --cli --no-images
CLI cover page:         python3 pdf_to_markdown.py --cli --cover-page 2
CLI without cover:      python3 pdf_to_markdown.py --cli --no-cover
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
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
# Cover extraction: render one PDF page (1-indexed) to an image, saved into the
# images folder separately from the Datalab pipeline.
EXTRACT_COVER = True
COVER_PAGE_DEFAULT = 1
COVER_IMAGE_STEM = "cover"
COVER_IMAGE_DPI = 200
# Default markdown image style when not overridden by UI/CLI.
# False → relative links (images/foo.jpg); True → data:image/...;base64,...
EMBED_IMAGES_AS_BASE64 = True
# Indented-paragraph (blockquote) detection.
# Datalab gives every block a bbox in PDF points; a text block whose left edge is
# indented past the page's dominant body-text margin is treated as a quote and
# rendered with a leading "> " in the Markdown.
DETECT_INDENTED_QUOTES = True
# Minimum indent (relative to the body margin) to count as a quote, expressed as
# a fraction of the page width and as an absolute floor in points (larger wins).
QUOTE_INDENT_MIN_FRACTION = 0.02
QUOTE_INDENT_MIN_POINTS = 12.0
API_KEY_FILE = Path(__file__).resolve().parent / "datalab_api_key.txt"
API_KEY_PLACEHOLDER = "YOUR_API_KEY_HERE"
API_KEY_FILE_TEMPLATE = (
    "# Paste your Datalab API key on the next line (replace the placeholder).\n"
    "# Get a key at: https://www.datalab.to/app/keys\n"
    f"{API_KEY_PLACEHOLDER}\n"
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
    Normalize a page-range string. Empty/None means all pages.

    Raises ValueError if the format is invalid.
    """
    if page_range is None:
        return None
    page_range = page_range.strip()
    if not page_range:
        return None
    if not _PAGE_RANGE_RE.match(page_range):
        raise ValueError(
            f"Invalid page range: {page_range!r}. "
            "Use forms like 0-10 or 0-5,10,15-20 (0-indexed)."
        )
    return re.sub(r"\s+", "", page_range)


def prompt_page_range() -> str | None:
    """
    Ask which pages to convert (0-indexed, e.g. 0-10 or 0-5,10,15-20).

    Empty input means the whole document. Returns None for all pages.
    """
    print("📄 Page range (0-indexed, e.g. 0-10 or 0-5,10,20-25).")
    print("   Leave blank to convert all pages.")

    page_range = None
    if shutil.which("zenity"):
        try:
            result = subprocess.run(
                [
                    "zenity",
                    "--entry",
                    "--title=Page Range",
                    "--text=Page range (0-indexed, e.g. 0-10). Leave blank for all pages:",
                    "--entry-text=",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                page_range = result.stdout.strip()
            else:
                page_range = None
        except Exception:
            page_range = None

    if page_range is None:
        page_range = input("Page range: ").strip()

    try:
        normalized = parse_page_range(page_range)
    except ValueError as exc:
        print(f"❌ {exc}")
        sys.exit(1)

    if normalized is None:
        print("   → all pages")
    else:
        print(f"   → pages {normalized}")
    return normalized


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


def extract_cover_image(
    pdf_path: Path,
    images_dir: Path,
    cover_page: int = COVER_PAGE_DEFAULT,
    *,
    dpi: int = COVER_IMAGE_DPI,
) -> Path:
    """
    Render a single PDF page to an image and save it into ``images_dir``.

    Uses poppler's pdftoppm (falling back to pdftocairo). ``cover_page`` is
    1-indexed (1 = first page). Returns the saved image path. Raises on failure.
    """
    if cover_page < 1:
        raise ValueError(f"Cover page must be >= 1 (got {cover_page}).")

    images_dir.mkdir(parents=True, exist_ok=True)
    out_prefix = images_dir / COVER_IMAGE_STEM
    out_path = images_dir / f"{COVER_IMAGE_STEM}.jpg"

    # Both tools write <prefix>.jpg when given -singlefile.
    candidates = [
        ("pdftoppm", ["-jpeg"]),
        ("pdftocairo", ["-jpeg"]),
    ]
    last_error: str | None = None
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
            return out_path
        last_error = (
            f"{tool} exited {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()}"
        )

    raise RuntimeError(
        "Could not extract cover image. Install poppler (pdftoppm/pdftocairo)."
        + (f" Last error: {last_error}" if last_error else "")
    )


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
    r"^\s*<sup>\s*\d+\s*</sup>",
    re.IGNORECASE,
)
_FOOTNOTE_MARKER_PLAIN = re.compile(
    r"^\s*(?:\d+\s*[.)]|\[\^\d+\])",
)
_LEADING_MARKER_STRIP = re.compile(
    r"^\s*(?:"
    r"<sup>\s*\d+\s*</sup>|"
    r"\d+\s*[.)]|"
    r"\[\^\d+\]\s*:?"
    r")\s*",
    re.IGNORECASE,
)
_BODY_SUP_MARKER = re.compile(r"<sup>\s*(\d+)\s*</sup>", re.IGNORECASE)
_FN_PLACEHOLDER = re.compile(r"%%FNREF(\d+)%%")


def html_to_plain_text(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)


def starts_with_footnote_marker(text: str) -> bool:
    """True if this footnote block begins a new note (has a leading marker)."""
    if not text or not text.strip():
        return False
    if _FOOTNOTE_MARKER_HTML.match(text):
        return True
    # Marker often wraps content: <p><sup>1</sup> …</p>
    soup = BeautifulSoup(text, "html.parser")
    first_sup = soup.find("sup")
    if first_sup is not None:
        # Treat as a leading marker if nothing but whitespace precedes it.
        before = ""
        for el in first_sup.previous_elements:
            if getattr(el, "name", None) in {None, "p", "div", "span"}:
                chunk = str(el) if isinstance(el, str) else ""
                if getattr(el, "name", None):
                    continue
                before = chunk + before
            else:
                break
        if before.strip() == "" and re.fullmatch(r"\s*\d+\s*", first_sup.get_text()):
            return True
    plain = soup.get_text(" ", strip=False)
    return bool(_FOOTNOTE_MARKER_PLAIN.match(plain))


def strip_leading_footnote_marker(text: str) -> str:
    text = _LEADING_MARKER_STRIP.sub("", text, count=1)
    text = re.sub(r"^\s*\d+\s*[.)]\s*", "", text, count=1)
    return text.strip()


def strip_leading_marker_from_html(html: str) -> str:
    """Remove a leading <sup>n</sup> (or plain n./n)) from footnote HTML."""
    soup = BeautifulSoup(html or "", "html.parser")
    first_sup = soup.find("sup")
    if first_sup is not None and re.fullmatch(r"\s*\d+\s*", first_sup.get_text()):
        before = ""
        for el in first_sup.previous_elements:
            if isinstance(el, str):
                before = el + before
            elif getattr(el, "name", None) in {"p", "div", "span"}:
                continue
            else:
                before = "x"
                break
        if before.strip() == "":
            first_sup.decompose()
            return str(soup)
    return _LEADING_MARKER_STRIP.sub("", html or "", count=1)


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
    """True if a text block is indented past the body margin by >= threshold."""
    if margin is None or block_type not in _QUOTE_CANDIDATE_TYPES:
        return False
    bbox = block_bbox(child)
    if bbox is None:
        return False
    return (bbox[0] - margin) >= threshold


def wrap_blockquote(html: str) -> str:
    return f"<blockquote>{html}</blockquote>"


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


def extract_body_html_and_footnotes(document_json) -> tuple[str, list[str]]:
    """
    Build body HTML from non-footnote blocks, and collect Footnote blocks.

    Footnote blocks that do not start with a marker are treated as continuations
    of the previous footnote (page-break splits). Text blocks split across pages
    (possibly with images between them) are merged back into one paragraph.
    """
    pages_body_blocks: list[list[tuple[str, str]]] = []
    footnotes: list[str] = []
    merged_continuations = 0
    quote_count = 0

    for page in iter_pages(document_json):
        page_blocks: list[tuple[str, str]] = []
        children = page.get("children") or []

        # Body margin + indent threshold for this page (used to spot quotes).
        page_margin = dominant_left_margin(children) if DETECT_INDENTED_QUOTES else None
        page_bbox = block_bbox(page)
        page_width = (page_bbox[2] - page_bbox[0]) if page_bbox else None
        indent_threshold = (
            max(QUOTE_INDENT_MIN_POINTS, QUOTE_INDENT_MIN_FRACTION * page_width)
            if page_width
            else QUOTE_INDENT_MIN_POINTS
        )

        i = 0
        n = len(children)
        while i < n:
            child = children[i]
            if not isinstance(child, dict):
                i += 1
                continue
            block_type = child.get("block_type") or "Text"

            if block_type == "Footnote":
                html = footnote_html(child)
                # Detect/strip markers on HTML (before markdownify removes <sup>).
                is_new = not footnotes or starts_with_footnote_marker(html)
                if not is_new:
                    cont = footnote_html_to_text(html)
                    footnotes[-1] = join_footnote_parts(footnotes[-1], cont)
                    merged_continuations += 1
                else:
                    stripped_html = strip_leading_marker_from_html(html)
                    text = footnote_html_to_text(stripped_html)
                    text = strip_leading_footnote_marker(text)
                    footnotes.append(text)
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
            if (
                DETECT_INDENTED_QUOTES
                and html.strip()
                and is_indented_quote(child, block_type, page_margin, indent_threshold)
            ):
                html = wrap_blockquote(html)
                block_type = _BLOCKQUOTE_TYPE
                quote_count += 1
            page_blocks.append((block_type, html))
            i += 1
        pages_body_blocks.append(page_blocks)

    if merged_continuations:
        print(f"🔗 Recombined {merged_continuations} page-split footnote continuation(s)")
    if quote_count:
        print(f"❝ Detected {quote_count} indented paragraph(s) → blockquote")

    body_blocks = merge_cross_page_paragraphs(pages_body_blocks)
    body_html = "\n".join(html for _, html in body_blocks if html)
    return body_html, footnotes


def replace_body_markers_with_placeholders(html: str) -> tuple[str, int]:
    """Replace <sup>n</sup> markers in document order with %%FNREF k%% (k = 1..N)."""
    counter = 0

    def repl(_match: re.Match) -> str:
        nonlocal counter
        counter += 1
        return f"%%FNREF{counter}%%"

    return _BODY_SUP_MARKER.sub(repl, html), counter


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


def normalize_list_html(html: str) -> str:
    """
    Fix Marker list HTML so markdownify can emit proper nested markdown lists.

    Marker often encodes indented items as sibling <ul><li class="list-indent-N">
    inside the parent <ul>. Nest those under the previous <li> instead.
    Also wrap runs of orphan <li> tags in a <ul>.
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

    return str(soup)


def normalize_list_markdown(text: str) -> str:
    """Tighten spacing around markdown list blocks."""
    # Remove blank lines between consecutive list items.
    text = re.sub(
        r"(?m)^([ \t]*(?:[-*+] |\d+\. ).+)\n\n+(?=[ \t]*(?:[-*+] |\d+\. ))",
        r"\1\n",
        text,
    )
    return text


_FIGURE_BLOCK_RE = re.compile(r"<figure\b.*?</figure>", re.IGNORECASE | re.DOTALL)
_FIGURE_PLACEHOLDER_RE = re.compile(r"%%FIGURE(\d+)%%")


def html_to_markdown(html: str) -> str:
    # Stash <figure> blocks so markdownify emits them verbatim (it would
    # otherwise drop the <figure>/<figcaption> wrappers as unknown tags).
    figures: list[str] = []

    def _stash_figure(match: re.Match) -> str:
        figures.append(match.group(0))
        return f"\n\n%%FIGURE{len(figures) - 1}%%\n\n"

    html = _FIGURE_BLOCK_RE.sub(_stash_figure, html)

    html = normalize_list_html(html)
    converter = MarkdownConverter(
        heading_style="ATX",
        bullets="-",
        escape_misc=False,
        escape_underscores=True,
        escape_asterisks=True,
        strong_em_symbol="*",  # *italic*, **bold**, ***both***
    )
    markdown = converter.convert(html)
    markdown = html_emphasis_tags_to_markdown(markdown)
    markdown = normalize_list_markdown(markdown)
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
    progress: ProgressFn | None = None,
) -> bool:
    """Write markdown next to the source. Returns True if image links were rewritten."""
    report_progress(progress, 90, "Converting JSON → Markdown…")
    print("📝 Converting JSON → Markdown...")
    html, footnotes = extract_body_html_and_footnotes(document_json)

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

    html, marker_count = replace_body_markers_with_placeholders(html)
    markdown = html_to_markdown(html)
    markdown = finalize_footnote_refs(markdown)
    markdown = append_footnotes_section(markdown, footnotes)
    md_path.write_text(markdown, encoding="utf-8")

    if footnotes or marker_count:
        print(
            f"📎 Footnotes: {len(footnotes)} definition(s), "
            f"{marker_count} in-text marker(s)"
        )
        if footnotes and marker_count and len(footnotes) != marker_count:
            print(
                "⚠ Marker count and footnote count differ; "
                "they were paired by document order (1…N)."
            )

    return use_images


def book_output_dir(source_path: Path) -> Path:
    """
    Book folder next to the source file, named after the file stem.

    Example: /books/MyBook.pdf → /books/MyBook/
    """
    return source_path.parent / source_path.stem


def ensure_book_dir(source_path: Path) -> Path:
    out_dir = book_output_dir(source_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


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
    out_dir = ensure_book_dir(pdf_path)
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

    if extract_cover:
        report_progress(progress, 85, f"Extracting cover (page {cover_page})…")
        try:
            cover_path = extract_cover_image(pdf_path, images_dir, cover_page)
            print(f"🖼️  Saved cover (page {cover_page}) → {IMAGES_DIR_NAME}/{cover_path.name}")
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
    # If the JSON already lives in a book folder named after the stem, use it;
    # otherwise create that folder next to the JSON (and write outputs there).
    if json_path.parent.name == stem:
        out_dir = json_path.parent
        working_json = json_path
    else:
        out_dir = ensure_book_dir(json_path)
        working_json = out_dir / f"{stem}.json"
        if working_json.resolve() != json_path.resolve():
            working_json.write_text(
                json.dumps(document_json, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"💾 Copied JSON → {working_json}")

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
    print(f"   JSON:     {working_json}")
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
        self.title("PDF → Markdown (Datalab)")
        self.minsize(520, 220)
        self.resizable(True, False)

        self._running = False
        self._file_var = tk.StringVar()
        self._page_range_var = tk.StringVar()
        self._download_images_var = tk.BooleanVar(value=True)
        self._base64_var = tk.BooleanVar(value=EMBED_IMAGES_AS_BASE64)
        self._extract_cover_var = tk.BooleanVar(value=EXTRACT_COVER)
        self._cover_page_var = tk.StringVar(value=str(COVER_PAGE_DEFAULT))
        self._status_var = tk.StringVar(value="Select a PDF or JSON file to begin.")
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
            text="0-indexed, e.g. 0-10 — leave blank for all (PDF only)",
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
        ttk.Label(cover_row, text="(1 = first page; PDF only)", foreground="#555").grid(
            row=0, column=2, sticky="w", padx=(8, 0)
        )

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
        """Use the same native zenity picker as the CLI."""
        try:
            path = select_file_zenity()
        except Exception as exc:
            messagebox.showerror("File picker error", str(exc))
            return
        if path is not None:
            self._file_var.set(str(path))

    def _sync_page_range_state(self) -> None:
        path = self._file_var.get().strip()
        is_pdf = path.lower().endswith(".pdf")
        state = "normal" if is_pdf else "disabled"
        self._page_entry.configure(state=state)
        if not is_pdf:
            self._page_range_var.set("")
        self._sync_cover_state()

    def _sync_cover_state(self) -> None:
        # Cover extraction needs a source PDF; disable for JSON input.
        path = self._file_var.get().strip()
        is_pdf = path.lower().endswith(".pdf") or path == ""
        self._cover_check.configure(state="normal" if is_pdf else "disabled")
        entry_state = "normal" if (is_pdf and self._extract_cover_var.get()) else "disabled"
        self._cover_page_entry.configure(state=entry_state)

    def _sync_image_options(self) -> None:
        # base64 embedding works whether or not images are downloaded to disk,
        # so the checkbox stays enabled either way; this keeps that intent clear.
        self._base64_check.configure(state="normal")

    def _set_progress(self, percent: float, message: str) -> None:
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

        extract_cover = is_pdf and bool(self._extract_cover_var.get())
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

        embed_base64 = bool(self._base64_var.get())
        download_images = bool(self._download_images_var.get())
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
            self.after(0, lambda e=exc: self._on_error(e))
            return
        self.after(0, lambda d=out_dir: self._on_success(d))

    def _on_success(self, out_dir: Path) -> None:
        self._running = False
        self._convert_btn.configure(state="normal")
        self._set_progress(100, f"Done — saved to {out_dir}")
        messagebox.showinfo("Conversion complete", f"Output folder:\n{out_dir}")

    def _on_error(self, exc: BaseException) -> None:
        self._running = False
        self._convert_btn.configure(state="normal")
        self._status_var.set(f"Error: {exc}")
        messagebox.showerror("Conversion failed", str(exc))


def main_gui() -> None:
    app = ConversionApp()
    app.mainloop()


def main_cli() -> None:
    print("📂 Opening native Linux file picker...\n")

    input_path = None
    try:
        input_path = select_file_zenity()
    except Exception as exc:
        print(f"⚠ Could not open file picker: {exc}")

    if input_path is None:
        raw_path = input("Enter path to PDF or JSON file: ").strip()
        if not raw_path:
            print("❌ No file path provided.")
            sys.exit(0)
        input_path = Path(raw_path).expanduser()

    if not input_path.exists():
        print(f"❌ File not found: {input_path}")
        sys.exit(1)

    print(f"📄 Selected: {input_path.name}")

    page_range = None
    if input_path.suffix.lower() == ".pdf":
        page_range = prompt_page_range()

    embed_base64 = EMBED_IMAGES_AS_BASE64 or ("--base64-images" in sys.argv)
    download_images = "--no-images" not in sys.argv

    extract_cover = EXTRACT_COVER and ("--no-cover" not in sys.argv)
    cover_page = COVER_PAGE_DEFAULT
    if "--cover-page" in sys.argv:
        idx = sys.argv.index("--cover-page")
        try:
            cover_page = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            print("❌ --cover-page requires a whole number >= 1.")
            sys.exit(1)
        if cover_page < 1:
            print("❌ --cover-page must be >= 1 (1 = first page).")
            sys.exit(1)

    try:
        run_conversion(
            input_path,
            page_range=page_range,
            embed_images_as_base64=embed_base64,
            download_images=download_images,
            extract_cover=extract_cover,
            cover_page=cover_page,
        )
    except Exception as exc:
        print(f"❌ {exc}")
        sys.exit(1)


def main() -> None:
    ensure_api_key_file()
    if "--cli" in sys.argv:
        main_cli()
    else:
        main_gui()


if __name__ == "__main__":
    main()
