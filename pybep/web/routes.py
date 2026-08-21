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
import tempfile

from flask import (Blueprint, render_template, request, redirect, url_for,
                   session, flash, send_file, abort, current_app)

from ..core import (DataFormatError, library_names, select_library_curves,
                    battery_library_names, load_battery_library_curve,
                    RESULT_FORMATS, RESULT_MEDIA_TYPES)
from . import pipeline

bp = Blueprint('main', __name__)

CURVE_TYPES = ('cathode', 'anode', 'battery')
CANDIDATE_TYPES = ('cathode', 'anode')

# What the sliders show before anything has been run: one pass, all the
# weight on the OCV curve itself rather than its derivative.
DEFAULT_SETTINGS = {'iterations': 1, 'battery_weight': 1.0,
                    'derivative_weight': 0.0}


def _previous_uploads():
    """
    The files this session has already uploaded, grouped by curve type.

    A browser will not put a file back into a file input, so the form
    always comes back with those boxes empty. Naming the files is the
    difference between the user picking them again and quietly running
    without them.
    """
    names = {curve_type: [] for curve_type in CURVE_TYPES}
    for meta in session.get('files') or []:
        if meta['curve_type'] in names and os.path.exists(meta['path']):
            names[meta['curve_type']].append(pipeline.curve_label(meta['path']))
    return names


def _form_context(previous=False):
    """
    Everything the run form needs to render.

    The form is the left-hand column of both the front page and the
    results, so both routes need these values and building them in one
    place is what stops the two pages disagreeing. `previous` fills the
    form in from the last run — what the results page wants, so that
    moving one slider and going again is a single click. The front page
    leaves it False: arriving there means starting over.
    """
    if previous:
        chosen = session.get('library') or {}
        settings = session.get('settings') or DEFAULT_SETTINGS
        battery_choice = session.get('battery_choice') or ''
        carried = _previous_uploads()
    else:
        chosen, settings, battery_choice = {}, DEFAULT_SETTINGS, ''
        carried = {curve_type: [] for curve_type in CURVE_TYPES}

    return {
        'max_iterations': current_app.config['MAX_ITERATIONS'],
        'cathode_library': library_names('cathode'),
        'anode_library': library_names('anode'),
        'battery_library': battery_library_names(),
        # None rather than [] when there is no previous run: the template
        # reads that as "tick everything", which is the first-visit state.
        'chosen_cathodes': chosen.get('cathode'),
        'chosen_anodes': chosen.get('anode'),
        'battery_choice': battery_choice,
        'settings': settings,
        'carried': carried,
    }


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


def _session_workdir():
    """
    This session's working directory, or None if there isn't a usable one.

    The path is checked rather than taken at face value: it arrives from a
    cookie, and the routes below hand it to send_file. Belt and braces
    alongside a strong SECRET_KEY.
    """
    workdir = session.get('workdir')
    if not pipeline.is_session_workdir(workdir):
        return None
    real = os.path.realpath(workdir)
    return real if os.path.isdir(real) else None


def _safe_join(base, *parts):
    """
    Join under base, or None if the result would escape it.

    For any path built from a value that travelled through the browser, so
    that "../" in a name cannot reach outside the session's directory.
    """
    real_base = os.path.realpath(base)
    target = os.path.realpath(os.path.join(real_base, *parts))
    if target != real_base and not target.startswith(real_base + os.sep):
        return None
    return target


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
    """
    The front page: the run form on the left, an empty results panel on
    the right. This is also where "start over" lands, so the form shows
    its defaults rather than whatever the last run used.
    """
    return render_template('index.html', **_form_context())


@bp.route('/help')
def help_page():
    """
    The instructions. A page rather than a tooltip: it is long enough to
    need headings, and being a real URL means it can be linked to a
    colleague and printed.
    """
    return render_template(
        'help.html',
        max_iterations=current_app.config['MAX_ITERATIONS'],
        max_files=current_app.config['MAX_CURVE_FILES'],
        max_upload_mb=current_app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024),
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
        session['choices'] = {}
        return _run_and_render(files_meta, {}, session['settings'], workdir)

    return render_template('confirm.html', previews=previews, errors=errors)


@bp.route('/confirm', methods=['POST'])
def confirm():
    files_meta = session.get('files')
    settings = session.get('settings')
    workdir = _session_workdir()

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

    # Kept so /adjust can re-run without asking about columns again.
    session['choices'] = choices
    return _run_and_render(files_meta, choices, settings, workdir)


