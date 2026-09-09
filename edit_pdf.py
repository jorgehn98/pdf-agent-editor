#!/usr/bin/env python3
"""Generic single-page PDF text editor for agent-supervised edits.

Reads a declarative JSON config, replaces configured source spans on one
page, and validates the result before atomically publishing the output.

All workspace-relative paths (``source``, ``output``, ``fonts_dir``) resolve
against the directory containing the config file. ``source``, ``output`` and
``fonts_dir`` must live inside that workspace directory; the output must not
overwrite the source or the config. Existing outputs are preserved unless
``overwrite`` is explicitly ``true``.
"""
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, deque
from pathlib import Path

import pymupdf


class EditorError(RuntimeError):
    """Typed error with a stable code and a privacy-safe public message.

    Direct ``run()``/``validate()`` callers see the detailed message via
    ``str(error)``; the CLI boundary reports only ``code``/``public_message``
    so default output never carries absolute paths, source/replacement text
    or raw validator output.
    """

    def __init__(self, code, message, public_message="Processing failed."):
        super().__init__(message)
        self.code = code
        self.public_message = public_message


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
        collected.append(document.xref_stream(xref))
    total = document.xref_length()
    for xref in range(1, total):
        descriptor = document.xref_object(xref)
        if "/Subtype" in descriptor and "/Form" in descriptor:
            collected.append(document.xref_stream(xref))
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


def verify_new_fonts(output_document, output_path, expected_stems, require_external=False, external_timeout=60):
    """Verify expected CID TrueType/Identity-H fonts and external metadata when available."""
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
            raise EditorError(
                "VALIDATION_FAILED",
                f"New font {stem!r} is not embedded as CID TrueType Identity-H "
                f"(ttf/Type0/Identity-H) in PDF resources",
                "Validation failed.",
            )
    if shutil.which("pdffonts") is None:
        if require_external:
            raise EditorError(
                "VALIDATOR_FAILED",
                "pdffonts is not available (required by "
                "validation.require_external_validators)",
                "External validator failed.",
            )
        return
    pdffonts_output = command_output(["pdffonts", str(output_path)], timeout=external_timeout)
    by_object = {int(row["obj"]): row for row in parse_pdffonts(pdffonts_output)}
    for stem in sorted(expected_stems):
        candidates = [
            font for font in by_ref.get(stem, [])
            if font[1] == "ttf" and font[2] == "Type0" and font[5] == "Identity-H"
        ]
        for font in candidates:
            row = by_object.get(int(font[0]))
            if row is None:
                raise EditorError(
                    "VALIDATOR_FAILED",
                    f"New font {stem!r} (object {font[0]}) missing in pdffonts output",
                    "External validator failed.",
                )
            if (row["type"] != "CID TrueType" or row["encoding"] != "Identity-H"
                    or row["emb"] != "yes" or row["uni"] != "yes"):
                raise EditorError(
                    "VALIDATOR_FAILED",
                    f"New font {stem!r} pdffonts check failed: expected CID TrueType "
                    f"Identity-H emb=yes uni=yes, found {row}",
                    "External validator failed.",
                )


def spans(page):
    return [span for block in page.get_text("dict")["blocks"] if block["type"] == 0
            for line in block["lines"] for span in line["spans"]]


def cmyk(value):
    """Map a gray ``#rrggbb`` hex string to a K-only CMYK tuple.

    The two documented gray values keep their exact mappings; any other
    neutral gray (``rr == gg == bb``) maps to ``(0, 0, 0, 1 - v/255)``.
    Unsupported colors raise ``EditorError``.
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
    raise EditorError(
        "CONFIG_INVALID",
        f"Unsupported color {value!r}: only K-only CMYK allowed",
        "Invalid configuration.",
    )


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
    raise EditorError(
        "CONFIG_INVALID",
        f"Unsupported color {spec!r}: expected a gray hex string or a list "
        f"of 1/3/4 components in [0, 1]",
        "Invalid configuration.",
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
                raise EditorError(
                    "INTERNAL_ERROR",
                    f"Could not probe span color for {key!r}",
                    "Processing failed.",
                )
            _PROBE_CACHE[key] = probe[0]["color"]
        finally:
            document.close()
    return _PROBE_CACHE[key]


def boxes(item):
    return item["boxes"] if "boxes" in item else [item["box"]]


def _span_rect(span):
    """Return an output span bbox as a Rect, failing closed on invalid data."""
    try:
        return pymupdf.Rect(span["bbox"])
    except EditorError:
        raise
    except Exception as error:
        raise EditorError(
            "VALIDATION_FAILED",
            f"Invalid span bbox: {error}",
            "Validation failed.",
        )


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
            elif kind == "qu":
                quad = entry[1]
                try:
                    quad_points = list(quad)
                except TypeError:
                    quad_points = [quad.ul, quad.ur, quad.ll, quad.lr]
                points = tuple((round(float(point.x), 3), round(float(point.y), 3))
                               for point in quad_points)
                items.append((kind,) + points)
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
    a key-sorted tuple of the referenced PDF dictionary, so semantically
    irrelevant key ordering does not cause a preservation mismatch.

    Inspection failures raise instead of returning ``None`` (fail-closed).
    """
    raw = document.xref_object(page.xref)
    match = re.search(r"/Group\s+(\d+)\s+\d+\s+R", raw)
    if not match:
        return None
    group_xref = int(match.group(1))
    return tuple(sorted(
        (
            key,
            value_type,
            value if value_type in {"string", "hexstring"}
            else re.sub(r"\s+", " ", value).strip(),
        )
        for key in document.xref_get_keys(group_xref)
        for value_type, value in [document.xref_get_key(group_xref, key)]
    ))


