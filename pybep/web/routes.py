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
                   session, flash, send_file, abort, current_app, jsonify)

from ..core import (DataFormatError, library_names, select_library_curves,
                    battery_library_names, load_battery_library_curve,
                    RESULT_FORMATS, RESULT_MEDIA_TYPES)
from . import pipeline

bp = Blueprint('main', __name__)

CURVE_TYPES = ('cathode', 'anode', 'battery')
CANDIDATE_TYPES = ('cathode', 'anode')

# The file boxes on the run form, and the kind of curve each one holds.
# /columns goes by this, so a field name arriving from the browser can
# only ever name a curve type that exists.
STAGED_FIELDS = {'battery_file': 'battery',
                 'cathode_files': 'cathode',
                 'anode_files': 'anode'}

# What the sliders show before anything has been run: one pass, all the
# weight on the OCV curve itself rather than its derivative.
DEFAULT_SETTINGS = {'iterations': 1, 'battery_weight': 1.0,
                    'derivative_weight': 0.0}


def _staged_workdir(create=False):
    """
    Where a file waits between being picked on the form and being run.

    Its own directory, not the run's: /upload makes a fresh run directory
    each time and that wipes the old one, which would throw away files the
    user had already picked and answered the column question for. Same
    reasoning as format_workdir.
    """
    workdir = _session_workdir('staged_workdir')
    if workdir is None and create:
        workdir = pipeline.create_session_workdir()
        session['staged_workdir'] = workdir
    return workdir


def _staged():
    """Everything waiting to be run, in the order it was picked."""
    workdir = _staged_workdir()
    return pipeline.load_staged(workdir) if workdir else []


def _staged_groups(entries):
    """
    The staged files as the form and the browser want them: id and name
    only, grouped by the box each came from.

    The stored path never goes out — the browser has no use for it, and it
    names a real directory on the server.
    """
    groups = {curve_type: [] for curve_type in CURVE_TYPES}
    for entry in entries:
        if entry['curve_type'] in groups:
            groups[entry['curve_type']].append(
                {'file_id': entry['file_id'], 'name': entry['filename']})
    return groups


def _answered(files_meta):
    """
    The column choices already settled, keyed by file id.

    A file that came through /columns carries its answer with it, so
    neither the run nor a later /adjust has to ask again.
    """
    return {meta['file_id']: tuple(meta['choice'])
            for meta in files_meta if meta.get('choice')}


def _form_context(from_session=True):
    """
    Everything the run form needs to render.

    By default it is filled in from whatever this session has done, so the
    form standing beside a set of results describes the run that produced
    them. A session that has run nothing gets the first-visit form anyway:
    every curve ticked, defaults on the sliders.

    from_session=False asks for that first-visit form outright, which is
    what a page that has been reset shows — otherwise "back to the start"
    would mean only the panel on the right, leaving the battery curve and
    the sliders still set to the run that is no longer on screen.
    """
    if from_session:
        chosen = session.get('library') or {}
        battery_choice = session.get('battery_choice') or ''
        settings = session.get('settings') or DEFAULT_SETTINGS
        staged = _staged_groups(_staged())
    else:
        chosen, battery_choice = {}, ''
        settings = DEFAULT_SETTINGS
        # Starting over throws the waiting files away too, so an empty
        # list here is the truth rather than a page hiding them.
        staged = _staged_groups([])

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
        # A copy: a caller that ever edited what it was handed would
        # otherwise rewrite the defaults for every session in the process.
        'settings': dict(settings),
        'staged': staged,
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


