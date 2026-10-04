import tempfile
import unittest
from pathlib import Path

from core.paths import ControlPaths
from sandbox.changes import ChangeSetError, build_changeset, export_patch, patch_text
from sandbox.generations import GenerationError, seal_generation


class PatchExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'x.txt').write_text('before')
        self.paths = ControlPaths.create(self.root / 'state', 's', self.source)
        self.work = self.root / 'working'
        self.work.mkdir()
        (self.work / 'x.txt').write_text('before')
        self.before = self.seal()
        (self.work / 'x.txt').write_text('after\n')
        (self.work / 'new.txt').write_text('new')
        self.current = self.seal()
        self.export = self.root / 'export'
        self.export.mkdir()

    def seal(self, **kwargs):
        return seal_generation(self.work, self.root / 'gens', max_bytes=100*1024**2, max_entries=10000, **kwargs)

    def changes(self):
        return build_changeset(self.paths, before_generation=self.before, current_generation=self.current, quiescent=True)

    def test_exact_sealed_patch_and_original_untouched(self):
        changes = self.changes()
        content = patch_text(changes)
        self.assertIn('No newline at end of file', content)
        self.assertIn('new file mode', content)
        exported = export_patch(self.paths, changes, changes.change_set_hash, self.export, 'change.patch', lambda text,sink: text)
        self.assertEqual(exported.read_text(), content)
        self.assertEqual((self.source / 'x.txt').read_text(), 'before')
        with self.assertRaises(ChangeSetError):
            export_patch(self.paths, changes, 'bad', self.export, 'bad.patch', lambda text,sink: text)

    def test_export_path_races_and_denied_disclosure(self):
        changes = self.changes()
        (self.export / 'escape').symlink_to(self.source, target_is_directory=True)
        for name in ('../escape', '/absolute', 'escape/p.patch'):
            with self.subTest(name=name), self.assertRaises((ChangeSetError, ValueError)):
                export_patch(self.paths, changes, changes.change_set_hash, self.export, name, lambda text,sink: text)
        with self.assertRaises(ChangeSetError):
            export_patch(self.paths, changes, changes.change_set_hash, self.export, 'no.patch', None)
        export_patch(self.paths, changes, changes.change_set_hash, self.export, 'existing.patch', lambda text,sink: text)
        with self.assertRaises(ChangeSetError):
            export_patch(self.paths, changes, changes.change_set_hash, self.export, 'existing.patch', lambda text,sink: text)

    def test_bounded_harvest_and_quiescence(self):
        with self.assertRaises(ChangeSetError):
            build_changeset(self.paths, before_generation=self.before, current_generation=self.current, quiescent=False)
        (self.work / 'large').write_bytes(b'x'*101)
        with self.assertRaises(GenerationError):
            self.seal(max_file_bytes=100)
        (self.work / 'large').unlink()
        (self.work / 'unsafe').write_bytes(b'\xff')
        current = self.seal()
        with self.assertRaises(ChangeSetError):
            build_changeset(self.paths, before_generation=self.before, current_generation=current, quiescent=True)
