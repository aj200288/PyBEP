try:
    # When imported as a package/module
    from .data_formatter import load_ocv_curve
except ImportError:
    # Fallback to absolute import for direct execution
    from data_formatter import load_ocv_curve


def load_soc_ocv_data(txt_file):
    """
    Load SOC and OCV data from a data file (.txt/.csv/.xlsx).

    Parameters:
    - txt_file (str): Path to the file containing SOC and OCV data.

    Returns:
    - numpy.ndarray, numpy.ndarray: SOC_battery and OCV_battery arrays.
    """
    SOC_battery, OCV_battery, _warnings, _used_columns = load_ocv_curve(
        txt_file, curve_type='battery')
    return SOC_battery, OCV_battery
