import os
import numpy as np
from scipy.interpolate import interp1d
from scipy.optimize import differential_evolution
from joblib import Parallel, delayed
import json


def calculate_inverse_derivative(x, y):
    """
    Calculate the inverse of the derivative of a function.

    Parameters:
    - x: array-like
        X-axis values.
    - y: array-like
        Y-axis values.

    Returns:
    - yi: array-like
        Inverse of the derivative of the function.
    """
    yd = np.gradient(y, x)
    yi = 1 / yd
    return yi


def optimization(params, anode_values, anode_x_values, cathode_values,
                 cathode_x_values, OCV_battery, SOC_battery,
                 OCV_battery_d_in=None, battery=1, derivative_inverse=0):
    """
    Objective function for optimization.

    Parameters:
    - params: tuple
        Optimization parameters:
        e_percentage, f_percentage, g_percentage, h_percentage.
    - anode_values: array-like
        Anode OCP evaluated on anode_x_values. Precomputed by the caller
        once per (cathode, anode) pair, since it does not depend on params.
    - anode_x_values: array-like
        X-axis values for the anode.
    - cathode_values: array-like
        Cathode OCP evaluated on cathode_x_values. Precomputed by the
        caller once per (cathode, anode) pair for the same reason.
    - cathode_x_values: array-like
        X-axis values for the cathode.
    - OCV_battery: array-like
        Measured battery open-circuit voltage (OCV).
    - SOC_battery: array-like
        State of charge (SOC) values for the battery.
    - OCV_battery_d_in: array-like, optional
        Precomputed inverse derivative of the measured battery OCV.
        Only needed (and only computed by the caller) when
        derivative_inverse is non-zero.
    - battery, derivative_inverse: float, optional
        Weighting factors for different components of the objective function.

    Returns:
    - RMSD: float
        Root Mean Square Deviation, the objective value for optimization.
    """
    e_percentage, f_percentage, g_percentage, h_percentage = params

    e = int(e_percentage * len(anode_x_values) * 0.3)
    f = len(anode_x_values) - int(f_percentage * len(anode_x_values) * 0.3)
    g = int(g_percentage * len(cathode_x_values) * 0.15)
    h = len(cathode_x_values) - int(
        h_percentage * len(cathode_x_values) * 0.15)

    if f > len(anode_x_values):
        f = len(anode_x_values)

    if h > len(cathode_x_values):
        h = len(cathode_x_values)

    axv = anode_x_values
    w = interp1d(axv[e:f], anode_values[e:f], kind='cubic', fill_value='extrapolate')
    x_a = np.linspace(axv[e:f][0], axv[e:f][-1], 1001)

    cxv = cathode_x_values
    r = interp1d(cxv[g:h], cathode_values[g:h], kind='cubic', fill_value='extrapolate')
    x_c = np.linspace(cxv[g:h][0], cxv[g:h][-1], 1001)

    calculated_battery_OCV = r(x_c) - w(x_a)

    RMSD = battery * np.sqrt(
        np.mean((calculated_battery_OCV - OCV_battery) ** 2))

    # Skip the derivative term entirely when its weight is zero: it's
    # wasted work, and a flat plateau in calculated_battery_OCV can make
    # the inverse derivative blow up to inf/nan, which would otherwise
    # poison the RMSD via `0 * nan == nan` even though the weight is 0.
    if derivative_inverse:
        calculated_battery_OCV_d_in = calculate_inverse_derivative(
            SOC_battery, calculated_battery_OCV)
        RMSD += derivative_inverse * np.sqrt(
            np.mean((calculated_battery_OCV_d_in - OCV_battery_d_in) ** 2))

    return RMSD


def perform_optimization(cathode_number, cathode_info, anode_number,
                         anode_info, OCV_battery, SOC_battery, battery,
                         derivative_inverse):
    """
    Perform optimization for a specific cathode and anode combination.

    Parameters:
    - cathode_number: int
        Identifier for the cathode data.
    - cathode_info: dict
        Information about the cathode,
        including interpolated function and x values.
    - anode_number: int
        Identifier for the anode data.
    - anode_info: dict
        Information about the anode,
        including interpolated function and x values.
    - OCV_battery: array-like
        Measured battery open-circuit voltage (OCV).
    - SOC_battery: array-like
        State of charge (SOC) values for the battery.

    Returns:
    - optimization_results: dict
        Dictionary containing optimization results,
        including cathode and anode data IDs,
        optimized parameters, and RMSD (Root Mean Square Deviation).
    """
    cathode_interp = cathode_info['interpolated_function']
    cathode_x_values = cathode_info['x_values']

    anode_interp = anode_info['interpolated_function']
    anode_x_values = anode_info['x_values']

    # These are invariant across every optimization() call that
    # differential_evolution makes below, so compute them once here
    # rather than on every objective-function evaluation.
    anode_values = anode_interp(anode_x_values)
    cathode_values = cathode_interp(cathode_x_values)
    OCV_battery_d_in = (
        calculate_inverse_derivative(SOC_battery, OCV_battery)
        if derivative_inverse else None
    )

    bounds = [(0, 1), (0, 1), (0, 1), (0, 1)]

    opt_result = differential_evolution(
        lambda params: optimization(params, anode_values, anode_x_values,
                                    cathode_values, cathode_x_values,
                                    OCV_battery, SOC_battery,
                                    OCV_battery_d_in=OCV_battery_d_in,
                                    battery=battery,
                                    derivative_inverse=derivative_inverse),
        bounds
    )
    optimized_params = opt_result.x
    RMSD_opt = opt_result.fun

    return {
        'cathode_data_ID': cathode_number,
        'anode_data_ID': anode_number,
        'optimized_params': optimized_params,
        'RMSD': RMSD_opt
    }


