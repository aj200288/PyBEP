try:
    # When imported as a package/module
    from .data_formatter import parse_txt_file
except ImportError:
    # Fallback to absolute import for direct execution
    from data_formatter import parse_txt_file


def load_soc_ocv_data(txt_file):
    """
    Load SOC and OCV data from a txt file.

    Parameters:
    - txt_file (str): Path to the txt file containing SOC and OCV data.

    Returns:
    - numpy.ndarray, numpy.ndarray: SOC_battery and OCV_battery arrays.
    """
    SOC_battery, OCV_battery = parse_txt_file(txt_file)
    return SOC_battery, OCV_battery
