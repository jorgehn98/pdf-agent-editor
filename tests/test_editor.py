"""Public synthetic contracts for the one-page PDF editor.

Minimal public interface under test (module ``edit_pdf`` at the worktree root):

- ``contains_rgb_operators(raw: bytes | str) -> bool``
- ``drawing_signature(page)``, ``image_signature(page)``, ``spans(page)``
- ``boxes(item)`` (``box`` singular or ``boxes`` plural)
- ``cmyk(value)`` (any neutral gray maps to K-only CMYK; chromatic raises)
- ``unchanged_span_key(span: dict) -> hashable``
- ``source_metadata(document, page)``, ``validate(...)``, ``run(config_path)``
- CLI ``python edit_pdf.py <config.json>`` requires an explicit config path
  and reports JSON ``{"status": ...}`` on stdout.
- ``validate(...)`` honors ``validation.require_cmyk_group`` (default False,
  so generic pages need no transparency group).

All fixtures (source PDFs, tiny images, configs, outputs) live in
``tempfile.TemporaryDirectory`` and are cleaned automatically. The only
external font file used is the public asset
``examples/synthetic/assets/Barlow-Regular.ttf``.
No test reads checked-in PDFs, client font files or client configuration paths.
"""
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

WORKTREE_ROOT = Path(__file__).resolve().parent.parent
EDIT_PDF = WORKTREE_ROOT / "edit_pdf.py"

sys.path.insert(0, str(WORKTREE_ROOT))

import edit_pdf  # noqa: E402
import pymupdf  # noqa: E402

PUBLIC_FONT_ASSET = (
    WORKTREE_ROOT / "examples" / "synthetic" / "assets" / "Barlow-Regular.ttf"
)
PUBLIC_FONT_NAME = "Barlow-Regular"
PUBLIC_FONT_FILE = "Barlow-Regular.ttf"


def write_tiny_png(path):
    """Write a 1x1 red PNG with stdlib only."""
    raw = b"\x00\xff\x00\x00"  # filter byte 0 + one red pixel
    compressed = zlib.compress(raw)

    def chunk(chunk_type, data):
        body = struct.pack(">I", len(data)) + chunk_type + data
        body += struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
        return body

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )
    Path(path).write_bytes(png)


def sha256_of(path):
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def run_cli(*args, cwd=None):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    completed = subprocess.run(
        [sys.executable, str(EDIT_PDF), *args],
        capture_output=True,
        text=True,
        cwd=cwd or str(WORKTREE_ROOT),
        env=env,
        timeout=120,
    )
    return completed


class TestRgbOperators(unittest.TestCase):
    def test_detects_all_pdf_whitespace_and_device_rgb(self):
        positives = [
            b"0 0 1 rg",
            b"0 0 1\nrg",
            b"0 0 1\rrg",
            b"0 0 1\trg",
            b"0 0 1\x0crg",
            b"0 0 1\x00rg",
            b"0 0 1 RG",
            b"0 0 1\nRG",
            b"/DeviceRGB cs",
            b"/DeviceRGB CS",
            b"/DeviceRGB\ncs",
            b"q\n0 0 1 rg\n1 0 0 re f\nQ",
            "0 0 1 rg",
        ]
        for raw in positives:
            with self.subTest(raw=raw):
                self.assertTrue(edit_pdf.contains_rgb_operators(raw))

        negatives = [
            b"",
            b"0 0 0 1 k",
            b"0 0 0 1 K",
            b"0 g",
            b"0 G",
            b"/DeviceCMYK cs",
            b"/DeviceGray cs",
            b"BT /F1 12 Tf (Hello) Tj ET",
        ]
        for raw in negatives:
            with self.subTest(raw=raw):
                self.assertFalse(edit_pdf.contains_rgb_operators(raw))

    def test_respects_pdf_delimiters(self):
        positives = [
            b"0 0 1 rg/Pattern",
            b"0 0 1 RG%comment",
            b"0 0 1 rg(R)",
            b"0 0 1 rg[0 0 1]",
            b"/DeviceRGB cs%comment",
        ]
        for raw in positives:
            with self.subTest(raw=raw):
                self.assertTrue(edit_pdf.contains_rgb_operators(raw))

        negatives = [b"xrg", b"rgx", b"/rg", b"0 0 1 rgx"]
        for raw in negatives:
            with self.subTest(raw=raw):
                self.assertFalse(edit_pdf.contains_rgb_operators(raw))


class _FakePage:
    def __init__(self, drawings):
        self._drawings = drawings

    def get_drawings(self):
        return self._drawings


def _signature_with_shape(**finish_kwargs):
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=200, height=200)
        shape = page.new_shape()
        shape.draw_rect(pymupdf.Rect(10, 10, 50, 50))
        shape.finish(color=(1, 0, 0), fill=(0, 1, 0), width=1, **finish_kwargs)
        shape.commit()
        return edit_pdf.drawing_signature(page)
    finally:
        doc.close()


def _signature_with_layer(layer_name=None):
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=200, height=200)
        oc = doc.add_ocg(layer_name, on=True) if layer_name else 0
        page.draw_rect(
            pymupdf.Rect(10, 10, 50, 50),
            color=(1, 0, 0),
            fill=(0, 1, 0),
            width=1,
            oc=oc,
        )
        return edit_pdf.drawing_signature(page)
    finally:
        doc.close()


class TestDrawingSignature(unittest.TestCase):
    def test_renderable_properties_change_signature_and_seqno_ignored(self):
        base = _signature_with_shape()
        for name, kwargs in [
            ("fill_opacity", {"fill_opacity": 0.5}),
            ("stroke_opacity", {"stroke_opacity": 0.5}),
            ("lineCap", {"lineCap": 1}),
            ("lineJoin", {"lineJoin": 1}),
        ]:
            with self.subTest(prop=name):
                self.assertNotEqual(base, _signature_with_shape(**kwargs))
        with self.subTest(prop="layer"):
            self.assertNotEqual(_signature_with_layer(), _signature_with_layer("L1"))

        template = {
            "rect": pymupdf.Rect(10, 10, 50, 50),
            "type": "fs",
            "color": (1, 0, 0),
            "fill": (0, 1, 0),
            "width": 1.0,
            "dashes": "[] 0",
            "closePath": False,
            "even_odd": False,
            "items": [("re", pymupdf.Rect(10, 10, 50, 50), 1)],
            "seqno": 0,
            "layer": "",
            "lineCap": (0, 0, 0),
            "lineJoin": 0.0,
            "fill_opacity": 1.0,
            "stroke_opacity": 1.0,
        }
        with self.subTest(prop="re-orientation"):
            flipped = dict(template, items=[("re", pymupdf.Rect(10, 10, 50, 50), -1)])
            self.assertNotEqual(
                edit_pdf.drawing_signature(_FakePage([template])),
                edit_pdf.drawing_signature(_FakePage([flipped])),
            )
        with self.subTest(prop="seqno-ignored"):
            bumped = dict(template, seqno=99)
            self.assertEqual(
                edit_pdf.drawing_signature(_FakePage([template])),
                edit_pdf.drawing_signature(_FakePage([bumped])),
            )


class TestUnchangedSpanKey(unittest.TestCase):
    def _base_span(self):
        return {
            "text": "Hello",
            "bbox": (10.0, 20.0, 60.0, 30.0),
            "font": "SyntheticSans-Regular",
            "size": 10.0,
            "color": 0x112233,
            "alpha": 255,
            "flags": 4,
            "char_flags": 16,
            "bidi": 0,
            "ascender": 1.0,
            "descender": -0.2,
            "origin": (10.0, 28.0),
        }

    def test_identical_spans_have_equal_keys(self):
        self.assertEqual(
            edit_pdf.unchanged_span_key(self._base_span()),
            edit_pdf.unchanged_span_key(self._base_span()),
        )

    def test_single_field_mutations_change_key(self):
        base = self._base_span()
        for field, value in [
            ("text", "Hallo"),
            ("font", "OtherSans-Regular"),
            ("size", 11.0),
            ("color", 0x445566),
            ("alpha", 0),
            ("flags", 5),
            ("char_flags", 0),
            ("bidi", 1),
            ("ascender", 1.5),
            ("descender", -0.5),
            ("origin", (11.0, 28.0)),
            ("bbox", (11.0, 20.0, 60.0, 30.0)),
        ]:
            with self.subTest(field=field):
                mutated = dict(base, **{field: value})
                self.assertNotEqual(
                    edit_pdf.unchanged_span_key(base),
                    edit_pdf.unchanged_span_key(mutated),
                )

    def test_small_numeric_noise_tolerated(self):
        base = self._base_span()
        for field, value in [
            ("bbox", (10.0003, 20.0003, 60.0003, 30.0003)),
            ("origin", (10.0003, 28.0003)),
            ("size", 10.0003),
            ("ascender", 1.0003),
            ("descender", -0.2003),
        ]:
            with self.subTest(field=field):
                noisy = dict(base, **{field: value})
                self.assertEqual(
                    edit_pdf.unchanged_span_key(base),
                    edit_pdf.unchanged_span_key(noisy),
                )

    def test_missing_fields_raise_without_silent_defaults(self):
        required = [
            "text", "bbox", "font", "size", "color", "alpha", "flags",
            "char_flags", "bidi", "ascender", "descender", "origin",
        ]
        for field in required:
            with self.subTest(field=field):
                incomplete = dict(self._base_span())
                del incomplete[field]
                with self.assertRaises(
                    (KeyError, ValueError, TypeError, RuntimeError, AttributeError)
                ):
                    edit_pdf.unchanged_span_key(incomplete)


