"""Project discovery, the annotation store, and merging.

These are the pieces the desktop application is built on, and they are
deliberately free of Qt: they are the same code the command-line tool could
use, so they are tested on their own -- fast, and without a display.
"""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.helpers import ROOT, build_docx_styled

from ilatool import allhtml, fileio, merge, notestore, project, render, sourcefmt
from ilatool.errors import InputError


def make_project(base: Path, *, texts=("alpha", "beta"), with_notes=True,
                 with_source=True, with_card=True) -> Path:
    """A small but complete project, laid out the way the real one is."""
    base.mkdir(parents=True, exist_ok=True)
    (base / "index.html").write_text("<html><title>Site</title></html>", encoding="utf-8")
    texts_dir = base / "texts"
    texts_dir.mkdir(exist_ok=True)
    sources = texts_dir / "sources"
    sources.mkdir(exist_ok=True)
    cases = []
    for name in texts:
        (texts_dir / f"{name}.html").write_text(
            "<html><head><title>%s</title>"
            "<meta name=\"description\" content=\"About %s\">"
            "<script defer src=\"%s-notes.js\"></script></head>"
            "<body><div id=\"para-a\"><p>First paragraph of %s.</p></div>"
            "</body></html>" % (name.title(), name, name, name),
            encoding="utf-8")
        if with_notes:
            (texts_dir / f"{name}-notes.js").write_text(
                render.render_notes_js({"para-a": [
                    {"author": "A. Writer", "text": f"A note about {name}."}]}),
                encoding="utf-8")
        if with_source:
            source = sources / f"{name}.txt"
            source.write_text(
                f"TITLE: {name.title()}\nHEADER: {name.title()}\n---\n\n"
                f"{{a}} ¶ First paragraph of {name}.\n", encoding="utf-8")
        cases.append({"title": name.title(), "subtitle": "", "desc": "", "href": f"./{name}.html"})
    if with_card:
        body = "var cases = [\n" + "".join(
            "  {title: '%s', subtitle: '', desc: '', href: '%s'},\n"
            % (c["title"], c["href"]) for c in cases)
        body += "  // Add further text entries here\n];\n"
        (texts_dir / "all.html").write_text(
            f"<html><script>{body}</script></html>", encoding="utf-8")
    return base


