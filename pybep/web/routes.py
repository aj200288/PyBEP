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

from ..core import (DataFormatError, library_names, select_library_curves,
                    battery_library_names, load_battery_library_curve,
                    RESULT_FORMATS, RESULT_MEDIA_TYPES)
from . import pipeline

bp = Blueprint('main', __name__)

CURVE_TYPES = ('cathode', 'anode', 'battery')
CANDIDATE_TYPES = ('cathode', 'anode')


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


def _unique_key(label, existing):
    """
    Make label unique within the dict `existing`, as "name (2)", "name (3)".

    Curves are keyed by filename, and two uploads can share one. Without
    this the second would silently replace the first, quietly dropping a
    candidate from the comparison and reporting a winner the user never
    saw fully evaluated.
    """
    if label not in existing:
        return label
    n = 2
    while f"{label} ({n})" in existing:
        n += 1
    return f"{label} ({n})"


def _chosen_library(field, curve_type):
    """
    The library curves ticked on the form, filtered against the real
    library so an edited form can only ever name a curve that exists.
    """
    available = set(library_names(curve_type))
    return [n for n in request.form.getlist(field) if n in available]


@bp.route('/')
def index():
    return render_template('index.html',
                           max_iterations=current_app.config['MAX_ITERATIONS'],
                           max_files=current_app.config['MAX_CURVE_FILES'],
                           cathode_library=library_names('cathode'),
                           anode_library=library_names('anode'),
                           battery_library=battery_library_names())


