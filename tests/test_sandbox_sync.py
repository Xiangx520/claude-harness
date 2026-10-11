import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from harness.config import HarnessConfig
from harness.sandbox import DockerSandbox, SandboxError, excluded, workspace_archive
from harness.sandbox_sync import Entry, WorkspaceSync, read_archive


class WorkspaceSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "app.txt").write_bytes(b"original")
        self.push = Mock()
        self.syncer = WorkspaceSync(self.root, self.snapshot(), self.snapshot, self.push, SandboxError)

    def snapshot(self):
        return read_archive(workspace_archive(self.root, 1024 * 1024), 1024 * 1024, excluded, SandboxError)

    def test_remote_changes_update_project_and_save_only_previous_versions(self):
        remote = {"app.txt": Entry(b"changed"), "binary": Entry(b"\x00\xff"), "empty": Entry(None, 0o755)}
        self.syncer.synchronize(remote)
        self.assertEqual((self.root / "app.txt").read_bytes(), b"changed")
        self.assertEqual((self.root / "binary").read_bytes(), b"\x00\xff")
        self.assertTrue((self.root / "empty").is_dir())
        backups = list((self.root / ".sandbox_history").rglob("app.txt"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"original")
        self.assertFalse((self.root / ".sandbox_exports").exists())
        self.assertFalse((self.root / ".sandbox_conflicts").exists())
        self.push.assert_not_called()
        self.syncer.synchronize(remote)
        self.assertEqual(len(list((self.root / ".sandbox_history").rglob("changes.json"))), 1)

    def test_local_edits_and_independent_remote_edits_merge(self):
        (self.root / "app.txt").write_bytes(b"editor")
        remote = {"app.txt": Entry(b"original"), "new": Entry(b"agent")}
        self.syncer.synchronize(remote)
        self.assertEqual((self.root / "app.txt").read_bytes(), b"editor")
        self.assertEqual((self.root / "new").read_bytes(), b"agent")
        self.assertEqual(self.push.call_args.args[0], {"app.txt": Entry(b"editor")})

    def test_same_change_on_both_sides_is_not_a_conflict(self):
        (self.root / "app.txt").write_bytes(b"same")
        self.syncer.synchronize({"app.txt": Entry(b"same")})
        self.assertFalse(self.syncer.conflicts)
        self.assertFalse((self.root / ".sandbox_history").exists())

    def test_remote_delete_and_rename_update_original(self):
        self.syncer.synchronize({"renamed.txt": Entry(b"original")})
        self.assertFalse((self.root / "app.txt").exists())
        self.assertEqual((self.root / "renamed.txt").read_bytes(), b"original")
        self.assertEqual(list((self.root / ".sandbox_history").rglob("app.txt"))[0].read_bytes(), b"original")

    def test_local_delete_is_uploaded(self):
        (self.root / "app.txt").unlink()
        self.syncer.synchronize({"app.txt": Entry(b"original")})
        self.assertEqual(self.push.call_args.args[0], {"app.txt": None})

    def test_deleted_directory_conflicts_with_new_remote_child(self):
        (self.root / "folder").mkdir()
        self.syncer.baseline = self.snapshot()
        (self.root / "folder").rmdir()
        remote = {"app.txt": Entry(b"original"), "folder": Entry(None, 0o755), "folder/new": Entry(b"agent")}
        with self.assertRaisesRegex(SandboxError, "sync conflict"):
            self.syncer.synchronize(remote)
        self.syncer.synchronize(remote, "remote")
        self.assertEqual((self.root / "folder/new").read_bytes(), b"agent")

    def test_file_directory_conflict_resolution_selects_whole_subtree(self):
        remote = {"app.txt": Entry(None, 0o755), "app.txt/child": Entry(b"agent")}
        (self.root / "app.txt").write_bytes(b"editor")
        with self.assertRaisesRegex(SandboxError, "sync conflict"):
            self.syncer.synchronize(remote)
        self.syncer.synchronize(remote, "local")
        self.assertEqual(self.syncer.baseline, {"app.txt": Entry(b"editor")})
        self.assertEqual(self.push.call_args.args[0], {"app.txt": Entry(b"editor"), "app.txt/child": None})

    def test_matching_directory_replacement_on_both_sides_is_not_conflict(self):
        (self.root / "app.txt").unlink()
        (self.root / "app.txt").mkdir()
        (self.root / "app.txt/child").write_bytes(b"same")
        self.syncer.synchronize({"app.txt": Entry(None, 0o755), "app.txt/child": Entry(b"same")})
        self.assertFalse(self.syncer.conflicts)

    @unittest.skipUnless(os.name == "nt", "case-insensitive Windows filesystem")
    def test_case_only_rename_updates_project(self):
        self.syncer.synchronize({"APP.txt": Entry(b"original")})
        self.assertIn("APP.txt", [p.name for p in self.root.iterdir()])
        self.assertNotIn("app.txt", [p.name for p in self.root.iterdir()])

    def test_conflict_preserves_local_and_remote_and_can_resolve_either_way(self):
        for choice in ("local", "remote"):
            with self.subTest(choice=choice):
                (self.root / "app.txt").write_bytes(b"local")
                self.syncer.baseline = {"app.txt": Entry(b"original")}
                remote = {"app.txt": Entry(b"remote")}
                with self.assertRaisesRegex(SandboxError, "sync conflict"):
                    self.syncer.synchronize(remote)
                self.assertEqual((self.root / "app.txt").read_bytes(), b"local")
                self.assertEqual((self.syncer.recovery / "remote/app.txt").read_bytes(), b"remote")
                recovery = self.syncer.recovery
                with self.assertRaises(SandboxError):
                    self.syncer.synchronize(remote)
                self.assertEqual(self.syncer.recovery, recovery)
                self.syncer.synchronize(remote, choice)
                self.assertEqual((self.root / "app.txt").read_bytes(), choice.encode())
                self.assertFalse(self.syncer.conflicts)

    def test_remote_delete_conflicts_with_local_edit(self):
        (self.root / "app.txt").write_bytes(b"editor")
        with self.assertRaisesRegex(SandboxError, "sync conflict"):
            self.syncer.synchronize({})
        self.assertEqual((self.root / "app.txt").read_bytes(), b"editor")
        self.syncer.synchronize({}, "remote")
        self.assertFalse((self.root / "app.txt").exists())

    def test_failed_upload_keeps_baseline_and_remote_recovery(self):
        (self.root / "app.txt").write_bytes(b"editor")
        self.push.side_effect = SandboxError("SSH offline")
        with self.assertRaisesRegex(SandboxError, "saved at"):
            self.syncer.synchronize({"app.txt": Entry(b"original"), "new": Entry(b"agent")})
        self.assertEqual(self.syncer.baseline, {"app.txt": Entry(b"original")})
        self.assertEqual((self.syncer.recovery / "remote/new").read_bytes(), b"agent")

    def test_editor_change_during_snapshot_prevents_overwrite(self):
        original = self.snapshot()
        def snapshot():
            (self.root / "app.txt").write_bytes(b"new editor change")
            return original if not hasattr(snapshot, "called") else self.snapshot()
        def racing_snapshot():
            value = snapshot()
            snapshot.called = True
            return value
        self.syncer.snapshot = racing_snapshot
        with self.assertRaisesRegex(SandboxError, "changed during"):
            self.syncer.synchronize({"app.txt": Entry(b"agent")})
        self.assertEqual((self.root / "app.txt").read_bytes(), b"new editor change")

    def test_link_in_destination_never_redirects_write(self):
        original = self.snapshot()
        self.syncer.snapshot = lambda: original
        with patch.object(Path, "is_symlink", lambda path: path == self.root / "app.txt"):
            with self.assertRaisesRegex(SandboxError, "path is a link"):
                self.syncer.synchronize({"app.txt": Entry(b"agent")})
        self.assertEqual((self.root / "app.txt").read_bytes(), b"original")

    def test_directory_delete_preserves_excluded_credentials(self):
        (self.root / "folder").mkdir()
        (self.root / "folder/code").write_bytes(b"source")
        (self.root / "folder/.env").write_bytes(b"private")
        self.syncer.baseline = self.snapshot()
        self.syncer.synchronize({"app.txt": Entry(b"original")})
        self.assertFalse((self.root / "folder/code").exists())
        self.assertEqual((self.root / "folder/.env").read_bytes(), b"private")

    def test_archive_rejects_parent_alias_and_file_directory_collision(self):
        for names in (("folder/a", "FOLDER/b"), ("file", "file/child")):
            payload = io.BytesIO()
            with tarfile.open(fileobj=payload, mode="w") as archive:
                for name in names:
                    member = tarfile.TarInfo(name)
                    member.size = 1
                    archive.addfile(member, io.BytesIO(b"x"))
            with self.assertRaises(SandboxError):
                read_archive(payload.getvalue(), 1024, excluded, SandboxError)

    def test_failed_final_pull_retains_container_and_retry_collects_it(self):
        sandbox = DockerSandbox(HarnessConfig(self.root, "unused", sandbox_mode="docker"))
        sandbox._containers[self.root] = "owned"
        sandbox._owned.add("owned")
        sandbox._syncers[self.root] = self.syncer
        with patch.object(sandbox, "_remote_snapshot", side_effect=SandboxError("offline")), patch.object(sandbox, "_checked") as remove:
            with self.assertRaisesRegex(SandboxError, "owned"):
                sandbox.close()
            remove.assert_not_called()
        self.assertIn("owned", sandbox._owned)
        with patch.object(sandbox, "_remote_snapshot", return_value={"app.txt": Entry(b"late result")}), patch.object(sandbox, "_checked") as remove:
            sandbox.close()
            remove.assert_called_once_with(["rm", "--force", "owned"])
        self.assertEqual((self.root / "app.txt").read_bytes(), b"late result")

    def test_final_conflict_is_saved_before_container_removal(self):
        sandbox = DockerSandbox(HarnessConfig(self.root, "unused", sandbox_mode="docker"))
        sandbox._containers[self.root] = "owned"
        sandbox._owned.add("owned")
        sandbox._syncers[self.root] = self.syncer
        (self.root / "app.txt").write_bytes(b"local")
        with patch.object(sandbox, "_remote_snapshot", return_value={"app.txt": Entry(b"remote")}), patch.object(sandbox, "_checked") as remove:
            with self.assertRaisesRegex(SandboxError, "sync conflict"):
                sandbox.close()
            remove.assert_called_once_with(["rm", "--force", "owned"])
        self.assertEqual((self.syncer.recovery / "remote/app.txt").read_bytes(), b"remote")


if __name__ == "__main__":
    unittest.main()
