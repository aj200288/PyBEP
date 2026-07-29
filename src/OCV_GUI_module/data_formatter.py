import os
import csv
import numpy as np
import pandas as pd
from scipy.interpolate import interp1d
from scipy.integrate import cumulative_trapezoid as cumtrapz
from scipy.signal import savgol_filter
from scipy.optimize import minimize
# tkinter is imported lazily inside the specific GUI-dialog functions below
# (show_column_selection_dialog, show_curve_type_dialog, format_folder_data)
# rather than at module level, so this module — including the headless,
# UI-agnostic parsing/validation pipeline (read_raw_table, load_ocv_curve,
# etc.) — stays importable on servers without tkinter installed (e.g. the
# PyBEP_spletna web app).


MIN_DATA_ROWS = 4
SOC_RANGE = (-0.02, 1.02)
OCV_RANGE = (-0.5, 6.0)


class DataFormatError(ValueError):
    """Raised when an input file cannot be parsed into a valid SOC/OCV table."""
    pass


def _to_float(value):
    """
    Parse a single cell as a float. Tolerates surrounding whitespace/quotes
    and a comma used as a decimal separator (only when there is no dot,
    since the delimiter itself has already been split out by this point).
    """
    if value is None:
        return None
    text = str(value).strip()
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        text = text[1:-1].strip()
    if text == '':
        return None
    try:
        return float(text)
    except ValueError:
        pass
    if text.count(',') == 1 and '.' not in text:
        try:
            return float(text.replace(',', '.'))
        except ValueError:
            return None
    return None


def _sniff_delimited_rows(lines):
    """
    Split raw text lines into rows of fields, auto-detecting the delimiter.
    Tries comma/semicolon/tab (quote-aware via csv.reader), scoring each by
    how many rows both share the dominant field count AND are fully numeric
    under that split (comma decimals tolerated) — not just by field-count
    consistency, since a comma used as a decimal separator can otherwise be
    mistaken for the delimiter itself when the real delimiter is a
    semicolon. Falls back to splitting on arbitrary whitespace if no
    delimiter explains most rows as numeric data.
    """
    non_empty = [line for line in lines if line.strip() != '']
    if not non_empty:
        return []

    best_rows = None
    best_score = -1

    for delim in (',', ';', '\t'):
        try:
            rows = list(csv.reader(non_empty, delimiter=delim))
        except csv.Error:
            continue
        lengths = [len(r) for r in rows]
        if not lengths:
            continue
        dominant_length = max(set(lengths), key=lengths.count)
        if dominant_length < 2:
            continue
        numeric_rows = sum(
            1 for r in rows
            if len(r) == dominant_length and all(_to_float(c) is not None for c in r)
        )
        if numeric_rows > best_score:
            best_score = numeric_rows
            best_rows = rows

    # Require the winning delimiter to explain (nearly) every row as
    # numeric data, tolerating up to 2 outliers for a header/footer line.
    if best_rows is not None and best_score >= max(1, len(non_empty) - 2):
        return best_rows

    return [line.split() for line in non_empty]


