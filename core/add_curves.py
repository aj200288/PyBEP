import os
from scipy.interpolate import interp1d

from .data_formatter import load_ocv_curve, list_data_files


def build_curve_entry(x_values, y_values):
    """
    Build the {'x_values', 'interpolated_function'} entry that
    optimization_functions.perform_full_optimization_parallel expects for
    each cathode/anode candidate. Shared by the folder loader below and by
    the web app, which gets its curves from uploads rather than a folder.
    """
    return {
        'x_values': x_values,
        'interpolated_function': interp1d(
            x_values, y_values, kind='cubic', fill_value='extrapolate'),
    }


def add_half_cell_data(directory_name, curve_type, column_resolver=None):
    """
    Add half-cell data from data files in the specified path to a dictionary.

    Parameters:
    - directory_name (str): The directory name containing data files
      (.txt/.csv/.xlsx) with SOC/OCV data.
    - curve_type (str): 'cathode' or 'anode' — controls orientation
      convention (see data_formatter.check_and_correct_orientation).
    - column_resolver: optional UI callback to confirm the SOC/OCV columns
      (see data_formatter.resolve_soc_ocv_columns). Consulted at most once
      per folder.

    Raises:
    - ValueError: If the specified directory does not exist.
    - DataFormatError: If a file in the directory cannot be parsed, or the
      user cancels the column-selection dialog.

    Returns:
    - dict: A dictionary containing half-cell data.
    """
    directory_path = os.path.abspath(directory_name)

    # Check if the directory exists
    if not os.path.isdir(directory_path):
        raise ValueError(f"The directory '{directory_path}' does not exist.")

    # Create a dictionary to store the half-cell data
    half_cell_dictionary = {}

    # Get a list of all supported data files in the specified directory
    data_files = list_data_files(directory_path)

    # Column mapping is confirmed once (on the first file) and reused for
    # the rest of the folder, since a folder is typically one consistent
    # export format.
    column_choice = None

    # Iterate through each data file
    for data_file in data_files:
        file_path = os.path.join(directory_path, data_file)

        x_values, y_values, _warnings, used_columns = load_ocv_curve(
            file_path, curve_type, column_choice=column_choice,
            column_resolver=column_resolver)
        if column_choice is None:
            column_choice = used_columns

        # Interpolate the OCP function and store it under the file's name
        data_id = os.path.splitext(data_file)[0]
        entry = build_curve_entry(x_values, y_values)
        entry['ID_number'] = data_id
        half_cell_dictionary[data_id] = entry

    return half_cell_dictionary
