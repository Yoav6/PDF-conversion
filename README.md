# pdf_to_markdown

Convert a PDF to clean Markdown (plus extracted images and a cover image) using the [Datalab](https://www.datalab.to) API, or convert an existing Datalab JSON export to Markdown locally. Ships with both a GUI and a CLI.

Datalab handles the layout analysis and OCR; the script then does a lot of extra work on the returned document to produce Markdown that reads like a real book rather than a raw dump of detected blocks.

The markdown is formatted to work with [Obsidian](https://obsidian.md) and [Readest](https://readest.com).

## Features

### Footnotes

- Inline footnote markers (`<sup>1</sup>`) in the body are converted to Pandoc-style references (`[^1]`), renumbered consecutively in the order they appear.
- Footnote blocks are pulled out of the page flow and collected into a single `# Footnotes` section at the end of the document as `[^n]:` definitions.
- Footnotes split across a page break are detected (a continuation block has no leading marker) and stitched back into a single note, handling hyphenated and mid-sentence breaks.

### Paragraph merging across pages

Paragraphs (and list groups) that are broken across a page boundary are merged back into one. The script recognizes hyphenated line breaks (`inter-\nnational` →
`international`), soft breaks that end mid-sentence, and Marker's `has-continuation` hints — and it can look past intervening blocks like images, captions, headers, and footers to find the other half.

### Indented quotes → blockquotes

Block quotes in the original are usually typeset with a wider left margin. The script estimates each page's dominant body-text margin from block bounding boxes, and any text block indented past it by more than a threshold is rendered as a markdown blockquote (`> …`). 

### Images and captions

- Images are extracted and either saved to an `images/` folder (relative links) or embedded directly in the Markdown as base64 data URIs (`--base64-images`).
- Figures and their captions are paired up into a single HTML figure element, and a standalone caption block that follows an image is attached to it.
- Datalab's AI-generated image captions are disabled (`DISABLE_IMAGE_CAPTIONS`) so only real captions from the document are used. 
- Image extraction can be turned off
  entirely with `--no-images`.

### Cover image

One PDF page (the first by default) is rendered to an image via poppler and saved into the output folder as the cover. The page number is configurable in the GUI or with `--cover-page N`, and it can be disabled in the GUI or with `--no-cover`.

### List and heading cleanup

Nested lists are normalized from Datalab's indent classes into proper nested Markdown lists, and heading/emphasis HTML is converted to clean Markdown. List markers are normalized too: source bullets become `*`, decimal numbers become `1. 2. 3.`, and alphabetic / roman lists keep their labels combined with markdown bullets (`* a. …`, `* i. …`). Consecutive same-level headings with only blank lines between them (a common split of a multi-line title) are combined into one heading.

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
[datalab.to/app/keys](https://www.datalab.to/app/keys). The easiest way to set it is
through the GUI: launch the app and click the **API Key…** button to enter and save
your key.

The key is stored in `datalab_api_key.txt` 

## Usage

Run the GUI (default):

```bash
python3 pdf_to_markdown.py
```

Or use the CLI:

```bash
python3 pdf_to_markdown.py --cli                 # convert with default settings
python3 pdf_to_markdown.py --cli --base64-images # embed images as base64 data URIs
python3 pdf_to_markdown.py --cli --no-images     # skip image extraction
python3 pdf_to_markdown.py --cli --cover-page 2  # render page 2 as the cover
python3 pdf_to_markdown.py --cli --no-cover      # don't extract a cover image
```

You can select either a PDF (sent to Datalab for conversion) or an existing Datalab
JSON export (converted to Markdown locally). For PDFs you can specify a page range.
Output is written to a folder next to the source file, containing the Markdown and an
`images/` directory.

## Notes

- The script uses a native Zenity file picker, and falls back to a text prompt if
  Zenity is unavailable.
- Conversion behaviour (mode, image handling, quote/indent detection, cover DPI,
  etc.) can be tuned via the constants in the `CONFIGURATION` section near the top of
  `pdf_to_markdown.py`.
