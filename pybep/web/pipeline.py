"""
Web adapter around ``core``.

This module is the only place the website touches the calculation code, and
it deliberately contains no parsing or optimization logic of its own — it
saves uploads, calls core, and turns the results into something a template
can render. Keeping it thin is what stops the website and the desktop app
from drifting apart.

Note the matplotlib backend is forced to 'Agg' below: there is no display
on a server, and the default backend would try to open a window.
"""
import base64
import io
import os
import shutil
import tempfile
import uuid
import zipfile

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402  (must follow matplotlib.use)

from ..core import (
    DataFormatError,
    read_raw_table,
    guess_column_roles,
    load_ocv_curve,
    build_curve_entry,
    format_files,
    perform_full_optimization_parallel,
    save_optimization_result_to_json,
    DATA_FILE_EXTENSIONS,
)

PREVIEW_ROWS = 5


def allowed_file(filename):
    return os.path.splitext(filename)[1].lower() in DATA_FILE_EXTENSIONS


def create_session_workdir(existing_workdir=None):
    """
    Create a fresh temp working directory for one upload session, removing
    any previous one for the same session first (bounds disk growth from
    repeated use).
    """
    if existing_workdir and os.path.isdir(existing_workdir):
        shutil.rmtree(existing_workdir, ignore_errors=True)
    return tempfile.mkdtemp(prefix="pybep_")


def save_upload(file_storage, curve_type, workdir):
    """
    Save one uploaded file into the session's working directory and return
    (file_id, saved_path).

    The stored name is prefixed with a random id, and the user-supplied
    name is reduced to its basename, so a crafted filename can't write
    outside workdir.
    """
    filename = file_storage.filename
    if not filename or not allowed_file(filename):
        raise DataFormatError(f"Unsupported file type: {filename}")

    file_id = uuid.uuid4().hex
    dest_dir = os.path.join(workdir, curve_type)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, f"{file_id}_{os.path.basename(filename)}")
    file_storage.save(dest_path)
    return file_id, dest_path


def save_and_preview(file_storage, curve_type, workdir):
    """
    Save an uploaded file and return a dict describing it for the column
    confirmation step.

    Only the heuristic guess and a small preview are computed here; the
    actual choice comes back from the browser form. This is the web
    equivalent of the desktop app's Tkinter confirmation dialog.

    Raises DataFormatError if the file can't be parsed at all.
    """
    file_id, dest_path = save_upload(file_storage, curve_type, workdir)

    df = read_raw_table(dest_path)
    guess_soc, guess_ocv = guess_column_roles(df)

    return {
        'file_id': file_id,
        'curve_type': curve_type,
        'filename': file_storage.filename,
        'path': dest_path,
        'n_cols': df.shape[1],
        'preview_rows': df.iloc[:PREVIEW_ROWS].values.tolist(),
        'guess_soc': guess_soc,
        'guess_ocv': guess_ocv,
    }


def finalize_curve(file_path, curve_type, column_choice):
    """
    Parse+clean+resample a single file now that its SOC/OCV columns have
    been confirmed by the user in the browser.

    Returns (x_1001, y_1001, warnings).
    """
    x_final, y_final, warnings, _used_columns = load_ocv_curve(
        file_path, curve_type, column_choice=column_choice)
    return x_final, y_final, warnings


def render_result_plot(battery_soc, battery_ocv, result):
    """Draw the optimization figure and return it as a base64 PNG string."""
    fig, ax = plt.subplots(figsize=(9, 6))
    ax.plot(battery_soc, battery_ocv, 'r-', label='Measured battery OCV')
    ax.plot(battery_soc, result['calculated_battery_OCV_opt'], 'b-',
            label='Optimized Battery OCV')
    ax.plot(result['c_SOC_full'], result['r1_ns_x_c1_ns'], 'r--')
    ax.plot(result['c_SOC'], result['r1_x_c1'], 'g-', label='Optimized Cathode OCP')
    ax.plot(result['a_SOC_full'], result['w1_ns_x_a1_ns'], 'r--')
    ax.plot(result['a_SOC'], result['w1_x_a1'], 'k-', label='Optimized Anode OCP')
    ax.set_title("Optimization graph", fontsize=20, fontweight="bold",
                 color="#79c0ff", pad=20)
    ax.set_xlabel('SOC (% / 100)')
    ax.set_ylabel('OCV (V)')
    ax.grid(True)
    ax.axhline(0, color="gray", linewidth=0.8)
    ax.axvline(0, color="gray", linewidth=0.8)
    ax.legend()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def run_optimization(cathodes, anodes, battery_soc, battery_ocv, settings,
                     workdir, n_jobs):
    """
    Run the optimization once, render the plot, and write the downloadable
    JSON to workdir/result.json. The /download route just serves that file,
    so downloading never recomputes anything.

    Returns a dict of template context values for results.html.
    """
    result = perform_full_optimization_parallel(
        battery_soc, battery_ocv, cathodes, anodes,
        iterations=settings['iterations'],
        battery=settings['battery_weight'],
        derivative_inverse=settings['derivative_weight'],
        n_jobs=n_jobs,
    )

    graph_b64 = render_result_plot(battery_soc, battery_ocv, result)
    save_optimization_result_to_json(result, os.path.join(workdir, "result.json"))

    return {
        'graph': graph_b64,
        'best_cathode': result['Best Cathode Data ID'],
        'best_anode': result['Best Anode Data ID'],
        'best_parameters': result['Best Parameters'],
        'lowest_rmsd': result['Lowest RMSD'],
    }


def format_uploads(file_storages, curve_type, workdir):
    """
    The website's version of the desktop app's "Format Data" button: save
    the uploaded files, convert them all via core.format_files, and bundle
    the results into a zip the browser can download.

    Returns (report, zip_path). zip_path is None when nothing converted.
    """
    input_dir = os.path.join(workdir, 'to_format')
    os.makedirs(input_dir, exist_ok=True)

    saved_paths = []
    rejected = {}
    for file_storage in file_storages:
        try:
            _file_id, path = save_upload(file_storage, 'to_format', workdir)
            saved_paths.append(path)
        except DataFormatError as e:
            rejected[file_storage.filename] = str(e)

    output_folder = os.path.join(workdir, 'formatted')
    report = format_files(saved_paths, curve_type, output_folder)

    # save_upload prefixes stored names with a random id to keep uploads
    # from colliding; strip it back off so the user gets their own names.
    report['failed'].update(rejected)
    report['failed'] = {_original_name(k): v for k, v in report['failed'].items()}
    report['warnings'] = {_original_name(k): v for k, v in report['warnings'].items()}

    if not report['successful']:
        return report, None

    zip_path = os.path.join(workdir, 'formatted_data.zip')
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name in report['successful']:
            zf.write(os.path.join(output_folder, name), _original_name(name))
    report['successful'] = [_original_name(n) for n in report['successful']]
    return report, zip_path


def _original_name(stored_name):
    """Strip the uuid4 hex prefix that save_upload adds to stored files."""
    prefix, sep, rest = stored_name.partition('_')
    if sep and len(prefix) == 32:
        try:
            int(prefix, 16)
        except ValueError:
            return stored_name
        return rest
    return stored_name