@bp.route('/upload', methods=['POST'])
def upload():
    # Candidate curves come from the bundled library by default; uploading
    # your own is optional and only needed for a curve that isn't there.
    library_cathodes = _chosen_library('library_cathodes', 'cathode')
    library_anodes = _chosen_library('library_anodes', 'anode')
    cathode_files = _uploaded('cathode_files')
    anode_files = _uploaded('anode_files')

    # The battery curve is either one of the bundled examples or an upload;
    # an empty choice means "upload my own".
    battery_choice = request.form.get('battery_choice', '')
    battery_files = _uploaded('battery_file')

    if battery_choice and battery_choice not in battery_library_names():
        flash("That battery OCV curve isn't one of the available files.", "error")
        return redirect(url_for('main.index'))
    if not battery_choice and not battery_files:
        flash("Please choose a battery OCV curve, or upload your own file.", "error")
        return redirect(url_for('main.index'))

    n_cathodes = len(library_cathodes) + len(cathode_files)
    n_anodes = len(library_anodes) + len(anode_files)
    if not n_cathodes or not n_anodes:
        flash("Please keep at least one cathode candidate and one anode candidate "
              "selected, or upload your own.", "error")
        return redirect(url_for('main.index'))

    max_files = current_app.config['MAX_CURVE_FILES']
    if n_cathodes > max_files or n_anodes > max_files:
        flash(f"Please keep at most {max_files} cathode and {max_files} anode "
              "candidates — the optimization compares every cathode against every "
              "anode, so larger batches take impractically long.", "error")
        return redirect(url_for('main.index'))

    workdir = pipeline.create_session_workdir(session.get('workdir'))
    session['workdir'] = workdir
    session['settings'] = _clamp_settings(request.form)
    session['library'] = {'cathode': library_cathodes, 'anode': library_anodes}
    session['battery_choice'] = battery_choice or None

    previews = []
    errors = []
    files_meta = []

    # Only uploaded files are previewed and column-confirmed. The library
    # curves were checked by hand and have a known layout, so there is
    # nothing for the user to decide about them.
    tagged_files = (
        [(f, 'cathode') for f in cathode_files]
        + [(f, 'anode') for f in anode_files]
        + ([] if battery_choice else [(battery_files[0], 'battery')])
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
        if errors:
            flash("None of the uploaded files could be read.", "error")
            return redirect(url_for('main.index'))
        # Everything came from the library, so there are no columns to
        # confirm — go straight to the answer instead of showing an
        # empty confirmation page.
        return _run_and_render(files_meta, {}, session['settings'], workdir)

    return render_template('confirm.html', previews=previews, errors=errors)


@bp.route('/confirm', methods=['POST'])
def confirm():
    files_meta = session.get('files')
    settings = session.get('settings')
    workdir = session.get('workdir')

    if files_meta is None or not settings or not workdir:
        flash("Your session expired, please start over.", "error")
        return redirect(url_for('main.index'))

    choices = {}
    for meta in files_meta:
        soc_key, ocv_key = f"soc_{meta['file_id']}", f"ocv_{meta['file_id']}"
        if soc_key in request.form and ocv_key in request.form:
            try:
                choices[meta['file_id']] = (int(request.form[soc_key]),
                                            int(request.form[ocv_key]))
            except ValueError:
                pass  # reported as a missing curve below

    return _run_and_render(files_meta, choices, settings, workdir)


def _run_and_render(files_meta, choices, settings, workdir):
    """
    Assemble the candidates (library selection plus any confirmed uploads),
    run the optimization and render the results.

    Shared by /confirm and by /upload, which skips the confirmation step
    when every curve came from the library and there is nothing to confirm.
    """

    # Start from the library selection made on the front page, then layer
    # any uploaded candidates on top of it.
    chosen = session.get('library') or {'cathode': [], 'anode': []}
    cathodes = dict(select_library_curves('cathode', chosen.get('cathode', [])))
    anodes = dict(select_library_curves('anode', chosen.get('anode', [])))
    battery_soc = battery_ocv = None
    warnings = []
    errors = []

    battery_name = session.get('battery_choice')
    if battery_name:
        loaded = load_battery_library_curve(battery_name)
        if loaded is not None:
            battery_soc, battery_ocv = loaded

    for meta in files_meta:
        choice = choices.get(meta['file_id'])
        if choice is None:
            continue
        try:
            x, y, file_warnings = pipeline.finalize_curve(
                meta['path'], meta['curve_type'], choice)
            label = pipeline.curve_label(meta['path'])

            if meta['curve_type'] == 'battery':
                battery_soc, battery_ocv = x, y
            else:
                target = cathodes if meta['curve_type'] == 'cathode' else anodes
                label = _unique_key(label, target)
                target[label] = pipeline.build_curve_entry(x, y)

            warnings.extend(f"{label}: {msg}" for msg in file_warnings)
        except (DataFormatError, ValueError) as e:
            errors.append(str(e))

    if battery_soc is None or not cathodes or not anodes:
        message = ("Need a readable battery OCV file, at least one cathode "
                   "candidate and at least one anode candidate.")
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

    # The uploaded files have been read; nothing needs them again.
    pipeline.discard_uploads(workdir)
    session.pop('files', None)

    context['warnings'] = warnings
    context['settings'] = settings
    context['n_cathodes'] = len(cathodes)
    context['n_anodes'] = len(anodes)
    return render_template('results.html', **context)


@bp.route('/curve/<curve_type>/<name>')
def curve_preview(curve_type, name):
    """A PNG of one library curve, for the previews on the front page."""
    if curve_type not in CANDIDATE_TYPES:
        abort(404)
    png = pipeline.render_curve_preview(curve_type, name)
    if png is None:
        abort(404)
    response = current_app.response_class(png, mimetype='image/png')
    # The library is fixed for the lifetime of the process.
    response.headers['Cache-Control'] = 'public, max-age=86400'
    return response


@bp.route('/download')
def download():
    workdir = session.get('workdir')
    if not workdir:
        abort(404)

    fmt = request.args.get('format', 'json')
    if fmt not in RESULT_FORMATS:
        abort(400)

    path = pipeline.result_download_path(workdir, fmt)
    if path is None:
        abort(404)
    return send_file(path, as_attachment=True,
                     download_name=f'pybep_result.{fmt}',
                     mimetype=RESULT_MEDIA_TYPES[fmt])


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