class TestBoxesHelper(unittest.TestCase):
    def test_box_singular_and_boxes_plural(self):
        self.assertEqual(edit_pdf.boxes({"box": [1, 2, 3, 4]}), [[1, 2, 3, 4]])
        self.assertEqual(
            edit_pdf.boxes({"boxes": [[1, 2, 3, 4], [5, 6, 7, 8]]}),
            [[1, 2, 3, 4], [5, 6, 7, 8]],
        )


class TestColorPolicy(unittest.TestCase):
    def test_documented_k_only_colors_still_map(self):
        self.assertEqual(edit_pdf.cmyk("#231f20"), (0, 0, 0, 1))
        self.assertEqual(edit_pdf.cmyk("#58595b"), (0, 0, 0, 0.8))

    def test_generic_k_only_black_is_accepted(self):
        """Any neutral gray maps to K-only CMYK, not just two hex codes."""
        self.assertEqual(edit_pdf.cmyk("#000000"), (0, 0, 0, 1))

    def test_true_rgb_is_rejected(self):
        with self.assertRaises(RuntimeError):
            edit_pdf.cmyk("#ff0000")


class TestExplicitConfigCli(unittest.TestCase):
    def test_explicit_missing_config_reports_partial_json(self):
        """Control: the CLI harness itself works (explicit path, JSON, exit 1)."""
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            missing = str(Path(tmpdir) / "does-not-exist.json")
            completed = run_cli(missing)
        self.assertNotEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout.strip())
        self.assertEqual(payload.get("status"), "partial")
        self.assertTrue(payload.get("error"))

    def test_no_arg_demands_explicit_config(self):
        """Running without a config must demand an explicit path."""
        completed = run_cli()
        self.assertNotEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout.strip())
        self.assertEqual(payload.get("status"), "partial")
        self.assertRegex(
            payload.get("error", ""),
            r"(?i)(explicit|required|usage|missing.*arg)",
        )


class TestGenericPageGeometry(unittest.TestCase):
    def test_matching_letter_geometry_passes_without_cmyk_group(self):
        """Generic pages validate fully with no transparency group by default.

        The opt-in gate is checked once here with an explicit
        ``validation.require_cmyk_group: true`` config.
        """
        src = pymupdf.open()
        out = pymupdf.open()
        try:
            src.new_page(width=612, height=792)
            out.new_page(width=612, height=792)
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
                tmp = Path(handle.name)
            out.save(tmp)
            config = {"required_text": [], "protected_spans": [], "replacements": []}
            result = edit_pdf.validate(config, src, src[0], tmp, [])
            self.assertEqual(result["outside_mask_pixel_changes"], 0)
            gated = dict(config, validation={"require_cmyk_group": True})
            with self.assertRaisesRegex(RuntimeError, r"(?i)(DeviceCMYK|transparency)"):
                edit_pdf.validate(gated, src, src[0], tmp, [])
        finally:
            src.close()
            out.close()
            tmp.unlink(missing_ok=True)

    def test_geometry_error_is_generic_without_fixed_size_name(self):
        """The size error must not name one hardcoded page size."""
        src = pymupdf.open()
        out = pymupdf.open()
        try:
            src.new_page(width=400, height=600)
            out.new_page(width=300, height=400)
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
                tmp = Path(handle.name)
            out.save(tmp)
            config = {"required_text": [], "protected_spans": [], "replacements": []}
            with self.assertRaises(RuntimeError) as ctx:
                edit_pdf.validate(config, src, src[0], tmp, [])
            self.assertNotIn("A5", str(ctx.exception))
        finally:
            src.close()
            out.close()
            tmp.unlink(missing_ok=True)


class TestMultilinePerBox(unittest.TestCase):
    def _validate_with_texts(self, box_texts):
        config = {
            "required_text": [],
            "protected_spans": [],
            "replacements": [
                {
                    "source": "SRC",
                    "text": "AAA\nBBB",
                    "boxes": [[50, 50, 200, 100], [50, 150, 200, 200]],
                    "font": f"{PUBLIC_FONT_NAME}.ttf",
                    "size": 10,
                    "align": "left",
                    "color": "#231f20",
                }
            ],
        }
        color = edit_pdf.cmyk("#231f20")
        src_doc = pymupdf.open()
        src_doc.new_page(width=420, height=595)
        out_doc = pymupdf.open()
        try:
            out_page = out_doc.new_page(width=420, height=595)
            for rect, text in box_texts:
                out_page.insert_textbox(
                    pymupdf.Rect(rect),
                    text,
                    fontname=PUBLIC_FONT_NAME,
                    fontfile=str(PUBLIC_FONT_ASSET),
                    fontsize=10,
                    align=0,
                    overlay=True,
                    color=color,
                )
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
                tmp = Path(handle.name)
            out_doc.save(tmp)
        finally:
            out_doc.close()
        self.addCleanup(lambda: tmp.unlink(missing_ok=True))
        self.addCleanup(src_doc.close)
        return edit_pdf.validate(config, src_doc, src_doc[0], tmp, [])

    def test_correct_distribution_passes_validations_without_cmyk_group(self):
        """Correct per-box distribution reaches render validation without a CMYK gate.

        Masks are empty in this direct-``validate`` harness, so the flow stops
        at the render stage: reaching it proves geometry, text, per-box, font,
        size, color, unedited-span and preserved-object checks passed.
        """
        with self.assertRaisesRegex(RuntimeError, r"(?i)outside edit masks"):
            self._validate_with_texts(
                [([50, 50, 200, 100], "AAA\nBBB"), ([50, 150, 200, 200], "AAA\nBBB")]
            )

    def test_per_box_distribution_enforced_with_multiplicity(self):
        for name, box_texts in [
            ("per-box-incomplete", [([50, 50, 200, 100], "AAA\nAAA"), ([50, 150, 200, 200], "BBB\nBBB")]),
            ("repeated-line", [([50, 50, 200, 100], "AAA\nAAA"), ([50, 150, 200, 200], "AAA\nBBB")]),
        ]:
            with self.subTest(case=name):
                with self.assertRaisesRegex(
                    RuntimeError, r"(?i)(box|counter|multiline|cardinality|multiplicity)"
                ):
                    self._validate_with_texts(box_texts)


def _write_preserved_pair(tmpdir, *, same):
    """Two synthetic single-page PDFs with text, an image, and drawings."""
    src_path = Path(tmpdir) / "source.pdf"
    out_path = Path(tmpdir) / "output.pdf"
    png_path = Path(tmpdir) / "tiny.png"
    write_tiny_png(png_path)
    for target, extra_rect in [(src_path, None), (out_path, None if same else True)]:
        doc = pymupdf.open()
        try:
            page = doc.new_page(width=400, height=600)
            page.insert_textbox(
                pymupdf.Rect(50, 100, 350, 140),
                "STEADY",
                fontname="helv",
                fontsize=12,
                align=0,
                overlay=True,
            )
            page.draw_rect(pymupdf.Rect(30, 30, 120, 80), color=(0, 0, 0), width=1)
            page.insert_image(pymupdf.Rect(10, 10, 30, 30), filename=str(png_path))
            if extra_rect:
                page.draw_rect(pymupdf.Rect(200, 200, 260, 240), color=(0, 0, 0), width=2)
            doc.save(target)
        finally:
            doc.close()
    return src_path, out_path


