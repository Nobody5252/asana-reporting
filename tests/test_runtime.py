"""Runtime secret selection and configured-owner protection, with invented data."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import asana_time_export as app


class RuntimeTests(unittest.TestCase):
    def test_explicit_file_accepts_bom_and_overrides_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "secret"
            path.write_text("\ufeffsynthetic-file-token\n", encoding="utf-8")
            with patch.dict("os.environ", {"ASANA_ACCESS_TOKEN": "different-token"}, clear=True):
                self.assertEqual(app.read_token(True, str(path)), "synthetic-file-token")

    def test_environment_file_for_compose(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "secret"
            path.write_text("synthetic-mounted-token\n")
            with patch.dict("os.environ", {"ASANA_ACCESS_TOKEN_FILE": str(path)}, clear=True):
                self.assertEqual(app.read_token(True), "synthetic-mounted-token")

    def test_environment_sources_cannot_be_ambiguous(self):
        with patch.dict("os.environ", {"ASANA_ACCESS_TOKEN_FILE": "/unused", "ASANA_ACCESS_TOKEN": "synthetic"}, clear=True):
            with self.assertRaisesRegex(app.ExportError, "one token source"):
                app.read_token(True)

    def test_bad_files_fail_without_falling_back_or_leaking_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "secret"
            with patch.dict("os.environ", {"ASANA_ACCESS_TOKEN": "fallback-must-not-be-used"}, clear=True), \
                    patch.object(app.getpass, "getpass") as prompt:
                for content in (b"", b" ", b"synthetic\nInjected: value", b"x" * 4097, b"\xff"):
                    path.write_bytes(content)
                    with self.assertRaises(app.ExportError) as error:
                        app.read_token(False, str(path))
                    self.assertNotIn("Injected", str(error.exception))
                with self.assertRaisesRegex(app.ExportError, "Cannot read"):
                    app.read_token(False, str(path / "missing"))
                prompt.assert_not_called()

    def test_expected_owner_mismatch_stops_before_collecting_or_writing(self):
        with patch.object(app, "read_token", return_value="synthetic"), \
                patch.object(app, "AsanaClient") as client, \
                patch.object(app, "collect_entries") as collect, \
                patch.object(app, "save_snapshot") as save, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            client.return_value.get.return_value = {"data": {"gid": "2"}}
            self.assertEqual(app.main(["--expected-owner", "1", "--non-interactive"]), 1)
            self.assertIn("does not match", errors.getvalue())
            client.return_value.get.assert_called_once()
            collect.assert_not_called()
            save.assert_not_called()

    def test_invalid_expected_owner_fails_before_reading_a_secret(self):
        with patch.object(app, "read_token") as read, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(app.main(["--expected-owner", "invalid"]), 1)
            read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
