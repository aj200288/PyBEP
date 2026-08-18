"""
Turn an optimization result into a downloadable file.

The JSON written by ``save_optimization_result_to_json`` is the canonical
form; CSV and XLSX are derived from it, so whichever format someone picks
they are looking at exactly the same run.

The curves are not all the same length — the "full" cathode and anode
curves run past the fitted window, so they have more points than the
battery curve. The table formats pad the short columns instead of
truncating to the shortest, which would silently throw away the ends of
those curves.
"""
import json

import pandas as pd

RESULT_FORMATS = ('json', 'csv', 'xlsx')

RESULT_MEDIA_TYPES = {
    'json': 'application/json',
    'csv': 'text/csv',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
}

# The scalar findings, in the order they should be presented. Everything
# else in the result is a curve.
SUMMARY_KEYS = ('Best Cathode Data ID', 'Best Anode Data ID',
                'Best Parameters', 'Lowest RMSD')


def result_summary(result_json):
    """The scalar findings as (label, value) pairs, in a fixed order."""
    rows = []
    for key in SUMMARY_KEYS:
        value = result_json.get(key)
        if isinstance(value, (list, tuple)):
            value = ', '.join(str(v) for v in value)
        rows.append((key, value))
    return rows


def result_curves(result_json):
    """
    The curve columns as a DataFrame, shorter columns padded with blanks.
    """
    columns = {key: value for key, value in result_json.items()
               if key not in SUMMARY_KEYS and isinstance(value, list)}
    return pd.DataFrame({key: pd.Series(value)
                         for key, value in columns.items()})


def write_result_csv(result_json, path):
    """
    One CSV of the curves, with the summary as leading ``#`` comment lines.

    Excel shows those as ordinary text rows; pandas skips them with
    ``read_csv(path, comment='#')``.
    """
    with open(path, 'w', newline='', encoding='utf-8') as f:
        for label, value in result_summary(result_json):
            f.write(f"# {label}: {value}\n")
        result_curves(result_json).to_csv(f, index=False)


def write_result_xlsx(result_json, path):
    """Two sheets: the scalar summary, and the curves."""
    summary = pd.DataFrame(result_summary(result_json),
                           columns=['Result', 'Value'])
    with pd.ExcelWriter(path, engine='openpyxl') as writer:
        summary.to_excel(writer, sheet_name='Summary', index=False)
        result_curves(result_json).to_excel(writer, sheet_name='Curves',
                                            index=False)


def write_result_as(result_json, path, fmt):
    """Write a result dict to ``path`` in one of RESULT_FORMATS."""
    if fmt == 'json':
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(result_json, f)
    elif fmt == 'csv':
        write_result_csv(result_json, path)
    elif fmt == 'xlsx':
        write_result_xlsx(result_json, path)
    else:
        raise ValueError(f"Unknown result format: {fmt!r}")