class TestPreservedObjectsWithoutCmykGate(unittest.TestCase):
    def test_identical_synthetic_pages_validate_without_cmyk_group(self):
        """Generic pages without a transparency group validate."""
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            src_path, out_path = _write_preserved_pair(tmpdir, same=True)
            src_doc = pymupdf.open(src_path)
            self.addCleanup(src_doc.close)
            config = {"required_text": [], "protected_spans": [], "replacements": []}
            edit_pdf.validate(config, src_doc, src_doc[0], out_path, [])

    def test_changed_drawings_report_preserved_mismatch(self):
        """Drawing changes report a preserved-object mismatch."""
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            src_path, out_path = _write_preserved_pair(tmpdir, same=False)
            src_doc = pymupdf.open(src_path)
            self.addCleanup(src_doc.close)
            config = {"required_text": [], "protected_spans": [], "replacements": []}
            with self.assertRaisesRegex(RuntimeError, r"(?i)preserved object mismatch"):
                edit_pdf.validate(config, src_doc, src_doc[0], out_path, [])


def _write_full_run_fixtures(tmpdir):
    """All run() fixtures inside one temporary directory."""
    tmpdir = Path(tmpdir)
    fonts_dir = tmpdir / "fonts"
    fonts_dir.mkdir()
    shutil.copy(PUBLIC_FONT_ASSET, fonts_dir / PUBLIC_FONT_FILE)
    write_tiny_png(tmpdir / "tiny.png")

    source_path = tmpdir / "source.pdf"
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=400, height=600)
        page.insert_image(pymupdf.Rect(10, 10, 30, 30), filename=str(tmpdir / "tiny.png"))
        page.draw_rect(pymupdf.Rect(40, 40, 120, 80), color=(0, 0, 0), width=1)
        page.insert_textbox(
            pymupdf.Rect(50, 200, 350, 240),
            "SOURCE_HELLO",
            fontname="helv",
            fontsize=14,
            align=0,
            overlay=True,
        )
        doc.save(source_path)
    finally:
        doc.close()

    probe = pymupdf.open(source_path)
    try:
        target = next(
            span for span in edit_pdf.spans(probe[0]) if span["text"] == "SOURCE_HELLO"
        )
        box = [float(value) for value in target["bbox"]]
        printed = probe[0].get_text().splitlines()[0]
    finally:
        probe.close()

    output_path = tmpdir / "output.pdf"
    config = {
        "source": str(source_path),
        "source_sha256": sha256_of(source_path),
        "page_index": 0,
        "printed_page": printed,
        "output": str(output_path),
        "fonts_dir": str(fonts_dir),
        "replacements": [
            {
                "source": "SOURCE_HELLO",
                "text": "BONJOUR",
                "count": 1,
                "box": box,
                "font": PUBLIC_FONT_FILE,
                "size": 14,
                "align": "left",
                "color": "#231f20",
            }
        ],
        "required_text": ["BONJOUR"],
        "protected_spans": [],
    }
    config_path = tmpdir / "config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return config_path, output_path


class TestSyntheticTempRun(unittest.TestCase):
    def test_full_run_inside_temporary_directory(self):
        """Config/source/output/fonts fully in temp dirs publish."""
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            config_path, output_path = _write_full_run_fixtures(tmpdir)
            result = edit_pdf.run(config_path)
            self.assertEqual(result.get("status"), "complete")
            self.assertTrue(Path(output_path).exists())
            probe = pymupdf.open(output_path)
            try:
                self.assertEqual((probe[0].rect.width, probe[0].rect.height), (400, 600))
                self.assertIn("BONJOUR", probe[0].get_text())
            finally:
                probe.close()

    def test_rotated_replacement_preserves_each_supported_direction(self):
        """Every supported rotation is observable in the published PDF."""
        expected_directions = {
            90: (0.0, -1.0),
            180: (-1.0, 0.0),
            270: (0.0, 1.0),
        }
        for rotate, expected_direction in expected_directions.items():
            with self.subTest(rotate=rotate):
                with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
                    config_path, output_path = _write_full_run_fixtures(tmpdir)
                    config = json.loads(config_path.read_text(encoding="utf-8"))
                    config["replacements"][0]["rotate"] = rotate
                    config["replacements"][0]["box"] = [40, 180, 360, 360]
                    config_path.write_text(json.dumps(config), encoding="utf-8")

                    result = edit_pdf.run(config_path)

                    self.assertEqual(result.get("status"), "complete")
                    probe = pymupdf.open(output_path)
                    try:
                        line = next(
                            line
                            for block in probe[0].get_text("dict")["blocks"]
                            for line in block.get("lines", [])
                            if any(span["text"] == "BONJOUR" for span in line["spans"])
                        )
                        self.assertEqual(tuple(line["dir"]), expected_direction)
                    finally:
                        probe.close()


class TestRelativeSourceResolution(unittest.TestCase):
    def test_relative_source_resolves_against_config_dir(self):
        """A relative source resolves from the config dir, not CWD."""
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            config_path, output_path = _write_full_run_fixtures(tmpdir)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["source"] = "source.pdf"
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            with tempfile.TemporaryDirectory(prefix="public-editor-cwd-") as elsewhere:
                previous = os.getcwd()
                try:
                    os.chdir(elsewhere)
                    self.assertFalse(Path("source.pdf").exists())
                    result = edit_pdf.run(config_path)
                finally:
                    os.chdir(previous)
            self.assertEqual(result.get("status"), "complete")
            self.assertTrue(Path(output_path).exists())


class TestCardinalityByUniqueBbox(unittest.TestCase):
    def test_single_box_for_two_unique_bboxes_rejected(self):
        """One configured box cannot cover two unique source bboxes."""
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            tmpdir = Path(tmpdir)
            source_path = tmpdir / "source.pdf"
            doc = pymupdf.open()
            try:
                page = doc.new_page(width=400, height=600)
                page.insert_textbox(
                    pymupdf.Rect(50, 100, 350, 130), "DUP",
                    fontname="helv", fontsize=12, align=0, overlay=True,
                )
                page.insert_textbox(
                    pymupdf.Rect(50, 300, 350, 330), "DUP",
                    fontname="helv", fontsize=12, align=0, overlay=True,
                )
                doc.save(source_path)
            finally:
                doc.close()
            probe = pymupdf.open(source_path)
            try:
                matches = [s for s in edit_pdf.spans(probe[0]) if s["text"] == "DUP"]
                self.assertEqual(len(matches), 2)
                first_box = [float(v) for v in matches[0]["bbox"]]
                printed = probe[0].get_text().splitlines()[0]
            finally:
                probe.close()
            fonts_dir = tmpdir / "fonts"
            fonts_dir.mkdir()
            config = {
                "source": str(source_path),
                "source_sha256": sha256_of(source_path),
                "page_index": 0,
                "printed_page": printed,
                "output": str(tmpdir / "output.pdf"),
                "fonts_dir": str(fonts_dir),
                "replacements": [
                    {
                        "source": "DUP", "text": None, "count": 2,
                        "boxes": [first_box],
                        "font": PUBLIC_FONT_FILE, "size": 12,
                        "align": "left", "color": "#231f20",
                    }
                ],
                "required_text": [],
                "protected_spans": [],
            }
            config_path = tmpdir / "config.json"
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(
                RuntimeError, r"(?i)(box|cardinality|unique)"
            ):
                edit_pdf.run(config_path)
            self.assertFalse((tmpdir / "output.pdf").exists())


class TestAtomicPublicationAndCleanup(unittest.TestCase):
    def test_failed_temp_run_publishes_no_output(self):
        """A rejected temp config must not publish any output file."""
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            tmpdir = Path(tmpdir)
            source_path = tmpdir / "source.pdf"
            doc = pymupdf.open()
            try:
                page = doc.new_page(width=400, height=600)
                page.insert_textbox(
                    pymupdf.Rect(50, 100, 350, 130), "DUP",
                    fontname="helv", fontsize=12, align=0, overlay=True,
                )
                page.insert_textbox(
                    pymupdf.Rect(50, 300, 350, 330), "DUP",
                    fontname="helv", fontsize=12, align=0, overlay=True,
                )
                doc.save(source_path)
            finally:
                doc.close()
            probe = pymupdf.open(source_path)
            try:
                printed = probe[0].get_text().splitlines()[0]
            finally:
                probe.close()
            output_path = tmpdir / "output.pdf"
            config = {
                "source": str(source_path),
                "source_sha256": "0" * 64,
                "page_index": 0,
                "printed_page": printed,
                "output": str(output_path),
                "fonts_dir": str(tmpdir / "fonts"),
                "replacements": [],
                "required_text": [],
                "protected_spans": [],
            }
            config_path = tmpdir / "config.json"
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                edit_pdf.run(config_path)
            self.assertFalse(output_path.exists())
            leftovers = [
                path for path in tmpdir.iterdir()
                if path.suffix == ".pdf" and path.name not in {"source.pdf"}
            ]
            self.assertEqual(leftovers, [])

    def test_failed_temp_run_leaves_no_strays_in_worktree(self):
        """A failed temp run must not pollute the worktree with temp PDFs."""
        before = set(WORKTREE_ROOT.glob(".pdf-edit-*.pdf"))
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            config_path, _ = _write_full_run_fixtures(tmpdir)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["source_sha256"] = "0" * 64
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                edit_pdf.run(config_path)
        after = set(WORKTREE_ROOT.glob(".pdf-edit-*.pdf"))
        self.assertEqual(before, after)