def _session_workdir(key='workdir'):
    """
    One of this session's working directories, or None if there isn't a
    usable one.

    There are two: 'workdir' holds the optimization's uploads and results,
    'format_workdir' holds Format Data's. Keeping them apart is what lets
    someone format a file without deleting the results they are still
    looking at.

    The path is checked rather than taken at face value: it arrives from a
    cookie, and the routes below hand it to send_file. Belt and braces
    alongside a strong SECRET_KEY.
    """
    workdir = session.get(key)
    if not pipeline.is_session_workdir(workdir):
        return None
    real = os.path.realpath(workdir)
    if not os.path.isdir(real):
        return None
    # Being used counts as being touched, which is what the six-hour
    # sweep goes by. Every route that reaches a directory comes through
    # here, so this is the one place that has to remember.
    pipeline.touch_workdir(real)
    return real


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
    The run page: the form on the left, and on the right either the last
    results or an empty panel waiting for them.

    The results are read back from the session's directory rather than
    held in the request that computed them, which is what lets them
    survive a look at Format Data or the instructions, a refresh, and the
    back button.
    """
    if request.args.get('reset'):
        # The results page asks for this when the browser tells it the
        # load was a reload. Refreshing means "put it back the way it
        # started", so the run is set aside — not deleted; the link on
        # the empty panel brings it back, and /download still works.
        # Files picked but not yet run do go: the form is about to say
        # nothing is waiting, and it has to be telling the truth.
        staged_workdir = _staged_workdir()
        if staged_workdir:
            pipeline.drop_staged(staged_workdir,
                                 pipeline.load_staged(staged_workdir), set())
        session['results_hidden'] = True
        return redirect(url_for('main.index'))
    if request.args.get('restore'):
        session.pop('results_hidden', None)
        return redirect(url_for('main.index'))

    context = _form_context()
    workdir = _session_workdir()
    view = pipeline.load_result_view(workdir) if workdir else None

    if view is None:
        return render_template('index.html', **context)

    if session.get('results_hidden'):
        # Reset by a refresh or by the logo, so the whole page reads as it
        # did before anything ran — the form included.
        context = _form_context(from_session=False)
        context['hidden_results'] = True
        return render_template('index.html', **context)

    # What was computed wins over what the form is offering to do next.
    context.update(view)
    return render_template('results.html', **context)


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


def _cell(value):
    """One preview cell as text, the way the confirmation page shows it."""
    try:
        return format(float(value), '.4g')
    except (TypeError, ValueError):
        return str(value)


@bp.route('/columns', methods=['POST'])
def columns():
    """
    Take the files just picked on the form, keep them, and answer with
    what the browser needs in order to ask about their columns.

    This is why picking a file no longer costs a page: the file arrives
    the moment it is chosen, the question is asked over the form that
    asked for it, and the answer is stored here — so "Run optimization"
    stays the one button that starts a run, whether or not any curve came
    from a file.

    JSON, because the browser is asking rather than navigating. If it
    never gets here — no JavaScript, or a request that failed — the file
    is still in its box and /upload falls back to the confirmation page.
    """
    workdir = _staged_workdir(create=True)
    entries = pipeline.load_staged(workdir)
    max_files = current_app.config['MAX_CURVE_FILES']
    previews, errors = [], []

    for field, curve_type in STAGED_FIELDS.items():
        incoming = _uploaded(field)
        if not incoming:
            continue

        # Picking again in a box replaces what was in it, the way the box
        # itself does. Without this every change of mind would leave a
        # file behind that the next run would quietly include.
        entries = pipeline.drop_staged(
            workdir, entries,
            {e['file_id'] for e in entries if e.get('field') != field})

        if curve_type == 'battery':
            incoming = incoming[:1]  # one measured curve per run
        room = max_files - sum(1 for e in entries
                               if e['curve_type'] == curve_type)
        if len(incoming) > room:
            errors.append({
                'filename': '',
                'message': f"Only {max_files} {curve_type} files can be used "
                           "in one run, so the rest were left out."})
            incoming = incoming[:max(room, 0)]

        for file_storage in incoming:
            try:
                info = pipeline.save_and_preview(file_storage, curve_type,
                                                 workdir)
            except DataFormatError as e:
                errors.append({'filename': file_storage.filename,
                               'message': str(e)})
                continue
            except Exception as e:
                errors.append({'filename': file_storage.filename,
                               'message': f"Unexpected error: {e}"})
                continue

            # The guess stands as the answer from the moment the file
            # lands, so one whose dialog is never confirmed still runs the
            # way the confirmation page would have run it.
            entries.append({'file_id': info['file_id'],
                            'curve_type': curve_type,
                            'field': field,
                            'filename': info['filename'],
                            'path': info['path'],
                            'n_cols': info['n_cols'],
                            'soc': info['guess_soc'],
                            'ocv': info['guess_ocv']})
            previews.append({
                'file_id': info['file_id'],
                'curve_type': curve_type,
                'filename': info['filename'],
                'n_cols': info['n_cols'],
                # Formatted here rather than in the browser: a raw table
                # can hold NaN, and JSON has no way of writing one that
                # JSON.parse will read back.
                'rows': [[_cell(v) for v in row]
                         for row in info['preview_rows']],
                'guess_soc': info['guess_soc'],
                'guess_ocv': info['guess_ocv']})

    pipeline.save_staged(workdir, entries)
    return jsonify(previews=previews, errors=errors,
                   staged=_staged_groups(entries))


@bp.route('/columns/keep', methods=['POST'])
def columns_keep():
    """
    Store the answers to the column question, and throw away any staged
    file the browser no longer lists.

    One route for three gestures, because they differ only in what is
    listed: confirming the dialog keeps everything, cancelling keeps what
    was already there, and removing one file keeps the rest.
    """
    workdir = _staged_workdir()
    if workdir is None:
        return jsonify(staged=_staged_groups([]))

    entries = pipeline.drop_staged(workdir, pipeline.load_staged(workdir),
                                   set(request.form.getlist('keep')))
    for entry in entries:
        soc = request.form.get(f"soc_{entry['file_id']}")
        ocv = request.form.get(f"ocv_{entry['file_id']}")
        # Absent means "not asked about this time", so the standing
        # answer — the guess, or an earlier choice — stays.
        if soc is None or ocv is None:
            continue
        try:
            soc, ocv = int(soc), int(ocv)
        except ValueError:
            continue
        # A column index reaches the parser by position, and one out of
        # range sinks the run with a message about an unreadable file.
        # Only real columns pass.
        if 0 <= soc < entry['n_cols'] and 0 <= ocv < entry['n_cols']:
            entry['soc'], entry['ocv'] = soc, ocv

    pipeline.save_staged(workdir, entries)
    return jsonify(staged=_staged_groups(entries))


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

    # Files picked earlier went to /columns as they were chosen and were
    # answered for over the form, so nothing is asked about them here —
    # which is what leaves one button in a run. A library curve picked
    # afterwards wins over a staged battery file: the box that file came
    # from is hidden while a library curve is selected.
    staged = [e for e in _staged()
              if not (battery_choice and e['curve_type'] == 'battery')]

    def staged_count(curve_type):
        return sum(1 for e in staged if e['curve_type'] == curve_type)

    if battery_choice and battery_choice not in battery_library_names():
        flash("That battery OCV curve isn't one of the available files.", "error")
        return redirect(url_for('main.index'))
    if not battery_choice and not battery_files and not staged_count('battery'):
        flash("Please choose a battery OCV curve, or upload your own file.", "error")
        return redirect(url_for('main.index'))

    n_cathodes = len(library_cathodes) + len(cathode_files) + staged_count('cathode')
    n_anodes = len(library_anodes) + len(anode_files) + staged_count('anode')
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

    # The run takes its own copy of each staged file. The two directories
    # are emptied at different moments — starting over clears the staged
    # ones — and a result still on screen has to stay re-runnable.
    for entry in staged:
        try:
            path = pipeline.adopt_staged(entry, workdir)
        except OSError as e:
            errors.append({'filename': entry['filename'],
                           'curve_type': entry['curve_type'],
                           'message': f"could not be read back: {e}"})
            continue
        files_meta.append({'file_id': entry['file_id'],
                           'curve_type': entry['curve_type'],
                           'path': path,
                           'choice': [entry['soc'], entry['ocv']]})

    # Only uploaded files are previewed and column-confirmed. The library
    # curves were checked by hand and have a known layout, so there is
    # nothing for the user to decide about them.
    tagged_files = (
        [(f, 'cathode') for f in cathode_files]
        + [(f, 'anode') for f in anode_files]
        # Empty when a curve was picked into the dialog instead, or
        # when one came from the library.
        + ([] if battery_choice else [(f, 'battery') for f in battery_files[:1]])
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
        if errors and not files_meta:
            flash("None of the uploaded files could be read.", "error")
            return redirect(url_for('main.index'))
        if errors:
            # Some of it can still run. Say what cannot, rather than
            # reporting a comparison that quietly left a candidate out.
            flash("Left out: " + "; ".join(
                f"{e['filename']} ({e['message']})" for e in errors), "error")
        # Every curve either came from the library or has been answered
        # for already, so there is nothing to confirm — go straight to the
        # answer instead of showing an empty confirmation page.
        session['choices'] = _answered(files_meta)
        return _run_and_store(files_meta, session['choices'],
                              session['settings'], workdir)

    # No JavaScript, or a browser that could not reach /columns: the
    # question still has to be asked, so it gets a page of its own.
    return render_template('confirm.html', previews=previews, errors=errors)


@bp.route('/confirm', methods=['POST'])
def confirm():
    files_meta = session.get('files')
    settings = session.get('settings')
    workdir = _session_workdir()

    if files_meta is None or not settings or not workdir:
        flash("Your session expired, please start over.", "error")
        return redirect(url_for('main.index'))

    # Files that came through /columns are answered for already and are
    # not on this page; only the ones it asked about come back in the form.
    choices = _answered(files_meta)
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
    return _run_and_store(files_meta, choices, settings, workdir)


@bp.route('/adjust', methods=['POST'])
def adjust():
    """
    Re-run the last optimization with different settings, reusing the
    curves already chosen — the point being not to pick the files again
    just to try three iterations instead of one.

    Nothing on the site posts here at present: the "Re-run with the same
    files" button that did was taken off the form, leaving one button that
    always goes through /upload. The route is kept because it is the only
    way to run without picking the files again, so putting that back is a
    button in a template rather than a feature to write.

    POST only: there was never a page of its own to GET.
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
    return _run_and_store(files_meta, session.get('choices') or {},
                          settings, workdir)


def _run_and_store(files_meta, choices, settings, workdir):
    """
    Assemble the candidates (library selection plus any confirmed uploads),
    run the optimization, save what it produced and send the browser to
    the page that shows it.

    Shared by /confirm and by /upload, which skips the confirmation step
    when every curve came from the library and there is nothing to confirm.

    Redirecting rather than rendering is what makes the results stick: the
    page the user lands on is a plain GET of "/", so it can be refreshed,
    gone back to, and returned to from another tab.
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
    context['warnings'] = warnings
    context['settings'] = settings
    context['n_cathodes'] = len(cathodes)
    context['n_anodes'] = len(anodes)
    pipeline.save_result_view(workdir, context)
    session.pop('results_hidden', None)
    return redirect(url_for('main.index'))


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

    # Its own directory, not the optimization's: formatting a file must
    # not delete a result the user still has open in the other tab.
    workdir = pipeline.create_session_workdir(session.get('format_workdir'))
    session['format_workdir'] = workdir

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
    workdir = _session_workdir('format_workdir')

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
    workdir = _session_workdir('format_workdir')
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
    workdir = _session_workdir('format_workdir')
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

