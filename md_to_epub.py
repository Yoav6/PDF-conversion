#!/usr/bin/env python3

import re
import shutil
import subprocess
import sys
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


# Whitelist of allowed EPUB metadata fields
EPUB_METADATA_FIELDS = {
    'title', 'author', 'date', 'description', 'cover-image', 'isbn', 
    'publisher', 'rights', 'lang'
}


def extract_yaml_metadata(text: str) -> dict:
    """Extract YAML frontmatter from markdown and filter to allowed fields."""
    yaml_match = re.match(r'^---\n(.*?)\n---\n', text, re.DOTALL)
    if not yaml_match:
        return {}
    
    try:
        metadata = yaml.safe_load(yaml_match.group(1)) or {}
        # Filter to only allowed fields
        filtered = {k: v for k, v in metadata.items() if k in EPUB_METADATA_FIELDS}
        return filtered
    except yaml.YAMLError:
        print("⚠ Warning: Could not parse YAML frontmatter, ignoring metadata")
        return {}


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


def run_pandoc(input_path: Path, output_path: Path, metadata: dict = None) -> None:
    """Run pandoc to convert markdown to EPUB."""
    if metadata is None:
        metadata = {}
    
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
    ]
    
    # Add metadata as -M arguments
    for key, value in metadata.items():
        cmd.extend(["-M", f"{key}={value}"])

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
    metadata = extract_yaml_metadata(content)
    
    # Write to temp file for pandoc
    temp_file = input_path.with_stem(input_path.stem + "_temp")
    temp_file.write_text(processed, encoding="utf-8")
    
    try:
        run_pandoc(temp_file, output_path, metadata)
    finally:
        temp_file.unlink()

    print(f"\n✅ EPUB created: {output_path}")


if __name__ == "__main__":
    main()