class TestCliDiagnosticPrivacy(unittest.TestCase):
    """Default CLI diagnostics must not leak workspace paths or content."""

    def test_default_error_has_stable_code_and_hides_absolute_path(self):
        """Default error carries a stable code and no absolute temp path."""
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            missing = str(Path(tmpdir) / "does-not-exist.json")
            first = run_cli(missing)
            second = run_cli(missing)
        for completed in (first, second):
            self.assertNotEqual(completed.returncode, 0)
            payload = json.loads(completed.stdout.strip())
            self.assertEqual(payload.get("status"), "partial")
            self.assertTrue(payload.get("error"))
        self.assertNotIn(tmpdir, first.stdout + first.stderr)
        first_payload = json.loads(first.stdout.strip())
        second_payload = json.loads(second.stdout.strip())
        self.assertNotRegex(
            first_payload.get("error", ""), r"/[\w.\-]+/[\w.\-]+"
        )
        for payload in (first_payload, second_payload):
            code = payload.get("code")
            self.assertIsInstance(code, str)
            self.assertRegex(code, r"^[A-Z][A-Z0-9_]+$")
        self.assertEqual(first_payload.get("code"), second_payload.get("code"))

    def test_default_error_hides_source_and_replacement_text(self):
        """Default error must not echo configured source/replacement strings."""
        source_sentinel = "SRC_SENTINEL_7F3A9C2E"
        replacement_sentinel = "REPL_SENTINEL_B81D4A6F"
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            tmpdir_path = Path(tmpdir)
            source_path = tmpdir_path / "source.pdf"
            doc = pymupdf.open()
            try:
                page = doc.new_page(width=400, height=600)
                page.insert_textbox(
                    pymupdf.Rect(50, 100, 350, 130),
                    source_sentinel,
                    fontname="helv",
                    fontsize=12,
                    align=0,
                    overlay=True,
                )
                doc.save(source_path)
            finally:
                doc.close()
            probe = pymupdf.open(source_path)
            try:
                printed = probe[0].get_text().splitlines()[0]
            finally:
                probe.close()
            fonts_dir = tmpdir_path / "fonts"
            fonts_dir.mkdir()
            base_config = {
                "source": str(source_path),
                "source_sha256": sha256_of(source_path),
                "page_index": 0,
                "printed_page": printed,
                "output": str(tmpdir_path / "output.pdf"),
                "fonts_dir": str(fonts_dir),
                "required_text": [],
                "protected_spans": [],
            }

            def _config_with(replacements, required_text):
                config = dict(
                    base_config,
                    replacements=replacements,
                    required_text=required_text,
                )
                config_path = tmpdir_path / "config.json"
                config_path.write_text(
                    json.dumps(config, ensure_ascii=False), encoding="utf-8"
                )
                return str(config_path)

            cases = [
                (
                    "source-text",
                    [
                        {
                            "source": source_sentinel,
                            "text": None,
                            "count": 2,
                            "box": [50, 100, 350, 130],
                            "font": PUBLIC_FONT_FILE,
                            "size": 12,
                            "align": "left",
                            "color": "#231f20",
                        }
                    ],
                    [],
                    source_sentinel,
                ),
                (
                    "replacement-text",
                    [
                        {
                            "source": source_sentinel,
                            "text": None,
                            "count": 1,
                            "box": [50, 100, 350, 130],
                            "font": PUBLIC_FONT_FILE,
                            "size": 12,
                            "align": "left",
                            "color": "#231f20",
                        }
                    ],
                    [replacement_sentinel],
                    replacement_sentinel,
                ),
            ]
            for name, replacements, required_text, sentinel in cases:
                with self.subTest(case=name):
                    completed = run_cli(
                        _config_with(replacements, required_text)
                    )
                    self.assertNotEqual(completed.returncode, 0)
                    payload = json.loads(completed.stdout.strip())
                    self.assertEqual(payload.get("status"), "partial")
                    self.assertNotIn(
                        sentinel, completed.stdout + completed.stderr
                    )
                    self.assertNotIn(
                        sentinel, payload.get("error", "")
                    )
                    code = payload.get("code")
                    self.assertIsInstance(code, str)
                    self.assertRegex(code, r"^[A-Z][A-Z0-9_]+$")

    def test_default_error_hides_validator_output(self):
        """Default error must not embed raw external validator stdout/stderr."""
        marker = "PDFTOTEXT_SENTINEL_9Z8X7Y"
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            tmpdir_path = Path(tmpdir)
            config_path, _ = _write_full_run_fixtures(str(tmpdir_path))
            fake_bin = tmpdir_path / "fakebin"
            fake_bin.mkdir()
            fake_pdftotext = fake_bin / "pdftotext"
            fake_pdftotext.write_text(
                f'#!/bin/sh\necho "{marker} stdout leak"\n'
                f'echo "{marker} stderr leak" >&2\nexit 1\n',
                encoding="utf-8",
            )
            os.chmod(fake_pdftotext, 0o755)
            previous_path = os.environ.get("PATH", "")
            try:
                os.environ["PATH"] = (
                    str(fake_bin) + os.pathsep + previous_path
                )
                completed = run_cli(str(config_path))
            finally:
                os.environ["PATH"] = previous_path
            self.assertNotEqual(completed.returncode, 0)
            payload = json.loads(completed.stdout.strip())
            self.assertEqual(payload.get("status"), "partial")
            self.assertNotIn(marker, completed.stdout + completed.stderr)
            self.assertNotIn(marker, payload.get("error", ""))
            self.assertNotIn(str(tmpdir_path), completed.stdout + completed.stderr)
            code = payload.get("code")
            self.assertIsInstance(code, str)
            self.assertRegex(code, r"^[A-Z][A-Z0-9_]+$")

    def test_success_reports_output_relative_to_workspace(self):
        """Success JSON must report output relative to the config workspace."""
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            config_path, _ = _write_full_run_fixtures(tmpdir)
            completed = run_cli(str(config_path))
            self.assertEqual(
                completed.returncode, 0, msg=completed.stderr + completed.stdout
            )
            payload = json.loads(completed.stdout.strip())
            self.assertEqual(payload.get("status"), "complete")
            output = payload.get("output")
            self.assertIsInstance(output, str)
            self.assertFalse(Path(output).is_absolute())
            self.assertNotIn(str(tmpdir), output)
            self.assertTrue((Path(tmpdir) / output).is_file())


class TestStrictSchema(unittest.TestCase):
    """Ambiguous/invalid configs must be rejected (pure schema seam)."""

    def _base_config(self, tmpdir, **overrides):
        base = {
            "source": "source.pdf",
            "source_sha256": "0" * 64,
            "page_index": 0,
            "output": "output.pdf",
            "fonts_dir": "fonts",
            "replacements": [],
            "required_text": [],
            "protected_spans": [],
        }
        base.update(overrides)
        return base

    def test_rejects_invalid_schema(self):
        valid_box = [0, 0, 10, 10]
        base_item = {
            "source": "A",
            "count": 1,
            "text": "X",
            "box": valid_box,
            "font": PUBLIC_FONT_FILE,
            "size": 10,
            "align": "left",
            "color": "#231f20",
        }
        with self.subTest(case="empty-text-rejected"):
            bad = dict(base_item, text="")
            with self.assertRaises(Exception):
                edit_pdf._validate_replacement(bad, 0)
        with self.subTest(case="null-deletion-allowed"):
            edit_pdf._validate_replacement({"source": "A", "count": 1, "text": None}, 0)
        with self.subTest(case="rotate-rejected-for-null-deletion"):
            with self.assertRaises(Exception):
                edit_pdf._validate_replacement(
                    {"source": "A", "count": 1, "text": None, "rotate": 90}, 0
                )
        with self.subTest(case="box-and-boxes-rejected"):
            bad = dict(base_item, boxes=[valid_box])
            with self.assertRaises(Exception):
                edit_pdf._validate_replacement(bad, 0)
        for rotate in (True, -90, 45, 360, "90"):
            with self.subTest(case=f"invalid-rotate-{rotate!r}"):
                with self.assertRaises(Exception):
                    edit_pdf._validate_replacement(dict(base_item, rotate=rotate), 0)
        with self.subTest(case="unknown-validation-key-rejected"):
            config = self._base_config(
                None, validation={"unknown_policy_key": True}
            )
            with self.assertRaises(Exception):
                edit_pdf._validate_config(config)
        for name, box in [
            ("non-finite-nan", [float("nan"), 0, 10, 10]),
            ("non-finite-inf", [float("inf"), 0, 10, 10]),
            ("degenerate-zero-width", [5, 5, 5, 10]),
            ("degenerate-inverted", [10, 10, 5, 5]),
        ]:
            with self.subTest(case=name):
                with self.assertRaises(Exception):
                    edit_pdf._check_box(box, "box")
        with self.subTest(case="duplicate-source-rejected"):
            config = self._base_config(
                None,
                replacements=[
                    {"source": "DUP", "count": 1, "text": None},
                    {"source": "DUP", "count": 1, "text": None},
                ],
            )
            with self.assertRaises(Exception):
                edit_pdf._validate_config(config)
        with self.subTest(case="overlapping-destination-rejected"):
            config = self._base_config(
                None,
                replacements=[
                    {"source": "AAA", "count": 1, "text": None, "box": [0, 0, 10, 10]},
                    {"source": "BBB", "count": 1, "text": None, "box": [5, 5, 15, 15]},
                ],
            )
            with self.assertRaises(Exception):
                edit_pdf._validate_config(config)