@bp.route('/adjust', methods=['POST'])
def adjust():
    """
    Re-run the last optimization with different settings, reusing the
    curves already chosen — the point being not to pick the files again
    just to try three iterations instead of one.

    POST only: the settings form lives on the results page beside the
    graph it produced, so there is no page of its own to GET.
    """
    files_meta = session.get('files')
    settings = session.get('settings')
    workdir = _session_workdir()

    if files_meta is None or not settings or not workdir:
        flash("Your previous run has expired, please start again.", "error")
        return redirect(url_for('main.index'))

    if any(not os.path.exists(meta['path']) for meta in files_meta):
        flash("The files from your last run are no longer available, "
              "please start again.", "error")
        return redirect(url_for('main.index'))

    settings = _clamp_settings(request.form)
    session['settings'] = settings
    return _run_and_render(files_meta, session.get('choices') or {},
                           settings, workdir)


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
        except (DataFormatError, ValueError, OSError) as e:
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

    # The uploads stay in the session's temp directory so /adjust can run
    # them again with different settings. They are wiped when the user
    # starts a new run (create_session_workdir) and never enter data/.

    # The form is this page's left-hand column, so it needs the form's
    # context too. The run's own values go on top: they describe what was
    # actually computed, not what the form is offering to do next.
    context.update(_form_context(previous=True))
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
    workdir = _session_workdir()
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
    for key in ('files', 'settings', 'choices', 'library', 'battery_choice'):
        session.pop(key, None)

    try:
        previews, rejected = pipeline.save_format_uploads(files, curve_type, workdir)
    except Exception as e:
        flash(f"Formatting failed: {e}", "error")
        return redirect(url_for('main.format_form'))

    if not previews:
        flash("None of the uploaded files could be read: "
              + "; ".join(f"{name} ({why})" for name, why in rejected.items()),
              "error")
        return redirect(url_for('main.format_form'))

    # Only the stored basenames go in the session; the workdir is already
    # there, and full paths would bloat the cookie for a large batch.
    session['format'] = {
        'curve_type': curve_type,
        'stored': [os.path.basename(p['path']) for p in previews],
        'rejected': rejected,
    }
    return render_template('format_confirm.html', previews=previews,
                           curve_type=curve_type, rejected=rejected)


@bp.route('/format/confirm', methods=['POST'])
def format_confirm():
    state = session.get('format')
    workdir = _session_workdir()

    if not state or not workdir:
        flash("Your session expired, please choose the files again.", "error")
        return redirect(url_for('main.format_form'))

    curve_type = state['curve_type']
    paths = [_safe_join(workdir, curve_type, name) for name in state['stored']]
    paths = [p for p in paths if p and os.path.isfile(p)]

    choices = {}
    for i, path in enumerate(paths):
        try:
            choices[path] = (int(request.form[f'soc_{i}']),
                             int(request.form[f'ocv_{i}']))
        except (KeyError, ValueError):
            continue  # left unconfirmed; format_files falls back to a guess

    try:
        report, zip_path = pipeline.convert_uploads(
            paths, curve_type, choices, workdir, rejected=state['rejected'])
    except Exception as e:
        flash(f"Formatting failed: {e}", "error")
        return redirect(url_for('main.format_form'))

    session['format_outputs'] = report['stored']
    return render_template('format_results.html', report=report,
                           curve_type=curve_type, have_zip=zip_path is not None)


@bp.route('/format/file/<int:index>')
def format_file(index):
    """
    One converted file on its own — most batches are small, and asking
    someone to unzip a single text file is a poor trade.

    The file is identified by its position in the list this session just
    produced, so a URL can only ever reach that session's own output.
    """
    outputs = session.get('format_outputs') or []
    workdir = _session_workdir()
    if not workdir or not 0 <= index < len(outputs):
        abort(404)

    stored = outputs[index]
    path = _safe_join(workdir, pipeline.FORMATTED_DIR, stored)
    if not path or not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=True,
                     download_name=pipeline.original_name(stored),
                     mimetype='text/plain')


@bp.route('/format/download')
def format_download():
    """
    Every converted file in one zip.

    The path is rebuilt from the session's own directory rather than read
    out of the cookie, so this route can only ever serve that session's
    zip — the same rule /download and /format/file follow.
    """
    workdir = _session_workdir()
    path = _safe_join(workdir, pipeline.FORMATTED_ZIP) if workdir else None
    if not path or not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=True,
                     download_name='pybep_formatted_data.zip')


# --- Error pages ------------------------------------------------------

def register_error_handlers(app):
    """
    Show the site's own error pages instead of Werkzeug's bare ones.

    413 is the one a real user meets by accident, by picking a file that
    is simply too big, and the default page gives them no way back.
    """
    max_mb = app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)
    pages = {
        404: ("Page not found",
              "That address does not exist. It may have been mistyped, or be "
              "a link to a result that has since been replaced."),
        405: ("Page not found",
              "That address cannot be reached this way. Start from the front "
              "page instead."),
        413: ("Those files are too large",
              f"Uploads are limited to {max_mb} MB in total. Try sending "
              "fewer files at once, or trimming the data first."),
        500: ("Something went wrong",
              "The server hit an unexpected problem. Starting the run again "
              "usually clears it; if it keeps happening, the file may be one "
              "PyBEP cannot handle."),
    }

    def render_error(code):
        heading, message = pages[code]
        return render_template('error.html', code=code, heading=heading,
                               message=message), code

    for code in pages:
        app.register_error_handler(code, lambda _e, code=code: render_error(code))