def group_is_cmyk(document, page):
    signature = group_signature(document, page)
    if signature is None:
        return False
    values = {key: (value_type, value) for key, value_type, value in signature}
    return (
        values.get("CS") == ("name", "/DeviceCMYK")
        and values.get("S") == ("name", "/Transparency")
    )


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
    antialias_padding = 5
    source = source_page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    output = output_page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    if (source.width, source.height, source.n) != (output.width, output.height, output.n):
        raise EditorError(
            "VALIDATION_FAILED",
            "Rendered page geometry changed",
            "Validation failed.",
        )
    pixel_count = source.width * source.height
    mask = bytearray(pixel_count)
    halo = bytearray(pixel_count)
    for rect in masks:
        rect = pymupdf.Rect(rect)
        left, top = max(0, int(rect.x0 * scale)), max(0, int(rect.y0 * scale))
        right = min(source.width, int(rect.x1 * scale) + 1)
        bottom = min(source.height, int(rect.y1 * scale) + 1)
        for y in range(top, bottom):
            mask[y * source.width + left:y * source.width + right] = b"\1" * (right - left)
        halo_left = max(0, left - antialias_padding)
        halo_top = max(0, top - antialias_padding)
        halo_right = min(source.width, right + antialias_padding)
        halo_bottom = min(source.height, bottom + antialias_padding)
        for y in range(halo_top, halo_bottom):
            halo[y * source.width + halo_left:y * source.width + halo_right] = b"\1" * (halo_right - halo_left)
    changed = bytearray(pixel_count)
    allowed = bytearray(pixel_count)
    queue = deque()
    stride = source.n
    source_samples, output_samples = source.samples, output.samples
    for pixel in range(pixel_count):
        offset = pixel * stride
        if any(source_samples[offset + channel] != output_samples[offset + channel] for channel in range(stride)):
            changed[pixel] = 1
            if mask[pixel]:
                allowed[pixel] = 1
                queue.append(pixel)
    while queue:
        pixel = queue.popleft()
        x, y = pixel % source.width, pixel // source.width
        for adjacent_y in range(max(0, y - 1), min(source.height, y + 2)):
            row = adjacent_y * source.width
            for adjacent_x in range(max(0, x - 1), min(source.width, x + 2)):
                adjacent = row + adjacent_x
                if changed[adjacent] and halo[adjacent] and not allowed[adjacent]:
                    allowed[adjacent] = 1
                    queue.append(adjacent)
    changes = sum(
        1 for pixel in range(pixel_count)
        if changed[pixel] and not allowed[pixel]
    )
    if changes:
        raise EditorError(
            "VALIDATION_FAILED",
            f"Visual diff has {changes} changed pixels outside edit masks",
            "Validation failed.",
        )
    return changes


def command_output(command, timeout=60):
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as error:
        raise EditorError(
            "VALIDATOR_FAILED",
            f"{' '.join(command)} timed out after {timeout}s: {error}",
            "External validator failed.",
        )
    output = (result.stdout + result.stderr).strip()
    if result.returncode:
        raise EditorError(
            "VALIDATOR_FAILED",
            f"{' '.join(command)} failed: {output}",
            "External validator failed.",
        )
    diagnostics = result.stderr.strip().replace("Syntax Warning: Invalid Font Weight", "").replace("Invalid Font Weight", "").strip()
    if diagnostics:
        raise EditorError(
            "VALIDATOR_FAILED",
            f"{' '.join(command)} emitted unexpected diagnostics: {diagnostics}",
            "External validator failed.",
        )
    return result.stdout.strip()


def run_external(command, require_external, timeout=60):
    """Run an external validator when its binary is available.

    Missing binaries are skipped unless ``require_external`` is True, in
    which case they raise. A present validator that fails or times out
    always raises: failures are never hidden.
    """
    if shutil.which(command[0]) is None:
        if require_external:
            raise EditorError(
                "VALIDATOR_FAILED",
                f"{command[0]} is not available (required by "
                f"validation.require_external_validators)",
                "External validator failed.",
            )
        return None
    return command_output(command, timeout=timeout)


