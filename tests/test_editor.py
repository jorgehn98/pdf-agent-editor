"""Public synthetic contracts for the one-page PDF editor (RED).

Minimal public interface under test (module ``edit_pdf`` at the worktree root):

- ``contains_rgb_operators(raw: bytes | str) -> bool``
- ``drawing_signature(page)``, ``image_signature(page)``, ``spans(page)``
- ``boxes(item)`` (``box`` singular or ``boxes`` plural)
- ``cmyk(value)`` (K-only policy)
- ``unchanged_span_key(span: dict) -> hashable``
- ``source_metadata(document, page)``, ``validate(...)``, ``run(config_path)``
- CLI ``python edit_pdf.py <config.json>`` requires an explicit config path
  and reports JSON ``{"status": ...}`` on stdout.

All fixtures (source PDFs, tiny images, configs, outputs) live in
``tempfile.TemporaryDirectory`` and are cleaned automatically. No test reads
checked-in PDFs, client font files or client configuration paths.
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

SYSTEM_TTF = "/usr/share/fonts/liberation-sans-fonts/LiberationSans-Regular.ttf"
PUBLIC_FONT_NAME = "LiberationSans"
PUBLIC_FONT_FILE = "LiberationSans.ttf"


def require_system_font(testcase):
    if not Path(SYSTEM_TTF).exists():
        testcase.skipTest(f"system font missing: {SYSTEM_TTF}")


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
        """RED: any K-only black must be configurable, not just two hex codes."""
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
        """RED: running without a config must demand an explicit path."""
        completed = run_cli()
        self.assertNotEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout.strip())
        self.assertEqual(payload.get("status"), "partial")
        self.assertRegex(
            payload.get("error", ""),
            r"(?i)(explicit|required|usage|missing.*arg)",
        )


class TestGenericPageGeometry(unittest.TestCase):
    def test_matching_letter_geometry_passes_geometry_check(self):
        """Control: same-size pages reach later checks instead of geometry."""
        src = pymupdf.open()
        out = pymupdf.open()
        try:
            src.new_page(width=612, height=792)
            page = out.new_page(width=612, height=792)
            del page
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
                tmp = Path(handle.name)
            out.save(tmp)
            config = {"required_text": [], "protected_spans": [], "replacements": []}
            with self.assertRaisesRegex(RuntimeError, r"(?i)(DeviceCMYK|transparency)"):
                edit_pdf.validate(config, src, src[0], tmp, [])
        finally:
            src.close()
            out.close()
            tmp.unlink(missing_ok=True)

    def test_geometry_error_is_generic_without_fixed_size_name(self):
        """RED: the size error must not name one hardcoded page size."""
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
        require_system_font(self)
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
                    fontfile=SYSTEM_TTF,
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

    def test_correct_distribution_reaches_transparency_gate(self):
        with self.assertRaisesRegex(RuntimeError, r"(?i)(DeviceCMYK|transparency)"):
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
    """Two synthetic single-page PDFs with text, one drawing and one image."""
    require_system_font_for_module()
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


def require_system_font_for_module():
    if not Path(SYSTEM_TTF).exists():
        raise unittest.SkipTest(f"system font missing: {SYSTEM_TTF}")


class TestPreservedObjectsWithoutCmykGate(unittest.TestCase):
    def test_identical_synthetic_pages_validate_without_cmyk_group(self):
        """RED: generic pages without a transparency group must still validate."""
        require_system_font(self)
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            src_path, out_path = _write_preserved_pair(tmpdir, same=True)
            src_doc = pymupdf.open(src_path)
            self.addCleanup(src_doc.close)
            config = {"required_text": [], "protected_spans": [], "replacements": []}
            edit_pdf.validate(config, src_doc, src_doc[0], out_path, [])

    def test_changed_drawings_report_preserved_mismatch(self):
        """RED: drawing changes must report a preserved-object mismatch."""
        require_system_font(self)
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            src_path, out_path = _write_preserved_pair(tmpdir, same=False)
            src_doc = pymupdf.open(src_path)
            self.addCleanup(src_doc.close)
            config = {"required_text": [], "protected_spans": [], "replacements": []}
            with self.assertRaisesRegex(RuntimeError, r"(?i)preserved object mismatch"):
                edit_pdf.validate(config, src_doc, src_doc[0], out_path, [])


def _write_full_run_fixtures(tmpdir):
    """All run() fixtures inside one temporary directory."""
    require_system_font_for_module()
    tmpdir = Path(tmpdir)
    fonts_dir = tmpdir / "fonts"
    fonts_dir.mkdir()
    shutil.copy(SYSTEM_TTF, fonts_dir / PUBLIC_FONT_FILE)
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
        """RED: config/source/output/fonts fully in temp dirs must publish."""
        require_system_font(self)
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


class TestRelativeSourceResolution(unittest.TestCase):
    def test_relative_source_resolves_against_config_dir(self):
        """RED: a relative source must resolve from the config dir, not CWD."""
        require_system_font(self)
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
        """RED: one configured box cannot cover two unique source bboxes."""
        require_system_font(self)
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
        require_system_font(self)
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
        require_system_font(self)
        before = set(WORKTREE_ROOT.glob(".page-*-*.pdf"))
        with tempfile.TemporaryDirectory(prefix="public-editor-") as tmpdir:
            config_path, _ = _write_full_run_fixtures(tmpdir)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["source_sha256"] = "0" * 64
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                edit_pdf.run(config_path)
        after = set(WORKTREE_ROOT.glob(".page-*-*.pdf"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
