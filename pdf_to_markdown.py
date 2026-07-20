#!/usr/bin/env python3
"""
Convert a PDF to JSON (and images) via the Datalab API, then turn that JSON into Markdown.
Or open an existing Datalab JSON file and convert it to Markdown only.

API docs: https://documentation.datalab.to/docs/welcome/api
Reads the API key from datalab_api_key.txt (or DATALAB_API_KEY env var).
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
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
API_KEY_FILE = Path(__file__).resolve().parent / "datalab_api_key.txt"
# ================================================================


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

    print("❌ No Datalab API key found.")
    print(f"   1. Open {API_KEY_FILE.name} and paste your key on its own line")
    print("   2. Or: export DATALAB_API_KEY='your_key_here'")
    print("   Create a key at: https://www.datalab.to/app/keys")
    sys.exit(1)


_PAGE_RANGE_RE = re.compile(
    r"^\s*\d+\s*(?:-\s*\d+)?(?:\s*,\s*\d+\s*(?:-\s*\d+)?)*\s*$"
)


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
            elif result.returncode == 1:
                # Cancel — fall through to terminal prompt.
                page_range = None
            else:
                page_range = None
        except Exception:
            page_range = None

    if page_range is None:
        page_range = input("Page range: ").strip()

    if not page_range:
        print("   → all pages")
        return None

    if not _PAGE_RANGE_RE.match(page_range):
        print(f"❌ Invalid page range: {page_range!r}")
        print("   Use forms like 0-10 or 0-5,10,15-20 (0-indexed).")
        sys.exit(1)

    # Normalize whitespace around commas/dashes.
    normalized = re.sub(r"\s+", "", page_range)
    print(f"   → pages {normalized}")
    return normalized


def submit_conversion(
    pdf_path: Path,
    api_key: str,
    *,
    page_range: str | None = None,
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

    with pdf_path.open("rb") as f:
        response = requests.post(
            API_URL,
            files={"file": (pdf_path.name, f, "application/pdf")},
            data=data,
            headers=headers,
            timeout=120,
        )

    if response.status_code != 200:
        print(f"❌ Upload failed ({response.status_code}): {response.text}")
        sys.exit(1)

    payload = response.json()
    if not payload.get("success", True) and "request_check_url" not in payload:
        print(f"❌ Upload rejected: {payload}")
        sys.exit(1)

    check_url = payload.get("request_check_url")
    if not check_url:
        print(f"❌ No request_check_url in response: {payload}")
        sys.exit(1)

    print(f"⏳ Submitted. Request ID: {payload.get('request_id', '?')}")
    return check_url


def poll_result(check_url: str, api_key: str) -> dict:
    """Poll until conversion completes and return the result payload."""
    headers = {"X-API-Key": api_key}

    for attempt in range(1, MAX_POLLS + 1):
        response = requests.get(check_url, headers=headers, timeout=60)
        if response.status_code != 200:
            print(f"❌ Poll failed ({response.status_code}): {response.text}")
            sys.exit(1)

        result = response.json()
        status = result.get("status")

        if status == "complete":
            if not result.get("success", True):
                print(f"❌ Conversion failed: {result.get('error', result)}")
                sys.exit(1)
            return result

        if status == "failed":
            print(f"❌ Conversion failed: {result.get('error', result)}")
            sys.exit(1)

        if attempt == 1 or attempt % 5 == 0:
            print(f"   … still processing (poll {attempt}/{MAX_POLLS})")
        time.sleep(POLL_INTERVAL_SEC)

    print("❌ Timed out waiting for conversion.")
    sys.exit(1)


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
        wrapped_children = [as_block(child) for child in children if child is not None]

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
_MERGEABLE_BLOCK_TYPES = _MERGEABLE_TEXT_TYPES | _MERGEABLE_LIST_TYPES
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


def unwrap_outer_paragraph(html: str) -> str:
    """If html is a single outer <p>, return its inner HTML; otherwise return as-is."""
    soup = BeautifulSoup(html or "", "html.parser")
    contents = [c for c in soup.contents if str(c).strip() != ""]
    if len(contents) == 1 and getattr(contents[0], "name", None) == "p":
        return contents[0].decode_contents().strip()
    return (html or "").strip()


def html_ends_incomplete(html: str) -> bool:
    """True if this block looks like a paragraph cut off mid-flow (fix_markdown rules)."""
    if has_continuation_marker(html):
        return True

    # Prefer the trailing visible text so wrappers like </li>/</p> don't hide the end.
    plain = html_to_plain_text(html)
    if re.search(r"(?:-|—|¬)\s*$", plain):
        return True
    if re.search(r"[A-Za-z,:;*()\[\]]\s*$", plain):
        return True

    inner = unwrap_outer_paragraph(html)
    if re.search(
        r"(?:-|—|¬)\s*(?:<sup>\s*\d+\s*</sup>\s*)?$",
        inner,
        re.IGNORECASE | re.DOTALL,
    ):
        return True
    if re.search(
        r"[A-Za-z,:;*()\[\]]\s*(?:<sup>\s*\d+\s*</sup>\s*)?$",
        inner,
        re.IGNORECASE | re.DOTALL,
    ):
        return True
    return False


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
        last_text = last_li.get_text(" ", strip=False).rstrip()
        next_text = first_item.get_text(" ", strip=False).lstrip()
        if html_ends_incomplete(str(last_li)) and re.match(r"[A-Za-z]", next_text or ""):
            # Merge item text using the same hyphen/soft-break rules.
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

    if not should_peel:
        return blocks, []

    kept = blocks[:j]
    pending = blocks[j:end]
    return kept, pending


def merge_cross_page_paragraphs(
    pages_body_blocks: list[list[tuple[str, str]]],
) -> list[tuple[str, str]]:
    """
    Merge Text/TextInlineMath/ListGroup blocks split across page boundaries.

    Intervening images/captions/headers between the halves are kept after the
    merged block. Only merges across pages to avoid joining unrelated same-page
    blocks.
    """
    result: list[tuple[str, str]] = []
    pending: list[tuple[str, str]] = []
    merges = 0
    list_merges = 0

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
                    and html_starts_like_continuation(next_html)
                ):
                    merged_html = merge_paragraph_html(first_html, next_html)
                    result.append((first_type, merged_html))
                    result.extend(intervening)
                    merges += 1
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
    return result


def normalize_broken_paragraphs_markdown(text: str) -> str:
    """
    Merge leftover broken paragraph newlines in markdown.

    Same ruleset as fix_markdown.py, also allowing [^n] / %%FNREFn%% at the join.
    """
    while True:
        new_text, changed = re.subn(r"-\s*\n\s*([A-Za-z])", r"\1", text)
        if changed:
            text = new_text
            continue

        new_text, changed = re.subn(
            r"([A-Za-z,:;*()\[\]])\s*\n\s*([A-Za-z])",
            r"\1 \2",
            text,
        )
        if changed:
            text = new_text
            continue

        new_text, changed = re.subn(
            r"([A-Za-z,:;*()\[\]])\s*(<sup>\s*\d+\s*</sup>)\s*\n\s*([A-Za-z])",
            r"\1\2 \3",
            text,
        )
        if changed:
            text = new_text
            continue

        new_text, changed = re.subn(
            r"([A-Za-z,:;*()\[\]])\s*(\[\^\d+\])\s*\n\s*([A-Za-z])",
            r"\1\2 \3",
            text,
        )
        if changed:
            text = new_text
            continue

        new_text, changed = re.subn(
            r"([A-Za-z,:;*()\[\]])\s*(%%FNREF\d+%%)\s*\n\s*([A-Za-z])",
            r"\1\2 \3",
            text,
        )
        if not changed:
            break
        text = new_text

    return text


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

    for page in iter_pages(document_json):
        page_blocks: list[tuple[str, str]] = []
        children = page.get("children") or []
        for child in children:
            if not isinstance(child, dict):
                continue
            if child.get("block_type") == "Footnote":
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
            else:
                block_type = child.get("block_type") or "Text"
                page_blocks.append((block_type, json_to_html(as_block(child))))
        pages_body_blocks.append(page_blocks)

    if merged_continuations:
        print(f"🔗 Recombined {merged_continuations} page-split footnote continuation(s)")

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


def rewrite_image_srcs(html: str, images_dirname: str) -> str:
    """Point <img src="..."> at the local images directory."""
    def repl(match: re.Match) -> str:
        src = match.group(1)
        if src.startswith(("http://", "https://", "data:", "/")):
            return match.group(0)
        filename = Path(src).name
        return f'src="{images_dirname}/{filename}"'

    return re.sub(r'src="([^"]+)"', repl, html)


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
    for node in soup.descendants:
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
            run.append(candidates[j])
            j += 1
        if run:
            wrapper = soup.new_tag("ul")
            run[0].insert_before(wrapper)
            for item in run:
                wrapper.append(item.extract())
        i = j

    return str(soup)


def normalize_list_markdown(text: str) -> str:
    """Tighten spacing around markdown list blocks."""
    # Remove blank lines between consecutive list items.
    text = re.sub(
        r"(?m)^([ \t]*[-*+] |\d+\. .+)\n\n+(?=[ \t]*(?:[-*+] |\d+\. ))",
        r"\1\n",
        text,
    )
    return text


def html_to_markdown(html: str) -> str:
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
    return markdown.strip() + "\n"


def load_document_json(json_path: Path):
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"❌ Invalid JSON file: {exc}")
        sys.exit(1)
    except OSError as exc:
        print(f"❌ Could not read JSON: {exc}")
        sys.exit(1)


def convert_json_to_markdown(
    document_json,
    md_path: Path,
    images_dir: Path,
    *,
    newly_saved_images: bool = False,
) -> bool:
    """Write markdown next to the source. Returns True if image links were rewritten."""
    print("📝 Converting JSON → Markdown...")
    html, footnotes = extract_body_html_and_footnotes(document_json)

    use_images = newly_saved_images or images_dir.is_dir()
    if use_images:
        rel_images = Path(os.path.relpath(images_dir, md_path.parent)).as_posix()
        html = rewrite_image_srcs(html, rel_images)

    html, marker_count = replace_body_markers_with_placeholders(html)
    markdown = html_to_markdown(html)
    markdown = normalize_broken_paragraphs_markdown(markdown)
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


def process_pdf(pdf_path: Path) -> None:
    api_key = get_api_key()
    page_range = prompt_page_range()

    print(
        f"🚀 Uploading to Datalab "
        f"(mode={MODE}, format=json, disable_image_captions={DISABLE_IMAGE_CAPTIONS})"
    )

    check_url = submit_conversion(pdf_path, api_key, page_range=page_range)
    result = poll_result(check_url, api_key)

    document_json = result.get("json")
    if document_json is None:
        print("❌ Response did not include JSON output.")
        sys.exit(1)

    stem = pdf_path.stem
    out_dir = ensure_book_dir(pdf_path)
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    images_dir = out_dir / IMAGES_DIR_NAME

    print(f"📁 Output folder: {out_dir}")

    json_path.write_text(
        json.dumps(document_json, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"💾 Saved JSON: {json_path.relative_to(out_dir)}")

    images = collect_images(result, document_json)
    if images:
        save_images(images, images_dir)
        print(f"🖼️  Saved {len(images)} image(s) → {IMAGES_DIR_NAME}/")
    else:
        print("ℹ️  No images in response.")

    used_images = convert_json_to_markdown(
        document_json, md_path, images_dir, newly_saved_images=bool(images)
    )

    page_count = result.get("page_count")
    quality = result.get("parse_quality_score")
    extras = []
    if page_count is not None:
        extras.append(f"{page_count} page(s)")
    if quality is not None:
        extras.append(f"quality={quality}")

    print("\n✅ Done!")
    print(f"   Folder:   {out_dir}")
    print(f"   JSON:     {json_path}")
    print(f"   Markdown: {md_path}")
    if used_images:
        print(f"   Images:   {images_dir}/")
    if extras:
        print(f"   Stats:    {', '.join(extras)}")


def process_json(json_path: Path) -> None:
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

    # Extract any base64 images still embedded in the JSON.
    images = collect_images({}, document_json)
    newly_saved = False
    if images:
        images_dir = out_dir / IMAGES_DIR_NAME
        save_images(images, images_dir)
        newly_saved = True
        print(f"🖼️  Saved {len(images)} embedded image(s) → {IMAGES_DIR_NAME}/")
    elif images_dir.is_dir():
        print(f"🖼️  Using existing images folder: {images_dir.name}/")
    else:
        print("ℹ️  No images folder found in the book directory.")

    used_images = convert_json_to_markdown(
        document_json, md_path, images_dir, newly_saved_images=newly_saved
    )

    print("\n✅ Done!")
    print(f"   Folder:   {out_dir}")
    print(f"   JSON:     {working_json}")
    print(f"   Markdown: {md_path}")
    if used_images:
        print(f"   Images:   {images_dir}/")


def main() -> None:
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

    suffix = input_path.suffix.lower()
    print(f"📄 Selected: {input_path.name}")

    if suffix == ".pdf":
        process_pdf(input_path)
    elif suffix == ".json":
        process_json(input_path)
    else:
        print("❌ Please select a PDF (*.pdf) or JSON (*.json) file.")
        sys.exit(1)


if __name__ == "__main__":
    main()
