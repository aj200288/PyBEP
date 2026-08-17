"""
Run every test file in this folder and report a combined result.

    python tests/run_all.py

Each test file is a plain script that exits non-zero on failure, so this
needs no test framework installed.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = ["test_pipeline.py", "test_gui.py", "test_web.py"]

failed = []
for name in TESTS:
    print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
    result = subprocess.run([sys.executable, os.path.join(HERE, name)])
    if result.returncode != 0:
        failed.append(name)

print(f"\n{'=' * 60}")
if failed:
    print("FAILED:", ", ".join(failed))
    sys.exit(1)
print(f"All {len(TESTS)} test files passed.")
