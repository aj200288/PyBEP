"""Non-interactive checks for the core data ingestion pipeline.

Exercises read_raw_table / resolve_soc_ocv_columns / normalize_soc_scale /
ensure_monotonic / load_ocv_curve against the fixtures in tests/fixtures
(generate them first with `python tests/make_fixtures.py`), passing an
explicit column_choice so no UI is involved.

Run:
    python tests/test_pipeline.py
"""
import os
import sys
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from pybep.core import submissions
from pybep.core.data_formatter import (
    read_raw_table, resolve_soc_ocv_columns, normalize_soc_scale,
    ensure_monotonic, load_ocv_curve, DataFormatError, guess_column_roles
)

FDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

if not os.path.isdir(FDIR) or not os.listdir(FDIR):
    print("No fixtures found - run `uv run python tests/make_fixtures.py` first.")
    sys.exit(1)

results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(("PASS" if cond else "FAIL"), "-", name, ("" if cond else f"  ({detail})"))


# 1. comma-delimited with header
df = read_raw_table(os.path.join(FDIR, "comma_header.csv"))
check("comma_header: 2 cols, 30 rows", df.shape == (30, 2), df.shape)
x, y, cols = resolve_soc_ocv_columns(df, "comma_header.csv", column_choice=(0, 1))
check("comma_header: soc range", 0 <= x.min() and x.max() <= 1.01, (x.min(), x.max()))

# 2. semicolon + comma decimals
df = read_raw_table(os.path.join(FDIR, "semicolon_commadecimal.csv"))
check("semicolon_commadecimal: 2 cols, 30 rows", df.shape == (30, 2), df.shape)
x, y, cols = resolve_soc_ocv_columns(df, "semicolon.csv", column_choice=(0, 1))
check("semicolon_commadecimal: values parsed as float not NaN", not np.isnan(x).any() and not np.isnan(y).any())

# 3. xlsx with header+footer
df = read_raw_table(os.path.join(FDIR, "with_header_footer.xlsx"))
check("xlsx header+footer: 2 cols, 30 rows", df.shape == (30, 2), df.shape)

# 4. swapped columns
df = read_raw_table(os.path.join(FDIR, "swapped_columns.txt"))
soc_idx, ocv_idx = guess_column_roles(df)
check("swapped_columns: heuristic picks col1 as SOC", soc_idx == 1, (soc_idx, ocv_idx))

# 5. percentage SOC
df = read_raw_table(os.path.join(FDIR, "percentage_soc.txt"))
x, y, cols = resolve_soc_ocv_columns(df, "percentage_soc.txt", column_choice=(0, 1))
check("percentage_soc: raw max > 1.5", x.max() > 1.5, x.max())
x_norm, was_pct = normalize_soc_scale(x)
check("percentage_soc: normalized to <=1.01 and flagged", was_pct and x_norm.max() <= 1.01, (was_pct, x_norm.max()))

# 6. non-monotonic
df = read_raw_table(os.path.join(FDIR, "non_monotonic.txt"))
x, y, cols = resolve_soc_ocv_columns(df, "non_monotonic.txt", column_choice=(0, 1))
x_clean, y_clean, is_mono = ensure_monotonic(x, y)
check("non_monotonic: flagged as not monotonic", is_mono is False, is_mono)
check("non_monotonic: x_clean still sorted ascending", np.all(np.diff(x_clean) > 0))

# 6b. control: the clean ascending fixture IS flagged monotonic
df2 = read_raw_table(os.path.join(FDIR, "comma_header.csv"))
x2, y2, _ = resolve_soc_ocv_columns(df2, "comma_header.csv", column_choice=(0, 1))
_, _, is_mono2 = ensure_monotonic(x2, y2)
check("comma_header (clean): flagged as monotonic", is_mono2 is True, is_mono2)

# 7. quoted CSV
df = read_raw_table(os.path.join(FDIR, "quoted.csv"))
check("quoted: 2 cols, 30 rows", df.shape == (30, 2), df.shape)

# 8. header + footer whitespace
df = read_raw_table(os.path.join(FDIR, "header_footer_whitespace.txt"))
check("header_footer_whitespace: 2 cols, 30 rows", df.shape == (30, 2), df.shape)

