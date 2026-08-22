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
import json
import os
import shutil
import tempfile
import time
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
    library_curve_points,
    perform_full_optimization_parallel,
    save_optimization_result_to_json,
    write_result_as,
    DATA_FILE_EXTENSIONS,
)

PREVIEW_ROWS = 5
CURVE_PREVIEW_SIZE = (5, 3)


def allowed_file(filename):
    return os.path.splitext(filename)[1].lower() in DATA_FILE_EXTENSIONS


WORKDIR_PREFIX = 'pybep_'

# How long an untouched session directory is kept. Generous, because
# deleting one out from under somebody mid-run costs them the run; the
# routes handle a vanished directory by asking them to start again.
WORKDIR_MAX_AGE_SECONDS = 6 * 60 * 60


def is_session_workdir(path):
    """
    True only for a directory this module could have created: directly
    inside the temp root, with our prefix.

    The check lives here rather than in the caller because the only thing
    done with the answer is a recursive delete. The path arrives from a
    session cookie, and a cookie is not evidence — without this a tampered
    one names any directory on the server and create_session_workdir
    removes it.
    """
    if not isinstance(path, str) or not path:
        return False
    real = os.path.realpath(path)
    if os.path.dirname(real) != os.path.realpath(tempfile.gettempdir()):
        return False
    return os.path.basename(real).startswith(WORKDIR_PREFIX)


def purge_stale_workdirs(max_age=WORKDIR_MAX_AGE_SECONDS):
    """
    Delete session directories nobody has touched for a while, and return
    how many went.

    A session only ever cleans up its own predecessor, so without this
    every visitor who uploads once and never comes back strands a
    directory permanently — on a public site that is an unbounded disk
    leak. Called when a new session directory is made, which keeps up
    easily and costs one listdir.
    """
    root = tempfile.gettempdir()
    cutoff = time.time() - max_age
    removed = 0
    try:
        names = os.listdir(root)
    except OSError:
        return 0

    for name in names:
        if not name.startswith(WORKDIR_PREFIX):
            continue
        path = os.path.join(root, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue  # vanished under us, or not ours to remove
    return removed


def touch_workdir(workdir):
    """
    Reset a session directory's idle clock.

    purge_stale_workdirs goes by the directory's own mtime, and that only
    moves when a file is created inside it — overwriting result.json in
    place leaves it alone. Without this, a session that ran once at nine
    and then spent the day adjusting sliders becomes eligible for deletion
    at three, while somebody is still using it.
    """
    try:
        os.utime(workdir, None)
    except OSError:
        pass  # already gone; the caller's next read will say so


def create_session_workdir(existing_workdir=None):
    """
    Create a fresh temp working directory for one upload session, removing
    any previous one for the same session first (bounds disk growth from
    repeated use).
    """
    if is_session_workdir(existing_workdir) and os.path.isdir(existing_workdir):
        shutil.rmtree(existing_workdir, ignore_errors=True)
    purge_stale_workdirs()
    return tempfile.mkdtemp(prefix=WORKDIR_PREFIX)


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


STAGED_MANIFEST = 'staged.json'


def load_staged(workdir):
    """
    The files this session has handed over but not yet run, each with the
    columns already settled for it. [] when there are none.

    Kept in a file beside the uploads rather than in the session cookie:
    it grows with every file picked, and the cookie already carries the
    run's own list of them. Same reasoning that put result_view.json on
    disk.
    """
    try:
        with open(os.path.join(workdir, STAGED_MANIFEST), encoding='utf-8') as f:
            entries = json.load(f)
    except (OSError, ValueError):
        return []
    return entries if isinstance(entries, list) else []


def save_staged(workdir, entries):
    """Write the staged manifest back."""
    with open(os.path.join(workdir, STAGED_MANIFEST), 'w', encoding='utf-8') as f:
        json.dump(entries, f)


def drop_staged(workdir, entries, keep_ids):
    """
    Delete every staged file whose id is not in keep_ids, save what is
    left, and return it.

    Paths come from the manifest this module wrote, never from the
    browser, which is why removing them by name is safe here.
    """
    kept = []
    for entry in entries:
        if entry.get('file_id') in keep_ids:
            kept.append(entry)
            continue
        try:
            os.remove(entry['path'])
        except OSError:
            pass  # already gone; the manifest is what the routes read
    save_staged(workdir, kept)
    return kept


def adopt_staged(entry, workdir):
    """
    Copy one staged file into the run's own directory and return the new
    path.

    The run keeps its own copy because the two directories are emptied at
    different moments: starting over clears the staged ones, and a result
    still on screen has to stay re-runnable after that.
    """
    dest_dir = os.path.join(workdir, entry['curve_type'])
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, os.path.basename(entry['path']))
    shutil.copy2(entry['path'], dest_path)
    return dest_path


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
                 color="#00223f", pad=20)  # the logo navy
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


