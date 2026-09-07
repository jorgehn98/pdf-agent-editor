#!/usr/bin/env python3
"""Generate and edit a neutral single-page PDF (all outputs under work/).

Builds ``work/source.pdf`` with plain text and one vector rectangle, writes
``work/config.json`` with paths relative to ``work/``, copies the bundled
OFL font into ``work/fonts/``, runs the editor, and prints the JSON result.
Everything generated lands in ``examples/synthetic/work/`` (ignored).
"""
import hashlib
import json
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
WORK = BASE / "work"
FONTS = WORK / "fonts"
SOURCE = WORK / "source.pdf"
CONFIG = WORK / "config.json"
OUTPUT = WORK / "output.pdf"

sys.path.insert(0, str(BASE.parent.parent))

import edit_pdf  # noqa: E402
import pymupdf  # noqa: E402

PAGE_WIDTH, PAGE_HEIGHT = 595, 842
FONT_FILE = "Barlow-Regular.ttf"

TITLE = "SYNTHETIC EXAMPLE"
SOURCE_LINE = "SOURCE LINE TO REPLACE"
REPLACEMENT_LINE = "EDITED REPLACEMENT LINE"
KEEP_LINE = "DOC-2026-001 KEEP"


def sha256_of(path):
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def build_source():
    WORK.mkdir(parents=True, exist_ok=True)
    FONTS.mkdir(parents=True, exist_ok=True)
    shutil.copy(BASE / "assets" / FONT_FILE, FONTS / FONT_FILE)
    document = pymupdf.open()
    try:
        page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
        page.insert_textbox(
            pymupdf.Rect(50, 60, 545, 100), TITLE,
            fontname="helv", fontsize=20, align=0, overlay=True,
        )
        page.insert_textbox(
            pymupdf.Rect(50, 200, 545, 240), SOURCE_LINE,
            fontname="helv", fontsize=14, align=0, overlay=True,
        )
        page.insert_textbox(
            pymupdf.Rect(50, 300, 545, 330), KEEP_LINE,
            fontname="helv", fontsize=12, align=0, overlay=True,
        )
        page.draw_rect(pymupdf.Rect(50, 400, 200, 460), color=(0, 0, 0), width=1)
        document.save(SOURCE, garbage=4, deflate=True)
    finally:
        document.close()


def build_config():
    probe = pymupdf.open(SOURCE)
    try:
        target = next(
            span for span in edit_pdf.spans(probe[0]) if span["text"] == SOURCE_LINE
        )
        box = [float(value) for value in target["bbox"]]
        printed = probe[0].get_text().splitlines()[0]
    finally:
        probe.close()
    box[0] -= 2
    box[1] -= 2
    box[2] += 60
    box[3] += 6
    config = {
        "source": "source.pdf",
        "source_sha256": sha256_of(SOURCE),
        "page_index": 0,
        "printed_page": printed,
        "output": "output.pdf",
        "fonts_dir": "fonts",
        "replacements": [
            {
                "source": SOURCE_LINE,
                "text": REPLACEMENT_LINE,
                "count": 1,
                "box": box,
                "font": FONT_FILE,
                "size": 14,
                "align": "left",
                "color": [0, 0, 0, 1],
            }
        ],
        "required_text": [REPLACEMENT_LINE],
        "protected_spans": [KEEP_LINE],
    }
    CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    build_source()
    build_config()
    result = edit_pdf.run(CONFIG)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result


if __name__ == "__main__":
    main()
