# Operating guide for agents

This repository contains a deterministic, generic single-page PDF editor.
Agents supervise the process; they do not delegate semantic editing to the
program.

## Required workflow

1. Inspect the source PDF with the available PDF/text tooling and identify
   the exact spans to replace.
2. Prepare a compact JSON config. Copy identifiers, text, coordinates, font
   references, and source hash from the inspected workspace; do not guess.
3. Keep `source`, `output`, and `fonts_dir` inside the config workspace. The
   editor rejects paths outside that workspace and rejects overwriting the
   source or config. Existing output is protected unless the config explicitly
   sets `overwrite: true`.
4. Run the editor with an explicit config path:

   ```sh
   .venv/bin/python edit_pdf.py path/to/config.json
   ```

5. Inspect the JSON result and the rendered output. A successful run has
   `status: "complete"`; anything else is not a releaseable result.

## Configuration contract

The required top-level fields are `source`, `source_sha256`, `page_index`,
`output`, `fonts_dir`, `replacements`, `required_text`, and
`protected_spans`. `printed_page` may pin the expected first line of the
selected page.

Each replacement has an exact source string and positive `count`. Replacement
source strings must be unique. A text replacement also needs `text`, one `box`
or a non-empty `boxes` list, a font
(`font` plus `fonts_dir`, or `fontfile`), positive `size`, optional `align`
(`left`, `center`, or `right`), and a color. A color may be a one-, three-, or
four-component list (Gray, RGB, or CMYK), with every component in `[0, 1]`,
or a neutral gray hex value, which is converted to K-only CMYK. Multiline text
is checked per configured box. `box` and `boxes` are mutually exclusive; every
box must be finite and non-degenerate, and destination boxes must not overlap.
Use `text: null` for the only supported deletion; `text: ""` is invalid.

Validation is generic by default. Set `validation.require_cmyk_group` only
when the source is known to require a referenced DeviceCMYK transparency
group. Set `validation.reject_rgb: true` as an opt-in policy for print jobs
that must prohibit RGB operators in the output. Set
`validation.require_external_validators` to require installed `pdfimages`,
`pdfinfo`, `pdffonts`, `pdftotext`, and `gs`; otherwise missing optional tools are
skipped, while failures from present tools still fail validation.
The accepted validation keys are only `require_cmyk_group`, `reject_rgb`,
`require_external_validators`, and `external_timeout`; unknown keys are
rejected. `validation.external_timeout` defaults to 60 seconds.

CLI failures return typed, privacy-safe codes and messages. Use `--debug` only
as an explicit local opt-in when diagnostic detail is needed; do not publish
that detail.

## Review boundary

The agent chooses and reviews copy, style, coordinates, and intended meaning.
The editor applies only declarative changes and validates structural,
metadata, text, font, color, and rendered-pixel invariants. Neither side
promises universal pixel-perfect output or autonomous semantic editing.

Translation motivated the project, but this workflow is not translation-
specific. It applies to controlled text edits in generic PDF documents.

## Privacy and licensing

Do not place customer names, private filesystem paths, credentials, source
documents, or generated PDFs in committed documentation, examples, tests, or
configs. Use synthetic data for reproducible examples.

The project license is GNU AGPL-3.0; the complete text is in `LICENSE`.
PyMuPDF is separately available under GNU AGPL or a commercial license. Check
the applicable PyMuPDF terms before distributing or operating the software.
The synthetic example bundles `assets/Barlow-Regular.ttf` under the SIL Open
Font License and includes its notice in `assets/OFL.txt`.