def render_curve_preview(curve_type, name):
    """
    A small PNG of one library curve, so a user can see what a candidate
    looks like before deciding whether to include it.

    Returns None when the name isn't in the library, which the route turns
    into a 404 rather than trusting the name enough to touch the disk.
    """
    points = library_curve_points(curve_type, name)
    if points is None:
        return None
    x, y = points

    fig, ax = plt.subplots(figsize=CURVE_PREVIEW_SIZE)
    ax.plot(x, y, '-', color='#0c78b4')  # the logo blue
    ax.set_title(name, fontsize=9, color='#00223f')
    ax.set_xlabel('SOC (% / 100)', fontsize=8)
    ax.set_ylabel('OCP (V)', fontsize=8)
    ax.tick_params(labelsize=7)
    ax.grid(True, alpha=.3)

    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=110, bbox_inches='tight')
    plt.close(fig)
    return buf.getvalue()


def result_download_path(workdir, fmt):
    """
    Path to the result file in the requested format, converting it from
    the stored JSON the first time that format is asked for.

    The JSON is written once when the optimization runs and is the source
    for the others, so switching format in the browser never re-runs the
    optimization and never gives two formats that disagree.

    Returns None if this session has no result to download.
    """
    source = os.path.join(workdir, 'result.json')
    if not os.path.exists(source):
        return None
    if fmt == 'json':
        return source

    target = os.path.join(workdir, f'result.{fmt}')
    if not os.path.exists(target):
        with open(source, encoding='utf-8') as f:
            write_result_as(json.load(f), target, fmt)
    return target


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


RESULT_VIEW = 'result_view.json'


def save_result_view(workdir, context):
    """
    Keep everything the results page needs on disk.

    The page is reached by redirect after a run, and again every time the
    user comes back to it from another tab, so these values have to
    outlive the request that produced them. The graph alone is some 40 kB
    of base64, which rules out the session cookie; this file sits beside
    result.json and dies with the session's directory.
    """
    with open(os.path.join(workdir, RESULT_VIEW), 'w', encoding='utf-8') as f:
        json.dump(context, f)


def load_result_view(workdir):
    """
    The stored results for this session, or None if there are none.

    A missing or half-written file means "nothing to show" rather than an
    error: the page it feeds simply falls back to its empty state.
    """
    try:
        with open(os.path.join(workdir, RESULT_VIEW), encoding='utf-8') as f:
            view = json.load(f)
    except (OSError, ValueError):
        return None
    # JSON has no tuples, and the optimizer's best parameters are one.
    # Without this the page would read [1, 701, 82, 982] where the desktop
    # app and every earlier version of this page read (1, 701, 82, 982).
    if isinstance(view.get('best_parameters'), list):
        view['best_parameters'] = tuple(view['best_parameters'])
    return view


FORMATTED_DIR = 'formatted'

# Both the writer below and the route that serves the file derive the
# path from this, so neither has to trust a name that came back from
# the browser.
FORMATTED_ZIP = 'formatted_data.zip'


def save_format_uploads(file_storages, curve_type, workdir):
    """
    Save the files submitted to Format Data and describe each one for the
    column-confirmation step.

    Returns (previews, rejected). Rejected maps a filename to why it could
    not be read at all; one bad file never stops the rest.
    """
    previews = []
    rejected = {}
    for file_storage in file_storages:
        try:
            previews.append(save_and_preview(file_storage, curve_type, workdir))
        except DataFormatError as e:
            rejected[file_storage.filename] = str(e)
        except Exception as e:
            rejected[file_storage.filename] = f"Unexpected error: {e}"
    return previews, rejected


def convert_uploads(paths, curve_type, column_choices, workdir, rejected=None):
    """
    Convert already-saved uploads with the columns the user confirmed, and
    bundle the output into a zip for people who want the lot in one go.

    Returns (report, zip_path); zip_path is None when nothing converted.
    Names in the report are the user's own, with save_upload's random
    prefix stripped back off.
    """
    output_folder = os.path.join(workdir, FORMATTED_DIR)
    report = format_files(paths, curve_type, output_folder,
                          column_choices=column_choices)

    report['failed'].update(rejected or {})
    report['failed'] = {original_name(k): v for k, v in report['failed'].items()}
    report['warnings'] = {original_name(k): v for k, v in report['warnings'].items()}
    # Keep the stored names too: they are what is actually on disk, and
    # the per-file download route needs them to find each file again.
    report['stored'] = list(report['successful'])
    report['successful'] = [original_name(n) for n in report['stored']]

    if not report['stored']:
        return report, None

    zip_path = os.path.join(workdir, FORMATTED_ZIP)
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for stored in report['stored']:
            zf.write(os.path.join(output_folder, stored), original_name(stored))
    return report, zip_path


def original_name(stored_name):
    """Strip the uuid4 hex prefix that save_upload adds to stored files."""
    prefix, sep, rest = stored_name.partition('_')
    if sep and len(prefix) == 32:
        try:
            int(prefix, 16)
        except ValueError:
            return stored_name
        return rest
    return stored_name


def curve_label(stored_path):
    """
    The name to show a user for one uploaded curve: their own filename,
    without save_upload's uuid prefix and without the extension.

    This label becomes the dictionary key handed to the optimizer, which
    reports it straight back as "Best Cathode/Anode Data ID". Deriving it
    here keeps that ID identical to the desktop app's, which uses the plain
    filename stem (see core.add_curves.add_half_cell_data).
    """
    return os.path.splitext(original_name(os.path.basename(stored_path)))[0]
