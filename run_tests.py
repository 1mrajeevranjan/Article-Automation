"""Run the offline test suites and exit non-zero if any of them fail.

The suites are deliberately standalone scripts rather than a pytest package — each one
prints a plain-English account of what it proved. That makes them readable, but it also
means `pytest` collects nothing (they are named tests_*.py, and pytest looks for
test_*.py), which is why CI reported "no tests ran / exit code 5" while every suite was
actually passing.

This is the entry point CI and humans should both use:

    python run_tests.py            # everything offline
    python run_tests.py --list     # just show what would run
    python run_tests.py -k parallel

Live suites that spend real API quota are excluded by name and never run here.
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# These call the real provider and spend the account's daily request allowance, so they
# are never part of an automated run. See TESTING.md.
LIVE_SUITES = {
    "tests_live_batch.py",
    "tests_live_resume.py",
    "tests_probe_models.py",
    "tests_routing_probe.py",
    "tests_routing_probe2.py",
}

# Suites that build Tk widgets. On a headless machine they need a virtual display;
# run_tests does not provide one, but it reports clearly when that is what failed.
GUI_SUITES = {
    "tests_hig_check.py",
    "tests_live_ui_check.py",
    "tests_models_everywhere_check.py",
    "tests_multisheet_check.py",
    "tests_parallel_gui_check.py",
    "tests_resume_check.py",
    "tests_runbutton_check.py",
    "tests_scroll_check.py",
    "tests_sheet_rules_check.py",
}

TIMEOUT_SECONDS = 600


def discover(pattern: str | None) -> list[Path]:
    suites = sorted(p for p in ROOT.glob("tests_*.py") if p.name not in LIVE_SUITES)
    if pattern:
        suites = [p for p in suites if pattern in p.name]
    return suites


def run_one(path: Path) -> tuple[bool, float, str]:
    started = time.time()
    try:
        result = subprocess.run(
            [sys.executable, path.name],
            cwd=ROOT, capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, time.time() - started, f"timed out after {TIMEOUT_SECONDS}s"
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, time.time() - started, output


def tail(output: str, lines: int = 12) -> str:
    return "\n".join(output.rstrip().splitlines()[-lines:])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-k", dest="pattern", help="only suites whose name contains this")
    parser.add_argument("--list", action="store_true", help="list suites and exit")
    args = parser.parse_args()

    suites = discover(args.pattern)
    if not suites:
        print("No suites matched.", file=sys.stderr)
        return 1

    if args.list:
        for path in suites:
            kind = "gui " if path.name in GUI_SUITES else "    "
            print(f"{kind}{path.name}")
        return 0

    headless = sys.platform.startswith("linux") and not os.environ.get("DISPLAY")
    if headless and any(p.name in GUI_SUITES for p in suites):
        print("No DISPLAY set — Tk suites need one. Run under: xvfb-run -a python run_tests.py\n")

    print(f"Running {len(suites)} suite(s) with {Path(sys.executable).name} "
          f"{sys.version_info.major}.{sys.version_info.minor}\n")

    failures: list[tuple[str, str]] = []
    for path in suites:
        ok, seconds, output = run_one(path)
        print(f"{'PASS' if ok else 'FAIL'}  {path.name:36s} {seconds:6.1f}s")
        if not ok:
            failures.append((path.name, output))

    print()
    if not failures:
        print(f"All {len(suites)} suites passed.")
        return 0

    for name, output in failures:
        print(f"\n{'=' * 70}\n{name}\n{'=' * 70}\n{tail(output)}")
    print(f"\n{len(failures)} of {len(suites)} suites failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