# 9. invalid interior row -> DataFormatError
try:
    read_raw_table(os.path.join(FDIR, "invalid_interior.txt"))
    check("invalid_interior: raises DataFormatError", False, "no exception raised")
except DataFormatError as e:
    check("invalid_interior: raises DataFormatError", True, str(e))

# 10. too few rows -> DataFormatError
try:
    read_raw_table(os.path.join(FDIR, "too_few_rows.txt"))
    check("too_few_rows: raises DataFormatError", False, "no exception raised")
except DataFormatError as e:
    check("too_few_rows: raises DataFormatError", True, str(e))

# Full pipeline end-to-end via load_ocv_curve (bypassing dialog) on a clean cathode-like file
x_final, y_final, warnings, used_cols = load_ocv_curve(
    os.path.join(FDIR, "comma_header.csv"), curve_type='cathode', column_choice=(0, 1))
check("load_ocv_curve: returns 1001 points", len(x_final) == 1001 and len(y_final) == 1001, len(x_final))
check("load_ocv_curve: no warnings for clean ascending cathode file", warnings == [], warnings)

# Full pipeline on percentage-SOC file
x_final2, y_final2, warnings2, used_cols2 = load_ocv_curve(
    os.path.join(FDIR, "percentage_soc.txt"), curve_type='cathode', column_choice=(0, 1))
check("load_ocv_curve percentage: 1001 points, percentage warning present",
      len(x_final2) == 1001 and any("percentage" in w for w in warnings2), warnings2)

# Full pipeline on the non-monotonic file: should fall back + warn, still produce 1001 pts
x_final3, y_final3, warnings3, used_cols3 = load_ocv_curve(
    os.path.join(FDIR, "non_monotonic.txt"), curve_type='cathode', column_choice=(0, 1))
check("load_ocv_curve non_monotonic: 1001 points, monotonic warning present",
      len(x_final3) == 1001 and any("not monotonic" in w for w in warnings3), warnings3)

# --- more than two columns --------------------------------------------
# The shape most lab exports have: a bare row index in front of the data.

df = read_raw_table(os.path.join(FDIR, "indexed_three_column.txt"))
check("indexed_three_column: 3 cols, 30 rows", df.shape == (30, 3), df.shape)
check("indexed_three_column: the index column survives as col 0",
      list(df.iloc[:3, 0]) == [1.0, 2.0, 3.0], list(df.iloc[:3, 0]))
check("indexed_three_column: index is not mistaken for SOC or OCV",
      guess_column_roles(df) == (1, 2), guess_column_roles(df))

df5 = read_raw_table(os.path.join(FDIR, "indexed_five_column.csv"))
check("indexed_five_column: 5 cols, 30 rows", df5.shape == (30, 5), df5.shape)
check("indexed_five_column: skips index, time and a constant current column",
      guess_column_roles(df5) == (2, 3), guess_column_roles(df5))

x_i, y_i, warn_i, used_i = load_ocv_curve(
    os.path.join(FDIR, "indexed_three_column.txt"), curve_type='cathode',
    column_choice=(1, 2))
check("indexed_three_column: the chosen columns give a clean curve",
      len(x_i) == 1001 and abs(x_i.min()) < 1e-9 and abs(x_i.max() - 1) < 1e-9
      and 2.4 < y_i.min() < 2.6 and 3.5 < y_i.max() < 3.7,
      (len(x_i), x_i.min(), x_i.max(), y_i.min(), y_i.max()))
check("indexed_three_column: no warnings when the right columns are used",
      warn_i == [], warn_i)

# Picking the index column as OCV must not pass quietly.
_x, _y, warn_bad, _u = load_ocv_curve(
    os.path.join(FDIR, "indexed_three_column.txt"), curve_type='cathode',
    column_choice=(1, 0))
check("indexed_three_column: using the index as OCV is flagged",
      any("outside the expected battery voltage" in w for w in warn_bad),
      warn_bad)

# --- the core/UI seam -------------------------------------------------
# core must never import a UI toolkit; it asks for one via column_resolver.

from pybep import core  # noqa: E402
import tempfile  # noqa: E402

