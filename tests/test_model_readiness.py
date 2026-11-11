"""Read-only LM Studio readiness and bounded CLI discovery tests (no daemon)."""
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import lms


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.cfg = {"provider": "lmstudio", "model": None,
                    "context_tokens": 8192, "lms_context_length": 8192}
        self.model = {"type": "llm", "identifier": "chat-alias",
                      "modelKey": "publisher/chat-model",
                      "path": "publisher/chat-model/model.gguf", "contextLength": 8192}
        self.loaded = self.stack.enter_context(patch.object(lms, "loaded_models", return_value=([], "")))
        self.stack.enter_context(patch.object(lms, "load", side_effect=AssertionError("unexpected config read")))
        self.stack.enter_context(patch.object(lms, "run_cli", side_effect=AssertionError("unexpected CLI call")))

    def check_unknown(self, result):
        self.assertIsNone(result["loaded"])
        self.assertIsNone(result["context_length"])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("недоступна", result["warnings"][0])
        self.assertNotIn("не загружена", result["warnings"][0])

    def test_no_loaded_models(self):
        for selected in (None, ""):
            with self.subTest(selected=selected):
                result = lms.readiness(dict(self.cfg, model=selected))
                self.assertIs(result["loaded"], False)
                self.assertIsNone(result["context_length"])
                self.assertEqual(len(result["warnings"]), 1)
                self.assertIn("чат-модель", result["warnings"][0])

    def test_selected_missing_even_when_another_model_is_loaded(self):
        self.loaded.return_value = ([self.model], "")
        self.cfg["model"] = "other-model"
        result = lms.readiness(self.cfg)
        self.assertIs(result["loaded"], False)
        self.assertIsNone(result["context_length"])
        self.assertIn("other-model", result["warnings"][0])
        self.assertIn("не загружена", result["warnings"][0])

    def test_each_exact_identifier_alias_is_accepted(self):
        self.loaded.return_value = ([self.model], "")
        for key in ("identifier", "modelKey", "path"):
            with self.subTest(key=key):
                result = lms.readiness(dict(self.cfg, model=self.model[key]))
                self.assertEqual(result, {"warnings": [], "loaded": True, "context_length": 8192})

    def test_substrings_and_case_variants_do_not_match(self):
        self.loaded.return_value = ([self.model], "")
        for selected in ("chat", "chat-alias-v2", "chat-model", "publisher/chat",
                         "model.gguf", "CHAT-ALIAS"):
            with self.subTest(selected=selected):
                result = lms.readiness(dict(self.cfg, model=selected))
                self.assertIs(result["loaded"], False)
                self.assertTrue(result["warnings"])

    def test_identifier_takes_priority_over_another_models_disk_alias(self):
        other = dict(self.model, identifier="other", modelKey="chat-alias", contextLength=1024)
        self.loaded.return_value = ([other, self.model], "")
        result = lms.readiness(dict(self.cfg, model="chat-alias"))
        self.assertEqual(result, {"warnings": [], "loaded": True, "context_length": 8192})

    def test_auto_selection_rejects_embeddings(self):
        for embedding in ({"type": "embedding", "identifier": "friendly-alias"},
                          {"identifier": "text-embedding-v1"},
                          {"identifier": "friendly", "modelKey": "publisher/embedding-model"},
                          {"type": "audio", "identifier": "speech-model"}):
            with self.subTest(embedding=embedding):
                self.loaded.return_value = ([embedding], "")
                result = lms.readiness(self.cfg)
                self.assertIs(result["loaded"], False)
                self.assertTrue(result["warnings"])

    def test_auto_selection_skips_embedding_before_chat(self):
        embedding = {"type": "embedding", "identifier": "vectors", "contextLength": 512}
        self.loaded.return_value = ([embedding, self.model], "")
        self.assertEqual(lms.readiness(self.cfg),
                         {"warnings": [], "loaded": True, "context_length": 8192})

    def test_legacy_model_without_type_is_accepted(self):
        self.loaded.return_value = ([{"identifier": "chat-model"}], "")
        self.assertEqual(lms.readiness(self.cfg),
                         {"warnings": [], "loaded": True, "context_length": None})

    def test_context_uses_maximum_configured_limit_for_both_json_shapes(self):
        for context in ({"contextLength": 4096}, {"config": {"contextLength": 4096}}):
            for limits in ((8192, 16384), (16384, 8192)):
                with self.subTest(context=context, limits=limits):
                    self.loaded.return_value = ([dict(identifier="chat", type="llm", **context)], "")
                    cfg = dict(self.cfg, context_tokens=limits[0], lms_context_length=limits[1])
                    result = lms.readiness(cfg)
                    self.assertIs(result["loaded"], True)
                    self.assertEqual(result["context_length"], 4096)
                    self.assertEqual(len(result["warnings"]), 1)
                    self.assertIn("4096", result["warnings"][0])
                    self.assertIn("16384", result["warnings"][0])

    def test_only_selected_models_context_is_compared(self):
        self.loaded.return_value = ([self.model, {"identifier": "small", "contextLength": 2048}], "")
        result = lms.readiness(dict(self.cfg, model="small"))
        self.assertEqual(result["context_length"], 2048)
        self.assertTrue(result["warnings"])

    def test_equal_or_larger_context_does_not_warn(self):
        for actual in (8192, 16384):
            with self.subTest(actual=actual):
                self.loaded.return_value = ([dict(self.model, contextLength=actual)], "")
                self.assertEqual(lms.readiness(self.cfg),
                                 {"warnings": [], "loaded": True, "context_length": actual})

    def test_top_level_context_takes_priority(self):
        self.loaded.return_value = ([dict(self.model, config={"contextLength": 1024})], "")
        self.assertEqual(lms.readiness(self.cfg)["context_length"], 8192)

    def test_numeric_strings_and_nested_context_fallback(self):
        self.loaded.return_value = ([dict(self.model, contextLength=None,
                                         config={"contextLength": "4096"})], "")
        result = lms.readiness(dict(self.cfg, context_tokens="16384", lms_context_length="8192"))
        self.assertEqual(result["context_length"], 4096)
        self.assertIn("16384", result["warnings"][0])

    def test_unknown_or_invalid_context_is_not_reported_as_a_mismatch(self):
        for actual in (None, "unknown", 0, -1, True, [], 4096.5):
            with self.subTest(actual=actual):
                self.loaded.return_value = ([dict(self.model, contextLength=actual, config=None)], "")
                self.assertEqual(lms.readiness(self.cfg),
                                 {"warnings": [], "loaded": True, "context_length": None})

    def test_invalid_configured_context_is_ignored(self):
        self.loaded.return_value = ([self.model], "")
        for invalid in (None, "auto", [], True, -1):
            with self.subTest(invalid=invalid):
                result = lms.readiness(dict(self.cfg, context_tokens=invalid, lms_context_length=invalid))
                self.assertEqual(result["warnings"], [])
        result = lms.readiness(dict(self.cfg, context_tokens="auto", lms_context_length=16384))
        self.assertIn("16384", result["warnings"][0])

    def test_cli_errors_mean_unknown_not_missing(self):
        for error in ("CLI not found", "daemon timeout", "permission denied", "invalid JSON"):
            for items in ([], [self.model]):
                with self.subTest(error=error, items=items):
                    self.loaded.return_value = (items, error)
                    result = lms.readiness(dict(self.cfg, model="selected"))
                    self.check_unknown(result)
                    self.assertIn(error, result["warnings"][0])

    def test_unexpected_cli_exceptions_do_not_prevent_startup(self):
        for error in (OSError("unavailable"), RuntimeError(), TypeError("invalid response")):
            with self.subTest(error=error):
                self.loaded.side_effect = error
                self.check_unknown(lms.readiness(self.cfg))

    def test_unexpected_metadata_means_unknown(self):
        for items in (None, {}, [None], ["invalid"], [{}]):
            with self.subTest(items=items):
                self.loaded.return_value = (items, "")
                self.check_unknown(lms.readiness(self.cfg))

    def test_non_lmstudio_never_checks_cli(self):
        for provider in ("ollama", "openai", None):
            with self.subTest(provider=provider):
                self.assertEqual(lms.readiness(dict(self.cfg, provider=provider)),
                                 {"warnings": [], "loaded": None, "context_length": None})
        self.loaded.assert_not_called()

    def test_diagnostics_are_recomputed_after_config_or_snapshot_changes(self):
        self.loaded.return_value = ([self.model], "")
        self.assertIs(lms.readiness(self.cfg)["loaded"], True)
        self.cfg["model"] = "missing"
        self.assertIs(lms.readiness(self.cfg)["loaded"], False)
        self.cfg["model"] = "chat-alias"
        self.cfg["context_tokens"] = 16384
        self.assertTrue(lms.readiness(self.cfg)["warnings"])
        self.loaded.return_value = ([], "CLI not found")
        self.check_unknown(lms.readiness(self.cfg))
        self.cfg["provider"] = "ollama"
        self.assertEqual(lms.readiness(self.cfg)["warnings"], [])
        self.cfg["provider"] = "lmstudio"
        self.loaded.return_value = ([self.model], "")
        self.assertIs(lms.readiness(self.cfg)["loaded"], True)
        self.assertEqual(self.loaded.call_count, 5)
        self.loaded.assert_called_with(self.cfg)


