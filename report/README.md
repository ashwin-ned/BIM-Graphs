# Results report

`results_2026_09_18.tex` / `results_2026_09_18.pdf` — consolidated results
record and measurement audit, 18 September 2026. 11 pages.

## Compiling

No system-wide LaTeX toolchain on this machine, but a complete TeX Live 2023
install exists at `/mnt/data/home/anedung/texlive/2023/` (the `texlive` conda
env at `/mnt/data/home/anedung/miniforge3/envs/texlive/` has the binaries only,
no packages — don't use that one, `tlmgr` there is broken too, missing a Perl
module). Use:

```bash
export PATH=/mnt/data/home/anedung/texlive/2023/bin/x86_64-linux:$PATH
pdflatex -interaction=nonstopmode results_2026_09_18.tex   # x3, for TOC/refs/longtable
```

Three passes, not one — `longtable` and the section cross-references need the
`.aux` file from the previous pass. `siunitx` is not installed in this tree
(everything else is); the document was written without it rather than pulling
a package over the network into someone else's TeX installation.

**Verified 21 September 2026:** compiles clean, 0 errors, only a benign
font-shape substitution warning (`cmtt` has no italic, cosmetic only). Checked
three ways: text-extracted and diffed 16 headline figures against the source
JSON (16/16 match — see the audit trail in the conversation this was produced
in); rendered pages 1, 5 and 8 to PNG and inspected visually for typography,
table rules, and the `longtable` page break; `\code{}`-wrapped identifiers with
underscores (`open_passage`, `connected_by_door`) confirmed to render correctly
— they only look wrong in naive `pdftotext`/text-layer extraction, which is a
`cmtt` font quirk, not a compile defect.

Every figure in the document was re-measured on 18 September 2026 against the
code and corpora present on that date. Section 12 lists eight documentation
figures this audit found stale and corrected at source; section 11 lists
metrics that are structurally incapable of measuring what their names suggest,
and must not be quoted.
