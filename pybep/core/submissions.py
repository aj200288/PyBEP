"""
Curves people have sent in, kept beside the library but not part of it.

The built-in curves in ``data/`` were checked by hand. These were not:
anyone can add one, and until somebody at the lab has looked at it, it is
shown as unverified everywhere it appears.

There is no database. One folder per curve type, and in it, per curve:

    submitted/cathode/NMC622-LICeM.txt          the curve, 1001 points
    submitted/cathode/NMC622-LICeM.json         who sent it, and from where
    submitted/cathode/original/NMC622-LICeM.csv the file as uploaded

The .txt is written in the same layout as everything in ``data/``, which
is what makes promoting one a plain file move::

    mv submitted/cathode/NMC622-LICeM.txt data/cathode_data/
    rm submitted/cathode/NMC622-LICeM.json

The original is kept beside it because that is what someone reviewing the
curve needs to see — the .txt has already been cleaned and resampled.

Where the folder lives is settable (``PYBEP_SUBMISSIONS_DIR``), and read
on every call rather than at import, so a test or a server can point it
somewhere else without the module caring.
"""
import json
import os
import re
import shutil
import time

from .add_curves import build_curve_entry
from .data_formatter import (DATA_FILE_EXTENSIONS, DataFormatError,
                             list_data_files, load_ocv_curve,
                             _write_curve_file)

CURVE_TYPES = ('cathode', 'anode', 'battery')

# The uploaded file sits in here, one level down, so that list_data_files
# on the curve type's own folder sees curves and nothing else.
ORIGINALS_DIR = 'original'

# submissions.py -> core -> pybep -> repo root
PROJECT_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))


def submission_root():
    """Where submitted curves are kept. Settable for a server or a test."""
    return os.environ.get('PYBEP_SUBMISSIONS_DIR') or os.path.join(
        PROJECT_ROOT, 'submitted')


def submission_dir(curve_type, create=False):
    """The folder for one curve type, made on demand when asked for."""
    if curve_type not in CURVE_TYPES:
        raise ValueError(f"Unknown curve type: {curve_type!r}")
    path = os.path.join(submission_root(), curve_type)
    if create:
        os.makedirs(os.path.join(path, ORIGINALS_DIR), exist_ok=True)
    return path


def safe_name(text, fallback='curve'):
    """
    A filename stem that cannot escape the folder it is written into.

    Everything outside letters, digits, dot, dash and underscore becomes an
    underscore, and leading dots go, so no name can turn into a path, a
    parent reference, or a hidden file.
    """
    stem = re.sub(r'[^A-Za-z0-9._-]+', '_', (text or '').strip())
    stem = stem.lstrip('.').strip('_')
    return (stem or fallback)[:80]


def unique_name(stem, taken):
    """``stem``, or ``stem (2)``, ``stem (3)``... until it is free."""
    if stem not in taken:
        return stem
    n = 2
    while f"{stem} ({n})" in taken:
        n += 1
    return f"{stem} ({n})"


def submission_files(curve_type):
    """``{name: filename}`` for the curves of one type, if any."""
    folder = submission_dir(curve_type)
    if not os.path.isdir(folder):
        return {}
    return {os.path.splitext(f)[0]: f for f in list_data_files(folder)}


def submission_names(curve_type):
    """
    Names of the submitted curves of one type, in display order.

    A directory listing, not a parse: the run form names these on every
    page it draws, and only a run or a preview needs the numbers.
    """
    return sorted(submission_files(curve_type))


def read_metadata(curve_type, name):
    """
    What was said about one curve when it was sent in.

    A missing or unreadable sidecar gives an empty dict rather than an
    error: a curve whose metadata has been lost is still a curve, and a
    hand-placed file may have no sidecar at all.
    """
    path = os.path.join(submission_dir(curve_type), f"{safe_name(name)}.json")
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def list_submissions(curve_type=None):
    """
    Every submitted curve with its metadata, newest first.

    One dict per curve: name, curve_type, and whatever the sidecar holds.
    """
    types = (curve_type,) if curve_type else CURVE_TYPES
    rows = []
    for kind in types:
        for name in submission_names(kind):
            row = {'name': name, 'curve_type': kind}
            row.update(read_metadata(kind, name))
            rows.append(row)
    rows.sort(key=lambda r: (r.get('submitted') or '', r['name']), reverse=True)
    return rows