class TestFailClosedInspection(unittest.TestCase):
    """Content/group inspection errors must raise, not return empty/None."""

    def test_inspection_errors_raise(self):
        class _BoomDoc:
            def xref_stream(self, _xref):
                raise RuntimeError("boom stream")

            def xref_length(self):
                raise RuntimeError("boom length")

            def xref_object(self, _xref):
                raise RuntimeError("boom object")

        class _BoomPage:
            xref = 1

            def get_contents(self):
                return [1]

        with self.subTest(case="content-streams"):
            with self.assertRaises(Exception):
                edit_pdf.page_content_streams(_BoomDoc(), _BoomPage())
        with self.subTest(case="group-signature"):
            with self.assertRaises(Exception):
                edit_pdf.group_signature(_BoomDoc(), _BoomPage())


class TestQuadSignature(unittest.TestCase):
    """Quad drawings must have stable signatures without crashing."""

    def test_quad_items_distinguished(self):
        quad_a = pymupdf.Quad(
            pymupdf.Point(0, 0),
            pymupdf.Point(10, 0),
            pymupdf.Point(0, 10),
            pymupdf.Point(10, 10),
        )
        quad_b = pymupdf.Quad(
            pymupdf.Point(0, 0),
            pymupdf.Point(20, 0),
            pymupdf.Point(0, 20),
            pymupdf.Point(20, 20),
        )

        def _page_for(quad):
            template = {
                "rect": pymupdf.Rect(0, 0, 20, 20),
                "type": "fs",
                "color": (1, 0, 0),
                "fill": (0, 1, 0),
                "width": 1.0,
                "dashes": "[] 0",
                "closePath": False,
                "even_odd": False,
                "items": [("qu", quad)],
                "seqno": 0,
                "layer": "",
                "lineCap": (0, 0, 0),
                "lineJoin": 0.0,
                "fill_opacity": 1.0,
                "stroke_opacity": 1.0,
            }
            return _FakePage([template])

        first = edit_pdf.drawing_signature(_page_for(quad_a))
        second = edit_pdf.drawing_signature(_page_for(quad_a))
        other = edit_pdf.drawing_signature(_page_for(quad_b))
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)


class TestPreexistingEqualText(unittest.TestCase):
    """Pre-existing spans equal to inserted text outside boxes must validate."""

    def test_preexisting_equal_text_outside_boxes_preserved(self):
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            tmpdir_path = Path(tmpdir)
            source_path = tmpdir_path / "source.pdf"
            doc = pymupdf.open()
            try:
                page = doc.new_page(width=400, height=600)
                page.insert_textbox(
                    pymupdf.Rect(50, 200, 350, 240),
                    "SOURCE_HELLO",
                    fontname="helv",
                    fontsize=14,
                    align=0,
                    overlay=True,
                )
                page.insert_textbox(
                    pymupdf.Rect(50, 300, 350, 330),
                    "BONJOUR",
                    fontname="helv",
                    fontsize=14,
                    align=0,
                    overlay=True,
                )
                doc.save(source_path)
            finally:
                doc.close()
            probe = pymupdf.open(source_path)
            try:
                printed = probe[0].get_text().splitlines()[0]
            finally:
                probe.close()
            fonts_dir = tmpdir_path / "fonts"
            fonts_dir.mkdir()
            shutil.copy(PUBLIC_FONT_ASSET, fonts_dir / PUBLIC_FONT_FILE)
            config = {
                "source": str(source_path),
                "source_sha256": sha256_of(source_path),
                "page_index": 0,
                "printed_page": printed,
                "output": str(tmpdir_path / "output.pdf"),
                "fonts_dir": str(fonts_dir),
                "replacements": [
                    {
                        "source": "SOURCE_HELLO",
                        "text": "BONJOUR",
                        "count": 1,
                        "box": [50, 200, 350, 240],
                        "font": PUBLIC_FONT_FILE,
                        "size": 14,
                        "align": "left",
                        "color": "#231f20",
                    }
                ],
                "required_text": ["BONJOUR"],
                "protected_spans": [],
            }
            config_path = tmpdir_path / "config.json"
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            result = edit_pdf.run(config_path)
            self.assertEqual(result.get("status"), "complete")


class TestOutputConfinement(unittest.TestCase):
    """Workspace confinement must cover output outside/source/config and source."""

    def _minimal_config(self, tmpdir_path, source_path, printed, output):
        fonts_dir = tmpdir_path / "fonts"
        fonts_dir.mkdir(exist_ok=True)
        return {
            "source": str(source_path),
            "source_sha256": sha256_of(source_path),
            "page_index": 0,
            "printed_page": printed,
            "output": str(output),
            "fonts_dir": str(fonts_dir),
            "replacements": [],
            "required_text": [],
            "protected_spans": [],
        }

    def _write_source(self, source_path):
        doc = pymupdf.open()
        try:
            page = doc.new_page(width=400, height=600)
            page.insert_textbox(
                pymupdf.Rect(50, 100, 350, 130),
                "HELLO",
                fontname="helv",
                fontsize=12,
                align=0,
                overlay=True,
            )
            doc.save(source_path)
        finally:
            doc.close()
        probe = pymupdf.open(source_path)
        try:
            printed = probe[0].get_text().splitlines()[0]
        finally:
            probe.close()
        return printed

    def test_workspace_confinement(self):
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            tmpdir_path = Path(tmpdir)
            source_path = tmpdir_path / "source.pdf"
            printed = self._write_source(source_path)
            with self.subTest(case="output-outside-rejected"):
                config = self._minimal_config(
                    tmpdir_path, source_path, printed, "/tmp/outside-probe.pdf"
                )
                config_path = tmpdir_path / "config.json"
                config_path.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaises(Exception):
                    edit_pdf.run(config_path)
            with self.subTest(case="output-overwrites-source-rejected"):
                config = self._minimal_config(
                    tmpdir_path, source_path, printed, str(source_path)
                )
                config_path = tmpdir_path / "config.json"
                config_path.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaises(Exception):
                    edit_pdf.run(config_path)
            with self.subTest(case="output-overwrites-config-rejected"):
                config_path = tmpdir_path / "config.json"
                config = self._minimal_config(
                    tmpdir_path, source_path, printed, str(config_path)
                )
                config_path.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaises(Exception):
                    edit_pdf.run(config_path)
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            with tempfile.TemporaryDirectory(prefix="public-editor-outer-") as outer:
                tmpdir_path = Path(tmpdir)
                outer_path = Path(outer)
                source_path = outer_path / "source.pdf"
                printed = self._write_source(source_path)
                with self.subTest(case="source-outside-rejected"):
                    config = self._minimal_config(
                        tmpdir_path, source_path, printed, str(tmpdir_path / "out.pdf")
                    )
                    config_path = tmpdir_path / "config.json"
                    config_path.write_text(json.dumps(config), encoding="utf-8")
                    with self.assertRaises(Exception):
                        edit_pdf.run(config_path)


