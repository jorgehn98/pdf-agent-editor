# Agent-supervised PDF editor

This project is a small, generic single-page PDF text editor. It reads a
declarative JSON configuration, replaces selected source text spans, and
validates the generated PDF before publishing it atomically.

The implementation is **agent-supervised**, not autonomous semantic editing:
an agent or human inspects the source spans, prepares the compact text/style/
box configuration, runs the deterministic editor, and reviews the result.
The program applies only the configured edits and its validators check the
result. Translation was the initial use case, but the editor is generic and
can be used for other controlled text edits.

The canonical operating procedure for agents is in [`AGENTS.md`](AGENTS.md).

## Requirements and setup

- Python 3
- PyMuPDF 1.28.2, installed from `requirements.txt`
- Optional external validators: `pdfimages`, `pdfinfo`, `pdffonts`,
  `pdftotext`, and Ghostscript (`gs`). They are required only when
  `validation.require_external_validators` is `true`.

Create an isolated environment and install the dependency:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Run an edit

The CLI requires one explicit config path:

```sh
.venv/bin/python edit_pdf.py examples/synthetic/work/config.json
```

The config is JSON. `source`, `output`, and `fonts_dir` resolve relative to
the config directory. A replacement identifies an exact `source` span and a
`count`; text replacements also specify a `box` or `boxes`, font, size,
alignment, and a color. Colors may be a one-, three-, or four-component list
(Gray, RGB, or CMYK), with every component in `[0, 1]`, or a neutral gray hex
value. Neutral gray hex values are converted to K-only CMYK. The source
SHA-256 is checked before editing.

The process prints JSON with `status: "complete"` after validation. A failed
CLI invocation prints JSON with `status: "partial"` and does not publish an
unvalidated output.

## Synthetic example

Generate a neutral source PDF, configuration, font workspace, and validated
output:

```sh
.venv/bin/python examples/synthetic/create_example.py
```

Generated files live under `examples/synthetic/work/`, which is ignored by
Git. See [`examples/synthetic/README.md`](examples/synthetic/README.md) for
the example-specific flow and bundled font notice.

## Validation and boundaries

Validation checks the page count and geometry, required and replaced text,
protected and otherwise unedited spans, configured font/size/color, embedded
fonts, preserved images/drawings/transparency metadata, external validators
when requested, and rendered pixels outside edit masks. Set
`validation.reject_rgb: true` as an opt-in policy for print jobs that must
prohibit RGB operators in the output.

These checks are guarantees about the conditions they test; they are not a
promise of universal pixel-perfect output. Text fitting, font metrics, PDF
features, and visual approval still require review for each document.

The editor does not infer meaning, translate text, choose replacement copy,
or decide whether a change is appropriate. Keep source documents and
configurations free of personal or confidential data unless your own
handling controls permit their use. Do not commit customer documents,
private paths, generated PDFs, or secrets.

## License

The project is licensed under the GNU Affero General Public License, version
3; see [`LICENSE`](LICENSE). It depends on PyMuPDF, which is available under
the GNU AGPL or a commercial license. Choose and comply with the applicable
PyMuPDF license for your distribution and use; the project's AGPL does not
remove PyMuPDF's separate licensing terms.