def unchanged_span_key(span):
    """Pure signature for unedited spans.

    Includes explicitly ``text``, ``bbox``, ``font``, ``size``, ``color``,
    ``alpha``, ``flags``, ``char_flags``, ``bidi``, ``ascender``,
    ``descender`` and ``origin``.  Missing fields raise ``KeyError`` (no
    silent defaults).  ``bbox`` and ``origin`` are normalized to tuples;
    small numeric noise in ``bbox``/``origin``/``size``/``ascender``/
    ``descender`` is rounded to 2 decimals while discrete fields compare
    exactly.
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
    for key in ("font", "fontfile"):
        if key in item and (not isinstance(item[key], str) or not item[key]):
            raise EditorError(
                "CONFIG_INVALID",
                f"Config error: replacement {key!r} must be a non-empty string",
                "Invalid configuration.",
            )
    reference = item.get("fontfile") or item.get("font")
    if not reference:
        raise EditorError(
            "CONFIG_INVALID",
            f"Replacement entry for {item.get('source')!r} is missing 'font'/'fontfile'",
            "Invalid configuration.",
        )
    return reference


def validate(config, source_document, source_page, output_path, masks):
    validation = config.get("validation", {}) or {}
    require_cmyk_group = bool(validation.get("require_cmyk_group", False))
    reject_rgb = bool(validation.get("reject_rgb", False))
    require_external = bool(validation.get("require_external_validators", False))
    external_timeout = validation.get("external_timeout", 60)
    output_document = pymupdf.open(output_path)
    try:
        if len(output_document) != 1:
            raise EditorError(
                "VALIDATION_FAILED",
                "Output geometry mismatch: expected exactly 1 page, "
                f"found {len(output_document)}",
                "Validation failed.",
            )
        page = output_document[0]
        if page.rect != source_page.rect:
            raise EditorError(
                "VALIDATION_FAILED",
                "Output geometry mismatch: source rect "
                f"{list(source_page.rect)} != output rect {list(page.rect)}",
                "Validation failed.",
            )
        source_spans = spans(source_page)
        output_spans = spans(page)
        text = page.get_text()
        required = [entry for entry in config["required_text"] if entry not in text]
        if required:
            raise EditorError(
                "VALIDATION_FAILED",
                f"Required text missing: {required}",
                "Validation failed.",
            )
        source_texts = [entry["source"] for entry in config["replacements"]]
        present_span_texts = {span["text"] for span in output_spans}
        remaining = [entry for entry in source_texts if entry in present_span_texts]
        if remaining:
            raise EditorError(
                "VALIDATION_FAILED",
                f"Source replacement spans remain: {remaining}",
                "Validation failed.",
            )
        source_span_texts = [span["text"] for span in source_spans]
        output_span_texts = [span["text"] for span in output_spans]
        protected_set = set(config["protected_spans"])
        if Counter(item for item in source_span_texts if item in protected_set) != Counter(item for item in output_span_texts if item in protected_set):
            raise EditorError(
                "VALIDATION_FAILED",
                "Protected spans multiset changed",
                "Validation failed.",
            )
        for item in config["replacements"]:
            if not item.get("text"):
                continue
            expected_lines = item["text"].splitlines()
            allowed = [pymupdf.Rect(box) for box in boxes(item)]
            matches = [
                span for span in output_spans
                if span["text"] in expected_lines
                and any(box.contains(_span_rect(span)) for box in allowed)
            ]
            expected_count = len(boxes(item)) * len(expected_lines)
            if len(matches) != expected_count:
                raise EditorError(
                    "VALIDATION_FAILED",
                    f"Edited span cardinality mismatch for {item['text']!r}: expected {expected_count}, found {len(matches)}",
                    "Validation failed.",
                )
            if {span["text"] for span in matches} != set(expected_lines):
                raise EditorError(
                    "VALIDATION_FAILED",
                    f"Edited multiline span incomplete: {item['text']}",
                    "Validation failed.",
                )
            expected_counter = Counter(expected_lines)
            for box in allowed:
                box_counter = Counter(
                    span["text"] for span in matches
                    if box.contains(_span_rect(span))
                )
                if box_counter != expected_counter:
                    raise EditorError(
                        "VALIDATION_FAILED",
                        f"Edited multiline per-box Counter mismatch for {item['text']!r} "
                        f"in box {list(box)}: expected {dict(expected_counter)}, "
                        f"found {dict(box_counter)}",
                        "Validation failed.",
                    )
            for span in matches:
                expected_font = Path(replacement_font_ref(item)).stem
                if span["font"] != expected_font:
                    raise EditorError(
                        "VALIDATION_FAILED",
                        f"Edited span font mismatch for {span['text']!r}: expected {expected_font}, found {span['font']}",
                        "Validation failed.",
                    )
                if abs(span["size"] - item["size"]) > 0.06:
                    raise EditorError(
                        "VALIDATION_FAILED",
                        f"Edited span size mismatch for {span['text']!r}: expected {item['size']}, found {span['size']}",
                        "Validation failed.",
                    )
                expected_color = probe_span_color(resolve_color(item["color"]))
                if span["color"] != expected_color:
                    raise EditorError(
                        "VALIDATION_FAILED",
                        f"Edited span color mismatch for {span['text']!r}",
                        "Validation failed.",
                    )
        source_texts_set = {entry["source"] for entry in config["replacements"]}
        inserted_lines_set = set()
        for entry in config["replacements"]:
            if entry.get("text"):
                inserted_lines_set.update(entry["text"].splitlines())
        insertion_rects = []
        for entry in config["replacements"]:
            if entry.get("text") and ("box" in entry or "boxes" in entry):
                for box in boxes(entry):
                    insertion_rects.append(pymupdf.Rect(box))

        def _is_inserted(span):
            if span["text"] not in inserted_lines_set:
                return False
            rect = _span_rect(span)
            return any(host.contains(rect) for host in insertion_rects)

        source_unchanged = Counter(
            unchanged_span_key(span) for span in source_spans
            if span["text"] not in source_texts_set and span["text"].strip() != ""
        )
        output_unchanged = Counter(
            unchanged_span_key(span) for span in output_spans
            if span["text"].strip() != "" and not _is_inserted(span)
        )
        if source_unchanged != output_unchanged:
            raise EditorError(
                "VALIDATION_FAILED",
                "Unedited spans changed: "
                f"source {sum(source_unchanged.values())} vs "
                f"output {sum(output_unchanged.values())}",
                "Validation failed.",
            )
        before, after = source_metadata(source_document, source_page), source_metadata(output_document, page)
        if require_cmyk_group:
            if not before["group_cmyk"]:
                raise EditorError(
                    "VALIDATION_FAILED",
                    "Source page has no referenced DeviceCMYK transparency group",
                    "Validation failed.",
                )
            if not after["group_cmyk"]:
                raise EditorError(
                    "VALIDATION_FAILED",
                    "Output page lost the referenced DeviceCMYK transparency group",
                    "Validation failed.",
                )
        if before != after:
            raise EditorError(
                "VALIDATION_FAILED",
                "Preserved object mismatch",
                "Validation failed.",
            )
        if reject_rgb:
            for raw in page_content_streams(output_document, page):
                if contains_rgb_operators(raw):
                    raise EditorError(
                        "VALIDATION_FAILED",
                        "Output content uses RGB operators (rejected by policy)",
                        "Validation failed.",
                    )
        expected_stems = {
            Path(replacement_font_ref(entry)).stem for entry in config["replacements"]
            if entry.get("text")
        }
        if expected_stems:
            verify_new_fonts(output_document, output_path, expected_stems, require_external, external_timeout)
        run_external(["pdfimages", "-list", str(output_path)], require_external, timeout=external_timeout)
        run_external(["pdfinfo", str(output_path)], require_external, timeout=external_timeout)
        run_external(["pdftotext", "-layout", str(output_path), "-"], require_external, timeout=external_timeout)
        run_external(["gs", "-q", "-o", os.devnull, "-sDEVICE=nullpage", str(output_path)], require_external, timeout=external_timeout)
        run_external(["gs", "-q", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite", "-o", os.devnull, str(output_path)], require_external, timeout=external_timeout)
        visual_changes = render_diff(source_page, page, masks)
        result = {"images": after["image_count"], "masks": after["mask_count"], "drawings": len(after["drawings"]), "outside_mask_pixel_changes": visual_changes}
    except BaseException as primary:
        try:
            output_document.close()
        except Exception as secondary:
            try:
                primary.add_note(f"cleanup close failed: {secondary}")
            except Exception:
                pass
        raise
    try:
        output_document.close()
    except Exception as secondary:
        raise EditorError(
            "CLOSE_FAILED",
            f"Could not close output: {secondary}",
            "Processing failed.",
        ) from secondary
    return result


def _cli_error_info(error):
    """Map a typed exception to a stable, privacy-safe CLI code/message.

    Direct ``run()``/``validate()`` callers keep the detailed exception;
    only the CLI boundary uses this mapping so default stdout/stderr never
    carry absolute paths, source/replacement text or raw validator output.
    Typed :class:`EditorError` values carry their own stable code; any other
    exception maps to a generic internal code without inspecting its text.
    """
    if isinstance(error, EditorError):
        return (error.code, error.public_message)
    return ("INTERNAL_ERROR", "Processing failed.")


def within_root(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _check_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EditorError(
            "CONFIG_INVALID",
            f"Config error: {label} must be a number, found {value!r}",
            "Invalid configuration.",
        )
    number = float(value)
    if not math.isfinite(number):
        raise EditorError(
            "CONFIG_INVALID",
            f"Config error: {label} must be finite, found {value!r}",
            "Invalid configuration.",
        )
    return number


def _check_box(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise EditorError(
            "CONFIG_INVALID",
            f"Config error: {label} must be a list of 4 numbers, found {value!r}",
            "Invalid configuration.",
        )
    coords = [float(_check_number(entry, label)) for entry in value]
    x0, y0, x1, y1 = coords
    if not (x0 < x1 and y0 < y1):
        raise EditorError(
            "CONFIG_INVALID",
            f"Config error: {label} must satisfy x0 < x1 and y0 < y1, found {value!r}",
            "Invalid configuration.",
        )
    return coords


def _validate_replacement(item, index):
    label = f"replacements[{index}]"
    if not isinstance(item, dict):
        raise EditorError(
            "CONFIG_INVALID",
            f"Config error: {label} must be an object, found {item!r}",
            "Invalid configuration.",
        )
    for key in item:
        if key not in {"source", "count", "text", "box", "boxes", "font",
                       "fontfile", "size", "align", "color", "lineheight",
                       "rotate"}:
            raise EditorError(
                "CONFIG_INVALID",
                f"Config error: {label} has unknown key {key!r}",
                "Invalid configuration.",
            )
    if "text" not in item:
        raise EditorError(
            "CONFIG_INVALID",
            f"Config error: {label}.text must be an explicit string or null",
            "Invalid configuration.",
        )
    if not isinstance(item.get("source"), str) or not item["source"]:
        raise EditorError(
            "CONFIG_INVALID",
            f"Config error: {label}.source must be a non-empty string",
            "Invalid configuration.",
        )
    count = item.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise EditorError(
            "CONFIG_INVALID",
            f"Config error: {label}.count must be an integer >= 1",
            "Invalid configuration.",
        )
    text = item.get("text")
    if text is not None and not isinstance(text, str):
        raise EditorError(
            "CONFIG_INVALID",
            f"Config error: {label}.text must be a string or null",
            "Invalid configuration.",
        )
    if text == "":
        raise EditorError(
            "CONFIG_INVALID",
            f"Config error: {label}.text must be a non-empty string or null",
            "Invalid configuration.",
        )
    if text is None and "rotate" in item:
        raise EditorError(
            "CONFIG_INVALID",
            f"Config error: {label}.rotate is only valid with replacement text",
            "Invalid configuration.",
        )
    if "box" in item and "boxes" in item:
        raise EditorError(
            "CONFIG_INVALID",
            f"Config error: {label} must use either 'box' or 'boxes', not both",
            "Invalid configuration.",
        )
    configured = None
    if "boxes" in item:
        if not isinstance(item["boxes"], (list, tuple)) or not item["boxes"]:
            raise EditorError(
                "CONFIG_INVALID",
                f"Config error: {label}.boxes must be a non-empty list",
                "Invalid configuration.",
            )
        configured = [_check_box(entry, f"{label}.boxes") for entry in item["boxes"]]
    elif "box" in item:
        configured = [_check_box(item["box"], f"{label}.box")]
    if text and configured is None:
        raise EditorError(
            "CONFIG_INVALID",
            f"Config error: {label} with text needs 'box' or 'boxes'",
            "Invalid configuration.",
        )
    if text:
        replacement_font_ref(item)
        size = _check_number(item.get("size"), f"{label}.size")
        if size <= 0:
            raise EditorError(
                "CONFIG_INVALID",
                f"Config error: {label}.size must be > 0",
                "Invalid configuration.",
            )
        align = item.get("align", "left")
        if not isinstance(align, str) or align not in _ALIGNMENTS:
            raise EditorError(
                "CONFIG_INVALID",
                f"Config error: {label}.align must be one of {sorted(_ALIGNMENTS)}",
                "Invalid configuration.",
            )
        rotate = item.get("rotate", 0)
        if (
            isinstance(rotate, bool)
            or not isinstance(rotate, int)
            or rotate not in {0, 90, 180, 270}
        ):
            raise EditorError(
                "CONFIG_INVALID",
                f"Config error: {label}.rotate must be one of [0, 90, 180, 270]",
                "Invalid configuration.",
            )
        if "color" not in item:
            raise EditorError(
                "CONFIG_INVALID",
                f"Config error: {label}.color is required with text",
                "Invalid configuration.",
            )
        resolve_color(item["color"])
        if "lineheight" in item:
            lineheight = _check_number(item["lineheight"], f"{label}.lineheight")
            if lineheight <= 0:
                raise EditorError(
                    "CONFIG_INVALID",
                    f"Config error: {label}.lineheight must be > 0",
                    "Invalid configuration.",
                )


def _validate_config(config):
    if not isinstance(config, dict):
        raise EditorError(
            "CONFIG_INVALID",
            "Config error: the config root must be a JSON object",
            "Invalid configuration.",
        )
    for key in config:
        if key not in {"source", "source_sha256", "page_index", "output",
                       "fonts_dir", "replacements", "required_text",
                       "protected_spans", "printed_page", "overwrite",
                       "validation"}:
            raise EditorError(
                "CONFIG_INVALID",
                f"Config error: unknown top-level key {key!r}",
                "Invalid configuration.",
            )
    for key in ("source", "source_sha256", "page_index", "output", "fonts_dir",
                "replacements", "required_text", "protected_spans"):
        if key not in config:
            raise EditorError(
                "CONFIG_INVALID",
                f"Config error: missing required key {key!r}",
                "Invalid configuration.",
            )
    if not isinstance(config["source"], str) or not config["source"]:
        raise EditorError(
            "CONFIG_INVALID",
            "Config error: 'source' must be a non-empty path string",
            "Invalid configuration.",
        )
    if not isinstance(config["source_sha256"], str) or not re.fullmatch(r"[0-9a-fA-F]{64}", config["source_sha256"]):
        raise EditorError(
            "CONFIG_INVALID",
            "Config error: 'source_sha256' must be a 64-character hex string",
            "Invalid configuration.",
        )
    if isinstance(config["page_index"], bool) or not isinstance(config["page_index"], int) or config["page_index"] < 0:
        raise EditorError(
            "CONFIG_INVALID",
            "Config error: 'page_index' must be an integer >= 0",
            "Invalid configuration.",
        )
    if not isinstance(config["output"], str) or not config["output"]:
        raise EditorError(
            "CONFIG_INVALID",
            "Config error: 'output' must be a non-empty path string",
            "Invalid configuration.",
        )
    if not isinstance(config["fonts_dir"], str) or not config["fonts_dir"]:
        raise EditorError(
            "CONFIG_INVALID",
            "Config error: 'fonts_dir' must be a non-empty path string",
            "Invalid configuration.",
        )
    if not isinstance(config["replacements"], list):
        raise EditorError(
            "CONFIG_INVALID",
            "Config error: 'replacements' must be a list",
            "Invalid configuration.",
        )
    for index, item in enumerate(config["replacements"]):
        _validate_replacement(item, index)
    if not isinstance(config["required_text"], list) or any(not isinstance(entry, str) for entry in config["required_text"]):
        raise EditorError(
            "CONFIG_INVALID",
            "Config error: 'required_text' must be a list of strings",
            "Invalid configuration.",
        )
    if not isinstance(config["protected_spans"], list) or any(not isinstance(entry, str) for entry in config["protected_spans"]):
        raise EditorError(
            "CONFIG_INVALID",
            "Config error: 'protected_spans' must be a list of strings",
            "Invalid configuration.",
        )
    if "printed_page" in config and not isinstance(config["printed_page"], str):
        raise EditorError(
            "CONFIG_INVALID",
            "Config error: 'printed_page' must be a string",
            "Invalid configuration.",
        )
    if "overwrite" in config and not isinstance(config["overwrite"], bool):
        raise EditorError(
            "CONFIG_INVALID",
            "Config error: 'overwrite' must be a boolean",
            "Invalid configuration.",
        )
    validation = config.get("validation", {})
    if validation is None:
        validation = {}
    if not isinstance(validation, dict):
        raise EditorError(
            "CONFIG_INVALID",
            "Config error: 'validation' must be an object",
            "Invalid configuration.",
        )
    allowed_validation = {
        "require_cmyk_group", "reject_rgb", "require_external_validators",
        "external_timeout",
    }
    for key in validation:
        if key not in allowed_validation:
            raise EditorError(
                "CONFIG_INVALID",
                f"Config error: unknown validation key {key!r}",
                "Invalid configuration.",
            )
    for key in ("require_cmyk_group", "reject_rgb", "require_external_validators"):
        if key in validation and not isinstance(validation[key], bool):
            raise EditorError(
                "CONFIG_INVALID",
                f"Config error: 'validation.{key}' must be a boolean",
                "Invalid configuration.",
            )
    if "external_timeout" in validation:
        timeout_value = validation["external_timeout"]
        if isinstance(timeout_value, bool) or not isinstance(timeout_value, (int, float)):
            raise EditorError(
                "CONFIG_INVALID",
                "Config error: 'validation.external_timeout' must be a number > 0",
                "Invalid configuration.",
            )
        timeout_number = float(timeout_value)
        if not math.isfinite(timeout_number) or timeout_number <= 0:
            raise EditorError(
                "CONFIG_INVALID",
                "Config error: 'validation.external_timeout' must be a number > 0",
                "Invalid configuration.",
            )
    sources = [item.get("source") for item in config["replacements"]
               if isinstance(item, dict)]
    if len(sources) != len(set(sources)):
        raise EditorError(
            "CONFIG_INVALID",
            "Config error: duplicate replacement 'source' entries",
            "Invalid configuration.",
        )
    destination_rects = []
    for item in config["replacements"]:
        if isinstance(item, dict) and ("box" in item or "boxes" in item):
            for entry in boxes(item):
                destination_rects.append(pymupdf.Rect(entry))
    for left in range(len(destination_rects)):
        for right in range(left + 1, len(destination_rects)):
            if destination_rects[left].intersects(destination_rects[right]):
                raise EditorError(
                    "CONFIG_INVALID",
                    "Config error: overlapping destination boxes",
                    "Invalid configuration.",
                )


def _resolve_workspace_path(raw, workspace):
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    return workspace / candidate


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EditorError(
                "CONFIG_INVALID",
                f"Config error: duplicate key {key!r}",
                "Invalid configuration.",
            )
        result[key] = value
    return result


def run(config_path):
    started = time.monotonic()
    config_path = Path(config_path).expanduser()
    if not config_path.is_absolute():
        config_path = (Path.cwd() / config_path).resolve()
    else:
        config_path = config_path.resolve()
    if not config_path.is_file():
        raise EditorError(
            "CONFIG_NOT_FOUND",
            f"Config file not found: {config_path}",
            "Config file not found.",
        )
    workspace = config_path.parent.resolve()
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as error:
        raise EditorError(
            "CONFIG_NOT_FOUND",
            f"Config file not readable: {config_path}: {error}",
            "Config file not found.",
        )
    try:
        config = json.loads(raw_text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as error:
        raise EditorError(
            "CONFIG_INVALID",
            f"Config error: invalid JSON in {config_path}: {error}",
            "Invalid configuration.",
        )
    _validate_config(config)
    source_path = _resolve_workspace_path(config["source"], workspace).resolve()
    output_path = _resolve_workspace_path(config["output"], workspace).resolve()
    fonts_dir = _resolve_workspace_path(config["fonts_dir"], workspace).resolve()
    if not within_root(source_path, workspace):
        raise EditorError(
            "CONFIG_INVALID",
            f"Source outside write root: {config['source']}",
            "Invalid configuration.",
        )
    if not within_root(fonts_dir, workspace):
        raise EditorError(
            "CONFIG_INVALID",
            f"Fonts directory outside write root: {config['fonts_dir']}",
            "Invalid configuration.",
        )
    if not within_root(output_path, workspace):
        raise EditorError(
            "OUTPUT_INVALID",
            "Output path outside write root",
            "Invalid output path.",
        )
    if output_path == workspace:
        raise EditorError(
            "OUTPUT_INVALID",
            "Output must be a file inside the write root",
            "Invalid output path.",
        )
    if output_path.exists() and output_path.is_dir():
        raise EditorError(
            "OUTPUT_INVALID",
            "Output path is a directory",
            "Invalid output path.",
        )
    if source_path == output_path:
        raise EditorError(
            "OUTPUT_INVALID",
            "Output must not overwrite the source",
            "Invalid output path.",
        )
    if config_path == output_path:
        raise EditorError(
            "OUTPUT_INVALID",
            "Output must not overwrite the config",
            "Invalid output path.",
        )
    if output_path.exists() and not bool(config.get("overwrite", False)):
        raise EditorError(
            "OUTPUT_INVALID",
            "Output already exists without explicit overwrite opt-in",
            "Invalid output path.",
        )
    if not source_path.is_file():
        raise EditorError(
            "SOURCE_NOT_FOUND",
            f"Source file not found: {source_path}",
            "Source file not found.",
        )
    try:
        source_bytes = source_path.read_bytes()
    except OSError as error:
        raise EditorError(
            "SOURCE_INVALID",
            f"Source file not readable: {error}",
            "Processing failed.",
        )
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    if source_hash != config["source_sha256"].lower():
        raise EditorError(
            "SOURCE_HASH_MISMATCH",
            "Source SHA-256 does not match the configuration",
            "Source hash mismatch.",
        )
    # NOTE: standard PyMuPDF text metrics are used deliberately and no global
    # TOOLS flag is toggled here. Probing a box and inserting into it must see
    # identical metrics however many edits already ran in this process.
    # Exact validated bytes are processed: both documents open from the same
    # in-memory snapshot so a source swap after the hash check cannot slip in.
    try:
        source = pymupdf.open(stream=bytes(source_bytes), filetype="pdf")
    except EditorError:
        raise
    except Exception as error:
        raise EditorError(
            "SOURCE_INVALID",
            f"Could not open source document: {error}",
            "Processing failed.",
        )
    document = None
    temporary = None
    try:
        if config["page_index"] >= len(source):
            raise EditorError(
                "CONFIG_INVALID",
                f"Config error: page_index {config['page_index']} out of range "
                f"for {len(source)} pages",
                "Invalid configuration.",
            )
        source_page = source[config["page_index"]]
        if "printed_page" in config:
            lines = source_page.get_text().splitlines()
            first_line = lines[0] if lines else ""
            if first_line != config["printed_page"]:
                raise EditorError(
                    "VALIDATION_FAILED",
                    "Configured source page is not the expected printed page",
                    "Validation failed.",
                )
        try:
            document = pymupdf.open(stream=bytes(source_bytes), filetype="pdf")
        except EditorError:
            raise
        except Exception as error:
            raise EditorError(
                "SOURCE_INVALID",
                f"Could not open source document: {error}",
                "Processing failed.",
            )
        document.select([config["page_index"]])
        page = document[0]
        original_spans = spans(page)
        masks = []
        for item in config["replacements"]:
            matches = [span for span in original_spans if span["text"] == item["source"]]
            if len(matches) != item["count"]:
                raise EditorError(
                    "VALIDATION_FAILED",
                    f"Expected {item['count']} spans for {item['source']!r}, found {len(matches)}",
                    "Validation failed.",
                )
            unique_source_boxes = {tuple(round(value, 4) for value in span["bbox"]) for span in matches}
            if len(unique_source_boxes) != len(matches):
                raise EditorError(
                    "VALIDATION_FAILED",
                    f"Ambiguous duplicate source spans for {item['source']!r}: "
                    f"{len(matches)} spans share {len(unique_source_boxes)} unique boxes",
                    "Validation failed.",
                )
            if "box" in item or "boxes" in item:
                unique_boxes = {tuple(round(value, 4) for value in span["bbox"]) for span in matches}
                configured_boxes = boxes(item)
                if len(configured_boxes) != len(unique_boxes):
                    raise EditorError(
                        "VALIDATION_FAILED",
                        f"Box cardinality mismatch for {item['source']!r}: "
                        f"expected {len(unique_boxes)} unique source boxes, "
                        f"found {len(configured_boxes)} configured boxes",
                        "Validation failed.",
                    )
                unique_rects = [pymupdf.Rect(list(entry)) for entry in unique_boxes]
                configured_rects = [pymupdf.Rect(entry) for entry in configured_boxes]
                for source_rect in unique_rects:
                    if not any(source_rect.intersects(target) for target in configured_rects):
                        raise EditorError(
                            "VALIDATION_FAILED",
                            f"Box spatial mismatch for {item['source']!r}: "
                            f"source box {list(source_rect)} has no intersecting configured box",
                            "Validation failed.",
                        )
                for target in configured_rects:
                    if not any(target.intersects(source_rect) for source_rect in unique_rects):
                        raise EditorError(
                            "VALIDATION_FAILED",
                            f"Box spatial mismatch for {item['source']!r}: "
                            f"configured box {list(target)} has no intersecting source box",
                            "Validation failed.",
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
                raise EditorError(
                    "FONT_UNAVAILABLE",
                    f"Font outside write root: {replacement_font_ref(item)}",
                    "Font file not available.",
                )
            if not font_path.is_file():
                raise EditorError(
                    "FONT_UNAVAILABLE",
                    f"Font file not found: {font_path}",
                    "Font file not available.",
                )
            for box in boxes(item):
                masks.append(box)
                result = page.insert_textbox(pymupdf.Rect(box), item["text"], fontname=Path(replacement_font_ref(item)).stem,
                                             fontfile=str(font_path), fontsize=item["size"],
                                             lineheight=item.get("lineheight", 1), color=resolve_color(item["color"]),
                                             align=_ALIGNMENTS[item.get("align", "left")],
                                             rotate=item.get("rotate", 0), overlay=True)
                if result < 0:
                    raise EditorError(
                        "VALIDATION_FAILED",
                        f"Edited text does not fit {item['text']!r}: {result:.2f}",
                        "Validation failed.",
                    )
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise EditorError(
                "OUTPUT_INVALID",
                f"Could not create output directory: {error}",
                "Invalid output path.",
            )
        try:
            with tempfile.NamedTemporaryFile(dir=output_path.parent, prefix=".pdf-edit-", suffix=".pdf", delete=False) as handle:
                temporary = Path(handle.name)
        except EditorError:
            raise
        except Exception as error:
            raise EditorError(
                "SAVE_FAILED",
                f"Could not stage output: {error}",
                "Processing failed.",
            )
        try:
            document.save(temporary, garbage=4, deflate=True)
        except EditorError:
            raise
        except Exception as error:
            raise EditorError(
                "SAVE_FAILED",
                f"Could not save output: {error}",
                "Processing failed.",
            )
        try:
            document.close()
        except EditorError:
            raise
        except Exception as error:
            raise EditorError(
                "CLOSE_FAILED",
                f"Could not close document: {error}",
                "Processing failed.",
            )
        finally:
            document = None
        validation = validate(config, source, source_page, temporary, masks)
        try:
            source.close()
        except EditorError:
            raise
        except Exception as error:
            raise EditorError(
                "CLOSE_FAILED",
                f"Could not close source: {error}",
                "Processing failed.",
            )
        source = None
        overwrite = bool(config.get("overwrite", False))
        if not overwrite:
            # Single-operation no-clobber publish: link fails if the
            # destination exists and never overwrites it. Staging lives in
            # the same directory, so the same filesystem applies.
            try:
                os.link(temporary, output_path)
            except FileExistsError:
                raise EditorError(
                    "OUTPUT_INVALID",
                    "Output already exists without explicit overwrite opt-in",
                    "Invalid output path.",
                )
            except OSError as error:
                raise EditorError(
                    "PUBLISH_FAILED",
                    f"Could not publish output: {error}",
                    "Processing failed.",
                )
            try:
                temporary.unlink()
            except OSError as error:
                raise EditorError(
                    "CLOSE_FAILED",
                    f"Could not clean staging file: {error}",
                    "Processing failed.",
                )
            temporary = None
        else:
            try:
                os.replace(temporary, output_path)
            except OSError as error:
                raise EditorError(
                    "PUBLISH_FAILED",
                    f"Could not publish output: {error}",
                    "Processing failed.",
                )
            temporary = None
        try:
            relative_output = output_path.relative_to(workspace).as_posix()
        except ValueError:
            relative_output = output_path.name
        return {"status": "complete", "code": "COMPLETE", "output": relative_output, "duration_seconds": round(time.monotonic() - started, 3), "validation": validation}
    finally:
        primary = sys.exception()
        if document is not None:
            try:
                document.close()
            except Exception as secondary:
                if primary is None:
                    raise EditorError(
                        "CLOSE_FAILED",
                        f"Could not close document: {secondary}",
                        "Processing failed.",
                    )
                try:
                    primary.add_note(f"cleanup document close failed: {secondary}")
                except Exception:
                    pass
        if source is not None:
            try:
                source.close()
            except Exception as secondary:
                if primary is None:
                    raise EditorError(
                        "CLOSE_FAILED",
                        f"Could not close source: {secondary}",
                        "Processing failed.",
                    )
                try:
                    primary.add_note(f"cleanup source close failed: {secondary}")
                except Exception:
                    pass
        if temporary is not None:
            try:
                if temporary.exists():
                    temporary.unlink()
            except Exception as secondary:
                if primary is None:
                    raise EditorError(
                        "CLOSE_FAILED",
                        f"Could not clean staging file: {secondary}",
                        "Processing failed.",
                    )
                try:
                    primary.add_note(f"cleanup staging unlink failed: {secondary}")
                except Exception:
                    pass


if __name__ == "__main__":
    _debug = "--debug" in sys.argv[1:]
    _args = [arg for arg in sys.argv[1:] if arg != "--debug"]
    try:
        if len(_args) != 1 or not _args[0]:
            raise EditorError(
                "MISSING_CONFIG_ARG",
                "Missing explicit config path argument (usage: edit_pdf.py <config.json> [--debug])",
                "Missing explicit config path argument.",
            )
        print(json.dumps(run(_args[0]), ensure_ascii=False, sort_keys=True))
    except Exception as error:
        _code, _safe = _cli_error_info(error)
        _payload = {"status": "partial", "code": _code, "error": _safe}
        if _debug:
            _payload["detail"] = str(error)
        print(json.dumps(_payload, ensure_ascii=False, sort_keys=True))
        sys.exit(1)
