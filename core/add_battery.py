from .data_formatter import load_ocv_curve


def load_soc_ocv_data(txt_file, column_resolver=None):
    """
    Load SOC and OCV data from a data file (.txt/.csv/.xlsx).

    Parameters:
    - txt_file (str): Path to the file containing SOC and OCV data.
    - column_resolver: optional UI callback to confirm the SOC/OCV columns
      (see data_formatter.resolve_soc_ocv_columns).

    Returns:
    - numpy.ndarray, numpy.ndarray: SOC_battery and OCV_battery arrays.
    """
    SOC_battery, OCV_battery, _warnings, _used_columns = load_ocv_curve(
        txt_file, curve_type='battery', column_resolver=column_resolver)
    return SOC_battery, OCV_battery