class LoadedModelCacheTests(unittest.TestCase):
    def test_readiness_only_runs_read_only_ps_command(self):
        cfg = {"provider": "lmstudio", "model": "chat", "context_tokens": 8192}
        proc = subprocess.CompletedProcess([], 0, stdout=(
            b'[{"type":"llm","identifier":"chat","config":{"contextLength":4096}}]'), stderr=b"")
        with patch.dict(lms._CACHE, ps=None, ps_at=0, ps_err=""), \
                patch.object(lms, "cli_path", return_value="mock-lms") as cli, \
                patch.object(lms.subprocess, "run", return_value=proc) as run:
            result = lms.readiness(cfg)
            self.assertIs(result["loaded"], True)
            self.assertEqual(result["context_length"], 4096)
            self.assertTrue(result["warnings"])
            cli.assert_called_once_with(cfg)
            run.assert_called_once_with(["mock-lms", "ps", "--json"], capture_output=True,
                                        timeout=20, creationflags=lms.NO_WINDOW)

    def test_empty_failed_cli_response_is_unverified_not_missing(self):
        proc = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"")
        with patch.dict(lms._CACHE, ps=None, ps_at=0, ps_err=""), \
                patch.object(lms, "cli_path", return_value="mock-lms"), \
                patch.object(lms.subprocess, "run", return_value=proc):
            result = lms.readiness({"provider": "lmstudio", "model": "chat"})
            self.assertIsNone(result["loaded"])
            self.assertIsNone(result["context_length"])
            self.assertIn("недоступна", result["warnings"][0])

    def test_readiness_reuses_existing_cli_snapshot_without_caching_diagnostics(self):
        model = {"type": "llm", "identifier": "chat", "contextLength": 4096}
        with patch.dict(lms._CACHE, ps=[model], ps_at=time.time(), ps_err=""), \
                patch.object(lms, "json_cli", side_effect=AssertionError("unexpected CLI call")):
            result = lms.readiness({"provider": "lmstudio", "model": "chat", "context_tokens": 8192})
            self.assertTrue(result["warnings"])
            result = lms.readiness({"provider": "lmstudio", "model": "chat", "context_tokens": 4096})
            self.assertEqual(result, {"warnings": [], "loaded": True, "context_length": 4096})


class CliPathTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.cfg = {"provider": "lmstudio", "providers": {"lmstudio": {"lms_cli": "configured-cli"}}}
        self.home = os.path.abspath(os.path.join(os.path.dirname(__file__), "mock-home"))
        self.local = os.path.join(self.home, "AppData", "Local")
        self.programs = os.path.join(self.home, "Program Files")
        self.stack.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": self.local,
                                                 "PROGRAMFILES": self.programs,
                                                 "PROGRAMFILES(X86)": self.programs + " (x86)"}))
        self.stack.enter_context(patch.object(lms.os.path, "expanduser", return_value=self.home))
        self.exists = self.stack.enter_context(patch.object(lms.os.path, "isfile", return_value=False))
        self.which = self.stack.enter_context(patch.object(lms.shutil, "which", return_value=None))
        self.stack.enter_context(patch.object(lms, "load", side_effect=AssertionError("unexpected config read")))
        self.stack.enter_context(patch("glob.glob", side_effect=AssertionError("unexpected glob scan")))
        self.stack.enter_context(patch.object(lms.os, "walk", side_effect=AssertionError("unexpected directory walk")))
        self.executable = "lms.exe" if os.name == "nt" else "lms"

    def test_explicit_cli_has_priority(self):
        self.exists.return_value = True
        self.assertEqual(lms.cli_path(self.cfg), "configured-cli")
        self.exists.assert_called_once_with("configured-cli")
        self.which.assert_not_called()

    def test_standard_home_cli_has_priority_over_path(self):
        candidate = os.path.join(self.home, ".lmstudio", "bin", self.executable)
        self.exists.side_effect = lambda path: path == candidate
        self.assertEqual(lms.cli_path(self.cfg), candidate)
        self.which.assert_not_called()

    def test_path_cli_is_used_before_install_directories(self):
        self.which.return_value = "path-cli"
        self.assertEqual(lms.cli_path(self.cfg), "path-cli")
        self.which.assert_called_once_with("lms")
        self.assertEqual(self.exists.call_count, 2)

    def test_known_install_directories(self):
        for root in (os.path.join(self.local, "Programs", "LM Studio"),
                     os.path.join(self.local, "LM Studio"),
                     os.path.join(self.programs, "LM Studio")):
            for suffix in ((), ("bin",), ("resources", "app", ".webpack")):
                with self.subTest(root=root, suffix=suffix):
                    candidate = os.path.join(root, *suffix, self.executable)
                    self.exists.side_effect = lambda path: path == candidate
                    self.assertEqual(lms.cli_path(self.cfg), candidate)

    def test_missing_cli_has_a_fixed_small_probe_bound(self):
        self.assertIsNone(lms.cli_path(self.cfg))
        self.assertLessEqual(self.exists.call_count, 30)
        self.which.assert_called_once_with("lms")

    def test_empty_config_is_not_replaced_by_disk_config(self):
        self.assertIsNone(lms.cli_path({}))


if __name__ == "__main__":
    unittest.main()
