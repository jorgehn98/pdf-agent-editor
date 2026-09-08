#!/usr/bin/env python3
"""Generic single-page PDF text editor for agent-supervised edits.

Reads a declarative JSON config, replaces configured source spans on one
page, and validates the result before atomically publishing the output.

All workspace-relative paths (``source``, ``output``, ``fonts_dir``) resolve
against the directory containing the config file. The output must live inside
that workspace directory and must not overwrite the source or the config.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import pymupdf


CMYK_K_ONLY = {"#58595b": (0, 0, 0, 0.8), "#231f20": (0, 0, 0, 1)}

_RGB_OP_RE = re.compile(rb'(?:^|[\x00\t\n\f\r ])(?:rg|RG)(?:$|[\x00\t\n\f\r \(\)<>\[\]\{\}/%])')
_DEVICERGB_RE = re.compile(rb'/DeviceRGB[\x00\t\n\f\r ]+(?:cs|CS)(?:$|[\x00\t\n\f\r \(\)<>\[\]\{\}/%])')

_ALIGNMENTS = {"left": pymupdf.TEXT_ALIGN_LEFT, "center": pymupdf.TEXT_ALIGN_CENTER,
               "right": pymupdf.TEXT_ALIGN_RIGHT}

_HEX_COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')

_PROBE_CACHE = {}


def contains_rgb_operators(raw):
    """Detect RGB color operators using real PDF whitespace.

    Accepts ``bytes`` (raw content-stream bytes) or ``str`` and returns
    ``True`` when a non-stroking/stroking RGB operator (``rg``/``RG``) or a
    ``/DeviceRGB cs``/``CS`` colorspace selection is present.
    """
    if isinstance(raw, str):
        data = raw.encode("latin1", errors="ignore")
    else:
        data = bytes(raw)
    return bool(_RGB_OP_RE.search(data) or _DEVICERGB_RE.search(data))


def page_content_streams(document, page):
    """Return page content streams plus every Form XObject stream."""
    collected = []
    for xref in page.get_contents():
        try:
            collected.append(document.xref_stream(xref))
        except Exception:
            continue
    try:
        total = document.xref_length()
    except Exception:
        return collected
    for xref in range(1, total):
        try:
            descriptor = document.xref_object(xref)
        except Exception:
            continue
        if "/Subtype" in descriptor and "/Form" in descriptor:
            try:
                collected.append(document.xref_stream(xref))
            except Exception:
                continue
    return collected


def parse_pdffonts(output):
    """Parse real ``pdffonts`` output into a list of dict rows."""
    entries = []
    lines = output.splitlines()[2:]
    pattern = re.compile(
        r'^(?P<name>.+?)\s+'
        r'(?P<type>CID TrueType|CID Type 0C?|Type 1C?|Type 3|TrueType|Type 0)\s+'
        r'(?P<encoding>\S+)\s+'
        r'(?P<emb>yes|no)\s+(?P<sub>yes|no)\s+(?P<uni>yes|no)\s+'
        r'(?P<obj>\d+)\s+(?P<gen>\d+)\s*$'
    )
    for line in lines:
        if not line.strip():
            continue
        match = pattern.match(line)
        if match:
            entries.append(match.groupdict())
    return entries


def verify_new_fonts(output_document, output_path, expected_stems, require_external=False):
    """Verify each newly embedded font is CID TrueType/Identity-H/emb/uni."""
    fonts = output_document.get_page_fonts(0)
    by_ref = {}
    for entry in fonts:
        by_ref.setdefault(entry[4], []).append(entry)
    for stem in sorted(expected_stems):
        candidates = [
            font for font in by_ref.get(stem, [])
            if font[1] == "ttf" and font[2] == "Type0" and font[5] == "Identity-H"
        ]
        if not candidates:
            raise RuntimeError(
                f"New font {stem!r} is not embedded as CID TrueType Identity-H "
                f"(ttf/Type0/Identity-H) in PDF resources"
            )
    if shutil.which("pdffonts") is None:
        if require_external:
            raise RuntimeError(
                "pdffonts is not available (required by "
                "validation.require_external_validators)"
            )
        return
    pdffonts_output = command_output(["pdffonts", str(output_path)])
    by_object = {int(row["obj"]): row for row in parse_pdffonts(pdffonts_output)}
    for stem in sorted(expected_stems):
        candidates = [
            font for font in by_ref.get(stem, [])
            if font[1] == "ttf" and font[2] == "Type0" and font[5] == "Identity-H"
        ]
        for font in candidates:
            row = by_object.get(int(font[0]))
            if row is None:
                raise RuntimeError(
                    f"New font {stem!r} (object {font[0]}) missing in pdffonts output"
                )
            if (row["type"] != "CID TrueType" or row["encoding"] != "Identity-H"
                    or row["emb"] != "yes" or row["uni"] != "yes"):
                raise RuntimeError(
                    f"New font {stem!r} pdffonts check failed: expected CID TrueType "
                    f"Identity-H emb=yes uni=yes, found {row}"
                )


def spans(page):
    return [span for block in page.get_text("dict")["blocks"] if block["type"] == 0
            for line in block["lines"] for span in line["spans"]]


def cmyk(value):
    """Map a gray ``#rrggbb`` hex string to a K-only CMYK tuple.

    The two documented gray values keep their exact mappings; any other
    neutral gray (``rr == gg == bb``) maps to ``(0, 0, 0, 1 - v/255)``.
    True chromatic colors raise ``RuntimeError``.
    """
    key = value.lower()
    if key in CMYK_K_ONLY:
        return CMYK_K_ONLY[key]
    if _HEX_COLOR_RE.match(value):
        red = int(value[1:3], 16)
        green = int(value[3:5], 16)
        blue = int(value[5:7], 16)
        if red == green == blue:
            return (0, 0, 0, 1 - red / 255)
    raise RuntimeError(f"Unsupported color {value!r}: only K-only CMYK allowed")


def resolve_color(spec):
    """Resolve a configured color to a PyMuPDF color tuple.

    Accepts a gray ``#rrggbb`` hex string (via :func:`cmyk`) or a list of
    1 (gray), 3 (RGB) or 4 (CMYK) components, each in [0, 1].
    """
    if isinstance(spec, str):
        return cmyk(spec)
    if isinstance(spec, (list, tuple)) and len(spec) in (1, 3, 4):
        components = []
        for value in spec:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                break
            number = float(value)
            if not 0.0 <= number <= 1.0:
                break
            components.append(number)
        else:
            return tuple(components)
    raise RuntimeError(
        f"Unsupported color {spec!r}: expected a gray hex string or a list "
        f"of 1/3/4 components in [0, 1]"
    )


def probe_span_color(color):
    """Return the integer span color PyMuPDF reports for a color tuple.

    PyMuPDF converts each colorspace to sRGB with its own rounding, so the
    expected value is measured with the engine itself instead of a formula.
    Results are cached per color tuple.
    """
    key = tuple(color)
    if key not in _PROBE_CACHE:
        document = pymupdf.open()
        try:
            page = document.new_page(width=200, height=200)
            page.insert_textbox(
                pymupdf.Rect(10, 10, 190, 100), "X", fontname="helv",
                fontsize=10, align=0, overlay=True, color=key,
            )
            probe = [span for span in spans(page) if span["text"] == "X"]
            if not probe:
                raise RuntimeError(f"Could not probe span color for {key!r}")
            _PROBE_CACHE[key] = probe[0]["color"]
        finally:
            document.close()
    return _PROBE_CACHE[key]


def boxes(item):
    return item["boxes"] if "boxes" in item else [item["box"]]


def image_signature(page):
    return [(entry["digest"].hex(), tuple(round(value, 5) for value in entry["transform"]),
             entry["has-mask"]) for entry in page.get_image_info(hashes=True, xrefs=True)]


def drawing_signature(page):
    signature = []
    for drawing in page.get_drawings():
        items = []
        for entry in drawing["items"]:
            kind = entry[0]
            if kind == "re":
                rect = entry[1]
                orient = entry[2] if len(entry) > 2 else 1
                items.append((kind, round(rect.x0, 3), round(rect.y0, 3),
                              round(rect.x1, 3), round(rect.y1, 3), orient))
            else:
                points = tuple((round(float(point.x), 3), round(float(point.y), 3))
                               for point in entry[1:])
                items.append((kind,) + points)
        rect = drawing["rect"]
        signature.append((
            round(rect.x0, 3), round(rect.y0, 3), round(rect.x1, 3), round(rect.y1, 3),
            drawing["type"], repr(drawing["color"]), repr(drawing["fill"]),
            round(float(drawing["width"] or 0), 3), repr(drawing["dashes"]),
            repr(drawing["closePath"]), repr(drawing.get("even_odd")), tuple(items),
            repr(drawing.get("fill_opacity")), repr(drawing.get("stroke_opacity")),
            repr(drawing.get("lineCap")), repr(drawing.get("lineJoin")),
            repr(drawing.get("layer")),
        ))
    return tuple(signature)


def group_signature(document, page):
    """Return a normalized signature of the page transparency group.

    Returns ``None`` when the page references no ``/Group`` object, otherwise
    a whitespace-normalized string of the referenced group object, so any
    group (CMYK or not) is compared and preserved exactly.
    """
    try:
        raw = document.xref_object(page.xref)
    except Exception:
        return None
    match = re.search(r"/Group\s+(\d+)\s+\d+\s+R", raw)
    if not match:
        return None
    try:
        group = document.xref_object(int(match.group(1)))
    except Exception:
        return None
    return re.sub(r"\s+", " ", group).strip()


def group_is_cmyk(document, page):
    signature = group_signature(document, page)
    if signature is None:
        return False
    if not re.search(r"/CS\s+/DeviceCMYK", signature):
        return False
    if not re.search(r"/S\s+/Transparency", signature):
        return False
    return True


def source_metadata(document, page):
    images = page.get_images(full=True)
    return {
        "images": image_signature(page),
        "image_count": len(images),
        "mask_count": sum(1 for image in images if image[1]),
        "drawings": drawing_signature(page),
        "group": group_signature(document, page),
        "group_cmyk": group_is_cmyk(document, page),
    }


def render_diff(source_page, output_page, masks):
    scale = 4
    source = source_page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    output = output_page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    if (source.width, source.height, source.n) != (output.width, output.height, output.n):
        raise RuntimeError("Rendered page geometry changed")
    mask = bytearray(source.width * source.height)
    for rect in masks:
        rect = pymupdf.Rect(rect)
        left, top = max(0, int(rect.x0 * scale)), max(0, int(rect.y0 * scale))
        right, bottom = min(source.width, int(rect.x1 * scale) + 1), min(source.height, int(rect.y1 * scale) + 1)
        for y in range(top, bottom):
            mask[y * source.width + left:y * source.width + right] = b"\1" * (right - left)
    changes = 0
    stride = source.n
    source_samples, output_samples = source.samples, output.samples
    for pixel in range(source.width * source.height):
        offset = pixel * stride
        if not mask[pixel] and any(source_samples[offset + channel] != output_samples[offset + channel] for channel in range(stride)):
            changes += 1
    if changes:
        raise RuntimeError(f"Visual diff has {changes} changed pixels outside edit masks")
    return changes


def command_output(command):
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    output = (result.stdout + result.stderr).strip()
    if result.returncode:
        raise RuntimeError(f"{' '.join(command)} failed: {output}")
    diagnostics = result.stderr.strip().replace("Syntax Warning: Invalid Font Weight", "").replace("Invalid Font Weight", "").strip()
    if diagnostics:
        raise RuntimeError(f"{' '.join(command)} emitted unexpected diagnostics: {diagnostics}")
    return result.stdout.strip()


def run_external(command, require_external):
    """Run an external validator when its binary is available.

    Missing binaries are skipped unless ``require_external`` is True, in
    which case they raise. A present validator that fails always raises:
    failures are never hidden.
    """
    if shutil.which(command[0]) is None:
        if require_external:
            raise RuntimeError(
                f"{command[0]} is not available (required by "
                f"validation.require_external_validators)"
            )
        return None
    return command_output(command)


def unchanged_span_key(span):
    """Pure signature for unedited spans.

    Includes explicitly ``text``, ``bbox``, ``font``, ``size``, ``color``,
    ``alpha``, ``flags``, ``char_flags``, ``bidi``, ``ascender``,
    ``descender`` and ``origin``.  Missing fields raise ``KeyError`` (no
    silent defaults).  Lists/tuples are normalized; small numeric noise in
    ``bbox``/``origin``/``size``/``ascender``/``descender`` is rounded to
    2 decimals while discrete fields compare exactly.
    """
    text = span["text"]
    bbox = span["bbox"]
    font = span["font"]
    size = span["size"]
    color = span["color"]
    alpha = span["alpha"]
    flags = span["flags"]
    char_flags = span["char_flags"]
    bidi = span["bidi"]
    ascender = span["ascender"]
    descender = span["descender"]
    origin = span["origin"]
    bbox_key = tuple(round(float(value), 2) for value in bbox)
    origin_key = tuple(round(float(value), 2) for value in origin)
    return (
        text,
        bbox_key,
        font,
        round(float(size), 2),
        color,
        alpha,
        flags,
        char_flags,
        bidi,
        round(float(ascender), 2),
        round(float(descender), 2),
        origin_key,
    )


def replacement_font_ref(item):
    """Return the configured font reference (``fontfile`` wins over ``font``)."""
    reference = item.get("fontfile") or item.get("font")
    if not reference:
        raise RuntimeError(
            f"Replacement entry for {item.get('source')!r} is missing 'font'/'fontfile'"
        )
    return reference


def validate(config, source_document, source_page, output_path, masks):
    validation = config.get("validation", {}) or {}
    require_cmyk_group = bool(validation.get("require_cmyk_group", False))
    reject_rgb = bool(validation.get("reject_rgb", False))
    require_external = bool(validation.get("require_external_validators", False))
    output_document = pymupdf.open(output_path)
    try:
        if len(output_document) != 1:
            raise RuntimeError(
                "Output geometry mismatch: expected exactly 1 page, "
                f"found {len(output_document)}"
            )
        page = output_document[0]
        if page.rect != source_page.rect:
            raise RuntimeError(
                "Output geometry mismatch: source rect "
                f"{list(source_page.rect)} != output rect {list(page.rect)}"
            )
        source_spans = spans(source_page)
        output_spans = spans(page)
        text = page.get_text()
        required = [entry for entry in config["required_text"] if entry not in text]
        if required:
            raise RuntimeError(f"Required text missing: {required}")
        source_texts = [entry["source"] for entry in config["replacements"]]
        present_span_texts = {span["text"] for span in output_spans}
        remaining = [entry for entry in source_texts if entry in present_span_texts]
        if remaining:
            raise RuntimeError(f"Source replacement spans remain: {remaining}")
        source_span_texts = [span["text"] for span in source_spans]
        output_span_texts = [span["text"] for span in output_spans]
        protected_set = set(config["protected_spans"])
        if Counter(item for item in source_span_texts if item in protected_set) != Counter(item for item in output_span_texts if item in protected_set):
            raise RuntimeError("Protected spans multiset changed")
        for item in config["replacements"]:
            if not item.get("text"):
                continue
            expected_lines = item["text"].splitlines()
            allowed = [pymupdf.Rect(box) for box in boxes(item)]
            matches = [
                span for span in output_spans
                if span["text"] in expected_lines
                and any(box.contains(pymupdf.Rect(span["bbox"])) for box in allowed)
            ]
            expected_count = len(boxes(item)) * len(expected_lines)
            if len(matches) != expected_count:
                raise RuntimeError(f"Edited span cardinality mismatch for {item['text']!r}: expected {expected_count}, found {len(matches)}")
            if {span["text"] for span in matches} != set(expected_lines):
                raise RuntimeError(f"Edited multiline span incomplete: {item['text']}")
            expected_counter = Counter(expected_lines)
            for box in allowed:
                box_counter = Counter(
                    span["text"] for span in matches
                    if box.contains(pymupdf.Rect(span["bbox"]))
                )
                if box_counter != expected_counter:
                    raise RuntimeError(
                        f"Edited multiline per-box Counter mismatch for {item['text']!r} "
                        f"in box {list(box)}: expected {dict(expected_counter)}, "
                        f"found {dict(box_counter)}"
                    )
            for span in matches:
                expected_font = Path(replacement_font_ref(item)).stem
                if span["font"] != expected_font:
                    raise RuntimeError(f"Edited span font mismatch for {span['text']!r}: expected {expected_font}, found {span['font']}")
                if abs(span["size"] - item["size"]) > 0.06:
                    raise RuntimeError(f"Edited span size mismatch for {span['text']!r}: expected {item['size']}, found {span['size']}")
                expected_color = probe_span_color(resolve_color(item["color"]))
                if span["color"] != expected_color:
                    raise RuntimeError(f"Edited span color mismatch for {span['text']!r}")
        source_texts_set = {entry["source"] for entry in config["replacements"]}
        inserted_lines_set = set()
        for entry in config["replacements"]:
            if entry.get("text"):
                inserted_lines_set.update(entry["text"].splitlines())

        source_unchanged = Counter(
            unchanged_span_key(span) for span in source_spans
            if span["text"] not in source_texts_set and span["text"].strip() != ""
        )
        output_unchanged = Counter(
            unchanged_span_key(span) for span in output_spans
            if span["text"] not in inserted_lines_set and span["text"].strip() != ""
        )
        if source_unchanged != output_unchanged:
            raise RuntimeError(
                "Unedited spans changed: "
                f"source {sum(source_unchanged.values())} vs "
                f"output {sum(output_unchanged.values())}"
            )
        before, after = source_metadata(source_document, source_page), source_metadata(output_document, page)
        if require_cmyk_group:
            if not before["group_cmyk"]:
                raise RuntimeError("Source page has no referenced DeviceCMYK transparency group")
            if not after["group_cmyk"]:
                raise RuntimeError("Output page lost the referenced DeviceCMYK transparency group")
        if before != after:
            raise RuntimeError("Preserved object mismatch")
        if reject_rgb:
            for raw in page_content_streams(output_document, page):
                if contains_rgb_operators(raw):
                    raise RuntimeError("Output content uses RGB operators (rejected by policy)")
        expected_stems = {
            Path(replacement_font_ref(entry)).stem for entry in config["replacements"]
            if entry.get("text")
        }
        if expected_stems:
            verify_new_fonts(output_document, output_path, expected_stems, require_external)
        run_external(["pdfimages", "-list", str(output_path)], require_external)
        run_external(["pdfinfo", str(output_path)], require_external)
        run_external(["pdftotext", "-layout", str(output_path), "-"], require_external)
        run_external(["gs", "-q", "-o", os.devnull, "-sDEVICE=nullpage", str(output_path)], require_external)
        run_external(["gs", "-q", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite", "-o", os.devnull, str(output_path)], require_external)
        visual_changes = render_diff(source_page, page, masks)
        return {"images": after["image_count"], "masks": after["mask_count"], "drawings": len(after["drawings"]), "outside_mask_pixel_changes": visual_changes}
    finally:
        output_document.close()


def _cli_error_info(error):
    """Map a detailed exception to a stable, privacy-safe CLI code/message.

    Direct ``run()``/``validate()`` callers keep the detailed exception;
    only the CLI boundary uses this mapping so default stdout/stderr never
    carry absolute paths, source/replacement text or raw validator output.
    """
    message = str(error)
    low = message.lower()
    if "missing explicit config" in low:
        return ("MISSING_CONFIG_ARG", "Missing explicit config path argument.")
    if "config file not found" in low:
        return ("CONFIG_NOT_FOUND", "Config file not found.")
    if "invalid json" in low or "config error" in low or "unsupported color" in low \
            or "is missing 'font'" in low:
        return ("CONFIG_INVALID", "Invalid configuration.")
    if "source file not found" in low:
        return ("SOURCE_NOT_FOUND", "Source file not found.")
    if "sha-256" in low or "sha256" in low:
        return ("SOURCE_HASH_MISMATCH", "Source hash mismatch.")
    if "output path outside" in low or "output must" in low \
            or "output path is a directory" in low:
        return ("OUTPUT_INVALID", "Invalid output path.")
    if "font outside" in low or "font file not found" in low:
        return ("FONT_UNAVAILABLE", "Font file not available.")
    if "pdffonts" in low or "pdfimages" in low or "pdfinfo" in low \
            or "pdftotext" in low or "unexpected diagnostics" in low \
            or "is not available (required by" in low:
        return ("VALIDATOR_FAILED", "External validator failed.")
    if " gs " in f" {message} " or message.startswith("gs "):
        return ("VALIDATOR_FAILED", "External validator failed.")
    return ("VALIDATION_FAILED", "Validation failed.") if (
        "required text missing" in low or "spans remain" in low
        or "protected spans" in low or "cardinality" in low
        or "multiline" in low or "per-box" in low or "counter mismatch" in low
        or "edited span" in low or "edited text does not fit" in low
        or "unedited spans" in low or "preserved object" in low
        or "devicecmyk" in low or "transparency group" in low
        or "rgb" in low or "geometry" in low or "rendered page" in low
        or "outside edit masks" in low or "visual diff" in low
        or "spans for" in low or "box " in low or "printed page" in low
        or "not the expected printed page" in low or "does not fit" in low
        or "new font" in low or "is not embedded" in low
    ) else ("INTERNAL_ERROR", "Processing failed.")


def within_root(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _check_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"Config error: {label} must be a number, found {value!r}")
    return float(value)


def _check_box(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise RuntimeError(f"Config error: {label} must be a list of 4 numbers, found {value!r}")
    return [float(_check_number(entry, label)) for entry in value]


def _validate_replacement(item, index):
    label = f"replacements[{index}]"
    if not isinstance(item, dict):
        raise RuntimeError(f"Config error: {label} must be an object, found {item!r}")
    if not isinstance(item.get("source"), str) or not item["source"]:
        raise RuntimeError(f"Config error: {label}.source must be a non-empty string")
    count = item.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise RuntimeError(f"Config error: {label}.count must be an integer >= 1")
    text = item.get("text")
    if text is not None and not isinstance(text, str):
        raise RuntimeError(f"Config error: {label}.text must be a string or null")
    configured = None
    if "boxes" in item:
        if not isinstance(item["boxes"], (list, tuple)) or not item["boxes"]:
            raise RuntimeError(f"Config error: {label}.boxes must be a non-empty list")
        configured = [_check_box(entry, f"{label}.boxes") for entry in item["boxes"]]
    elif "box" in item:
        configured = [_check_box(item["box"], f"{label}.box")]
    if text and configured is None:
        raise RuntimeError(f"Config error: {label} with text needs 'box' or 'boxes'")
    if text:
        replacement_font_ref(item)
        size = _check_number(item.get("size"), f"{label}.size")
        if size <= 0:
            raise RuntimeError(f"Config error: {label}.size must be > 0")
        align = item.get("align", "left")
        if align not in _ALIGNMENTS:
            raise RuntimeError(f"Config error: {label}.align must be one of {sorted(_ALIGNMENTS)}")
        if "color" not in item:
            raise RuntimeError(f"Config error: {label}.color is required with text")
        resolve_color(item["color"])
        if "lineheight" in item:
            lineheight = _check_number(item["lineheight"], f"{label}.lineheight")
            if lineheight <= 0:
                raise RuntimeError(f"Config error: {label}.lineheight must be > 0")


def _validate_config(config):
    if not isinstance(config, dict):
        raise RuntimeError("Config error: the config root must be a JSON object")
    for key in ("source", "source_sha256", "page_index", "output", "fonts_dir",
                "replacements", "required_text", "protected_spans"):
        if key not in config:
            raise RuntimeError(f"Config error: missing required key {key!r}")
    if not isinstance(config["source"], str) or not config["source"]:
        raise RuntimeError("Config error: 'source' must be a non-empty path string")
    if not isinstance(config["source_sha256"], str) or not re.fullmatch(r"[0-9a-fA-F]{64}", config["source_sha256"]):
        raise RuntimeError("Config error: 'source_sha256' must be a 64-character hex string")
    if isinstance(config["page_index"], bool) or not isinstance(config["page_index"], int) or config["page_index"] < 0:
        raise RuntimeError("Config error: 'page_index' must be an integer >= 0")
    if not isinstance(config["output"], str) or not config["output"]:
        raise RuntimeError("Config error: 'output' must be a non-empty path string")
    if not isinstance(config["fonts_dir"], str) or not config["fonts_dir"]:
        raise RuntimeError("Config error: 'fonts_dir' must be a non-empty path string")
    if not isinstance(config["replacements"], list):
        raise RuntimeError("Config error: 'replacements' must be a list")
    for index, item in enumerate(config["replacements"]):
        _validate_replacement(item, index)
    if not isinstance(config["required_text"], list) or any(not isinstance(entry, str) for entry in config["required_text"]):
        raise RuntimeError("Config error: 'required_text' must be a list of strings")
    if not isinstance(config["protected_spans"], list) or any(not isinstance(entry, str) for entry in config["protected_spans"]):
        raise RuntimeError("Config error: 'protected_spans' must be a list of strings")
    if "printed_page" in config and not isinstance(config["printed_page"], str):
        raise RuntimeError("Config error: 'printed_page' must be a string")
    validation = config.get("validation", {})
    if validation is None:
        validation = {}
    if not isinstance(validation, dict):
        raise RuntimeError("Config error: 'validation' must be an object")
    for key in ("require_cmyk_group", "reject_rgb", "require_external_validators"):
        if key in validation and not isinstance(validation[key], bool):
            raise RuntimeError(f"Config error: 'validation.{key}' must be a boolean")


def _resolve_workspace_path(raw, workspace):
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    return workspace / candidate


def run(config_path):
    started = time.monotonic()
    config_path = Path(config_path).expanduser()
    if not config_path.is_absolute():
        config_path = (Path.cwd() / config_path).resolve()
    else:
        config_path = config_path.resolve()
    if not config_path.is_file():
        raise RuntimeError(f"Config file not found: {config_path}")
    workspace = config_path.parent.resolve()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Config error: invalid JSON in {config_path}: {error}")
    _validate_config(config)
    source_path = _resolve_workspace_path(config["source"], workspace).resolve()
    output_path = _resolve_workspace_path(config["output"], workspace).resolve()
    fonts_dir = _resolve_workspace_path(config["fonts_dir"], workspace).resolve()
    if not within_root(output_path, workspace):
        raise RuntimeError("Output path outside write root")
    if output_path == workspace:
        raise RuntimeError("Output must be a file inside the write root")
    if output_path.exists() and output_path.is_dir():
        raise RuntimeError("Output path is a directory")
    if source_path == output_path:
        raise RuntimeError("Output must not overwrite the source")
    if config_path == output_path:
        raise RuntimeError("Output must not overwrite the config")
    if not source_path.is_file():
        raise RuntimeError(f"Source file not found: {source_path}")
    with source_path.open("rb") as handle:
        source_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    if source_hash != config["source_sha256"].lower():
        raise RuntimeError("Source SHA-256 does not match the configuration")
    # NOTE: standard PyMuPDF text metrics are used deliberately and no global
    # TOOLS flag is toggled here. Probing a box and inserting into it must see
    # identical metrics however many edits already ran in this process.
    source = pymupdf.open(source_path)
    document = None
    temporary = None
    try:
        if config["page_index"] >= len(source):
            raise RuntimeError(
                f"Config error: page_index {config['page_index']} out of range "
                f"for {len(source)} pages"
            )
        source_page = source[config["page_index"]]
        if "printed_page" in config:
            lines = source_page.get_text().splitlines()
            first_line = lines[0] if lines else ""
            if first_line != config["printed_page"]:
                raise RuntimeError("Configured source page is not the expected printed page")
        document = pymupdf.open(source_path)
        document.select([config["page_index"]])
        page = document[0]
        original_spans = spans(page)
        masks = []
        for item in config["replacements"]:
            matches = [span for span in original_spans if span["text"] == item["source"]]
            if len(matches) != item["count"]:
                raise RuntimeError(f"Expected {item['count']} spans for {item['source']!r}, found {len(matches)}")
            if "box" in item or "boxes" in item:
                unique_boxes = {tuple(round(value, 4) for value in span["bbox"]) for span in matches}
                configured_boxes = boxes(item)
                if len(configured_boxes) != len(unique_boxes):
                    raise RuntimeError(
                        f"Box cardinality mismatch for {item['source']!r}: "
                        f"expected {len(unique_boxes)} unique source boxes, "
                        f"found {len(configured_boxes)} configured boxes"
                    )
                unique_rects = [pymupdf.Rect(list(entry)) for entry in unique_boxes]
                configured_rects = [pymupdf.Rect(entry) for entry in configured_boxes]
                for source_rect in unique_rects:
                    if not any(source_rect.intersects(target) for target in configured_rects):
                        raise RuntimeError(
                            f"Box spatial mismatch for {item['source']!r}: "
                            f"source box {list(source_rect)} has no intersecting configured box"
                        )
                for target in configured_rects:
                    if not any(target.intersects(source_rect) for source_rect in unique_rects):
                        raise RuntimeError(
                            f"Box spatial mismatch for {item['source']!r}: "
                            f"configured box {list(target)} has no intersecting source box"
                        )
            for match in matches:
                masks.append(match["bbox"])
        redactions = {tuple(round(value, 4) for value in mask) for mask in masks}
        for rect in redactions:
            page.add_redact_annot(pymupdf.Rect(rect), fill=None, cross_out=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE, text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        for item in config["replacements"]:
            if not item.get("text"):
                continue
            if "fontfile" in item:
                font_path = _resolve_workspace_path(item["fontfile"], workspace).resolve()
            else:
                font_path = (fonts_dir / item["font"]).resolve()
            if not within_root(font_path, workspace):
                raise RuntimeError(f"Font outside write root: {replacement_font_ref(item)}")
            if not font_path.is_file():
                raise RuntimeError(f"Font file not found: {font_path}")
            for box in boxes(item):
                masks.append(box)
                result = page.insert_textbox(pymupdf.Rect(box), item["text"], fontname=Path(replacement_font_ref(item)).stem,
                                             fontfile=str(font_path), fontsize=item["size"],
                                             lineheight=item.get("lineheight", 1), color=resolve_color(item["color"]),
                                             align=_ALIGNMENTS[item.get("align", "left")], overlay=True)
                if result < 0:
                    raise RuntimeError(f"Edited text does not fit {item['text']!r}: {result:.2f}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=output_path.parent, prefix=".pdf-edit-", suffix=".pdf", delete=False) as handle:
            temporary = Path(handle.name)
        document.save(temporary, garbage=4, deflate=True)
        try:
            document.close()
        finally:
            document = None
        validation = validate(config, source, source_page, temporary, masks)
        os.replace(temporary, output_path)
        temporary = None
        try:
            relative_output = output_path.relative_to(workspace).as_posix()
        except ValueError:
            relative_output = output_path.name
        return {"status": "complete", "code": "COMPLETE", "output": relative_output, "duration_seconds": round(time.monotonic() - started, 3), "validation": validation}
    finally:
        if document is not None:
            try:
                document.close()
            except Exception:
                pass
        source.close()
        if temporary and temporary.exists():
            temporary.unlink()


if __name__ == "__main__":
    _debug = "--debug" in sys.argv[1:]
    _args = [arg for arg in sys.argv[1:] if arg != "--debug"]
    try:
        if len(_args) != 1 or not _args[0]:
            raise RuntimeError("Missing explicit config path argument (usage: edit_pdf.py <config.json> [--debug])")
        print(json.dumps(run(_args[0]), ensure_ascii=False, sort_keys=True))
    except Exception as error:
        _code, _safe = _cli_error_info(error)
        _payload = {"status": "partial", "code": _code, "error": _safe}
        if _debug:
            _payload["detail"] = str(error)
        print(json.dumps(_payload, ensure_ascii=False, sort_keys=True))
        sys.exit(1)