check("core does not pull in tkinter",
      not any(m == "tkinter" or m.startswith("tkinter.") for m in sys.modules),
      [m for m in sys.modules if m.startswith("tkinter")])

calls = []


def fake_resolver(df, file_label):
    """Stand-in for the desktop app's Tkinter dialog."""
    calls.append(file_label)
    return (1, 0)  # deliberately the reverse of the real mapping


x_r, y_r, _w, used = core.load_ocv_curve(
    os.path.join(FDIR, "comma_header.csv"), curve_type='cathode',
    column_resolver=fake_resolver)
check("column_resolver is consulted when no choice is given",
      calls == ["comma_header.csv"], calls)
check("column_resolver's answer is honoured", used == (1, 0), used)


def cancelling_resolver(df, file_label):
    return None


try:
    core.load_ocv_curve(os.path.join(FDIR, "comma_header.csv"),
                        curve_type='cathode',
                        column_resolver=cancelling_resolver)
    check("cancelled resolver raises DataFormatError", False, "no exception")
except DataFormatError:
    check("cancelled resolver raises DataFormatError", True)

# With neither a choice nor a resolver, columns are guessed - and said so.
_x, _y, warns_auto, _u = core.load_ocv_curve(
    os.path.join(FDIR, "comma_header.csv"), curve_type='cathode')
check("auto-detected columns are reported as a warning",
      any("auto-detected" in w for w in warns_auto), warns_auto)

try:
    resolve_soc_ocv_columns(df, "x.txt", column_choice=(1, 1))
    check("identical SOC/OCV columns rejected", False, "no exception")
except DataFormatError:
    check("identical SOC/OCV columns rejected", True)

# --- format_files (shared by the GUI button and the website) ----------
with tempfile.TemporaryDirectory() as tmp:
    report = core.format_files(
        [os.path.join(FDIR, "comma_header.csv"),
         os.path.join(FDIR, "percentage_soc.txt"),
         os.path.join(FDIR, "too_few_rows.txt")],  # this one must fail
        curve_type="cathode",
        output_folder=os.path.join(tmp, "out"))
    check("format_files converts the good files",
          len(report["successful"]) == 2, report["successful"])
    check("format_files reports the bad file instead of aborting",
          list(report["failed"]) == ["too_few_rows.txt"], report["failed"])
    written = os.path.join(tmp, "out", report["successful"][0])
    rows = open(written).read().strip().splitlines()
    check("format_files writes 1001 rows", len(rows) == 1001, len(rows))

# --- curves people send in ---------------------------------------------
# A submission is stored under a name taken from the file it arrived as.
# Through the website that has already been reduced to a basename, but
# save_submission is a library call too, and there the name is whatever
# the caller hands over.
with tempfile.TemporaryDirectory() as tmp:
    os.environ["PYBEP_SUBMISSIONS_DIR"] = tmp
    try:
        filed = submissions.save_submission(
            "anode", "../escaped.txt", np.linspace(0.0, 1.0, 1001),
            np.linspace(1.5, 0.1, 1001), {"submitter": "A Person"})
        inside = sorted(os.listdir(os.path.join(tmp, "anode")))
        check("a submitted curve cannot be written outside its own folder",
              "/" not in filed and os.sep not in filed
              and inside == [f"{filed}.json", f"{filed}.txt", "original"]
              and not os.path.exists(os.path.join(tmp, "escaped.txt")),
              (filed, inside))
        check("and what was said about it is stored beside it",
              submissions.read_metadata("anode", filed).get("submitter")
              == "A Person", submissions.read_metadata("anode", filed))
        check("a second curve of the same name is filed beside the first, "
              "never over it",
              submissions.save_submission(
                  "anode", "escaped", np.linspace(0.0, 1.0, 1001),
                  np.linspace(1.5, 0.1, 1001), {}) != filed
              and len(submissions.submission_names("anode")) == 2,
              submissions.submission_names("anode"))
    finally:
        os.environ.pop("PYBEP_SUBMISSIONS_DIR", None)

n_pass = sum(1 for _, ok, _ in results if ok)
n_fail = sum(1 for _, ok, _ in results if not ok)
print(f"\n{n_pass} passed, {n_fail} failed")
sys.exit(0 if n_fail == 0 else 1)
