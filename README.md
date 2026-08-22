# pdf_to_markdown

GUI app to convert a PDF to Markdown (including images) using the [Datalab](https://www.datalab.to) API, or to convert an existing Datalab JSON export to Markdown locally.

Datalab handles the layout analysis and OCR; the script then does a lot of extra work on the returned document to produce Markdown that reads like a real book rather than a raw dump of detected blocks.

The markdown is formatted to work with [Obsidian](https://obsidian.md) and [Readest](https://readest.com).

## Features

### Footnotes

- Inline footnote markers (`<sup>1</sup>`) in the body are converted to Pandoc-style references (`[^1]`), renumbered consecutively in the order they appear.
- Footnote blocks are pulled out of the page flow and collected into a single `# Footnotes` section at the end of the document as `[^n]:` definitions.
- A block is a new note if it starts with `<sup>n</sup>`, `n.` / `n)`, a bare number plus text (`1 ברור`), or `*` / `**`. Blocks with no marker are treated as page-break continuations and stitched back (including hyphenated and mid-sentence breaks). A leading 4-digit year is not treated as a note number unless it continues the sequence.
- A single Footnote block that already contains several notes (for example `<sup>227</sup>…<br/><sup>228</sup>…`) is split into separate definitions.
- After a "Notes" / "Endnotes" heading, following paragraphs and list items are collected as definitions and omitted from the body, so chapter endnotes pair with in-text markers. Superscripts inside those sections are not counted as body refs. In-text `*` / `**` markers (e.g. `Harrison,*`) and their asterisk notes are numbered in the same sequence as superscripts, in the order they appear in the body.

### Paragraph merging across pages

Paragraphs (and list groups) that are broken across a page boundary are merged back into one. The script recognizes hyphenated line breaks (`inter-\nnational` →
`international`), soft breaks that end mid-sentence, and Marker's `has-continuation` hints — and it can look past intervening blocks like images, captions, headers, and footers to find the other half.

### Indented quotes → blockquotes

Block quotes in the original are usually typeset with a wider left margin. The script estimates each page's dominant body-text margin from block bounding boxes, and any text block indented past it by more than a threshold is rendered as a markdown blockquote (`> …`).

### Images and captions

- Images are extracted and can be saved to an `images/` folder (relative links) and/or embedded directly in the Markdown as base64 data URIs.
- Figures and their captions are paired up into a single HTML figure element, and a standalone caption block that follows an image is attached to it.
- Datalab's AI-generated image captions are disabled (`DISABLE_IMAGE_CAPTIONS`) so only real captions from the document are used.
- Image extraction can be turned off in the GUI.

### Cover image and YAML frontmatter

Every Markdown file starts with empty YAML properties (`cover-image`, `isbn`, `title`, `subtitle`, `author`, `identifier`, `language`, `publisher`, `pubdate`, `description`, `series`, `series_index`) for Obsidian / Readest metadata.

When **Extract cover** is on (PDF input only), one page (the first by default) is rendered via poppler and written into the first property, `cover-image`. That value is a base64 data URI if **Embed images as base64** is selected, otherwise a relative link (`images/cover.jpg`). The cover file is saved to `images/` only when **Download images** is selected. If both of those image options are off, extract cover is unavailable in the GUI and is skipped. 

### List and heading cleanup

- **Lists** — nested lists are rebuilt from Datalab's indent classes into proper Markdown lists, and source markers are normalized: bullets become `*`, decimal numbers become `1. 2. 3.`, and alphabetic / roman lists keep their labels combined with markdown bullets (`* a. …`, `* i. …`).
- **Headings** — heading/emphasis HTML is converted to clean Markdown. Consecutive same-level headings with only blank lines between them (a common split of a multi-line title) are combined into one heading joined with ` | ` (e.g. `## Chapter One | The Beginning`).

### Two input modes

Feed it a **PDF** (uploaded to Datalab for conversion) or an existing Datalab **JSON** export (converted to Markdown locally, with no API call). For PDFs you can restrict conversion to a page range.

## Requirements

- Python 3.10+
- Python packages:

```bash
pip install requests beautifulsoup4 markdownify
```

- System tools (install via your package manager, e.g. `apt`):
  - **zenity** — native file picker (`sudo apt install zenity`)
  - **poppler** — `pdftoppm`/`pdftocairo`, used to render the cover image (`sudo apt install poppler-utils`)

## Setup

You need a Datalab API key — get one at
[datalab.to/app/keys](https://www.datalab.to/app/keys). Launch the app and click **API Key…** to enter and save your key, or put it in `datalab_api_key.txt` / set `DATALAB_API_KEY`.

## Usage

```bash
python3 pdf_to_markdown.py
```

In the GUI, select a PDF (sent to Datalab) or an existing Datalab JSON export (converted locally). For PDFs you can set a page range, choose how images are handled, and control cover extraction. Output goes in a folder named after the source file (created next to it, and the PDF/JSON is moved in, unless that folder already exists and already contains the file). Markdown and an `images/` directory are written there. Progress also logs to the terminal.

## Notes

- The Browse button uses a native Zenity file picker.
- Conversion behaviour (mode, image handling, quote/indent detection, cover DPI, etc.) can be tuned via the constants in the `CONFIGURATION` section near the top of `pdf_to_markdown.py`.
- Optional helpers live in `Additional scripts/` (`fix_markdown.py`, `md_to_epub.py`, `renumber_references.py`).
