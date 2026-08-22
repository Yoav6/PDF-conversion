#!/usr/bin/env python3

import base64
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml


def extract_headings(text: str) -> list[tuple[int, str]]:
    """Extract heading level and title from markdown."""
    headings = []
    for line in text.split('\n'):
        match = re.match(r'^(#{1,6})\s+(.+)$', line)
        if match:
            level = len(match.group(1))
            title = match.group(2).strip()
            headings.append((level, title))
    return headings


# Whitelist of allowed EPUB metadata fields, including aliases used in
# pdf_to_markdown YAML (language/pubdate).
EPUB_METADATA_FIELDS = {
    'title', 'author', 'date', 'description', 'cover-image', 'isbn',
    'publisher', 'rights', 'lang', 'language', 'pubdate',
}

METADATA_FIELD_ALIASES = {
    'language': 'lang',
    'pubdate': 'date',
}

DATA_URI_RE = re.compile(
    r'^data:(image/[A-Za-z0-9.+-]+)(?:;charset=[^;]+)?;base64,(.+)$',
    re.DOTALL,
)

MIME_TO_SUFFIX = {
    'image/jpeg': '.jpg',
    'image/jpg': '.jpg',
    'image/png': '.png',
    'image/gif': '.gif',
    'image/webp': '.webp',
}

YAML_FRONTMATTER_RE = re.compile(r'^---\r?\n(.*?)\r?\n---\r?\n', re.DOTALL)


def extract_yaml_metadata(text: str) -> dict:
    """Extract YAML frontmatter from markdown and filter to allowed fields."""
    yaml_match = YAML_FRONTMATTER_RE.match(text)
    if not yaml_match:
        return {}

    try:
        metadata = yaml.safe_load(yaml_match.group(1)) or {}
        return {k: v for k, v in metadata.items() if k in EPUB_METADATA_FIELDS}
    except yaml.YAMLError:
        print("⚠ Warning: Could not parse YAML frontmatter, ignoring metadata")
        return {}


def strip_yaml_frontmatter(text: str) -> str:
    """Remove YAML frontmatter so pandoc does not treat cover-image as a path."""
    yaml_match = YAML_FRONTMATTER_RE.match(text)
    if not yaml_match:
        return text
    return text[yaml_match.end():]


def normalize_metadata(metadata: dict) -> dict:
    """Map field aliases and drop empty values."""
    normalized = {}
    for key, value in metadata.items():
        key = METADATA_FIELD_ALIASES.get(key, key)
        if key in normalized and value in (None, ""):
            continue
        normalized[key] = value
    return {k: v for k, v in normalized.items() if v not in (None, "")}


def materialize_cover_image(
    cover_value: object,
    md_path: Path,
    dest_dir: Path,
) -> Path | None:
    """Turn a cover-image YAML value into a file path pandoc can use.

    pdf_to_markdown stores either a data URI or a relative path. Pandoc's
    --epub-cover-image needs a real file, and a data URI is far too large to
    pass as a command-line -M argument.
    """
    if not isinstance(cover_value, str):
        return None
    cover_value = cover_value.strip()
    if not cover_value:
        return None

    match = DATA_URI_RE.match(cover_value)
    if match:
        mime, b64 = match.group(1).lower(), match.group(2)
        suffix = MIME_TO_SUFFIX.get(mime, '.jpg')
        out_path = dest_dir / f"cover{suffix}"
        try:
            out_path.write_bytes(base64.b64decode(b64))
        except (ValueError, OSError) as exc:
            print(f"⚠ Warning: Could not decode cover-image data URI: {exc}")
            return None
        return out_path

    path = Path(cover_value)
    if not path.is_absolute():
        path = (md_path.parent / path).resolve()
    if path.exists():
        return path
    print(f"⚠ Warning: Cover image not found: {cover_value}")
    return None


def anchor_from_heading(title: str) -> str:
    """Generate a pandoc-compatible anchor ID from heading text."""
    # Lowercase and replace spaces with hyphens
    anchor = title.lower().replace(' ', '-')
    # Remove special characters, keep only alphanumeric, hyphens, underscores
    anchor = re.sub(r'[^\w\-]', '', anchor)
    # Remove multiple consecutive hyphens
    anchor = re.sub(r'-+', '-', anchor)
    # Strip leading/trailing hyphens
    anchor = anchor.strip('-')
    return anchor


def generate_toc(headings: list[tuple[int, str]]) -> str:
    """Generate markdown TOC with links from headings."""
    if not headings:
        return ""

    min_level = min(level for level, _ in headings)
    toc_lines = []

    for level, title in headings:
        # Skip first heading (assumed to be document title)
        if len(toc_lines) == 0 and level == min_level:
            continue

        indent = "  " * (level - min_level - 1)
        anchor = anchor_from_heading(title)
        toc_lines.append(f"{indent}- [{title}](#{anchor})")

    return "\n".join(toc_lines)