def perform_full_optimization_parallel(SOC_battery, OCV_battery,
                                       interpolated_cathodes,
                                       interpolated_anodes, iterations=5,
                                       battery=1, derivative_inverse=0):
    """
    Perform parallelized full optimization for multiple iterations
    and find the overall best optimization result.

    Parameters:
    - SOC_battery: array-like
        State of charge (SOC) values for the battery.
    - OCV_battery: array-like
        Measured battery open-circuit voltage (OCV).
    - interpolated_cathodes: dict
        Dictionary containing information about interpolated cathode functions.
    - interpolated_anodes: dict
        Dictionary containing information about interpolated anode functions.
    - iterations: int, optional
        Number of iterations for optimization.
    - battery, derivative_inverse: float, optional
        Weighting factors for different components of the objective function.

    Returns:
    - result: dict
        Dictionary containing optimization results and plots.
    """
    best_optimization_results = []

    for _ in range(iterations):
        optimization_results = Parallel(n_jobs=-1)(
            delayed(perform_optimization)(cathode_number, cathode_info,
                                          anode_number, anode_info,
                                          OCV_battery, SOC_battery, battery,
                                          derivative_inverse)
            for cathode_number, cathode_info in interpolated_cathodes.items()
            for anode_number, anode_info in interpolated_anodes.items()
        )

        best_optimization_results.append(
            min(optimization_results, key=lambda x: x['RMSD']))

    best_optimization_result = min(
        best_optimization_results, key=lambda x: x['RMSD'])

    best_cathode_data_ID = best_optimization_result['cathode_data_ID']
    best_anode_data_ID = best_optimization_result['anode_data_ID']

    Best_Cathode = interpolated_cathodes.get(best_cathode_data_ID)
    Best_Anode = interpolated_anodes.get(best_anode_data_ID)

    if Best_Cathode is not None and Best_Anode is not None:
        e_percentage_opt, f_percentage_opt, g_percentage_opt, \
            h_percentage_opt = best_optimization_result['optimized_params']
        e_opt = int(e_percentage_opt * len(Best_Anode['x_values']) * 0.3)
        f_opt = len(Best_Anode['x_values']) - \
            int(f_percentage_opt * len(Best_Anode['x_values']) * 0.3)
        g_opt = int(g_percentage_opt * len(Best_Cathode['x_values']) * 0.15)
        h_opt = len(Best_Cathode['x_values']) - \
            int(h_percentage_opt * len(Best_Cathode['x_values']) * 0.15)
        best_parameters = e_opt, f_opt, g_opt, h_opt

        v1 = Best_Anode['interpolated_function'](Best_Anode['x_values'])
        axv_opt = Best_Anode['x_values']
        w1 = interp1d(
            axv_opt[e_opt:f_opt], v1[e_opt:f_opt],
            kind='cubic', fill_value='extrapolate')
        w1_ns = interp1d(
            axv_opt, v1,
            kind='cubic', fill_value='extrapolate')
        x_a1 = np.linspace(
            axv_opt[e_opt:f_opt][0], axv_opt[e_opt:f_opt][-1], 1001)
        x_a1_ns = np.linspace(
            axv_opt[0], axv_opt[-1], 1001+e_opt+(1001-f_opt))
        q1 = Best_Cathode['interpolated_function'](Best_Cathode['x_values'])
        cxv_opt = Best_Cathode['x_values']
        r1 = interp1d(
            cxv_opt[g_opt:h_opt], q1[g_opt:h_opt],
            kind='cubic', fill_value='extrapolate')
        r1_ns = interp1d(
            cxv_opt, q1,
            kind='cubic', fill_value='extrapolate')
        x_c1 = np.linspace(
            cxv_opt[g_opt:h_opt][0], cxv_opt[g_opt:h_opt][-1], 1001)
        x_c1_ns = np.linspace(
            cxv_opt[0], cxv_opt[-1], 1001+g_opt+(1001-h_opt))

        calculated_battery_OCV_opt = r1(x_c1) - w1(x_a1)

        cscalesoc = x_c1 - min(x_c1)
        c_SOC = cscalesoc / max(cscalesoc)

        cfullscalesoc = x_c1_ns - min(x_c1)
        c_SOC_full = cfullscalesoc / max(cscalesoc)
        ascalesoc = x_a1 - min(x_a1)
        a_SOC = ascalesoc / max(ascalesoc)

        afullscalesoc = x_a1_ns - min(x_a1)
        a_SOC_full = afullscalesoc / max(ascalesoc)

    result = {
        'Best Cathode Data ID': best_cathode_data_ID,
        'Best Anode Data ID': best_anode_data_ID,
        'Best Parameters': best_parameters,
        'Lowest RMSD': best_optimization_result['RMSD'],
        'SOC_battery': SOC_battery,
        'OCV_battery': OCV_battery,
        'calculated_battery_OCV_opt': calculated_battery_OCV_opt,
        'c_SOC_full': c_SOC_full,
        'r1_ns_x_c1_ns': r1_ns(x_c1_ns),
        'c_SOC': c_SOC,
        'r1_x_c1': r1(x_c1),
        'a_SOC_full': a_SOC_full,
        'w1_ns_x_a1_ns': w1_ns(x_a1_ns),
        'a_SOC': a_SOC,
        'w1_x_a1': w1(x_a1)
    }

    return result


