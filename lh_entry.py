"""Entry point for the frozen build: LocalHarness.exe [serve|cli <вопрос>|check|wake|open|tools|config]."""
import sys

from harness.__main__ import main
from harness.stdout_fix import force_utf8

force_utf8()

KNOWN = ("serve", "cli", "chat", "check", "wake", "open", "tools", "config")


def run():
    args = sys.argv[1:]
    if args and args[0] in KNOWN:
        return main(args)
    return main(["serve"] + args)


if __name__ == "__main__":
    raise SystemExit(run())
