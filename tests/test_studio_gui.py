"""The desktop application, driven headlessly.

These are skipped unless PySide6 is installed, so the rest of the suite still
runs on a machine without Qt.  They use the ``offscreen`` platform plugin, so
no display is needed.

They are deliberately end-to-end: open a project, add, edit, duplicate,
delete, search, import with a merge, export, save, notice an outside edit.
That is the workflow the application exists for, and it is the only way to
know the pieces are wired together.
"""

from __future__ import annotations

import json
import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.helpers import ROOT
from tests.test_studio_services import make_project

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:                                     # pragma: no cover
    HAVE_QT = False

from ilatool import merge, notestore, render


def _app():
    return QApplication.instance() or QApplication([])


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class StudioWindowTests(unittest.TestCase):
    """One window, reused, over a throwaway project."""

    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def setUp(self):
        # A fresh copy of the project for every test: several of them save,
        # and a saved note must not turn up in the next test.
        from ilatool.gui.mainwindow import MainWindow
        from ilatool.gui.services import ProjectSession
        self._tmp = TemporaryDirectory()
        self.root = make_project(Path(self._tmp.name) / "site",
                                 texts=("alpha", "beta"))
        self.window = MainWindow(ProjectSession(), start=self.root)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        # closeEvent asks before discarding unsaved work, which is right in
        # the application and would block forever with no one to answer.
        self.discard()
        self.window.close()
        self.app.processEvents()
        self._tmp.cleanup()

    def discard(self):
        for store in self.window.session.stores.values():
            store.dirty = False

    # -- helpers -------------------------------------------------------------

    def select(self, name: str):
        from ilatool.gui.mainwindow import PAGE_NOTES
        doc = self.window.session.documents[name]
        self.window._select_document(doc)
        self.app.processEvents()
        return doc, self.window.session.store_for(doc)

    # -- tests ---------------------------------------------------------------

    def test_opening_a_project_fills_the_tree_and_the_overview(self):
        session = self.window.session
        self.assertIsNotNone(session.project)
        self.assertEqual(len(session.project.documents), 2)
        self.assertEqual(self.window.structure.topLevelItemCount(), 1)
        self.assertIn("Project overview", self.window.overview.toHtml())
        self.assertEqual(self.window.windowTitle(), "Annotation Studio — site")

    def test_the_table_shows_the_notes_of_the_selected_text(self):
        doc, store = self.select("alpha")
        self.assertEqual(self.window.model.rowCount(), store.count)
        self.assertEqual(self.window.doc_label.text(), "Alpha")
        self.assertIn("1 annotation", self.window.count_label.text())

    def test_adding_a_note_through_the_store_updates_the_view(self):
        doc, store = self.select("alpha")
        change = store.add("para-a", {"author": "New", "text": "Written in the app."})
        self.window.session.after_edit(change.summary)
        self.app.processEvents()
        self.assertEqual(self.window.model.rowCount(), store.count)
        self.assertEqual(self.window.model.rowCount(), 2)
        self.assertIn("unsaved", self.window.dirty_label.text())

    def test_editing_and_deleting_and_undoing(self):
        doc, store = self.select("alpha")
        store.add("para-a", {"author": "New", "text": "Second note."})
        self.window.session.after_edit("added")
        store.update("para-a", 1, {"author": "New", "text": "Second note, edited."})
        self.window.session.after_edit("edited")
        self.assertEqual(store.notes["para-a"][1]["text"], "Second note, edited.")
        store.delete("para-a", 1)
        self.window.session.after_edit("deleted")
        self.assertEqual(store.count, 1)
        self.window.on_undo()
        self.app.processEvents()
        self.assertEqual(store.count, 2)
        self.window.on_redo()
        self.app.processEvents()
        self.assertEqual(store.count, 1)

    def test_search_and_author_filters_narrow_the_table(self):
        doc, store = self.select("alpha")
        store.add("para-a", {"author": "Someone Else", "text": "A different voice."})
        self.window.session.after_edit("added")
        self.window.search.setText("different voice")
        self.app.processEvents()
        self.assertEqual(self.window.proxy.rowCount(), 1)
        self.window.search.setText("")
        self.window.author_filter.setCurrentIndex(
            self.window.author_filter.findData("Someone Else"))
        self.app.processEvents()
        self.assertEqual(self.window.proxy.rowCount(), 1)
        self.window.author_filter.setCurrentIndex(0)
        self.app.processEvents()
        self.assertEqual(self.window.proxy.rowCount(), 2)

    def test_saving_writes_the_project_file_and_makes_a_backup(self):
        doc, store = self.select("alpha")
        notes_path = Path(doc.notes)
        before = notes_path.read_text()
        store.add("para-a", {"author": "Saver", "text": "Please persist me."})
        self.assertTrue(self.window.session.save_document(doc, backup=True))
        self.assertNotEqual(notes_path.read_text(), before)
        self.assertIn("Please persist me", notes_path.read_text())
        self.assertTrue(notes_path.with_name(notes_path.name + ".bak").exists())
        self.assertFalse(store.dirty)
        self.assertIn("saved", self.window.dirty_label.text())

    def test_an_outside_edit_is_refused_rather_than_overwritten(self):
        doc, store = self.select("alpha")
        store.add("para-a", {"author": "Mine", "text": "Mine to keep."})
        notes_path = Path(doc.notes)
        notes_path.write_text(render.render_notes_js(
            {"para-a": [{"author": "Them", "text": "Written by someone else."}]}),
            encoding="utf-8")
        store.dirty = True
        self.assertFalse(self.window.session.save_document(doc))
        self.assertIn("Written by someone else", notes_path.read_text())
        self.assertTrue(any(line.level == "error" for line in self.window.session.log))

    def test_a_malformed_notes_file_opens_with_a_warning_and_no_writing(self):
        broken = self.root / "texts" / "beta-notes.js"
        original = broken.read_text()
        broken.write_text("window.NOTES = { this is not json", encoding="utf-8")
        try:
            from ilatool.gui.mainwindow import MainWindow
            from ilatool.gui.services import ProjectSession
            window = MainWindow(ProjectSession(), start=self.root)
            window.show()
            doc = window.session.documents["beta"]
            window._select_document(doc)
            self.app.processEvents()
            store = window.session.store_for(doc)
            self.assertEqual(store.count, 0)
            self.assertTrue(store.unreadable)
            self.assertFalse(window.banner.isHidden())
            self.assertFalse(window.act_save.isEnabled())
            self.assertFalse(window.act_add.isEnabled())
            self.assertFalse(window.session.save_document(doc))
            self.assertEqual(broken.read_text(), "window.NOTES = { this is not json")
            for store in window.session.stores.values():
                store.dirty = False
            window.close()
        finally:
            broken.write_text(original, encoding="utf-8")

    def test_importing_a_text_file_merges_without_duplicating(self):
        doc, store = self.select("alpha")
        start = store.count
        incoming = self.root / "incoming.txt"
        incoming.write_text(
            "@para-a\nAuthor: Imported\nTitle: From a text file\n"
            "A note that is not in the project yet.\n", encoding="utf-8")
        plan = self.window.session.import_annotations(doc, incoming)
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan.new), 1)
        added, _replaced, _skipped = plan.apply(store)
        self.assertEqual(added, 1)
        self.assertEqual(store.count, start + 1)

        # importing the same file again must change nothing
        plan2 = self.window.session.import_annotations(doc, incoming)
        self.assertEqual(plan2.new, [])
        self.assertEqual(len(plan2.duplicates), 1)
        added2, _r2, _s2 = plan2.apply(store)
        self.assertEqual(added2, 0)
        self.assertEqual(store.count, start + 1)

    def test_exporting_round_trips(self):
        doc, store = self.select("alpha")
        store.add("para-a", {"author": "Ex", "title": "T", "text": "Export me."})
        out = Path(self._tmp.name) / "exported.txt"
        self.assertTrue(self.window.session.export_annotations(doc, out, "txt"))
        back = merge.load_incoming(out, self.window.session.source_for(doc))
        self.assertEqual(back.count, store.count)
        js = Path(self._tmp.name) / "exported-notes.js"
        self.assertTrue(self.window.session.export_annotations(doc, js, "js"))
        self.assertIn("window.NOTES", js.read_text())
        data = Path(self._tmp.name) / "exported.json"
        self.assertTrue(self.window.session.export_annotations(doc, data, "json"))
        self.assertEqual(len(json.loads(data.read_text())), store.count)

    def test_the_merge_dialog_lists_new_duplicates_and_conflicts(self):
        from ilatool.gui.dialogs import MergeDialog
        doc, store = self.select("alpha")
        incoming = self.root / "mixed.txt"
        incoming.write_text(
            "@para-a\nAuthor: A. Writer\nA note about alpha, revised.\n\n"
            "@para-a\nAuthor: Fresh\nSomething entirely new.\n", encoding="utf-8")
        plan = self.window.session.import_annotations(doc, incoming)
        dialog = MergeDialog(plan, store, doc.name, self.window)
        self.assertEqual(len(plan.conflicts), 1)
        self.assertGreaterEqual(len(plan.new), 1)
        # a conflict must be resolved before the merge can be applied
        self.assertFalse(dialog.ok.isEnabled())
        dialog.decision.setCurrentIndex(1)          # replace with the imported note
        dialog.table.selectRow(0)
        dialog._decide()
        dialog._apply_to_all()
        self.assertTrue(dialog.ok.isEnabled())
        dialog.close()

    def test_the_editor_validates_before_saving(self):
        from ilatool.gui.dialogs import AnnotationEditor
        doc, store = self.select("alpha")
        editor = AnnotationEditor(store, "para-a", None, self.window)
        editor.text.setPlainText("   ")
        editor._accept()
        self.assertFalse(editor.result() == AnnotationEditor.Accepted)
        self.assertIn("some text", editor.error.text())
        editor.text.setPlainText("Now it has text.")
        editor.author.setText("Writer")
        editor._accept()
        self.assertEqual(editor.result(), AnnotationEditor.Accepted)
        self.assertEqual(editor.selected_note["text"], "Now it has text.")
        self.assertEqual(editor.selected_para, "para-a")

    def test_the_editor_exposes_every_field_the_format_has(self):
        from ilatool.gui.dialogs import AnnotationEditor
        doc, store = self.select("alpha")
        editor = AnnotationEditor(store, "para-a",
                                  {"author": "A", "title": "T", "text": "Body",
                                   "source": "S"}, self.window)
        note = editor.result_note()
        self.assertEqual(set(note), {"author", "title", "text", "source"})
        self.assertEqual(note, {"author": "A", "title": "T", "text": "Body",
                                "source": "S"})

    def test_orphaned_notes_are_visible(self):
        doc, store = self.select("alpha")
        store.add("para-not-in-source", {"author": "X", "text": "Homeless note."})
        self.window.session.after_edit("added")
        self.app.processEvents()
        self.assertTrue(store.is_orphan("para-not-in-source"))
        self.window.orphan_only.setChecked(True)
        self.app.processEvents()
        self.assertEqual(self.window.proxy.rowCount(), 1)
        self.window.orphan_only.setChecked(False)

    def test_recent_projects_remember_what_was_opened(self):
        recent = self.window.session.recent_projects()
        self.assertIn(str(self.root.resolve()), [str(Path(p).resolve()) for p in recent])

    def test_closing_a_project_clears_the_interface(self):
        self.window.session.close_project()
        self.app.processEvents()
        self.assertIsNone(self.window.session.project)
        self.assertEqual(self.window.structure.topLevelItemCount(), 0)
        self.assertIn("No project open", self.window.overview.toHtml())


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class RealProjectTests(unittest.TestCase):
    """The application against the repository it was written for."""

    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def test_the_real_project_opens_and_every_text_loads(self):
        from ilatool.gui.mainwindow import MainWindow
        from ilatool.gui.services import ProjectSession
        window = MainWindow(ProjectSession(), start=ROOT)
        window.show()
        self.app.processEvents()
        session = window.session
        self.assertIsNotNone(session.project)
        total = 0
        for doc in session.project.documents:
            store = session.store_for(doc)
            self.assertFalse(store.unreadable, doc.name)
            total += store.count
            self.assertEqual(store.count, len(store.entries()), doc.name)
        self.assertGreater(total, 100)
        # every note points at a paragraph the page actually has
        for doc in session.project.documents:
            store = session.store_for(doc)
            by_id = {b.id for b in session.source_for(doc).paragraphs}
            self.assertTrue(set(store.targets()) <= by_id, doc.name)
        window.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
