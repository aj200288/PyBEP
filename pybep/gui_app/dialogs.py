"""
Tkinter dialogs for the desktop app.

These used to live in pybep/core/data_formatter.py, which meant importing the
parsing code pulled in tkinter and made it unusable on a headless server.
They live here now, and core reaches them only through the
``column_resolver`` callback it accepts — see
core.data_formatter.resolve_soc_ocv_columns.
"""
from tkinter import (filedialog, messagebox, Toplevel, Button, Label, Frame,
                     StringVar, Radiobutton)

from ..core import guess_column_roles, format_folder, DataFormatError


def show_column_selection_dialog(df, file_label):
    """
    Show a preview of the parsed columns and let the user confirm/assign
    which column is SOC and which is OCV (also used to pick 2 out of more
    than 2 columns). Returns (soc_idx, ocv_idx), or None if cancelled.

    This is the desktop app's ``column_resolver``: pass it to
    core.load_ocv_curve / add_half_cell_data / load_soc_ocv_data.
    """
    n_cols = df.shape[1]
    guess_soc, guess_ocv = guess_column_roles(df)

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


def show_curve_type_dialog():
    """
    Show a dialog to ask user what type of curves they're formatting.

    Returns:
    - str: 'cathode', 'anode', 'battery', 'mixed', or None if cancelled
    """
    result = [None]  # Use list to allow modification in nested function

    def on_selection(choice):
        result[0] = choice
        dialog.destroy()

    # Create dialog window
    dialog = Toplevel()
    dialog.title("Curve Type Selection")
    dialog.geometry("600x300")  # Larger default size
    dialog.minsize(500, 250)     # Minimum size
    dialog.resizable(True, True)  # Allow resizing
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
    "Format Data" button flow: ask for a folder and a curve type, hand the
    work to core.format_folder, and report the outcome. The conversion
    itself lives in core so the website can offer the same feature.
    """
    folder_path = filedialog.askdirectory(
        title="Select folder containing data files to format"
    )

    if not folder_path:
        return  # User cancelled

    curve_type = show_curve_type_dialog()

    if curve_type is None:
        return  # User cancelled

    if curve_type == 'mixed':
        messagebox.showinfo(
            "Mixed Types",
            "Please separate your data into different folders according to curve type:\n"
            "- Cathode OCP files in one folder\n"
            "- Anode OCP files in another folder\n"
            "- Battery OCV files in a third folder\n\n"
            "Then run the formatter on each folder separately.")
        return

    try:
        report = format_folder(folder_path, curve_type,
                               column_resolver=show_column_selection_dialog)
    except DataFormatError as e:
        messagebox.showwarning("No Files", str(e))
        return

    message = "Data formatting complete!\n\n"
    message += f"Processed: {len(report['successful'])} files successfully\n"
    if report['failed']:
        message += f"Failed: {len(report['failed'])} files\n"
    message += f"Curve type: {curve_type.title()}\n"
    message += f"\nFormatted files saved to:\n{report['output_folder']}\n"

    if report['warnings']:
        message += "\nWarnings:\n"
        for fname, warns in report['warnings'].items():
            for w in warns:
                message += f"- {fname}: {w}\n"

    if report['failed']:
        message += "\nFailed files:\n"
        for fname, reason in report['failed'].items():
            message += f"- {fname}: {reason}\n"

    messagebox.showinfo("Formatting Complete", message)