class TestExistingOutputSafety(unittest.TestCase):
    """Existing outputs need opt-in overwrite and survive post-save failure."""

    def test_existing_output_requires_optin_and_survives_failure(self):
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            tmpdir_path = Path(tmpdir)
            config_path, _ = _write_full_run_fixtures(str(tmpdir_path))
            config = json.loads(config_path.read_text(encoding="utf-8"))
            output_path = Path(config["output"])
            sentinel = b"OLD-KNOWN-GOOD-SENTINEL"
            output_path.write_bytes(sentinel)
            before = output_path.read_bytes()
            with self.subTest(case="overwrite-requires-optin"):
                with self.assertRaises(Exception):
                    edit_pdf.run(config_path)
                self.assertEqual(output_path.read_bytes(), before)
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            tmpdir_path = Path(tmpdir)
            config_path, _ = _write_full_run_fixtures(str(tmpdir_path))
            config = json.loads(config_path.read_text(encoding="utf-8"))
            output_path = Path(config["output"])
            sentinel = b"OLD-KNOWN-GOOD-SENTINEL"
            output_path.write_bytes(sentinel)
            before = output_path.read_bytes()
            config["required_text"] = ["MISSING_SENTINEL_XYZ"]
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            with self.subTest(case="preserved-on-postsave-failure"):
                with self.assertRaises(Exception):
                    edit_pdf.run(config_path)
                self.assertEqual(output_path.read_bytes(), before)


class TestValidatorTimeout(unittest.TestCase):
    """External validators must support bounded execution."""

    def test_external_validators_are_bounded(self):
        import inspect

        has_timeout = (
            "timeout" in inspect.signature(edit_pdf.command_output).parameters
            or "timeout" in inspect.signature(edit_pdf.run_external).parameters
        )
        self.assertTrue(has_timeout, msg="external validators must support timeout")
        with self.assertRaises(Exception):
            edit_pdf.run_external(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                False,
                timeout=1,
            )


class TestStableTypedCode(unittest.TestCase):
    """CLI/API codes must be stable typed values independent of configured text."""

    def _count_mismatch_config(self, tmpdir_path, sentinel):
        source_path = tmpdir_path / "source.pdf"
        doc = pymupdf.open()
        try:
            page = doc.new_page(width=400, height=600)
            page.insert_textbox(
                pymupdf.Rect(50, 100, 350, 130),
                "HELLO",
                fontname="helv",
                fontsize=12,
                align=0,
                overlay=True,
            )
            doc.save(source_path)
        finally:
            doc.close()
        probe = pymupdf.open(source_path)
        try:
            printed = probe[0].get_text().splitlines()[0]
        finally:
            probe.close()
        fonts_dir = tmpdir_path / "fonts"
        fonts_dir.mkdir(exist_ok=True)
        config = {
            "source": str(source_path),
            "source_sha256": sha256_of(source_path),
            "page_index": 0,
            "printed_page": printed,
            "output": str(tmpdir_path / f"output-{sentinel}.pdf"),
            "fonts_dir": str(fonts_dir),
            "replacements": [{"source": sentinel, "count": 1, "text": None}],
            "required_text": [],
            "protected_spans": [],
        }
        config_path = tmpdir_path / f"config-{sentinel}.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        return config_path

    def test_cli_code_independent_of_configured_text(self):
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            tmpdir_path = Path(tmpdir)
            codes = {}
            for sentinel in ["ALPHA_SENTINEL", "pdftotext"]:
                config_path = self._count_mismatch_config(tmpdir_path, sentinel)
                completed = run_cli(str(config_path))
                self.assertNotEqual(completed.returncode, 0)
                payload = json.loads(completed.stdout.strip())
                codes[sentinel] = payload.get("code")
            self.assertEqual(codes["ALPHA_SENTINEL"], codes["pdftotext"])

    def test_run_raises_typed_error_with_stable_code(self):
        self.assertTrue(hasattr(edit_pdf, "EditorError"))
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            tmpdir_path = Path(tmpdir)
            seen = {}
            for sentinel in ["ALPHA_SENTINEL", "BETA_SENTINEL"]:
                config_path = self._count_mismatch_config(tmpdir_path, sentinel)
                with self.assertRaises(edit_pdf.EditorError) as ctx:
                    edit_pdf.run(config_path)
                code = ctx.exception.code
                self.assertRegex(code, r"^[A-Z][A-Z0-9_]+$")
                seen[sentinel] = code
            self.assertEqual(seen["ALPHA_SENTINEL"], seen["BETA_SENTINEL"])


class TestValidMultiboxFontfile(unittest.TestCase):
    """Valid null/fontfile/multibox runs must keep succeeding (controls)."""

    def test_valid_multibox_fontfile_and_null_runs_succeed(self):
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            tmpdir_path = Path(tmpdir)
            source_path = tmpdir_path / "source.pdf"
            doc = pymupdf.open()
            try:
                page = doc.new_page(width=400, height=600)
                page.insert_textbox(
                    pymupdf.Rect(50, 100, 350, 130),
                    "DUP",
                    fontname="helv",
                    fontsize=12,
                    align=0,
                    overlay=True,
                )
                page.insert_textbox(
                    pymupdf.Rect(50, 300, 350, 330),
                    "DUP",
                    fontname="helv",
                    fontsize=12,
                    align=0,
                    overlay=True,
                )
                doc.save(source_path)
            finally:
                doc.close()
            probe = pymupdf.open(source_path)
            try:
                matches = [s for s in edit_pdf.spans(probe[0]) if s["text"] == "DUP"]
                boxes = [[float(value) for value in span["bbox"]] for span in matches]
                printed = probe[0].get_text().splitlines()[0]
            finally:
                probe.close()
            fonts_dir = tmpdir_path / "fonts"
            fonts_dir.mkdir()
            shutil.copy(PUBLIC_FONT_ASSET, fonts_dir / PUBLIC_FONT_FILE)
            shutil.copy(PUBLIC_FONT_ASSET, tmpdir_path / PUBLIC_FONT_FILE)
            with self.subTest(case="multibox-text"):
                config = {
                    "source": str(source_path),
                    "source_sha256": sha256_of(source_path),
                    "page_index": 0,
                    "printed_page": printed,
                    "output": str(tmpdir_path / "out-multi.pdf"),
                    "fonts_dir": str(fonts_dir),
                    "replacements": [
                        {
                            "source": "DUP",
                            "text": "HI",
                            "count": 2,
                            "boxes": boxes,
                            "font": PUBLIC_FONT_FILE,
                            "size": 12,
                            "align": "left",
                            "color": "#231f20",
                        }
                    ],
                    "required_text": [],
                    "protected_spans": [],
                }
                config_path = tmpdir_path / "config-multi.json"
                config_path.write_text(
                    json.dumps(config, ensure_ascii=False), encoding="utf-8"
                )
                result = edit_pdf.run(config_path)
                self.assertEqual(result.get("status"), "complete")
            with self.subTest(case="fontfile"):
                single_path = tmpdir_path / "single.pdf"
                doc = pymupdf.open()
                try:
                    page = doc.new_page(width=400, height=600)
                    page.insert_textbox(
                        pymupdf.Rect(50, 200, 350, 240),
                        "SOURCE_HELLO",
                        fontname="helv",
                        fontsize=14,
                        align=0,
                        overlay=True,
                    )
                    doc.save(single_path)
                finally:
                    doc.close()
                probe = pymupdf.open(single_path)
                try:
                    single_printed = probe[0].get_text().splitlines()[0]
                finally:
                    probe.close()
                config = {
                    "source": str(single_path),
                    "source_sha256": sha256_of(single_path),
                    "page_index": 0,
                    "printed_page": single_printed,
                    "output": str(tmpdir_path / "out-fontfile.pdf"),
                    "fonts_dir": str(fonts_dir),
                    "replacements": [
                        {
                            "source": "SOURCE_HELLO",
                            "text": "BONJOUR",
                            "count": 1,
                            "box": [50, 200, 350, 240],
                            "fontfile": PUBLIC_FONT_FILE,
                            "size": 14,
                            "align": "left",
                            "color": "#231f20",
                        }
                    ],
                    "required_text": ["BONJOUR"],
                    "protected_spans": [],
                }
                config_path = tmpdir_path / "config-fontfile.json"
                config_path.write_text(
                    json.dumps(config, ensure_ascii=False), encoding="utf-8"
                )
                result = edit_pdf.run(config_path)
                self.assertEqual(result.get("status"), "complete")
            with self.subTest(case="null-deletion"):
                config = {
                    "source": str(source_path),
                    "source_sha256": sha256_of(source_path),
                    "page_index": 0,
                    "printed_page": printed,
                    "output": str(tmpdir_path / "out-null.pdf"),
                    "fonts_dir": str(fonts_dir),
                    "replacements": [
                        {"source": "DUP", "count": 2, "text": None, "boxes": boxes}
                    ],
                    "required_text": [],
                    "protected_spans": [],
                }
                config_path = tmpdir_path / "config-null.json"
                config_path.write_text(
                    json.dumps(config, ensure_ascii=False), encoding="utf-8"
                )
                result = edit_pdf.run(config_path)
                self.assertEqual(result.get("status"), "complete")


