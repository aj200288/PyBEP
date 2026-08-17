"""
PyBEP core: battery OCV decomposition, independent of any user interface.

Everything the desktop GUI (``gui_app``) and the website (``web``) share
lives here: reading and cleaning SOC/OCV data files, and the optimization
that decomposes a battery OCV curve into cathode and anode OCP curves.

Nothing in this package may import tkinter, flask, or any other UI
toolkit — that is what lets both front ends build on the same code instead
of each keeping their own copy. Anywhere a user would need to make a
choice, core takes a callback instead (see
``data_formatter.resolve_soc_ocv_columns``).

Typical use::

    from core import add_half_cell_data, load_soc_ocv_data
    from core import perform_full_optimization_parallel

    cathodes = add_half_cell_data("data/cathode_data", curve_type="cathode")
    anodes = add_half_cell_data("data/anode_data", curve_type="anode")
    soc, ocv = load_soc_ocv_data("data/NMC811vsGraphite_OCV-LICeM.txt")
    result = perform_full_optimization_parallel(soc, ocv, cathodes, anodes)
"""
from .data_formatter import (
    DataFormatError,
    read_raw_table,
    guess_column_roles,
    resolve_soc_ocv_columns,
    load_ocv_curve,
    format_files,
    format_folder,
    list_data_files,
    DATA_FILE_EXTENSIONS,
)
from .add_curves import add_half_cell_data, build_curve_entry
from .add_battery import load_soc_ocv_data
from .optimization_functions import (
    perform_full_optimization_parallel,
    save_optimization_result_to_json,
    perform_full_optimization_parallel_to_json,
)

__all__ = [
    "DataFormatError",
    "read_raw_table",
    "guess_column_roles",
    "resolve_soc_ocv_columns",
    "load_ocv_curve",
    "format_files",
    "format_folder",
    "list_data_files",
    "DATA_FILE_EXTENSIONS",
    "add_half_cell_data",
    "build_curve_entry",
    "load_soc_ocv_data",
    "perform_full_optimization_parallel",
    "save_optimization_result_to_json",
    "perform_full_optimization_parallel_to_json",
]