def read_raw_table(file_path):
    """
    Read a .txt/.csv/.xlsx file into a DataFrame of purely numeric columns,
    auto-detecting delimiter and decimal separator, and stripping a
    contiguous header block (top) and footer block (bottom) of non-numeric
    rows. A non-numeric row surrounded by numeric rows on both sides is
    treated as a hard error rather than silently skipped, since otherwise
    it's impossible to tell a real header/footer from corrupted data.

    Raises DataFormatError on anything that doesn't resolve to a clean,
    rectangular block of numeric data with at least 2 columns and
    MIN_DATA_ROWS rows.
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext == '.xlsx':
        raw_df = pd.read_excel(file_path, header=None, dtype=str)
        rows = [['' if pd.isna(c) else str(c) for c in row]
                for row in raw_df.values.tolist()]
    elif ext in ('.txt', '.csv'):
        with open(file_path, 'r', encoding='utf-8-sig', errors='replace') as f:
            lines = [line.rstrip('\n\r') for line in f]
        rows = _sniff_delimited_rows(lines)
    else:
        raise DataFormatError(
            f"Unsupported file type '{ext}'. Supported types: .txt, .csv, .xlsx")

    if not rows:
        raise DataFormatError("File contains no data rows.")

    parsed_rows = []
    numeric_flags = []
    for row in rows:
        cells = [c for c in row if c is not None and str(c).strip() != '']
        values = [_to_float(c) for c in cells]
        is_numeric = len(values) >= 2 and all(v is not None for v in values)
        parsed_rows.append(values if is_numeric else row)
        numeric_flags.append(is_numeric)

    start = 0
    while start < len(numeric_flags) and not numeric_flags[start]:
        start += 1
    end = len(numeric_flags)
    while end > start and not numeric_flags[end - 1]:
        end -= 1

    if start >= end:
        raise DataFormatError(
            "No numeric data rows found (the file may be entirely header/footer text).")

    body = parsed_rows[start:end]
    body_flags = numeric_flags[start:end]

    if not all(body_flags):
        bad_row_number = body_flags.index(False) + start + 1
        raise DataFormatError(
            f"Row {bad_row_number} is not numeric but appears in the middle of the "
            "data block — the file may be corrupted or use an unsupported layout.")

    lengths = {len(r) for r in body}
    if len(lengths) != 1:
        raise DataFormatError("Inconsistent number of columns across data rows.")

    n_cols = lengths.pop()
    if n_cols < 2:
        raise DataFormatError("Need at least 2 columns of data (e.g. SOC and OCV).")

    if len(body) < MIN_DATA_ROWS:
        raise DataFormatError(
            f"Not enough numeric data rows: found {len(body)}, need at least {MIN_DATA_ROWS}.")

    return pd.DataFrame(body, columns=[f"col{i}" for i in range(n_cols)])


def _heuristic_column_roles(df):
    """
    Guess which column is SOC and which is OCV from value ranges: SOC is
    expected to be bounded in roughly [0, 1] or [0, 100], OCV is not.
    """
    n_cols = df.shape[1]
    scores = []
    for i in range(n_cols):
        col = df.iloc[:, i].to_numpy(dtype=float)
        if col.min() >= -0.05 and col.max() <= 1.05:
            score = 2
        elif col.min() >= -1 and col.max() <= 105:
            score = 1
        else:
            score = 0
        scores.append(score)

    soc_idx = int(np.argmax(scores))
    remaining = [i for i in range(n_cols) if i != soc_idx]
    ocv_idx = remaining[0] if remaining else soc_idx
    return soc_idx, ocv_idx


def show_column_selection_dialog(df, file_label):
    """
    Show a preview of the parsed columns and let the user confirm/assign
    which column is SOC and which is OCV (also used to pick 2 out of more
    than 2 columns). Returns (soc_idx, ocv_idx), or None if cancelled.
    """
    from tkinter import Toplevel, Button, Label, Frame, StringVar, Radiobutton

    n_cols = df.shape[1]
    guess_soc, guess_ocv = _heuristic_column_roles(df)

    result = [None]

    dialog = Toplevel()
    dialog.title("Confirm SOC / OCV Columns")
    dialog.geometry("600x360")
    dialog.minsize(500, 320)
    dialog.resizable(True, True)
    dialog.configure(bg="#2C2F33")
    dialog.transient()
    dialog.grab_set()

    Label(dialog, text=f"Confirm data columns for:\n{file_label}",
          font=("Arial", 11, "bold"), bg="#2C2F33", fg="white",
          justify="left").pack(pady=(15, 5))

    preview_frame = Frame(dialog, bg="#2C2F33")
    preview_frame.pack(pady=5, padx=15, fill="both", expand=True)

    header_row = Frame(preview_frame, bg="#2C2F33")
    header_row.pack(fill="x")
    Label(header_row, text="", width=6, bg="#2C2F33").pack(side="left")
    for i in range(n_cols):
        preview_vals = ", ".join(f"{v:.4g}" for v in df.iloc[:5, i])
        Label(header_row, text=f"col {i}\n{preview_vals}", font=("Arial", 8),
              bg="#2C2F33", fg="white", width=18, justify="left",
              wraplength=140).pack(side="left", padx=2)

    soc_var = StringVar(value=str(guess_soc))
    ocv_var = StringVar(value=str(guess_ocv))

    def make_role_row(label_text, var):
        row = Frame(preview_frame, bg="#2C2F33")
        row.pack(fill="x", pady=2)
        Label(row, text=label_text, width=6, bg="#2C2F33", fg="white").pack(side="left")
        for i in range(n_cols):
            Radiobutton(row, variable=var, value=str(i), bg="#2C2F33",
                        activebackground="#2C2F33", selectcolor="#2C2F33",
                        width=18).pack(side="left", padx=2)

    make_role_row("SOC", soc_var)
    make_role_row("OCV", ocv_var)

    status_label = Label(dialog, text="", font=("Arial", 9), bg="#2C2F33", fg="#FF6B6B")
    status_label.pack(pady=(5, 0))

    def on_confirm():
        soc_i, ocv_i = int(soc_var.get()), int(ocv_var.get())
        if soc_i == ocv_i:
            status_label.config(text="SOC and OCV must be different columns.")
            return
        result[0] = (soc_i, ocv_i)
        dialog.destroy()

    def on_cancel():
        result[0] = None
        dialog.destroy()

    button_frame = Frame(dialog, bg="#2C2F33")
    button_frame.pack(pady=15)
    Button(button_frame, text="Confirm", command=on_confirm, width=12).pack(side="left", padx=5)
    Button(button_frame, text="Cancel", command=on_cancel, width=12).pack(side="left", padx=5)

    dialog.wait_window()
    return result[0]


def resolve_soc_ocv_columns(df, file_label, column_choice=None):
    """
    Determine which dataframe columns are SOC and OCV. If column_choice
    (soc_idx, ocv_idx) is given, it's applied directly without a dialog —
    used to reuse a folder-level confirmation across files. Otherwise the
    confirmation dialog is always shown.

    Returns (x_values, y_values, (soc_idx, ocv_idx)).
    """
    if column_choice is None:
        column_choice = show_column_selection_dialog(df, file_label)
        if column_choice is None:
            raise DataFormatError("Column selection cancelled by user.")

    soc_idx, ocv_idx = column_choice
    if soc_idx >= df.shape[1] or ocv_idx >= df.shape[1]:
        raise DataFormatError(
            f"Column mapping (SOC=col{soc_idx}, OCV=col{ocv_idx}) does not match "
            f"this file's {df.shape[1]} columns.")

    x_values = df.iloc[:, soc_idx].to_numpy(dtype=float)
    y_values = df.iloc[:, ocv_idx].to_numpy(dtype=float)
    return x_values, y_values, (soc_idx, ocv_idx)


def normalize_soc_scale(x_values):
    """Convert a 0-100 percentage-scale SOC to a 0-1 fraction, if detected."""
    if np.nanmax(x_values) > 1.5:
        return x_values / 100.0, True
    return x_values, False


def validate_range(x_values, y_values, curve_type):
    """Sanity-check value ranges; returns human-readable warnings (non-fatal)."""
    warnings = []
    if x_values.min() < SOC_RANGE[0] or x_values.max() > SOC_RANGE[1]:
        warnings.append(
            f"SOC values fall outside the expected [0, 1] range "
            f"({x_values.min():.3f} to {x_values.max():.3f}) — check the source data/columns.")
    if y_values.min() < OCV_RANGE[0] or y_values.max() > OCV_RANGE[1]:
        warnings.append(
            f"OCV values fall outside the expected battery voltage range "
            f"({y_values.min():.3f} to {y_values.max():.3f} V) — check the source data/columns.")
    return warnings


def _is_monotonic_sequence(x_values, tolerance_fraction=0.02):
    """
    Check whether x_values is monotonic in its original (file) order,
    tolerating small noise-scale local reversals. A dataset with a large
    fraction of steps going against the dominant direction (e.g. a
    charge+discharge hysteresis loop concatenated in one file) is flagged
    as non-monotonic.
    """
    diffs = np.diff(x_values)
    data_range = np.max(x_values) - np.min(x_values)
    if data_range == 0 or len(diffs) == 0:
        return False
    tol = tolerance_fraction * data_range / len(x_values)
    n_increasing = np.sum(diffs > tol)
    n_decreasing = np.sum(diffs < -tol)
    frac_against = min(n_increasing, n_decreasing) / len(diffs)
    return bool(frac_against < 0.02)


def fix_duplicates_inplace(x_values, y_values, tolerance=1e-10):
    """
    Fix duplicate x values by replacing them with interpolated values between neighbors.
    This preserves the original array length (e.g., keeps exactly 1001 points).
    Assumes x_values is already sorted in ascending order.

    Parameters:
    - x_values (numpy.ndarray): Original x values
    - y_values (numpy.ndarray): Original y values
    - tolerance (float): Tolerance for considering x values as duplicates

    Returns:
    - numpy.ndarray, numpy.ndarray: x and y values with duplicates fixed
    """
    x_fixed = x_values.copy()
    y_fixed = y_values.copy()

    # Iteratively fix duplicates until none remain
    max_iterations = 100  # Safety limit to prevent infinite loops
    iteration = 0

    while iteration < max_iterations:
        # Find duplicates using tolerance
        rounded_x = np.round(x_fixed / tolerance) * tolerance
        unique_x, counts = np.unique(rounded_x, return_counts=True)

        # If no duplicates found, we're done
        if np.all(counts == 1):
            break

        # Process each group of duplicates
        duplicates_fixed = False

        for unique_val in unique_x[counts > 1]:
            # Find all indices where this duplicate occurs
            duplicate_indices = np.where(np.abs(rounded_x - unique_val) < tolerance)[0]

            if len(duplicate_indices) > 1:
                # Find the range to interpolate within
                first_idx = duplicate_indices[0]
                last_idx = duplicate_indices[-1]

                # Get boundary points for interpolation
                left_idx = max(0, first_idx - 1)
                right_idx = min(len(x_fixed) - 1, last_idx + 1)

                # If duplicates are at the edges, use available neighbors
                if first_idx == 0:
                    # Duplicates at start - use right neighbor
                    if right_idx < len(x_fixed) - 1:
                        left_val = x_fixed[right_idx]
                        right_val = x_fixed[right_idx + 1] if right_idx + 1 < len(x_fixed) else left_val + tolerance
                    else:
                        left_val = x_fixed[0]
                        right_val = left_val + len(duplicate_indices) * tolerance
                elif last_idx == len(x_fixed) - 1:
                    # Duplicates at end - use left neighbor
                    if left_idx > 0:
                        right_val = x_fixed[left_idx]
                        left_val = x_fixed[left_idx - 1] if left_idx - 1 >= 0 else right_val - tolerance
                    else:
                        right_val = x_fixed[-1]
                        left_val = right_val - len(duplicate_indices) * tolerance
                else:
                    # Duplicates in middle - use both neighbors
                    left_val = x_fixed[left_idx]
                    right_val = x_fixed[right_idx]

                # Create interpolated x values for the duplicate group
                if len(duplicate_indices) == 2:
                    # Simple case: replace with midpoint
                    x_fixed[duplicate_indices] = np.array([
                        (left_val + right_val) / 2 - tolerance/2,
                        (left_val + right_val) / 2 + tolerance/2
                    ])
                else:
                    # Multiple duplicates: distribute evenly
                    new_x_values = np.linspace(left_val + tolerance, right_val - tolerance, len(duplicate_indices))
                    x_fixed[duplicate_indices] = new_x_values

                # Interpolate corresponding y values
                # Use linear interpolation between boundary points
                left_y = y_fixed[left_idx] if left_idx != first_idx else y_fixed[duplicate_indices[0]]
                right_y = y_fixed[right_idx] if right_idx != last_idx else y_fixed[duplicate_indices[-1]]

                # Linear interpolation for y values
                new_y_values = np.linspace(left_y, right_y, len(duplicate_indices))
                y_fixed[duplicate_indices] = new_y_values

                duplicates_fixed = True

        if not duplicates_fixed:
            break  # No more duplicates to fix

        iteration += 1

    if iteration >= max_iterations:
        print(f"Warning: Maximum iterations reached while fixing duplicates")

    return x_fixed, y_fixed


def ensure_monotonic(x_values, y_values):
    """
    Sort (x, y) pairs by x ascending and dedup near-equal x values, so the
    data is safe to feed into interp1d. Also reports whether the data was
    already monotonic in its ORIGINAL order (allowing small noise), since a
    file where SOC genuinely doubles back (e.g. a charge+discharge sweep
    concatenated together) shouldn't be smoothed via derivative-based
    fitting even though sorting always makes it look "monotonic" after
    the fact.

    Returns (x_clean, y_clean, is_monotonic).
    """
    is_monotonic = _is_monotonic_sequence(x_values)
    order = np.argsort(x_values, kind='stable')
    x_sorted, y_sorted = x_values[order], y_values[order]
    x_clean, y_clean = fix_duplicates_inplace(x_sorted, y_sorted)
    return x_clean, y_clean, is_monotonic


def smooth_data_via_derivative(x_values, y_values, window_length=31, polyorder=3):
    """
    Smooth data using derivative filtering and integration approach from the notebook.

    Steps:
    1. Fix any duplicates in input data
    2. Interpolate to fine grid (1001 points)
    3. Calculate derivative using np.diff
    4. Smooth derivative using Savitzky-Golay filter
    5. Integrate smoothed derivative
    6. Find optimal additive constant to match original data

    Parameters:
    - x_values (numpy.ndarray): Original x values
    - y_values (numpy.ndarray): Original y values
    - window_length (int): Window length for Savitzky-Golay filter (must be odd)
    - polyorder (int): Polynomial order for Savitzky-Golay filter

    Returns:
    - numpy.ndarray, numpy.ndarray: Smoothed x and y values with 1001 points
    """
    # Fix duplicates in input data first
    x_clean, y_clean = fix_duplicates_inplace(x_values, y_values)
    # Create fine stoichiometry grid (1001 points)
    x_fine = np.linspace(x_clean.min(), x_clean.max(), 1001)
    x_fine_derivative = np.linspace(x_clean.min() + 0.0005 * (x_clean.max() - x_clean.min()),
                                   x_clean.max() - 0.0005 * (x_clean.max() - x_clean.min()), 1000)

    # Create interpolation function from cleaned data
    interp_func = interp1d(x_clean, y_clean, kind='cubic', bounds_error=True)

    # Interpolate original data to fine grid
    y_fine = interp_func(x_fine)

    # Calculate discrete derivative
    diff_y = np.diff(y_fine)

    # Smooth the derivative using Savitzky-Golay filter
    # Ensure window_length is odd and smaller than data length
    window_length = min(window_length, len(diff_y))
    if window_length % 2 == 0:
        window_length -= 1  # Make it odd
    if window_length < polyorder + 1:
        polyorder = max(1, window_length - 1)

    diff_y_smoothed = savgol_filter(diff_y, window_length=window_length, polyorder=polyorder)

    # Integrate the smoothed derivative
    y_integrated = cumtrapz(diff_y_smoothed, initial=0)

    # Ensure we have exactly 1001 points by interpolating the integrated result back to x_fine
    # y_integrated has 1000 points, we need to map it back to 1001 points
    y_integrated_interp = interp1d(x_fine_derivative, y_integrated,
                                  kind='linear', fill_value='extrapolate')

    # Create y_integrated on the full 1001-point grid
    y_integrated_full = y_integrated_interp(x_fine)

    # Find optimal additive constant by minimizing RMSD to original data
    avg_integrated = np.average(y_integrated_full)

    def cost_function(n):
        y_smooth_shifted = n + y_integrated_full
        rmsd = np.sqrt(np.sum((y_smooth_shifted - y_fine) ** 2) / len(x_fine))
        return rmsd

    # Minimize to find best additive constant
    result = minimize(cost_function, avg_integrated)
    n_optimal = result.x[0]

    # Create final smooth curve with exactly 1001 points
    y_smooth = n_optimal + y_integrated_full

    # Verify we have exactly 1001 points
    assert len(x_fine) == 1001, f"Expected 1001 x points, got {len(x_fine)}"
    assert len(y_smooth) == 1001, f"Expected 1001 y points, got {len(y_smooth)}"

    return x_fine, y_smooth


def format_data_to_1001_points(x_values, y_values, use_smoothing=True):
    """
    Convert data to exactly 1001 points with optional derivative-based smoothing.

    Parameters:
    - x_values (numpy.ndarray): Original x values
    - y_values (numpy.ndarray): Original y values
    - use_smoothing (bool): Whether to apply derivative-based smoothing

    Returns:
    - numpy.ndarray, numpy.ndarray: Formatted x and y values with 1001 points
    """
    if len(x_values) < MIN_DATA_ROWS:
        raise ValueError("Not enough data points for interpolation (need at least 4)")

    if use_smoothing and len(x_values) > 10:
        # Use derivative-based smoothing for noisy data
        try:
            x_formatted, y_formatted = smooth_data_via_derivative(x_values, y_values)
            # Fix any duplicates that might have been created during smoothing
            x_final, y_final = fix_duplicates_inplace(x_formatted, y_formatted)
            return x_final, y_final
        except Exception as e:
            print(f"Smoothing failed ({str(e)}), falling back to simple interpolation")
            # Fall through to simple interpolation below

    # Either smoothing wasn't requested, the dataset is too small for it, or
    # smoothing failed above: use plain cubic interpolation to 1001 points.
    x_clean, y_clean = fix_duplicates_inplace(x_values, y_values)
    interp_func = interp1d(x_clean, y_clean, kind='cubic', bounds_error=True)
    x_formatted = np.linspace(x_clean.min(), x_clean.max(), 1001)
    y_formatted = interp_func(x_formatted)
    x_final, y_final = fix_duplicates_inplace(x_formatted, y_formatted)
    return x_final, y_final


def check_and_correct_orientation(x_values, y_values, curve_type):
    """
    Check and correct curve orientation based on electrode type.

    Parameters:
    - x_values: X-axis data (SOC or Capacity)
    - y_values: Y-axis data (Voltage)
    - curve_type: 'cathode', 'anode', or 'battery'

    Returns:
    - corrected_x, corrected_y: Arrays with proper orientation
    """
    # Create a copy to avoid modifying original data
    x_data = np.array(x_values)
    y_data = np.array(y_values)

    # Check current orientation of y values
    y_is_ascending = y_data[-1] > y_data[0]

    if curve_type in ['cathode', 'battery']:
        # Cathodes and batteries should have ascending y values (low to high voltage)
        if not y_is_ascending:
            # Reverse only y values to make y ascending
            y_data = y_data[::-1]
            return x_data, y_data, True  # Orientation was corrected

    elif curve_type == 'anode':
        # Anodes should have descending y values (high to low voltage)
        if y_is_ascending:
            # Reverse only y values to make y descending
            y_data = y_data[::-1]
            return x_data, y_data, True  # Orientation was corrected

    return x_data, y_data, False  # No correction needed


def load_ocv_curve(file_path, curve_type, column_choice=None):
    """
    Load a single SOC/OCV file (.txt/.csv/.xlsx, any supported delimiter/
    decimal/header/footer/column-order layout) and return a clean,
    1001-point (x, y) curve.

    Parameters:
    - file_path: path to the input file.
    - curve_type: 'cathode', 'anode', or 'battery' (controls orientation
      convention, see check_and_correct_orientation).
    - column_choice: optional (soc_idx, ocv_idx) to skip the column
      confirmation dialog (used to reuse a folder-level choice).

    Returns:
    - x_final, y_final: numpy arrays, 1001 points each.
    - warnings: list of human-readable warning strings.
    - used_columns: the (soc_idx, ocv_idx) that were used, for reuse on
      subsequent files in the same folder.
    """
    warnings = []
    df = read_raw_table(file_path)

    file_label = os.path.basename(file_path)
    x_values, y_values, used_columns = resolve_soc_ocv_columns(
        df, file_label, column_choice)

    x_values, was_percentage = normalize_soc_scale(x_values)
    if was_percentage:
        warnings.append(
            "SOC values looked like a 0-100 percentage scale and were converted to 0-1.")

    x_clean, y_clean, is_monotonic = ensure_monotonic(x_values, y_values)
    if not is_monotonic:
        warnings.append(
            "SOC is not monotonic even after cleanup (possible hysteresis/duplicate "
            "sweep) — using simple interpolation instead of differential-capacity "
            "smoothing for this file.")

    x_oriented, y_oriented, _ = check_and_correct_orientation(x_clean, y_clean, curve_type)

    x_final, y_final = format_data_to_1001_points(
        x_oriented, y_oriented, use_smoothing=is_monotonic)

    warnings.extend(validate_range(x_final, y_final, curve_type))

    return x_final, y_final, warnings, used_columns


def _write_curve_file(x_values, y_values, output_path):
    """Save formatted data to a new txt file with no header: tab-separated, 6dp."""
    with open(output_path, 'w') as file:
        for x, y in zip(x_values, y_values):
            file.write(f"{x:.6f}\t{y:.6f}\n")


def show_curve_type_dialog():
    """
    Show a dialog to ask user what type of curves they're formatting.

    Returns:
    - str: 'cathode', 'anode', 'battery', 'mixed', or None if cancelled
    """
    from tkinter import Toplevel, Button, Label, Frame

    result = [None]  # Use list to allow modification in nested function

    def on_selection(choice):
        result[0] = choice
        dialog.destroy()

    # Create dialog window
    dialog = Toplevel()
    dialog.title("Curve Type Selection")
    dialog.geometry("600x300")  # Larger default size
    dialog.minsize(500, 250)     # Minimum size
    dialog.resizable(True, True) # Allow resizing
    dialog.configure(bg="#2C2F33")

    # Center the dialog
    dialog.transient()
    dialog.grab_set()

    # Add instruction label
    label = Label(dialog,
                 text="What type of curves are you formatting?",
                 font=("Arial", 12, "bold"),
                 bg="#2C2F33", fg="white")
    label.pack(pady=20)

    # Create button frame
    button_frame = Frame(dialog, bg="#2C2F33")
    button_frame.pack(pady=10)

    # Add buttons for each option
    buttons_info = [
        ("Cathode OCP", "cathode"),
        ("Anode OCP", "anode"),
        ("Battery OCV", "battery"),
        ("Mixed Types", "mixed")
    ]

    for text, value in buttons_info:
        btn = Button(button_frame, text=text,
                    command=lambda v=value: on_selection(v),
                    width=12, height=1,
                    font=("Arial", 10))
        btn.pack(side="left", padx=5)

    # Add cancel button
    cancel_btn = Button(dialog, text="Cancel",
                       command=lambda: on_selection(None),
                       font=("Arial", 10))
    cancel_btn.pack(pady=(10, 20))

    # Wait for dialog to close
    dialog.wait_window()

    return result[0]


def format_folder_data():
    """
    Main function to format all data files (.txt/.csv/.xlsx) in a selected
    folder. Opens file dialog, processes all files, creates _FORMATTED
    folder and files.
    """
    from tkinter import filedialog, messagebox

    # Open folder selection dialog
    folder_path = filedialog.askdirectory(
        title="Select folder containing data files to format"
    )

    if not folder_path:
        return  # User cancelled

    # Get all supported data files in the folder
    data_files = [f for f in os.listdir(folder_path)
                  if f.lower().endswith(('.txt', '.csv', '.xlsx'))]

    if not data_files:
        messagebox.showwarning("No Files", "No .txt/.csv/.xlsx files found in the selected folder.")
        return

    # Ask user about curve type
    curve_type = show_curve_type_dialog()

    if curve_type is None:
        return  # User cancelled

    if curve_type == 'mixed':
        messagebox.showinfo("Mixed Types",
                          "Please separate your data into different folders according to curve type:\n"
                          "- Cathode OCP files in one folder\n"
                          "- Anode OCP files in another folder\n"
                          "- Battery OCV files in a third folder\n\n"
                          "Then run the formatter on each folder separately.")
        return

    # Create output folder with _FORMATTED suffix
    folder_name = os.path.basename(folder_path)
    parent_dir = os.path.dirname(folder_path)
    output_folder = os.path.join(parent_dir, f"{folder_name}_FORMATTED")

    # Create output directory if it doesn't exist
    os.makedirs(output_folder, exist_ok=True)

    # Process each data file
    successful_files = 0
    failed_files = 0
    file_warnings = {}
    failed_reasons = {}
    column_choice = None  # confirmed once, then reused across the folder

    for data_file in data_files:
        input_path = os.path.join(folder_path, data_file)
        name_without_ext = os.path.splitext(data_file)[0]
        output_filename = f"{name_without_ext}_FORMATTED.txt"
        output_path = os.path.join(output_folder, output_filename)

        try:
            x_final, y_final, warnings, used_columns = load_ocv_curve(
                input_path, curve_type, column_choice=column_choice)
            if column_choice is None:
                column_choice = used_columns
            _write_curve_file(x_final, y_final, output_path)
            successful_files += 1
            if warnings:
                file_warnings[data_file] = warnings
        except Exception as e:
            failed_files += 1
            failed_reasons[data_file] = str(e)

    # Show results
    message = f"Data formatting complete!\n\n"
    message += f"Processed: {successful_files} files successfully\n"
    if failed_files > 0:
        message += f"Failed: {failed_files} files\n"
    message += f"Curve type: {curve_type.title()}\n"
    message += f"\nFormatted files saved to:\n{output_folder}\n"

    if file_warnings:
        message += "\nWarnings:\n"
        for fname, warns in file_warnings.items():
            for w in warns:
                message += f"- {fname}: {w}\n"

    if failed_reasons:
        message += "\nFailed files:\n"
        for fname, reason in failed_reasons.items():
            message += f"- {fname}: {reason}\n"

    messagebox.showinfo("Formatting Complete", message)