class TestClosedSchemaRed(unittest.TestCase):
    """T12 RED: closed schema must reject ambiguous/unknown/duplicate/type gaps."""

    def _minimal_config(self):
        return {
            "source": "source.pdf",
            "source_sha256": "0" * 64,
            "page_index": 0,
            "output": "output.pdf",
            "fonts_dir": "fonts",
            "replacements": [],
            "required_text": [],
            "protected_spans": [],
        }

    def test_missing_text_key_rejected(self):
        """A replacement without explicit 'text' must be CONFIG_INVALID."""
        with self.assertRaises(edit_pdf.EditorError) as ctx:
            edit_pdf._validate_replacement({"source": "A", "count": 1}, 0)
        self.assertEqual(ctx.exception.code, "CONFIG_INVALID")

    def test_unknown_top_level_and_replacement_keys_rejected(self):
        with self.subTest(case="unknown-top-level"):
            bad = dict(self._minimal_config(), extra_top_level=1)
            with self.assertRaises(edit_pdf.EditorError) as ctx:
                edit_pdf._validate_config(bad)
            self.assertEqual(ctx.exception.code, "CONFIG_INVALID")
        with self.subTest(case="unknown-replacement-key"):
            with self.assertRaises(edit_pdf.EditorError) as ctx:
                edit_pdf._validate_replacement(
                    {"source": "A", "count": 1, "text": None, "bogus_key": 1}, 0
                )
            self.assertEqual(ctx.exception.code, "CONFIG_INVALID")

    def test_duplicate_json_members_rejected_raw(self):
        """Raw duplicate members must be rejected even when values agree."""
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            config_path, _ = _write_full_run_fixtures(tmpdir)
            raw = Path(config_path).read_text(encoding="utf-8")
            dup = raw.replace('"page_index": 0', '"page_index": 0, "page_index": 0', 1)
            self.assertIn('"page_index": 0, "page_index": 0', dup)
            Path(config_path).write_text(dup, encoding="utf-8")
            with self.assertRaises(edit_pdf.EditorError) as ctx:
                edit_pdf.run(config_path)
            self.assertEqual(ctx.exception.code, "CONFIG_INVALID")
            self.assertIn("duplicate", str(ctx.exception).lower())

    def test_font_fontfile_align_scalar_types_rejected(self):
        box = [0, 0, 10, 10]
        cases = [
            ("font-int", {"source": "A", "count": 1, "text": "X", "box": box,
                          "font": 123, "size": 10, "align": "left",
                          "color": "#231f20"}),
            ("fontfile-int", {"source": "A", "count": 1, "text": "X", "box": box,
                              "fontfile": 123, "size": 10, "align": "left",
                              "color": "#231f20"}),
            ("align-list", {"source": "A", "count": 1, "text": "X", "box": box,
                            "font": PUBLIC_FONT_FILE, "size": 10,
                            "align": ["left"], "color": "#231f20"}),
        ]
        for name, item in cases:
            with self.subTest(case=name):
                with self.assertRaises(edit_pdf.EditorError) as ctx:
                    edit_pdf._validate_replacement(item, 0)
                self.assertEqual(ctx.exception.code, "CONFIG_INVALID")

    def test_same_bbox_duplicate_source_ambiguity_rejected(self):
        """Two spans sharing one bbox must not collapse to one insertion."""
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            tmpdir_path = Path(tmpdir)
            source_path = tmpdir_path / "source.pdf"
            doc = pymupdf.open()
            try:
                page = doc.new_page(width=400, height=600)
                for _ in range(2):
                    page.insert_textbox(
                        pymupdf.Rect(50, 100, 350, 130), "DUP",
                        fontname="helv", fontsize=12, align=0, overlay=True,
                    )
                doc.save(source_path)
            finally:
                doc.close()
            probe = pymupdf.open(source_path)
            try:
                matches = [s for s in edit_pdf.spans(probe[0]) if s["text"] == "DUP"]
                self.assertEqual(len(matches), 2)
                uniq = {tuple(round(v, 4) for v in s["bbox"]) for s in matches}
                self.assertEqual(len(uniq), 1)
                single_box = [float(v) for v in matches[0]["bbox"]]
                printed = probe[0].get_text().splitlines()[0]
            finally:
                probe.close()
            fonts_dir = tmpdir_path / "fonts"
            fonts_dir.mkdir()
            config = {
                "source": str(source_path),
                "source_sha256": sha256_of(source_path),
                "page_index": 0,
                "printed_page": printed,
                "output": str(tmpdir_path / "output.pdf"),
                "fonts_dir": str(fonts_dir),
                "replacements": [
                    {"source": "DUP", "count": 2, "text": None, "boxes": [single_box]}
                ],
                "required_text": [],
                "protected_spans": [],
            }
            config_path = tmpdir_path / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(edit_pdf.EditorError):
                edit_pdf.run(config_path)
            self.assertFalse((tmpdir_path / "output.pdf").exists())


