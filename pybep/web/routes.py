"""
Flask routes for the PyBEP website. HTTP orchestration only — all parsing
and optimization lives in core, reached through pipeline.py.

The upload -> confirm -> results -> download flow keeps its state in a
per-session temp directory on the server, tracked by a session cookie.
That assumes a single running instance (or sticky sessions), which is what
the small hosts this is aimed at give you anyway. Running several
instances behind a load balancer would need shared storage instead.
"""
import os

from flask import (Blueprint, render_template, request, redirect, url_for,
                   session, flash, send_file, abort, current_app)

from ..core import DataFormatError
from . import pipeline

bp = Blueprint('main', __name__)

CURVE_TYPES = ('cathode', 'anode', 'battery')


def _clamp_settings(form):
    """
    Read the optimization settings out of the form and force them into
    their valid ranges.

    This is a security boundary, not a convenience: iterations drives how
    much CPU one request can consume, and the browser-side slider bounds
    are trivially bypassed by posting the form directly.
    """
    try:
        iterations = int(form.get('iterations', 3))
    except (TypeError, ValueError):
        iterations = 3
    iterations = max(current_app.config['MIN_ITERATIONS'],
                     min(iterations, current_app.config['MAX_ITERATIONS']))

    def weight(name, default):
        try:
            value = float(form.get(name, default))
        except (TypeError, ValueError):
            return default
        if value != value:  # NaN would poison the RMSD
            return default
        return max(0.0, min(value, 1.0))

    battery_weight = weight('slider_a', 0.5)
    # The two weights are two views of one split, exactly as in the
    # desktop app where moving either slider moves the other.
    derivative_weight = round(1.0 - battery_weight, 2)

    return {
        'iterations': iterations,
        'battery_weight': battery_weight,
        'derivative_weight': derivative_weight,
    }


def _uploaded(field):
    """Uploaded files for a form field, ignoring empty file inputs."""
    return [f for f in request.files.getlist(field) if f and f.filename]


@bp.route('/')
def index():
    return render_template('index.html',
                           max_iterations=current_app.config['MAX_ITERATIONS'],
                           max_files=current_app.config['MAX_CURVE_FILES'])


@bp.route('/upload', methods=['POST'])
def upload():
    cathode_files = _uploaded('cathode_files')
    anode_files = _uploaded('anode_files')
    battery_files = _uploaded('battery_file')

    if not cathode_files or not anode_files or not battery_files:
        flash("Please select at least one cathode file, one anode file, and a battery file.", "error")
        return redirect(url_for('main.index'))

    max_files = current_app.config['MAX_CURVE_FILES']
    if len(cathode_files) > max_files or len(anode_files) > max_files:
        flash(f"Please select at most {max_files} cathode and {max_files} anode files — "
              "the optimization compares every cathode against every anode, so larger "
              "batches take impractically long.", "error")
        return redirect(url_for('main.index'))

    workdir = pipeline.create_session_workdir(session.get('workdir'))
    session['workdir'] = workdir
    session['settings'] = _clamp_settings(request.form)

    previews = []
    errors = []
    files_meta = []

    tagged_files = (
        [(f, 'cathode') for f in cathode_files]
        + [(f, 'anode') for f in anode_files]
        + [(battery_files[0], 'battery')]
    )
    for file_storage, curve_type in tagged_files:
        try:
            info = pipeline.save_and_preview(file_storage, curve_type, workdir)
            previews.append(info)
            files_meta.append({
                'file_id': info['file_id'],
                'curve_type': info['curve_type'],
                'path': info['path'],
            })
        except DataFormatError as e:
            errors.append({'filename': file_storage.filename, 'curve_type': curve_type,
                           'message': str(e)})
        except Exception as e:
            errors.append({'filename': file_storage.filename, 'curve_type': curve_type,
                           'message': f"Unexpected error: {e}"})

    session['files'] = files_meta

    if not previews:
        flash("None of the uploaded files could be read.", "error")
        return redirect(url_for('main.index'))

    return render_template('confirm.html', previews=previews, errors=errors)


