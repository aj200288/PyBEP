"""
Interactive manual test for pybep/core/data_formatter.py.

Lets you pick one or more real data files via a file dialog, runs each
through the real load_ocv_curve() pipeline (including the real Tk column-
confirmation dialog), and prints/plots the result so you can eyeball it.

Run:
    python tests/manual_test_formatter.py
"""
import os
import sys
import tkinter as tk
from tkinter import filedialog

import matplotlib.pyplot as plt

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from pybep.core import load_ocv_curve, DataFormatError
from pybep.gui_app.dialogs import show_column_selection_dialog

CURVE_TYPES = ("cathode", "anode", "battery")

root = tk.Tk()
root.withdraw()  # keep the root window hidden; only dialogs should show

file_paths = filedialog.askopenfilenames(
    title="Select one or more data files to test",
    filetypes=[("Data files", "*.txt *.csv *.xlsx"), ("All files", "*.*")],
)
if not file_paths:
    print("No files selected.")
    sys.exit(0)

print("Curve type options:", ", ".join(f"{i}={t}" for i, t in enumerate(CURVE_TYPES)))
choice = input("Curve type for this batch [0]: ").strip()
curve_type = CURVE_TYPES[int(choice)] if choice else CURVE_TYPES[0]

column_choice = None  # confirmed on first file (via dialog), reused after that
reuse = input("Reuse the same SOC/OCV column choice for all files? [y/N]: ").strip().lower() == "y"

for file_path in file_paths:
    label = os.path.basename(file_path)
    print(f"\n--- {label} ---")
    try:
        x, y, warnings, used_columns = load_ocv_curve(
            file_path, curve_type, column_choice=column_choice,
            column_resolver=show_column_selection_dialog)
    except DataFormatError as e:
        print(f"  DataFormatError: {e}")
        continue
    except Exception as e:
        print(f"  Unexpected error: {e}")
        continue

    if reuse and column_choice is None:
        column_choice = used_columns

    print(f"  columns used (soc_idx, ocv_idx): {used_columns}")
    print(f"  points: {len(x)}, x range: [{x.min():.4f}, {x.max():.4f}], "
          f"y range: [{y.min():.4f}, {y.max():.4f}]")
    for w in warnings:
        print(f"  WARNING: {w}")

    plt.figure(figsize=(6, 4))
    plt.plot(x, y, "-")
    plt.title(f"{label} ({curve_type})")
    plt.xlabel("SOC")
    plt.ylabel("OCV (V)")
    plt.grid(True)

plt.show()
