import re
import sys
import subprocess
from pathlib import Path

# ========================= CONFIGURATION =========================
START_NUMBER = 1
OUTPUT_SUFFIX = " - converted"
# ================================================================

def normalize_broken_paragraphs(text):
    """Merge broken paragraph line breaks before and after footnote processing.

    A line break is considered part of a broken paragraph when the line ends with:
    - a hyphen: `-` followed by a newline and a letter
    - a letter, comma, asterisk, colon, semicolon, closing parenthesis,
      opening parenthesis, or opening bracket followed by a newline and a letter
    - any of the above followed by a `<sup>n</sup>` footnote marker,
      a newline, then a letter
    """
    while True:
        new_text, changed = re.subn(r'-\s*\n\s*([A-Za-z])', r'\1', text)
        if changed:
            text = new_text
            continue

        new_text, changed = re.subn(r'([A-Za-z,:;*()\[\]])\s*\n\s*([A-Za-z])', r'\1 \2', text)
        if changed:
            text = new_text
            continue

        new_text, changed = re.subn(
            r'([A-Za-z,:;*()\[\]])\s*(<sup>\s*\d+\s*</sup>)\s*\n\s*([A-Za-z])',
            r'\1\2 \3',
            text
        )
        if not changed:
            break
        text = new_text

    return text


def process_footnotes(text):
    """Convert footnote markers and remove their footnote paragraphs."""
    replacements = []
    footnote_texts = {}
    counter = START_NUMBER - 1
    footnote_count = 0
    sup_pattern = re.compile(r'<sup>\s*(\d+)\s*</sup>')

    for sup_match in sup_pattern.finditer(text):
        counter += 1
        old_num = sup_match.group(1)
        replacements.append((sup_match.start(), sup_match.end(), f"[^{counter}]"))

        footnote_pattern = re.compile(
            rf'^\s*{re.escape(old_num)}\.\s+(.+?)(?=\n\s*(?:\d+\.|$)|\Z)',
            re.MULTILINE | re.DOTALL
        )
        footnote_match = footnote_pattern.search(text, sup_match.end())

        if footnote_match:
            footnote_count += 1
            footnote_text = footnote_match.group(1).strip()
            footnote_texts[counter] = footnote_text
            replacements.append((footnote_match.start(), footnote_match.end(), ""))

    for start, end, replacement in sorted(replacements, key=lambda x: x[0], reverse=True):
        text = text[:start] + replacement + text[end:]

    text = re.sub(r'\n{3,}', '\n\n', text)
    return text, counter, footnote_count, footnote_texts


def select_file_zenity():
    """Open native Linux file picker using Zenity"""
    try:
        result = subprocess.run([
            "zenity", "--file-selection",
            "--title=Select Markdown File",
            "--file-filter=Markdown files | *.md *.markdown",
            f"--filename={Path.cwd()}/"
        ], capture_output=True, text=True, timeout=60)
        
        if result.returncode != 0:
            print("❌ No file selected or dialog cancelled.")
            sys.exit(0)
            
        return Path(result.stdout.strip())
        
    except FileNotFoundError:
        print("❌ Zenity not found. Please install it with:")
        print("   sudo apt install zenity   (or equivalent for your distro)")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error opening file dialog: {e}")
        sys.exit(1)


def normalize_heading_levels(text):
    """Normalize heading levels to remove gaps between levels.
    
    Keeps the first heading unchanged and cascades down other heading levels
    to remove gaps (e.g., if levels 3, 4, 6 are used, promote to 4, 5, 6).
    Warns and skips if all 6 levels are already in use.
    """
    
    lines = text.split('\n')
    headings = []
    
    # Find all headings and their indices
    for i, line in enumerate(lines):
        match = re.match(r'^(#+)\s+(.+)$', line)
        if match:
            level = len(match.group(1))
            title = match.group(2)
            headings.append((i, level, title))
    
    if len(headings) <= 1:
        # If 0 or 1 heading, nothing to do
        return text
    
    # Check heading levels used among non-first headings only
    levels_used = set()
    for i in range(1, len(headings)):
        idx, level, title = headings[i]
        levels_used.add(level)
    
    # If all 6 levels are used among non-first headings, warn and stop
    if levels_used == {1, 2, 3, 4, 5, 6}:
        print("⚠️  Warning: Document uses all 6 heading levels. Skipping normalization.")
        return text
    
    # Cascade promote headings from level 6 down to level 2
    for target_level in range(6, 1, -1):
        if target_level not in levels_used:
            source_level = target_level - 1
            if source_level in levels_used:
                # Update all non-first headings at source_level to target_level
                for i in range(1, len(headings)):
                    idx, level, title = headings[i]
                    if level == source_level:
                        lines[idx] = '#' * target_level + ' ' + title
                        headings[i] = (idx, target_level, title)
                
                # Update tracking
                levels_used.discard(source_level)
                levels_used.add(target_level)
    
    return '\n'.join(lines)


def main():
    print("📂 Opening native Linux file picker...\n")
    
    input_path = select_file_zenity()
    print(f"📄 Selected: {input_path.name}")

    # Read the file
    try:
        content = input_path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"❌ Could not read file: {e}")
        sys.exit(1)

    # Merge broken paragraph line breaks before handling footnotes
    content = normalize_broken_paragraphs(content)

    # Convert footnote markers and delete footnote paragraphs
    new_content, counter, footnote_count, footnote_texts = process_footnotes(content)

    # Merge broken paragraphs again after footnote deletion
    new_content = normalize_broken_paragraphs(new_content)

    # Normalize heading levels
    new_content = normalize_heading_levels(new_content)

    # Append footnotes to the end of the document
    if footnote_texts:
        new_content += "\n\n"
        for i in sorted(footnote_texts.keys()):
            new_content += f"[^{i}]: {footnote_texts[i]}\n\n"

    # Save output
    output_path = input_path.with_name(f"{input_path.stem}{OUTPUT_SUFFIX}{input_path.suffix}")
    output_path.write_text(new_content, encoding="utf-8")

    print(f"\n✅ Success!")
    print(f"   Converted {counter - START_NUMBER + 1} footnote marker(s)")
    print(f"   Processed {footnote_count} footnote(s)")
    print(f"   Output saved as: {output_path.name}")

if __name__ == "__main__":
    main()