@bp.route('/confirm', methods=['POST'])
def confirm():
    files_meta = session.get('files')
    settings = session.get('settings')
    workdir = session.get('workdir')

    if not files_meta or not settings or not workdir:
        flash("Your session expired, please start over.", "error")
        return redirect(url_for('main.index'))

    cathodes = {}
    anodes = {}
    battery_soc = battery_ocv = None
    warnings = []
    errors = []

    for meta in files_meta:
        soc_key, ocv_key = f"soc_{meta['file_id']}", f"ocv_{meta['file_id']}"
        if soc_key not in request.form or ocv_key not in request.form:
            continue
        try:
            soc_idx, ocv_idx = int(request.form[soc_key]), int(request.form[ocv_key])
            x, y, file_warnings = pipeline.finalize_curve(
                meta['path'], meta['curve_type'], (soc_idx, ocv_idx))
            label = os.path.splitext(os.path.basename(meta['path']))[0]
            warnings.extend(f"{label}: {msg}" for msg in file_warnings)

            if meta['curve_type'] == 'battery':
                battery_soc, battery_ocv = x, y
            else:
                target = cathodes if meta['curve_type'] == 'cathode' else anodes
                target[label] = pipeline.build_curve_entry(x, y)
        except (DataFormatError, ValueError) as e:
            errors.append(str(e))

    if battery_soc is None or not cathodes or not anodes:
        message = "Need at least one valid cathode file, one anode file, and the battery file."
        if errors:
            message += " Errors: " + "; ".join(errors)
        flash(message, "error")
        return redirect(url_for('main.index'))

    try:
        context = pipeline.run_optimization(
            cathodes, anodes, battery_soc, battery_ocv, settings, workdir,
            n_jobs=current_app.config['N_JOBS'])
    except Exception as e:
        flash(f"Optimization failed: {e}", "error")
        return redirect(url_for('main.index'))

    context['warnings'] = warnings
    context['settings'] = settings
    return render_template('results.html', **context)


@bp.route('/download')
def download():
    workdir = session.get('workdir')
    result_path = os.path.join(workdir, 'result.json') if workdir else None
    if not result_path or not os.path.exists(result_path):
        abort(404)
    return send_file(result_path, as_attachment=True,
                     download_name='pybep_result.json')


# --- Format Data ------------------------------------------------------
# The website's equivalent of the desktop app's "Format Data" button.

@bp.route('/format')
def format_form():
    return render_template('format.html',
                           max_files=current_app.config['MAX_CURVE_FILES'])


@bp.route('/format', methods=['POST'])
def format_run():
    files = _uploaded('data_files')
    curve_type = request.form.get('curve_type')

    if not files:
        flash("Please select at least one file to format.", "error")
        return redirect(url_for('main.format_form'))

    if curve_type not in CURVE_TYPES:
        flash("Please choose whether these are cathode, anode or battery curves.", "error")
        return redirect(url_for('main.format_form'))

    max_files = current_app.config['MAX_CURVE_FILES']
    if len(files) > max_files:
        flash(f"Please format at most {max_files} files at a time.", "error")
        return redirect(url_for('main.format_form'))

    workdir = pipeline.create_session_workdir(session.get('workdir'))
    session['workdir'] = workdir
    # A format run invalidates any earlier optimization in this session.
    session.pop('files', None)
    session.pop('settings', None)

    try:
        report, zip_path = pipeline.format_uploads(files, curve_type, workdir)
    except Exception as e:
        flash(f"Formatting failed: {e}", "error")
        return redirect(url_for('main.format_form'))

    session['format_zip'] = zip_path
    return render_template('format_results.html', report=report,
                           curve_type=curve_type, have_zip=zip_path is not None)


@bp.route('/format/download')
def format_download():
    zip_path = session.get('format_zip')
    if not zip_path or not os.path.exists(zip_path):
        abort(404)
    return send_file(zip_path, as_attachment=True,
                     download_name='pybep_formatted_data.zip')