def submission_count():
    """How many curves are stored, across all three types."""
    return sum(len(submission_files(kind)) for kind in CURVE_TYPES)


def save_submission(curve_type, stem, x_values, y_values, metadata,
                    original_path=None, original_filename=None):
    """
    Store one curve permanently, and return the name it was filed under.

    The curve is written in the project's own layout, so the file is ready
    to be moved into ``data/`` as it stands. The name is derived from what
    the submitter called their file and made unique against what is
    already there — nothing is ever overwritten.
    """
    folder = submission_dir(curve_type, create=True)
    # Only a data extension comes off. Splitting blindly would eat the tail
    # of a DOI: NMC811-10.1016_j.xcrp.2020.100253 ends in ".100253".
    root, extension = os.path.splitext(stem or '')
    if extension.lower() in DATA_FILE_EXTENSIONS:
        stem = root
    name = unique_name(safe_name(stem), set(submission_files(curve_type)))

    _write_curve_file(x_values, y_values, os.path.join(folder, f"{name}.txt"))

    if original_path and os.path.isfile(original_path):
        # Chosen from a fixed set rather than sanitised: safe_name strips
        # the leading dot, and an extension is the one part of a filename
        # that has to keep it.
        extension = os.path.splitext(original_filename or original_path)[1].lower()
        if extension not in DATA_FILE_EXTENSIONS:
            extension = '.dat'
        shutil.copy2(original_path,
                     os.path.join(folder, ORIGINALS_DIR, f"{name}{extension}"))

    record = dict(metadata or {})
    record.update({
        'name': name,
        'curve_type': curve_type,
        'original_filename': original_filename,
        'submitted': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    })
    with open(os.path.join(folder, f"{name}.json"), 'w', encoding='utf-8') as f:
        json.dump(record, f, indent=2)
    return name


def _submission_path(curve_type, name):
    """The stored curve for one name, or None if there is no such curve."""
    filename = submission_files(curve_type).get(name)
    if filename is None:
        return None
    # Looked up rather than joined, so a name off a web form can only ever
    # reach a file this folder already lists.
    return os.path.join(submission_dir(curve_type), filename)


def load_submission(curve_type, name):
    """``(x, y)`` for one submitted curve, or None if the name is unknown."""
    path = _submission_path(curve_type, name)
    if path is None:
        return None
    x_values, y_values, _warnings, _columns = load_ocv_curve(path, curve_type)
    return x_values, y_values


def select_submitted_curves(curve_type, names, label=None):
    """
    The submitted curves matching ``names``, ready for the optimization.

    Keys carry a suffix so that a submitted curve and a built-in one of
    the same name cannot collide in the candidate dictionary — and so the
    result names which one actually won.
    """
    chosen = {}
    for name in names:
        try:
            loaded = load_submission(curve_type, name)
        except (DataFormatError, OSError, ValueError):
            # Anyone can put a file here and nobody has checked it. One
            # that will not parse is left out of the run rather than
            # taking the whole run down with it.
            continue
        if loaded is None:
            continue
        key = f"{name}{label}" if label else name
        chosen[key] = build_curve_entry(*loaded)
    return chosen


def submission_curve_points(curve_type, name):
    """
    ``(x, y)`` for a preview plot, or None if there is nothing to draw.

    None covers an unreadable file as well as an unknown name: the caller
    turns either into a 404, which is the right answer for both.
    """
    try:
        return load_submission(curve_type, name)
    except (DataFormatError, OSError, ValueError):
        return None


__all__ = [
    'CURVE_TYPES',
    'DataFormatError',
    'submission_root',
    'submission_dir',
    'safe_name',
    'submission_names',
    'read_metadata',
    'list_submissions',
    'submission_count',
    'save_submission',
    'load_submission',
    'select_submitted_curves',
    'submission_curve_points',
]
