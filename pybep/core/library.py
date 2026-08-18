"""
The reference curve library that ships with PyBEP.

These are the cathode and anode curves in ``data/``: checked by hand,
known good, and the same files the desktop app points at. Because their
layout is already verified they skip the column-confirmation step that
uploaded files go through — there is nothing for a user to decide about
them.

The library is read-only. Uploads live in their own temporary directory
and are never written into ``data/``, so using the website can't change
what the next visitor sees.
"""
import os
import threading

from .add_curves import add_half_cell_data
from .add_battery import load_soc_ocv_data
from .data_formatter import list_data_files

# library.py -> core -> pybep -> repo root
PROJECT_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
DATA_ROOT = os.path.join(PROJECT_ROOT, 'data')

LIBRARY_DIRS = {
    'cathode': os.path.join(DATA_ROOT, 'cathode_data'),
    'anode': os.path.join(DATA_ROOT, 'anode_data'),
}

_cache = {}
_cache_lock = threading.Lock()


def library_curves(curve_type):
    """
    Every bundled curve of one type, as ``{name: curve_entry}``.

    Parsed once and kept, since the files cannot change while the program
    is running and re-reading them per request would cost more than the
    optimization itself. The returned entries are shared, so treat them as
    read-only.
    """
    if curve_type not in LIBRARY_DIRS:
        raise ValueError(f"Unknown curve type: {curve_type!r}")

    with _cache_lock:
        if curve_type not in _cache:
            # No column_resolver: the layout of these files is known, so
            # the heuristic is allowed to settle it without asking.
            _cache[curve_type] = add_half_cell_data(
                LIBRARY_DIRS[curve_type], curve_type)
        return _cache[curve_type]


def library_names(curve_type):
    """Names of the bundled curves of one type, in display order."""
    return sorted(library_curves(curve_type))


def select_library_curves(curve_type, names):
    """
    The bundled curves matching ``names``, skipping anything unrecognised.

    Names are looked up in the library rather than joined onto a path, so a
    value arriving from a web form can never reach outside ``data/``.
    """
    available = library_curves(curve_type)
    return {name: available[name] for name in names if name in available}


def battery_library_files():
    """
    The bundled full-cell OCV files, as ``{display name: filename}``.

    These sit directly in ``data/``; the cathode and anode subfolders are
    skipped because they aren't data files themselves.
    """
    return {os.path.splitext(f)[0]: f for f in list_data_files(DATA_ROOT)}


def battery_library_names():
    """Names of the bundled full-cell OCV curves, in display order."""
    return sorted(battery_library_files())


def load_battery_library_curve(name):
    """
    ``(soc, ocv)`` for one bundled battery curve, or None if unknown.

    As with the half cells, the name is looked up rather than joined onto
    a path, so it can't be used to read an arbitrary file.
    """
    filename = battery_library_files().get(name)
    if filename is None:
        return None

    with _cache_lock:
        cached = _cache.get(('battery', name))
    if cached is None:
        cached = load_soc_ocv_data(os.path.join(DATA_ROOT, filename))
        with _cache_lock:
            _cache[('battery', name)] = cached
    return cached


def library_curve_points(curve_type, name):
    """
    ``(x, y)`` arrays for one bundled curve, for plotting a preview.

    Returns None if the name is not in the library.
    """
    entry = library_curves(curve_type).get(name)
    if entry is None:
        return None
    x = entry['x_values']
    return x, entry['interpolated_function'](x)
