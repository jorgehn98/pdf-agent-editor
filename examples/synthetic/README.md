# Synthetic example

This is a self-contained, neutral demonstration of the generic,
agent-supervised PDF editor. It creates a one-page source document, prepares a
configuration from the source span it finds, runs the editor, and validates
the generated output. No customer or private document is needed.

## Run from the repository root

After installing the dependency described in the root README:

```sh
.venv/bin/python examples/synthetic/create_example.py
```

The command prints the editor's JSON result. A successful run reports
`"status": "complete"` and creates these ignored files under this directory:

- `work/source.pdf`: generated source page;
- `work/config.json`: generated declarative edit config;
- `work/output.pdf`: validated result;
- `work/fonts/Barlow-Regular.ttf`: copied font used by the config.

The generator replaces `SOURCE LINE TO REPLACE`, preserves
`DOC-2026-001 KEEP`, and keeps the source, config, font, and output inside
the example workspace. Its generated config sets `overwrite: true`; this is
the explicit opt-in needed to replace the prior generated output, so the
command is safe to rerun. Delete `work/` to reset the example and run it again.

## What to inspect

The example demonstrates the normal boundary: an agent or human chooses the
replacement and reviews the output, while deterministic code applies the
declared span replacement and checks geometry, text, font, preserved content,
and rendered output invariants. It is not a claim of universal pixel-perfect
editing or autonomous semantic editing.

## Assets and licenses

- `assets/Barlow-Regular.ttf`: OFL font used for inserted text.
- `assets/OFL.txt`: the notice for that bundled font.

The editor project is GNU AGPL-3.0; see the repository `LICENSE`. PyMuPDF has
separate GNU AGPL and commercial licensing options; consult its applicable
terms when distributing or using the dependency.
