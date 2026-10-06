"""Paragraph/heading inference and the source text format."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.helpers import fixture, has_fixture

from ilatool import backends, layout, sourcefmt, structure
from ilatool.errors import Diagnostics, InputError


def _blocks(name, **options):
    doc = backends.load_pdf(fixture(name))
    diag = Diagnostics()
    laid_out = layout.analyse_document(doc, diag)
    opts = structure._Options(
        split_recitals=options.get("split_recitals", True),
        drop_front_matter=options.get("drop_front_matter", True),
        skip_title=doc.title,
    )
    return structure.build_blocks(laid_out.pages, laid_out.body_size, opts, diag), diag


@unittest.skipUnless(has_fixture("simple"), "fixtures missing")
class SimpleStructureTests(unittest.TestCase):
    def test_title_becomes_a_heading_and_paragraphs_stay_separate(self):
        doc, _ = _blocks("simple")
        kinds = [b.kind for b in doc.blocks]
        self.assertEqual(kinds[0], "heading")
        self.assertEqual(doc.blocks[0].text, "A Short Note on Treaties")
        self.assertEqual(len(doc.paragraphs), 3)

    def test_ids_are_content_addressed_and_stable(self):
        first, _ = _blocks("simple")
        second, _ = _blocks("simple")
        self.assertEqual([b.id for b in first.paragraphs],
                         [b.id for b in second.paragraphs])
        for block in first.paragraphs:
            self.assertTrue(block.key.startswith("k"))
            self.assertTrue(block.id.startswith("para-k"))


@unittest.skipUnless(has_fixture("paragraphs_paged"), "fixtures missing")
class PageBreakTests(unittest.TestCase):
    def test_a_paragraph_continues_across_a_page_break(self):
        doc, _ = _blocks("paragraphs_paged")
        numbered = [b for b in doc.paragraphs if b.label in ("1.", "2.", "3.")]
        self.assertEqual(len(numbered), 3)
        second = next(b for b in numbered if b.label == "2.")
        self.assertIn("continue onto the following page", second.text)
        self.assertTrue(second.text.endswith("purpose, and this example sentence is "
                                             "deliberately made long enough that it has to "
                                             "continue onto the following page."),
                        second.text)

    def test_the_running_header_does_not_leak_into_the_text(self):
        doc, _ = _blocks("paragraphs_paged")
        for block in doc.blocks:
            self.assertNotIn("(continued)", block.text)


@unittest.skipUnless(has_fixture("fontsizes"), "fixtures missing")
class HeadingTests(unittest.TestCase):
    def test_larger_and_bold_lines_become_headings(self):
        doc, _ = _blocks("fontsizes")
        headings = [b.text for b in doc.headings]
        self.assertIn("Type Size Title", headings)
        self.assertIn("Section 1. Type Sizes", headings)

    def test_a_short_body_line_is_not_promoted(self):
        doc, _ = _blocks("fontsizes")
        texts = [b.text for b in doc.paragraphs]
        self.assertTrue(any("Times Italic" in t for t in texts))


@unittest.skipUnless(has_fixture("paragraphs"), "fixtures missing")
class MarkerTests(unittest.TestCase):
    def test_clause_markers_become_labels(self):
        doc, _ = _blocks("paragraphs")
        labels = {b.label for b in doc.paragraphs}
        self.assertIn("1.", labels)
        self.assertIn("(a)", labels)


class RecitalTests(unittest.TestCase):
    def test_recital_cues_split_a_fused_preamble(self):
        text = ("The States Parties to this Convention, Being of the opinion that "
                "uniform rules would help, Considering that trade matters, "
                "Have agreed as follows: The present Convention applies to "
                "contracts of sale of goods between parties whose places of "
                "business are in different States.")
        from ilatool.structure import _split_recitals, Block, content_key
        blocks = [Block(kind="para", text=text, label="¶", key=content_key(text))]
        out = _split_recitals(blocks, Diagnostics())
        self.assertGreater(len(out), 1)
        self.assertTrue(out[0].text.startswith("The States Parties"))
        self.assertTrue(any(b.text.startswith("Being of the opinion") for b in out))


class SourceFormatTests(unittest.TestCase):
    SAMPLE = """TITLE: A Treaty
DESC: Something short
HEADER: Treaty
IDMAP para-1=kabc1234
---
## Article 1
{kabc1234} 1. The first paragraph.
¶ A second paragraph.
"""

    def test_round_trip(self):
        source = sourcefmt.parse_source_text(self.SAMPLE)
        self.assertEqual(source.meta["TITLE"], "A Treaty")
        self.assertEqual(source.aliases["para-1"], "kabc1234")
        rendered = sourcefmt.render_source(source.meta, source.blocks, source.aliases)
        again = sourcefmt.parse_source_text(rendered)
        self.assertEqual([b.text for b in again.blocks], [b.text for b in source.blocks])
        self.assertEqual(again.aliases, source.aliases)

    def test_ids_resolve_directly_and_through_aliases(self):
        source = sourcefmt.parse_source_text(self.SAMPLE)
        self.assertEqual(source.resolve_ref("kabc1234"), "para-kabc1234")
        self.assertEqual(source.resolve_ref("para-kabc1234"), "para-kabc1234")
        self.assertEqual(source.resolve_ref("para-1"), "para-kabc1234")
        self.assertIsNone(source.resolve_ref("para-99"))

    def test_missing_delimiter_and_empty_body_are_rejected(self):
        with self.assertRaises(InputError):
            sourcefmt.parse_source_text("TITLE: x\nnothing here")

    def test_duplicate_keys_are_rejected(self):
        with self.assertRaises(InputError):
            sourcefmt.parse_source_text("---\n{k1} ¶ one\n{k1} ¶ two\n")

    def test_carry_over_aliases_matches_by_text_not_position(self):
        previous = sourcefmt.parse_source_text(
            "---\n¶ alpha\n¶ bravo\n¶ charlie\n")
        new_blocks = [
            structure.Block(kind="para", text="charlie", key="kccc"),
            structure.Block(kind="para", text="bravo", key="kbbb"),
            structure.Block(kind="para", text="alpha", key="kaaa"),
        ]
        aliases = sourcefmt.carry_over_aliases(previous, new_blocks)
        self.assertEqual(aliases["para-1"], "kaaa")
        self.assertEqual(aliases["para-2"], "kbbb")
        self.assertEqual(aliases["para-3"], "kccc")

    def test_changed_text_gets_no_alias(self):
        previous = sourcefmt.parse_source_text("---\n¶ one\n¶ two\n")
        new_blocks = [structure.Block(kind="para", text="completely rewritten", key="kx")]
        self.assertEqual(sourcefmt.carry_over_aliases(previous, new_blocks), {})


class ConversionAliasTests(unittest.TestCase):
    @unittest.skipUnless(has_fixture("simple"), "fixtures missing")
    def test_reconversion_keeps_alias_for_unchanged_paragraphs(self):
        from ilatool import pipeline
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "simple.txt"
            request = pipeline.RunRequest(
                source=fixture("simple"), out_source=out,
                meta={"TITLE": "Simple"},
                build=pipeline.BuildOptions(validate=False, write_diagnostics=False))
            first = pipeline.run(request)
            self.assertTrue(first.ok, first.error)
            aliases_before = dict(first.conversion.source.aliases)

            request.convert.keep_aliases_from = out
            second = pipeline.run(request)
            self.assertTrue(second.ok, second.error)
            aliases_after = dict(second.conversion.source.aliases)
            for old_id, key in aliases_before.items():
                self.assertEqual(aliases_after.get(old_id), key,
                                 f"{old_id} should still point at the same paragraph")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
