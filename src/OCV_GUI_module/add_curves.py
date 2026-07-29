import os
from scipy.interpolate import interp1d

try:
    # When imported as a package/module
    from .data_formatter import load_ocv_curve
except ImportError:
    # Fallback to absolute import for direct execution
    from data_formatter import load_ocv_curve


def add_half_cell_data(directory_name, curve_type):
    """
    Add half-cell data from data files in the specified path to a dictionary.

    Parameters:
    - directory_name (str): The directory name containing data files
      (.txt/.csv/.xlsx) with SOC/OCV data.
    - curve_type (str): 'cathode' or 'anode' — controls orientation
      convention (see data_formatter.check_and_correct_orientation).

    Raises:
    - ValueError: If the specified directory does not exist.
    - DataFormatError: If a file in the directory cannot be parsed, or the
      user cancels the column-selection dialog.

    Returns:
    - dict: A dictionary containing half-cell data.
    """
    directory_path = os.path.join(os.getcwd(), directory_name)

    # Check if the directory exists
    if not os.path.exists(directory_path):
        raise ValueError(f"The directory '{directory_path}' does not exist.")

    # Create a dictionary to store the half-cell data
    half_cell_dictionary = {}

    # Get a list of all supported data files in the specified directory
    data_files = [f for f in os.listdir(directory_path)
                  if f.lower().endswith(('.txt', '.csv', '.xlsx'))]

    # Column mapping is confirmed once (on the first file) and reused for
    # the rest of the folder, since a folder is typically one consistent
    # export format.
    column_choice = None

    # Iterate through each data file
    for data_file in data_files:
        file_path = os.path.join(directory_path, data_file)

        x_values, y_values, _warnings, used_columns = load_ocv_curve(
            file_path, curve_type, column_choice=column_choice)
        if column_choice is None:
            column_choice = used_columns

        # Interpolate the OCP function
        interpolated_function = interp1d(
            x_values, y_values, kind='cubic', fill_value='extrapolate'
        )

        # Create a new dataset
        new_dataset = {
            'ID_number': os.path.splitext(data_file)[0],
            'x_values': x_values,
            'interpolated_function': interpolated_function
        }

        # Add the new dataset to the dictionary
        half_cell_dictionary[new_dataset['ID_number']] = new_dataset

    return half_cell_dictionary
