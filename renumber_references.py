"""
Renumber Markdown footnote references sequentially.

This script fixes the numbering of footnote references in a Markdown file so
they run consecutively starting from 1, in the order the numbers first appear.
This is useful after editing or converting a document (e.g. from PDF), where
footnotes may end up out of order or with gaps in their numbering.

How it works:
    - It scans the file for footnote references of the form ``[^N]`` (where N is
      a number), while ignoring footnote definitions of the form ``[^N]:``.
    - It collects the unique reference numbers, sorts them, and builds a mapping
      that reassigns them to consecutive values (1, 2, 3, ...).
    - It rewrites every reference using the new numbers, leaving footnote
      definitions untouched.

Usage:
    Run the script directly::

        python renumber_references.py

    A native Linux file picker (Zenity) opens so you can choose a Markdown
    file. The selected file is updated in place with the renumbered footnotes.

Requirements:
    - Zenity must be installed for the file picker
      (e.g. ``sudo apt install zenity``).
"""

import re
import sys
import subprocess
from pathlib import Path

def renumber_footnotes(content):
    # Find all footnote references: [^number] not followed by :
    ref_pattern = r'\[\^(\d+)\]'
    refs = []
    for match in re.finditer(ref_pattern, content):
        # Check if it's a definition (followed by : )
        if not re.match(r'\s*:', content[match.end():]):
            refs.append(match.group(1))
    
    # Get unique numbers, sort them
    numbers = sorted(set(map(int, refs)))
    
    # Create mapping: old -> new (consecutive starting from 1)
    mapping = {old: new for new, old in enumerate(numbers, 1)}
    
    # Replace references
    def replace_all(match):
        num = int(match.group(1))
        if re.match(r'\s*:', content[match.end():]):
            # It's a definition, keep as is
            return match.group(0)
        else:
            # It's a reference, renumber
            return f'[^{mapping[num]}]'
    
    content = re.sub(ref_pattern, replace_all, content)
    return content

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

if __name__ == "__main__":
    print("📂 Opening native Linux file picker...\n")
    
    input_path = select_file_zenity()
    print(f"📄 Selected: {input_path.name}")

    # Read the file
    try:
        content = input_path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"❌ Could not read file: {e}")
        sys.exit(1)

    new_content = renumber_footnotes(content)

    # Save back to the same file
    input_path.write_text(new_content, encoding="utf-8")

    print("Footnotes renumbered successfully.")