def insert_toc_after_contents(text: str, toc: str) -> str:
    """Insert TOC after 'contents' heading."""
    lines = text.split('\n')
    contents_idx = None
    contents_level = None

    for i, line in enumerate(lines):
        if re.match(r'^(#{1,6})\s+contents\s*$', line, re.IGNORECASE):
            contents_idx = i
            contents_level = len(re.match(r'^(#{1,6})', line).group(1))
            break

    if contents_idx is None:
        return text

    # Find next heading at same/higher level
    next_heading = None
    for i in range(contents_idx + 1, len(lines)):
        m = re.match(r'^(#{1,6})\s+', lines[i])
        if m and len(m.group(1)) <= contents_level:
            next_heading = i
            break

    # Insert TOC after contents heading
    result = lines[:contents_idx + 1] + [""] + toc.split('\n') + [""]
    if next_heading is not None:
        result += lines[next_heading:]

    return '\n'.join(result)


def select_file_zenity() -> Path:
    """Open a native Linux file picker using zenity and return the selected path."""
    result = subprocess.run(
        [
            "zenity",
            "--file-selection",
            "--title=Select Markdown File",
            "--file-filter=Markdown files | *.md *.markdown",
            f"--filename={Path.cwd()}/",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode != 0 or not result.stdout.strip():
        print("❌ No file selected or dialog cancelled.")
        sys.exit(0)

    return Path(result.stdout.strip())


def run_pandoc(
    input_path: Path,
    output_path: Path,
    *,
    resource_path: Path,
    metadata_file: Path | None = None,
    cover_image: Path | None = None,
) -> None:
    """Run pandoc to convert markdown to EPUB."""
    cmd = [
        "pandoc",
        str(input_path),
        "-o",
        str(output_path),
        "--from",
        "markdown+yaml_metadata_block+footnotes+raw_html+implicit_figures",
        "--epub-chapter-level=1",
        "--toc-depth=6",
        "--reference-location=document",
        "--resource-path",
        str(resource_path),
    ]

    if metadata_file is not None:
        cmd.extend(["--metadata-file", str(metadata_file)])
    if cover_image is not None:
        cmd.extend(["--epub-cover-image", str(cover_image)])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("❌ Pandoc conversion failed.")
        if result.stderr:
            print(result.stderr.strip())
        sys.exit(result.returncode)


def main() -> None:
    print("📂 Opening native Linux file picker...\n")

    input_path = None
    if shutil.which("zenity"):
        try:
            input_path = select_file_zenity()
        except Exception as exc:
            print(f"⚠ Could not open file picker: {exc}")

    if input_path is None:
        raw_path = input("Enter path to markdown file: ").strip()
        if not raw_path:
            print("❌ No file path provided.")
            sys.exit(0)
        input_path = Path(raw_path)

    if not input_path.exists():
        print(f"❌ File not found: {input_path}")
        sys.exit(1)

    if input_path.suffix.lower() not in {".md", ".markdown"}:
        print("❌ Please select a markdown file (*.md or *.markdown).")
        sys.exit(1)

    if shutil.which("pandoc") is None:
        print("❌ pandoc not found. Install pandoc first:")
        print("   sudo apt install pandoc")
        sys.exit(1)

    output_path = input_path.with_suffix(".epub")
    print(f"📄 Converting '{input_path.name}' to '{output_path.name}'...")

    # Read and process markdown
    content = input_path.read_text(encoding="utf-8")
    headings = extract_headings(content)
    toc = generate_toc(headings)
    processed = insert_toc_after_contents(content, toc)
    metadata = normalize_metadata(extract_yaml_metadata(content))
    cover_value = metadata.pop("cover-image", None)

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        temp_file = tmpdir / f"{input_path.stem}.md"
        temp_file.write_text(strip_yaml_frontmatter(processed), encoding="utf-8")

        metadata_file = None
        if metadata:
            metadata_file = tmpdir / "metadata.yaml"
            with metadata_file.open("w", encoding="utf-8") as fh:
                yaml.safe_dump(
                    metadata,
                    fh,
                    allow_unicode=True,
                    default_flow_style=False,
                    sort_keys=False,
                )

        cover_image = materialize_cover_image(cover_value, input_path, tmpdir)
        run_pandoc(
            temp_file,
            output_path,
            resource_path=input_path.parent,
            metadata_file=metadata_file,
            cover_image=cover_image,
        )

    print(f"\n✅ EPUB created: {output_path}")


if __name__ == "__main__":
    main()