def save_optimization_result_to_json(result, filepath):
    """
    Serialize a result dict produced by perform_full_optimization_parallel
    to a JSON file, converting numpy arrays to lists and using the
    human-readable key names expected in the JSON output.

    Parameters:
    - result: dict
        Result dictionary as returned by perform_full_optimization_parallel.
    - filepath: str
        Full path (including filename) to write the JSON file to.

    Returns:
    None
    """
    json_result = {
        'Best Cathode Data ID': result['Best Cathode Data ID'],
        'Best Anode Data ID': result['Best Anode Data ID'],
        'Best Parameters': result['Best Parameters'],
        'Lowest RMSD': result['Lowest RMSD'],
        'Battery SOC': result['SOC_battery'].tolist(),
        'Battery OCV': result['OCV_battery'].tolist(),
        'Calculated Battery OCV': result['calculated_battery_OCV_opt'].tolist(),
        'Cathode SOC full': result['c_SOC_full'].tolist(),
        'Cathode OCP full': result['r1_ns_x_c1_ns'].tolist(),
        'Cathode SOC': result['c_SOC'].tolist(),
        'Cathode OCP': result['r1_x_c1'].tolist(),
        'Anode SOC full': result['a_SOC_full'].tolist(),
        'Anode OCP full': result['w1_ns_x_a1_ns'].tolist(),
        'Anode SOC': result['a_SOC'].tolist(),
        'Anode OCP': result['w1_x_a1'].tolist(),
    }

    with open(filepath, 'w') as f:
        json.dump(json_result, f)


def perform_full_optimization_parallel_to_json_GUI(filename, SOC_battery,
                                                   OCV_battery,
                                                   interpolated_cathodes,
                                                   interpolated_anodes,
                                                   iterations=5,
                                                   battery=1,
                                                   derivative_inverse=0):
    """
    Perform parallelized full optimization for multiple iterations
    and write the overall best optimization result to a JSON file.

    Parameters:
    - filename: str
        Name of the JSON file to write the results.
    - SOC_battery: array-like
        State of charge (SOC) values for the battery.
    - OCV_battery: array-like
        Measured battery open-circuit voltage (OCV).
    - interpolated_cathodes: dict
        Dictionary containing information about interpolated cathode functions.
    - interpolated_anodes: dict
        Dictionary containing information about interpolated anode functions.
    - iterations: int, optional
        Number of iterations for optimization.
    - battery, derivative_inverse: float, optional
        Weighting factors for different components of the objective function.

    Returns:
    None
    """
    result = perform_full_optimization_parallel(
        SOC_battery, OCV_battery, interpolated_cathodes, interpolated_anodes,
        iterations=iterations, battery=battery,
        derivative_inverse=derivative_inverse)
    save_optimization_result_to_json(result, filename)


def perform_full_optimization_parallel_to_json(filename, file_location,
                                               SOC_battery,
                                               OCV_battery,
                                               interpolated_cathodes,
                                               interpolated_anodes,
                                               iterations=5,
                                               battery=1,
                                               derivative_inverse=0):
    """
    Perform parallelized full optimization for multiple iterations
    and write the overall best optimization result to a JSON file.

    Parameters:
    - filename: str
        Name of the JSON file to write the results.
    - file_location: str
        Location where the JSON file will be saved.
    - SOC_battery: array-like
        State of charge (SOC) values for the battery.
    - OCV_battery: array-like
        Measured battery open-circuit voltage (OCV).
    - interpolated_cathodes: dict
        Dictionary containing information about interpolated cathode functions.
    - interpolated_anodes: dict
        Dictionary containing information about interpolated anode functions.
    - iterations: int, optional
        Number of iterations for optimization.
    - battery, derivative_inverse: float, optional
        Weighting factors for different components of the objective function.

    Returns:
    None
    """
    result = perform_full_optimization_parallel(
        SOC_battery, OCV_battery, interpolated_cathodes, interpolated_anodes,
        iterations=iterations, battery=battery,
        derivative_inverse=derivative_inverse)
    save_optimization_result_to_json(result, os.path.join(file_location, filename))
