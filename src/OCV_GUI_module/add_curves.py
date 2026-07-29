import os
from scipy.interpolate import interp1d

try:
    # When imported as a package/module
    from .data_formatter import parse_txt_file
except ImportError:
    # Fallback to absolute import for direct execution
    from data_formatter import parse_txt_file


def add_half_cell_data(directory_name):
    """
    Add half-cell data from text files in the specified path to a dictionary.

    Parameters:
    - directory_name (str): The directory name containing text files with data.

    Raises:
    - ValueError: If the specified directory does not exist.

    Returns:
    - dict: A dictionary containing half-cell data.
    """
    directory_path = os.path.join(os.getcwd(), directory_name)

    # Check if the directory exists
    if not os.path.exists(directory_path):
        raise ValueError(f"The directory '{directory_path}' does not exist.")

    # Create a dictionary to store the half-cell data
    half_cell_dictionary = {}

    # Get a list of all txt files in the specified directory
    txt_files = [f for f in os.listdir(directory_path) if f.endswith('.txt')]

    # Iterate through each txt file
    for txt_file in txt_files:
        file_path = os.path.join(directory_path, txt_file)

        # Load x and y values from the file
        x_values, y_values = parse_txt_file(file_path)

        # Check if x values are in decreasing order
        if all(x > y for x, y in zip(x_values, x_values[1:])):
            # If decreasing, reverse the x, y values
            x_values = x_values[::-1]
            y_values = y_values[::-1]

        # Interpolate the OCP function
        interpolated_function = interp1d(
            x_values, y_values, kind='cubic', fill_value='extrapolate'
        )

        # Create a new dataset
        new_dataset = {
            'ID_number': os.path.splitext(txt_file)[0],
            'x_values': x_values,
            'interpolated_function': interpolated_function
        }

        # Add the new dataset to the dictionary
        half_cell_dictionary[new_dataset['ID_number']] = new_dataset

    return half_cell_dictionary