class DiscoveryTests(unittest.TestCase):
    def test_a_complete_project_is_found(self):
        with TemporaryDirectory() as tmp:
            root = make_project(Path(tmp) / "site")
            found = project.open_project(root)
            self.assertEqual(found.landing, (root / "index.html").resolve())
            self.assertEqual(found.all_html, (root / "texts" / "all.html").resolve())
            self.assertEqual([d.name for d in found.documents], ["alpha", "beta"])
            self.assertEqual(found.issues, [])
            for doc in found.documents:
                self.assertTrue(doc.notes.exists())
                self.assertIsNotNone(doc.source)
                self.assertIsNotNone(doc.card)
                self.assertEqual(doc.issues, [])

    def test_the_notes_file_comes_from_the_page_not_from_a_guess(self):
        """A page whose notes are called -notes.v7.js still resolves."""
        with TemporaryDirectory() as tmp:
            root = make_project(Path(tmp) / "site", texts=("alpha",))
            old = root / "texts" / "alpha-notes.js"
            new = root / "texts" / "alpha-notes.v7.js"
            old.rename(new)
            page = root / "texts" / "alpha.html"
            page.write_text(page.read_text().replace("alpha-notes.js",
                                                     "alpha-notes.v7.js"),
                            encoding="utf-8")
            found = project.open_project(root)
            self.assertEqual(found.documents[0].notes.name, "alpha-notes.v7.js")

    def test_a_missing_notes_file_is_reported_not_invented(self):
        with TemporaryDirectory() as tmp:
            root = make_project(Path(tmp) / "site", texts=("alpha",), with_notes=False)
            found = project.open_project(root)
            doc = found.documents[0]
            self.assertTrue(any("notes" in issue for issue in doc.issues),
                            doc.issues)

    def test_an_empty_folder_says_what_is_wrong(self):
        with TemporaryDirectory() as tmp:
            found = project.open_project(Path(tmp))
            self.assertEqual(found.documents, [])
            self.assertTrue(any("index.html" in issue for issue in found.issues))
            self.assertTrue(any("texts/" in issue for issue in found.issues))

    def test_a_file_is_not_a_project(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.txt"
            path.write_text("x", encoding="utf-8")
            with self.assertRaises(InputError):
                project.open_project(path)

    def test_a_missing_folder_is_an_error_not_a_crash(self):
        with self.assertRaises(InputError):
            project.open_project(Path("/nope/definitely/not/here"))

    def test_a_project_without_all_html_still_opens(self):
        with TemporaryDirectory() as tmp:
            root = make_project(Path(tmp) / "site", with_card=False)
            found = project.open_project(root)
            self.assertIsNone(found.all_html)
            self.assertTrue(any("all.html" in issue for issue in found.issues))
            self.assertEqual(len(found.documents), 2)

    def test_the_real_repository_is_discovered(self):
        found = project.open_project(ROOT)
        self.assertIsNotNone(found.landing)
        names = {doc.name for doc in found.documents}
        self.assertIn("cisg", names)
        for doc in found.documents:
            self.assertTrue(doc.page.exists())
            self.assertIsNotNone(doc.notes, doc.name)
            self.assertTrue(doc.notes.exists(), doc.name)


class NoteStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.path = self.base / "notes.js"
        self.path.write_text(render.render_notes_js({
            "para-a": [{"author": "A", "text": "First note."},
                       {"author": "B", "text": "Second note."}],
        }), encoding="utf-8")
        self.store = notestore.NoteStore.load(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_loading_keeps_the_format(self):
        self.assertEqual(self.store.count, 2)
        self.assertEqual(self.store.authors(), ["A", "B"])
        self.assertFalse(self.store.dirty)

    def test_add_edit_delete_and_undo(self):
        self.store.add("para-a", {"author": "C", "text": "Third note."})
        self.assertEqual(self.store.count, 3)
        self.assertTrue(self.store.dirty)
        self.store.update("para-a", 2, {"author": "C", "text": "Third note, edited."})
        self.assertEqual(self.store.notes["para-a"][2]["text"], "Third note, edited.")
        self.store.delete("para-a", 2)
        self.assertEqual(self.store.count, 2)
        self.assertTrue(self.store.undo())
        self.assertEqual(self.store.count, 3)
        self.assertTrue(self.store.redo())
        self.assertEqual(self.store.count, 2)

    def test_a_note_without_text_is_refused(self):
        with self.assertRaises(InputError):
            self.store.add("para-a", {"author": "X", "text": "   "})

    def test_reordering_stays_inside_the_paragraph(self):
        self.store.move("para-a", 1, -1)
        self.assertEqual([n["author"] for n in self.store.notes["para-a"]], ["B", "A"])
        with self.assertRaises(InputError):
            self.store.move("para-a", 0, -1)

    def test_saving_is_atomic_and_readable(self):
        self.store.add("para-a", {"author": "C", "text": "Third."})
        self.store.save()
        again = notestore.NoteStore.load(self.path)
        self.assertEqual(again.count, 3)
        self.assertEqual(list(again.notes), list(self.store.notes))
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())

    def test_an_outside_edit_is_not_overwritten(self):
        self.store.add("para-a", {"author": "C", "text": "Mine."})
        # something else rewrites the file
        other = notestore.NoteStore.load(self.path)
        other.add("para-a", {"author": "D", "text": "Theirs."})
        other.save()
        with self.assertRaises(InputError) as caught:
            self.store.save()
        self.assertIn("changed by something else", caught.exception.message)
        self.assertIn("Theirs", self.path.read_text())

    def test_an_unreadable_file_is_not_written_over(self):
        broken = self.base / "broken-notes.js"
        broken.write_text("window.NOTES = { not json", encoding="utf-8")
        with self.assertRaises(InputError):
            notestore.NoteStore.load(broken)
        stand_in = notestore.NoteStore.unreadable_store(broken, "could not be read")
        self.assertEqual(stand_in.count, 0)
        with self.assertRaises(InputError):
            stand_in.save()
        self.assertEqual(broken.read_text(), "window.NOTES = { not json")

    def test_orphans_are_recognised(self):
        store = notestore.NoteStore.load(self.path, {"para-a": "text"})
        self.assertFalse(store.is_orphan("para-a"))
        self.assertTrue(store.is_orphan("para-missing"))
        rows = store.search(only_orphans=True)
        self.assertEqual(rows, [])

    def test_search_covers_every_field(self):
        store = notestore.NoteStore.load(
            self.path, {"para-a": "The paragraph mentions arbitration."})
        self.assertEqual(len(store.search("arbitration")), 2)   # both notes share it
        self.assertEqual(len(store.search("Second")), 1)
        self.assertEqual(len(store.search("nobody")), 0)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.path = self.base / "notes.js"
        self.path.write_text(render.render_notes_js({
            "para-a": [{"author": "A", "text": "One."}],
            "para-b": [{"author": "A", "text": "Two."}],
        }), encoding="utf-8")
        self.store = notestore.NoteStore.load(self.path, {"para-a": "x", "para-b": "y"})

    def tearDown(self):
        self._tmp.cleanup()

    def _incoming(self, notes):
        return merge.Incoming(path=self.base / "in.txt", notes=notes, unplaced=[],
                              total=sum(len(v) for v in notes.values()), label="in.txt")

    def test_a_duplicate_is_recognised(self):
        plan = merge.plan_merge(self.store, self._incoming(
            {"para-a": [{"author": "A", "text": "One."}]}))
        self.assertEqual(len(plan.duplicates), 1)
        self.assertEqual(plan.new, [])
        self.assertEqual(plan.conflicts, [])

    def test_the_example_from_the_brief(self):
        """A B C + B C D E  ->  A B C D E, with no duplicates."""
        plan = merge.plan_merge(self.store, self._incoming({
            "para-b": [{"author": "A", "text": "Two."}],
            "para-c": [{"author": "C", "text": "Four."},
                       {"author": "C", "text": "Five."}],
        }))
        added, _replaced, skipped = plan.apply(self.store)
        self.assertEqual(added, 2)
        self.assertEqual(skipped, 1)
        self.assertEqual(self.store.count, 4)
        texts = [n["text"] for _p, _i, n in self.store.entries()]
        self.assertEqual(texts, ["One.", "Two.", "Four.", "Five."])

    def test_a_small_copy_edit_is_a_duplicate_not_a_new_note(self):
        """The project's rule: one note largely contained in the other."""
        long_text = ("The Parties shall take all appropriate measures to prevent "
                     "and mitigate the effects of climate change in accordance "
                     "with their common but differentiated responsibilities, "
                     "and shall report on the measures they have taken.")
        self.store.notes["para-a"] = [{"author": "A", "text": long_text}]
        edited = long_text.replace("they have taken", "they have adopted")
        plan = merge.plan_merge(self.store, self._incoming(
            {"para-a": [{"author": "A", "text": edited}]}))
        self.assertEqual(len(plan.duplicates), 1, plan.counts())

    def test_a_large_rewrite_is_not_treated_as_the_same_note(self):
        long_text = ("The Parties shall take all appropriate measures to prevent "
                     "and mitigate the effects of climate change in accordance "
                     "with their common but differentiated responsibilities, "
                     "and shall report on the measures they have taken.")
        self.store.notes["para-a"] = [{"author": "A", "text": long_text}]
        rewritten = ("Something else entirely about liability and compensation, "
                     "with none of the wording of the original note left in it.")
        plan = merge.plan_merge(self.store, self._incoming(
            {"para-a": [{"author": "A", "text": rewritten}]}))
        self.assertEqual(plan.duplicates, [])
        self.assertEqual(len(plan.new), 1)

    def test_a_series_that_differs_by_a_number_is_not_a_duplicate(self):
        base = ("See Advisory Council Opinion No %d for analysis of this "
                "provision: https://example.org/opinion-%d/.")
        self.store.notes["para-a"] = [{"author": "A", "text": base % (14, 14)}]
        plan = merge.plan_merge(self.store, self._incoming(
            {"para-a": [{"author": "A", "text": base % (15, 15)}]}))
        self.assertEqual(plan.duplicates, [])
        self.assertEqual(len(plan.new) + len(plan.conflicts), 1)

    def test_a_real_edit_is_offered_as_a_conflict(self):
        plan = merge.plan_merge(self.store, self._incoming(
            {"para-a": [{"author": "A", "text": "One, but rewritten a little."}]}))
        self.assertEqual(len(plan.conflicts), 1)
        self.assertIn("probably an edit", plan.conflicts[0].reason)

    def test_conflict_choices(self):
        incoming = self._incoming(
            {"para-a": [{"author": "A", "text": "One, but rewritten a little."}]})
        for choice, expected in ((merge.KEEP_EXISTING, "One."),
                                 (merge.TAKE_INCOMING, "One, but rewritten a little."),
                                 (merge.KEEP_BOTH, None)):
            store = notestore.NoteStore.load(self.path, {"para-a": "x"})
            plan = merge.plan_merge(store, incoming)
            plan.apply(store, {0: choice})
            texts = [n["text"] for _p, _i, n in store.entries()
                     if _p == "para-a"]
            if expected is None:
                self.assertEqual(len(texts), 2, choice)
            else:
                self.assertEqual(texts, [expected], choice)

    def test_the_incoming_file_repeating_itself_is_counted_separately(self):
        plan = merge.plan_merge(self.store, self._incoming({
            "para-c": [{"author": "C", "text": "Four."},
                       {"author": "C", "text": "Four."}]}))
        self.assertEqual(plan.internal_duplicates, 1)
        self.assertEqual(len(plan.new), 1)

    def test_a_note_on_another_paragraph_is_new(self):
        plan = merge.plan_merge(self.store, self._incoming(
            {"para-z": [{"author": "A", "text": "One."}]}))
        self.assertEqual(len(plan.new), 1)
        self.assertEqual(plan.duplicates, [])


class IncomingFileTests(unittest.TestCase):
    """Reading the annotation formats, through the project's own parsers."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.source = sourcefmt.parse_source_text(
            "TITLE: T\n---\n\n{a} ¶ The first paragraph of the text.\n")

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_text_format_is_read_and_placed(self):
        path = self.base / "a.txt"
        path.write_text("""@a
Author: A. Writer
Title: A heading
Source: Somewhere (2020)
The note itself.
""", encoding="utf-8")
        incoming = merge.load_incoming(path, self.source)
        self.assertEqual(incoming.count, 1)
        note = incoming.notes["para-a"][0]
        self.assertEqual(note["author"], "A. Writer")
        self.assertEqual(note["title"], "A heading")
        self.assertEqual(note["text"], "The note itself.")

    def test_an_excerpt_is_matched_to_its_paragraph(self):
        path = self.base / "b.txt"
        path.write_text('"""\nThe first paragraph of the text.\n"""\n'
                        "Author: B\nMatched by excerpt.\n", encoding="utf-8")
        incoming = merge.load_incoming(path, self.source)
        self.assertEqual(incoming.count, 1)
        self.assertIn("para-a", incoming.notes)

    def test_json_is_read(self):
        path = self.base / "c.json"
        path.write_text(json.dumps([{"para": "a", "author": "C", "text": "From JSON."}]),
                        encoding="utf-8")
        incoming = merge.load_incoming(path, self.source)
        self.assertEqual(incoming.notes["para-a"][0]["text"], "From JSON.")

    def test_a_word_file_is_read(self):
        from tests.helpers import build_docx
        path = build_docx(self.base / "notes.docx",
                          ["The first paragraph of the text."],
                          [("A. Reviewer", "A comment on the first paragraph.",
                            0, "The first paragraph of the text.")])
        incoming = merge.load_incoming(path, self.source)
        self.assertEqual(incoming.total, 1)
        self.assertIn("para-a", incoming.notes)

    def test_a_broken_file_is_reported_not_guessed(self):
        path = self.base / "d.json"
        path.write_text("{not json at all", encoding="utf-8")
        with self.assertRaises(Exception):
            merge.load_incoming(path, self.source)

    def test_without_a_source_the_notes_that_name_a_paragraph_still_load(self):
        path = self.base / "e.txt"
        path.write_text("@para-a\nAuthor: E\nNamed outright.\n", encoding="utf-8")
        incoming = merge.load_incoming(path, None)
        self.assertEqual(incoming.count, 1)
        self.assertFalse(incoming.needs_source)   # it named its paragraph

    def test_without_a_source_an_excerpt_is_reported_not_guessed(self):
        path = self.base / "f.txt"
        path.write_text('"""\nThe first paragraph of the text.\n"""\n'
                        "Author: F\nAnchored to an excerpt.\n", encoding="utf-8")
        incoming = merge.load_incoming(path, None)
        self.assertEqual(incoming.count, 0)
        self.assertEqual(len(incoming.unplaced), 1)
        self.assertTrue(incoming.needs_source)
        self.assertIn("no source text", incoming.unplaced[0][1])

    def test_export_round_trips_through_the_text_format(self):
        from ilatool.notestore import render_annotations_text
        store = notestore.NoteStore.load(
            self.base / "none.js", {"para-a": "The first paragraph of the text."})
        store.add("para-a", {"author": "A", "title": "T", "source": "S",
                             "text": "Round trip me."})
        text = render_annotations_text(store)
        path = self.base / "out.txt"
        path.write_text(text, encoding="utf-8")
        back = merge.load_incoming(path, self.source)
        self.assertEqual(back.count, 1)
        self.assertEqual(back.notes["para-a"][0]["text"], "Round trip me.")


class TextFormatSafetyTests(unittest.TestCase):
    """A note must never be lost by going through the text format."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.source = sourcefmt.parse_source_text(
            "TITLE: T\n---\n\n{a} ¶ The first paragraph of the text.\n")
        self.store = notestore.NoteStore.load(self.base / "none.js",
                                              {"para-a": "The first paragraph."})

    def tearDown(self):
        self._tmp.cleanup()

    def _round_trip(self, notes):
        for note in notes:
            self.store.add("para-a", note)
        path = self.base / "out.txt"
        path.write_text(notestore.render_annotations_text(self.store),
                        encoding="utf-8")
        return merge.load_incoming(path, self.source)

    def test_a_note_beginning_with_a_field_name_survives(self):
        dangerous = [
            {"author": "A", "text": "Author: see Smith (2020) for the argument."},
            {"author": "B", "text": "Title: a misleading first line"},
            {"author": "C", "text": "Source: somewhere, probably"},
        ]
        back = self._round_trip(dangerous)
        before = sorted((n.get("author", ""), n.get("text", ""))
                        for _p, _i, n in self.store.entries())
        after = sorted((n.get("author", ""), n.get("text", ""))
                       for v in back.notes.values() for n in v)
        self.assertEqual(before, after)

    def test_the_protection_is_reported(self):
        self._round_trip([{"author": "A", "text": "Author: see Smith."}])
        escapes = notestore.text_export_escapes(self.store)
        self.assertEqual(len(escapes), 1)
        self.assertIn("Author:", escapes[0])

    def test_an_ordinary_note_needs_no_protection(self):
        self._round_trip([{"author": "A", "text": "An ordinary note."}])
        self.assertEqual(notestore.text_export_escapes(self.store), [])

    def test_a_field_name_later_in_the_body_is_left_alone(self):
        back = self._round_trip(
            [{"author": "A", "text": "One line.\nAuthor: not a field"}])
        self.assertEqual(notestore.text_export_escapes(self.store), [])
        note = list(back.notes["para-a"])[0]
        self.assertIn("not a field", note["text"])


class FileSafetyTests(unittest.TestCase):
    def test_atomic_write_leaves_no_temporary_behind(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.txt"
            path.write_text("old", encoding="utf-8")
            fileio.atomic_write(path, "new")
            self.assertEqual(path.read_text(), "new")
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_a_backup_can_be_asked_for(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.txt"
            path.write_text("old", encoding="utf-8")
            made = fileio.atomic_write(path, "new", backup=True)
            self.assertIsNotNone(made)
            self.assertEqual(made.read_text(), "old")
            self.assertEqual(path.read_text(), "new")

    def test_the_fingerprint_notices_a_change_and_ignores_a_rewrite(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.txt"
            path.write_text("one", encoding="utf-8")
            state = fileio.FileState.read(path)
            self.assertTrue(state.matches())
            path.write_text("two", encoding="utf-8")
            self.assertFalse(state.matches())
            path.write_text("one", encoding="utf-8")
            self.assertTrue(state.matches())     # same content, later mtime


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
