"""Git-инструмент: status/diff/log/show/add/commit/branch на временном репозитории."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
os.environ["LH_DATA"] = os.path.join(ROOT, "tests", ".tmp")
os.environ["LH_CONFIG"] = os.path.join(ROOT, "tests", ".tmp", "no-such-config.json")

from harness import permissions, schemas  # noqa: E402
from harness.tools import Toolkit  # noqa: E402


class GitToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name
        self.git("init")
        self.git("config", "user.name", "LocalHarness Test")
        self.git("config", "user.email", "test@example.invalid")
        self.tk = Toolkit({"workspace": self.repo})

    def git(self, *args):
        proc = subprocess.run(["git"] + list(args), cwd=self.repo, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def test_status_add_commit_log(self):
        res = self.tk.git(action="status")
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["clean"], res)
        self.assertTrue(res["branch"])

        self.tk.write_file("hello.txt", "hello\n")
        res = self.tk.git(action="status")
        self.assertFalse(res["clean"])
        self.assertIn("hello.txt", res["stdout"])

        res = self.tk.git(action="add", path="hello.txt")
        self.assertTrue(res["ok"], res)
        self.assertIn("hello.txt", res["staged"])
        self.assertIn("A  hello.txt", self.tk.git(action="status")["stdout"])

        res = self.tk.git(action="commit", message="первый коммит")
        self.assertTrue(res["ok"], res)
        self.assertIn("первый коммит", res["commit"])

        res = self.tk.git(action="log")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["commits"], 1)
        self.assertIn("первый коммит", res["stdout"])

        res = self.tk.git(action="show")
        self.assertTrue(res["ok"], res)
        self.assertIn("hello.txt", res["stdout"])

    def test_cyrillic_names_and_diff(self):
        self.tk.write_file("тест.txt", "старое\n")
        res = self.tk.git(action="add", all=True)
        self.assertTrue(res["ok"], res)
        self.assertIn("тест.txt", res["stdout"] + str(res["staged"]))
        self.assertTrue(self.tk.git(action="commit", message="кириллица")["ok"])

        self.tk.write_file("тест.txt", "новое\n")
        res = self.tk.git(action="diff")
        self.assertTrue(res["ok"], res)
        self.assertIn("-старое", res["stdout"])
        self.assertIn("+новое", res["stdout"])

        res = self.tk.git(action="diff", stat=True)
        self.assertTrue(res["ok"], res)
        self.assertIn("тест.txt", res["stdout"])

        self.assertTrue(self.tk.git(action="add", all=True)["ok"])
        res = self.tk.git(action="diff", staged=True)
        self.assertTrue(res["ok"], res)
        self.assertIn("+новое", res["stdout"])

    def test_branches(self):
        self.tk.write_file("a.txt", "a\n")
        self.tk.git(action="add", all=True)
        self.tk.git(action="commit", message="base")
        res = self.tk.git(action="branch", name="feat-x")
        self.assertTrue(res["ok"], res)
        res = self.tk.git(action="status")
        self.assertEqual(res["branch"], "feat-x")
        self.assertIn("* feat-x", self.tk.git(action="branch")["stdout"])
        self.assertTrue(self.tk.git(action="branch", name="feat-x")["ok"])

    def test_errors_are_explained(self):
        # TEMP внутри профиля пользователя, а профиль здесь сам может быть под git:
        # берём папку в корне диска, выше которого репозитория быть не может
        try:
            outside = tempfile.TemporaryDirectory(dir=os.path.abspath(os.sep))
        except OSError:
            self.skipTest("нет прав создавать папку в корне диска")
        self.addCleanup(outside.cleanup)
        res = self.tk.git(action="status", cwd=outside.name)
        self.assertFalse(res["ok"])
        self.assertIn("не git-репозиторий", res["error"])

        res = self.tk.git(action="commit", message="пусто")
        self.assertFalse(res["ok"])
        self.assertIn("нечего коммитить", res["error"])
        self.assertIn("hint", res)

        res = self.tk.git(action="status", path=os.path.join(self.tmp.name, "..", "outside.txt"))
        self.assertFalse(res["ok"])
        self.assertIn("вне репозитория", res["error"])

        res = self.tk.git(action="nope")
        self.assertFalse(res["ok"])
        self.assertIn("action:", res["error"])

        sub = os.path.join(self.repo, "sub")
        os.makedirs(sub)
        self.tk.write_file(os.path.join(sub, "f.txt"), "f\n")
        res = self.tk.git(action="add", all=True, cwd=sub)
        self.assertFalse(res["ok"])
        self.assertIn("только из корня", res["error"])
        self.assertTrue(self.tk.git(action="add", path=os.path.join(sub, "f.txt"))["ok"])

    def test_registered_and_permissions(self):
        names = [s["name"] for s in schemas.entries("core")]
        self.assertIn("git", names)
        native = {t["function"]["name"] for t in schemas.native_tools("core")}
        self.assertIn("git", native)

        self.assertEqual(permissions.decide("git", {"action": "status"}, "ask"), ("allow", None))
        self.assertEqual(permissions.decide("git", {"action": "commit", "message": "x"}, "ask")[0], "confirm")
        self.assertEqual(permissions.decide("git", {"action": "add", "all": True}, "ask")[0], "confirm")
        self.assertEqual(permissions.decide("git", {"action": "branch", "name": "x"}, "ask")[0], "confirm")
        self.assertEqual(permissions.decide("git", {"action": "branch"}, "ask"), ("allow", None))
        self.assertEqual(permissions.decide("git", {"action": "commit"}, "auto"), ("allow", None))


if __name__ == "__main__":
    unittest.main()