class TestPublicationBoundaryRed(unittest.TestCase):
    """T12 RED: publication must be fail-closed and race-safe."""

    def test_source_close_failure_blocks_publication(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            config_path, _ = _write_full_run_fixtures(tmpdir)
            output_path = Path(
                json.loads(Path(config_path).read_text(encoding="utf-8"))["output"]
            )
            original_open = pymupdf.open
            calls = []

            def _open(*args, **kwargs):
                doc = original_open(*args, **kwargs)
                calls.append(doc)
                if len(calls) == 1:
                    def _bad_close(*a, **k):
                        raise OSError("simulated source close failure")
                    doc.close = _bad_close
                return doc

            with mock.patch.object(pymupdf, "open", side_effect=_open):
                with self.assertRaises(edit_pdf.EditorError):
                    edit_pdf.run(config_path)
            self.assertFalse(output_path.exists())
            self.assertEqual(list(Path(tmpdir).glob(".pdf-edit-*.pdf")), [])

    def test_no_clobber_atomic_when_overwrite_false(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            tmpdir_path = Path(tmpdir)
            config_path, _ = _write_full_run_fixtures(str(tmpdir_path))
            output_path = Path(
                json.loads(Path(config_path).read_text(encoding="utf-8"))["output"]
            )
            self.assertFalse(output_path.exists())
            original_validate = edit_pdf.validate

            def _concurrent(*args, **kwargs):
                output_path.write_bytes(b"CONCURRENT-WRITER")
                return original_validate(*args, **kwargs)

            with mock.patch.object(edit_pdf, "validate", side_effect=_concurrent):
                with self.assertRaises(edit_pdf.EditorError) as ctx:
                    edit_pdf.run(config_path)
                self.assertEqual(ctx.exception.code, "OUTPUT_INVALID")
            self.assertEqual(output_path.read_bytes(), b"CONCURRENT-WRITER")
            self.assertEqual(list(tmpdir_path.glob(".pdf-edit-*.pdf")), [])

    def test_post_save_preservation_reaches_validate_with_overwrite_true(self):
        """Control: overwrite:true reaches post-save validation and preserves."""
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            tmpdir_path = Path(tmpdir)
            config_path, _ = _write_full_run_fixtures(str(tmpdir_path))
            config = json.loads(Path(config_path).read_text(encoding="utf-8"))
            output_path = Path(config["output"])
            sentinel = b"OLD-KNOWN-GOOD-SENTINEL"
            output_path.write_bytes(sentinel)
            before = output_path.read_bytes()
            config["overwrite"] = True
            config["required_text"] = ["MISSING_SENTINEL_XYZ"]
            Path(config_path).write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(edit_pdf.EditorError) as ctx:
                edit_pdf.run(config_path)
            self.assertEqual(ctx.exception.code, "VALIDATION_FAILED")
            self.assertEqual(output_path.read_bytes(), before)
            self.assertEqual(list(tmpdir_path.glob(".pdf-edit-*.pdf")), [])

    def test_pdffonts_receives_external_timeout(self):
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            tmpdir_path = Path(tmpdir)
            config_path, _ = _write_full_run_fixtures(str(tmpdir_path))
            config = json.loads(Path(config_path).read_text(encoding="utf-8"))
            config["validation"] = {"external_timeout": 1}
            Path(config_path).write_text(json.dumps(config), encoding="utf-8")
            fakebin = tmpdir_path / "fakebin"
            fakebin.mkdir()
            (fakebin / "pdffonts").write_text("#!/bin/sh\nsleep 3\n", encoding="utf-8")
            os.chmod(fakebin / "pdffonts", 0o755)
            previous = os.environ.get("PATH", "")
            os.environ["PATH"] = str(fakebin) + os.pathsep + previous
            try:
                with self.assertRaises(edit_pdf.EditorError) as ctx:
                    edit_pdf.run(config_path)
            finally:
                os.environ["PATH"] = previous
            self.assertEqual(ctx.exception.code, "VALIDATOR_FAILED")
            self.assertIn("timed out", str(ctx.exception).lower())

    def test_operational_errors_typed_stable_codes(self):
        from unittest import mock
        with self.subTest(case="corrupt-source-FileDataError"):
            with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
                tmpdir_path = Path(tmpdir)
                source_path = tmpdir_path / "source.pdf"
                source_path.write_bytes(b"not a pdf")
                fonts_dir = tmpdir_path / "fonts"
                fonts_dir.mkdir()
                config = {
                    "source": str(source_path),
                    "source_sha256": sha256_of(source_path),
                    "page_index": 0,
                    "output": str(tmpdir_path / "out.pdf"),
                    "fonts_dir": str(fonts_dir),
                    "replacements": [],
                    "required_text": [],
                    "protected_spans": [],
                }
                config_path = tmpdir_path / "config.json"
                config_path.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaises(edit_pdf.EditorError) as ctx:
                    edit_pdf.run(config_path)
                self.assertRegex(ctx.exception.code, r"^[A-Z][A-Z0-9_]+$")
                self.assertNotEqual(ctx.exception.code, "INTERNAL_ERROR")
        with self.subTest(case="save-OSError"):
            with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
                config_path, _ = _write_full_run_fixtures(tmpdir)
                with mock.patch.object(
                    pymupdf.Document, "save", side_effect=OSError("sim save")
                ):
                    with self.assertRaises(edit_pdf.EditorError) as ctx:
                        edit_pdf.run(config_path)
                self.assertRegex(ctx.exception.code, r"^[A-Z][A-Z0-9_]+$")
                self.assertNotEqual(ctx.exception.code, "INTERNAL_ERROR")
        with self.subTest(case="publish-OSError"):
            with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
                config_path, _ = _write_full_run_fixtures(tmpdir)
                with mock.patch(
                    "edit_pdf.os.link", side_effect=OSError("sim publish")
                ):
                    with self.assertRaises(edit_pdf.EditorError) as ctx:
                        edit_pdf.run(config_path)
                self.assertEqual(ctx.exception.code, "PUBLISH_FAILED")


class TestRevalidationRed(unittest.TestCase):
    """T12 revalidation RED: race-after-reserve, temp SAVE_FAILED, bad-bbox,
    cleanup chaining, exact operational codes. No pdffonts duplicate."""

    def test_no_clobber_race_after_reservation_preserves_competitor(self):
        """Competitor racing the single no-clobber link must not be clobbered."""
        from unittest import mock
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            tmpdir_path = Path(tmpdir)
            config_path, _ = _write_full_run_fixtures(str(tmpdir_path))
            output_path = Path(
                json.loads(Path(config_path).read_text(encoding="utf-8"))["output"]
            )
            self.assertFalse(output_path.exists())
            original_link = os.link

            def _link(a, b):
                if Path(b) == output_path:
                    Path(b).write_bytes(b"COMPETITOR-JUST-BEFORE-COMMIT")
                return original_link(a, b)

            with mock.patch("edit_pdf.os.link", side_effect=_link):
                with self.assertRaises(edit_pdf.EditorError) as ctx:
                    edit_pdf.run(config_path)
                self.assertEqual(ctx.exception.code, "OUTPUT_INVALID")
            self.assertEqual(
                output_path.read_bytes(), b"COMPETITOR-JUST-BEFORE-COMMIT"
            )
            self.assertEqual(list(tmpdir_path.glob(".pdf-edit-*.pdf")), [])

    def test_temp_creation_and_close_oserror_maps_to_save_failed(self):
        from unittest import mock
        with self.subTest(case="create"):
            with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
                config_path, _ = _write_full_run_fixtures(tmpdir)
                with mock.patch(
                    "edit_pdf.tempfile.NamedTemporaryFile",
                    side_effect=OSError("sim tmp create"),
                ):
                    with self.assertRaises(edit_pdf.EditorError) as ctx:
                        edit_pdf.run(config_path)
                self.assertEqual(ctx.exception.code, "SAVE_FAILED")
        with self.subTest(case="close"):
            with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
                config_path, _ = _write_full_run_fixtures(tmpdir)
                original_ntf = tempfile.NamedTemporaryFile

                def _bad_ntf(*args, **kwargs):
                    handle_cm = original_ntf(*args, **kwargs)
                    handle = handle_cm.__enter__()

                    class _Wrapper:
                        def __enter__(self):
                            return handle

                        def __exit__(self, *exc):
                            try:
                                handle_cm.__exit__(*exc)
                            finally:
                                raise OSError("sim tmp close")

                    return _Wrapper()

                with mock.patch(
                    "edit_pdf.tempfile.NamedTemporaryFile", side_effect=_bad_ntf
                ):
                    with self.assertRaises(edit_pdf.EditorError) as ctx:
                        edit_pdf.run(config_path)
                self.assertEqual(ctx.exception.code, "SAVE_FAILED")

    def test_is_inserted_invalid_bbox_raises_validation_failed(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            config_path, _ = _write_full_run_fixtures(tmpdir)
            original_spans = edit_pdf.spans

            def _spans(page):
                real = list(original_spans(page))
                if any(span.get("text") == "BONJOUR" for span in real):
                    bad = dict(real[0])
                    bad["text"] = "BONJOUR"
                    bad["bbox"] = ["bad", 0, 10, 10]
                    real.append(bad)
                return real

            with mock.patch.object(edit_pdf, "spans", side_effect=_spans):
                with self.assertRaises(edit_pdf.EditorError) as ctx:
                    edit_pdf.run(config_path)
            self.assertEqual(ctx.exception.code, "VALIDATION_FAILED")

    def test_cleanup_close_errors_primary_preserved_and_close_failed(self):
        from unittest import mock
        original_open = pymupdf.open

        def _open_fail_output_close(*args, **kwargs):
            doc = original_open(*args, **kwargs)
            if "stream" not in kwargs and args:
                def _bad_close(*a, **k):
                    raise OSError("sim output close")
                doc.close = _bad_close
            return doc

        with self.subTest(case="no-primary-close-failed"):
            with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
                config_path, _ = _write_full_run_fixtures(tmpdir)
                with mock.patch.object(
                    pymupdf, "open", side_effect=_open_fail_output_close
                ):
                    with self.assertRaises(edit_pdf.EditorError) as ctx:
                        edit_pdf.run(config_path)
                self.assertEqual(ctx.exception.code, "CLOSE_FAILED")
        with self.subTest(case="primary-preserved-secondary-annotated"):
            with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
                config_path, _ = _write_full_run_fixtures(tmpdir)
                config = json.loads(Path(config_path).read_text(encoding="utf-8"))
                config["required_text"] = ["MISSING_SENTINEL_XYZ"]
                Path(config_path).write_text(json.dumps(config), encoding="utf-8")
                with mock.patch.object(
                    pymupdf, "open", side_effect=_open_fail_output_close
                ):
                    with self.assertRaises(edit_pdf.EditorError) as ctx:
                        edit_pdf.run(config_path)
                self.assertEqual(ctx.exception.code, "VALIDATION_FAILED")
                secondary = "sim output close"
                context_str = str(getattr(ctx.exception, "__context__", "") or "")
                notes = getattr(ctx.exception, "__notes__", None) or []
                notes_str = " ".join(str(note) for note in notes)
                self.assertTrue(
                    secondary in context_str or secondary in notes_str,
                    msg=f"secondary not annotated: context={context_str!r} "
                    f"notes={notes_str!r}",
                )

    def test_source_read_oserror_maps_to_source_invalid(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            config_path, _ = _write_full_run_fixtures(tmpdir)
            with mock.patch.object(
                Path, "read_bytes", side_effect=OSError("sim read")
            ):
                with self.assertRaises(edit_pdf.EditorError) as ctx:
                    edit_pdf.run(config_path)
            self.assertEqual(ctx.exception.code, "SOURCE_INVALID")


if __name__ == "__main__":
    unittest.main()
