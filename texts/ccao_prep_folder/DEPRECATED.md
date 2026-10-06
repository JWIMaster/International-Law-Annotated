# Superseded

These scripts predate the unified tool and duplicate parts of it
(`extract_icj.py` re-implements PDF parsing, `build_site.py` re-implements
rendering, `qc.py` re-implements validation).

Use the current tool instead -- it covers the same CSV workflow with the
better parser and shared rendering:

```bash
python3 build_tool.py build <source.txt or instrument.pdf> \
    -a instrument.csv -o site/instrument.html --title "Instrument Title"
```

Nothing here is maintained. Fixes to parsing, matching or rendering belong in
`ilatool/`.
