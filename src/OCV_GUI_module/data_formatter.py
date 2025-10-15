import os
import numpy as np
from scipy.interpolate import interp1d
from scipy.integrate import cumulative_trapezoid as cumtrapz
from scipy.signal import savgol_filter
from scipy.optimize import minimize
from tkinter import filedialog, messagebox, Toplevel, Button, Label, Frame


def parse_txt_file(file_path):
    """
    Parse a txt file, extract first two columns, ignore comments.
    
    Parameters:
    - file_path (str): Path to the txt file
    
    Returns:
    - numpy.ndarray, numpy.ndarray: x_values and y_values arrays
    """
    with open(file_path, 'r') as file:
        lines = file.readlines()
    
    # Extract x and y values from the file, skipping comment lines
    data_pairs = []
    for line in lines:
        line = line.strip()
        # Skip empty lines or lines that start with common comment characters
        if not line or line.startswith('#') or line.startswith('//') or line.startswith('%'):
            continue
        
        try:
            # Try to parse the line as numbers, take first two columns only
            values = line.split()
            if len(values) >= 2:
                x_val, y_val = float(values[0]), float(values[1])
                data_pairs.append((x_val, y_val))
        except ValueError:
            # If parsing fails, skip this line (it's likely a comment)
            continue
    
    if not data_pairs:
        raise ValueError(f"No valid numerical data found in {file_path}")
    
    x_values, y_values = zip(*data_pairs)
    return np.array(x_values), np.array(y_values)


def fix_duplicates_inplace(x_values, y_values, tolerance=1e-10):
    """
    Fix duplicate x values by replacing them with interpolated values between neighbors.
    This preserves the original array length (e.g., keeps exactly 1001 points).
    
    Parameters:
    - x_values (numpy.ndarray): Original x values
    - y_values (numpy.ndarray): Original y values  
    - tolerance (float): Tolerance for considering x values as duplicates
    
    Returns:
    - numpy.ndarray, numpy.ndarray: x and y values with duplicates fixed
    """
    x_fixed = x_values.copy()
    y_fixed = y_values.copy()
    
    # Iteratively fix duplicates until none remain
    max_iterations = 100  # Safety limit to prevent infinite loops
    iteration = 0
    
    while iteration < max_iterations:
        # Find duplicates using tolerance
        rounded_x = np.round(x_fixed / tolerance) * tolerance
        unique_x, counts = np.unique(rounded_x, return_counts=True)
        
        # If no duplicates found, we're done
        if np.all(counts == 1):
            break
            
        # Process each group of duplicates
        duplicates_fixed = False
        
        for unique_val in unique_x[counts > 1]:
            # Find all indices where this duplicate occurs
            duplicate_indices = np.where(np.abs(rounded_x - unique_val) < tolerance)[0]
            
            if len(duplicate_indices) > 1:
                # Find the range to interpolate within
                first_idx = duplicate_indices[0]
                last_idx = duplicate_indices[-1]
                
                # Get boundary points for interpolation
                left_idx = max(0, first_idx - 1)
                right_idx = min(len(x_fixed) - 1, last_idx + 1)
                
                # If duplicates are at the edges, use available neighbors
                if first_idx == 0:
                    # Duplicates at start - use right neighbor
                    if right_idx < len(x_fixed) - 1:
                        left_val = x_fixed[right_idx]
                        right_val = x_fixed[right_idx + 1] if right_idx + 1 < len(x_fixed) else left_val + tolerance
                    else:
                        left_val = x_fixed[0]
                        right_val = left_val + len(duplicate_indices) * tolerance
                elif last_idx == len(x_fixed) - 1:
                    # Duplicates at end - use left neighbor  
                    if left_idx > 0:
                        right_val = x_fixed[left_idx]
                        left_val = x_fixed[left_idx - 1] if left_idx - 1 >= 0 else right_val - tolerance
                    else:
                        right_val = x_fixed[-1]
                        left_val = right_val - len(duplicate_indices) * tolerance
                else:
                    # Duplicates in middle - use both neighbors
                    left_val = x_fixed[left_idx]
                    right_val = x_fixed[right_idx]
                
                # Create interpolated x values for the duplicate group
                if len(duplicate_indices) == 2:
                    # Simple case: replace with midpoint
                    x_fixed[duplicate_indices] = np.array([
                        (left_val + right_val) / 2 - tolerance/2,
                        (left_val + right_val) / 2 + tolerance/2
                    ])
                else:
                    # Multiple duplicates: distribute evenly
                    new_x_values = np.linspace(left_val + tolerance, right_val - tolerance, len(duplicate_indices))
                    x_fixed[duplicate_indices] = new_x_values
                
                # Interpolate corresponding y values
                # Use linear interpolation between boundary points
                left_y = y_fixed[left_idx] if left_idx != first_idx else y_fixed[duplicate_indices[0]]
                right_y = y_fixed[right_idx] if right_idx != last_idx else y_fixed[duplicate_indices[-1]]
                
                # Linear interpolation for y values
                new_y_values = np.linspace(left_y, right_y, len(duplicate_indices))
                y_fixed[duplicate_indices] = new_y_values
                
                duplicates_fixed = True
        
        if not duplicates_fixed:
            break  # No more duplicates to fix
            
        iteration += 1
    
    if iteration >= max_iterations:
        print(f"Warning: Maximum iterations reached while fixing duplicates")
    
    return x_fixed, y_fixed


def smooth_data_via_derivative(x_values, y_values, window_length=31, polyorder=3):
    """
    Smooth data using derivative filtering and integration approach from the notebook.
    
    Steps:
    1. Fix any duplicates in input data
    2. Interpolate to fine grid (1001 points)
    3. Calculate derivative using np.diff
    4. Smooth derivative using Savitzky-Golay filter
    5. Integrate smoothed derivative
    6. Find optimal additive constant to match original data
    
    Parameters:
    - x_values (numpy.ndarray): Original x values
    - y_values (numpy.ndarray): Original y values
    - window_length (int): Window length for Savitzky-Golay filter (must be odd)
    - polyorder (int): Polynomial order for Savitzky-Golay filter
    
    Returns:
    - numpy.ndarray, numpy.ndarray: Smoothed x and y values with 1001 points
    """
    # Fix duplicates in input data first
    x_clean, y_clean = fix_duplicates_inplace(x_values, y_values)
    # Create fine stoichiometry grid (1001 points)
    x_fine = np.linspace(x_clean.min(), x_clean.max(), 1001)
    x_fine_derivative = np.linspace(x_clean.min() + 0.0005 * (x_clean.max() - x_clean.min()), 
                                   x_clean.max() - 0.0005 * (x_clean.max() - x_clean.min()), 1000)
    
    # Create interpolation function from cleaned data
    interp_func = interp1d(x_clean, y_clean, kind='cubic', bounds_error=True)
    
    # Interpolate original data to fine grid
    y_fine = interp_func(x_fine)
    
    # Calculate discrete derivative
    diff_y = np.diff(y_fine)
    
    # Smooth the derivative using Savitzky-Golay filter
    # Ensure window_length is odd and smaller than data length
    window_length = min(window_length, len(diff_y))
    if window_length % 2 == 0:
        window_length -= 1  # Make it odd
    if window_length < polyorder + 1:
        polyorder = max(1, window_length - 1)
        
    diff_y_smoothed = savgol_filter(diff_y, window_length=window_length, polyorder=polyorder)
    
    # Integrate the smoothed derivative
    y_integrated = cumtrapz(diff_y_smoothed, initial=0)
    
    # Ensure we have exactly 1001 points by interpolating the integrated result back to x_fine
    # y_integrated has 1000 points, we need to map it back to 1001 points
    y_integrated_interp = interp1d(x_fine_derivative, y_integrated, 
                                  kind='linear', fill_value='extrapolate')
    
    # Create y_integrated on the full 1001-point grid
    y_integrated_full = y_integrated_interp(x_fine)
    
    # Find optimal additive constant by minimizing RMSD to original data
    avg_integrated = np.average(y_integrated_full)
    
    def cost_function(n):
        y_smooth_shifted = n + y_integrated_full
        rmsd = np.sqrt(np.sum((y_smooth_shifted - y_fine) ** 2) / len(x_fine))
        return rmsd
    
    # Minimize to find best additive constant
    result = minimize(cost_function, avg_integrated)
    n_optimal = result.x[0]
    
    # Create final smooth curve with exactly 1001 points
    y_smooth = n_optimal + y_integrated_full
    
    # Verify we have exactly 1001 points
    assert len(x_fine) == 1001, f"Expected 1001 x points, got {len(x_fine)}"
    assert len(y_smooth) == 1001, f"Expected 1001 y points, got {len(y_smooth)}"
    
    return x_fine, y_smooth


def format_data_to_1001_points(x_values, y_values, use_smoothing=True):
    """
    Convert data to exactly 1001 points with optional derivative-based smoothing.
    
    Parameters:
    - x_values (numpy.ndarray): Original x values
    - y_values (numpy.ndarray): Original y values
    - use_smoothing (bool): Whether to apply derivative-based smoothing
    
    Returns:
    - numpy.ndarray, numpy.ndarray: Formatted x and y values with 1001 points
    """
    if len(x_values) < 4:
        raise ValueError("Not enough data points for interpolation (need at least 4)")
    
    if use_smoothing and len(x_values) > 10:
        # Use derivative-based smoothing for noisy data
        try:
            x_formatted, y_formatted = smooth_data_via_derivative(x_values, y_values)
            # Fix any duplicates that might have been created during smoothing
            x_final, y_final = fix_duplicates_inplace(x_formatted, y_formatted)
            return x_final, y_final
        except Exception as e:
            print(f"Smoothing failed ({str(e)}), falling back to simple interpolation")
            # Fall back to simple interpolation if smoothing fails
            use_smoothing = False
    
    if not use_smoothing:
        # Simple interpolation without smoothing
        # Fix duplicates in original data first
        x_clean, y_clean = fix_duplicates_inplace(x_values, y_values)
        
        interp_func = interp1d(x_clean, y_clean, kind='cubic', bounds_error=True)
        x_formatted = np.linspace(x_clean.min(), x_clean.max(), 1001)
        y_formatted = interp_func(x_formatted)
        
        # Fix any duplicates that might occur in the final result
        x_final, y_final = fix_duplicates_inplace(x_formatted, y_formatted)
        return x_final, y_final


def save_formatted_file(x_values, y_values, output_path):
    """
    Save formatted data to a new txt file with no comments - just data points.
    
    Parameters:
    - x_values (numpy.ndarray): Formatted x values
    - y_values (numpy.ndarray): Formatted y values  
    - output_path (str): Path where to save the formatted file
    """
    with open(output_path, 'w') as file:
        # Write only data points - no comments to ensure compatibility
        for x, y in zip(x_values, y_values):
            file.write(f"{x:.6f}\t{y:.6f}\n")


def process_single_file(input_file_path, output_file_path, use_smoothing=True):
    """
    Process a single txt file: parse, format to 1001 points with optional smoothing, and save.
    
    Parameters:
    - input_file_path (str): Path to input file
    - output_file_path (str): Path to output file
    - use_smoothing (bool): Whether to apply derivative-based smoothing
    
    Returns:
    - bool: True if successful, False if failed
    """
    try:
        # Parse the original file
        x_values, y_values = parse_txt_file(input_file_path)
        
        # Format to 1001 points with optional smoothing
        x_formatted, y_formatted = format_data_to_1001_points(x_values, y_values, use_smoothing)
        
        # Save formatted file
        save_formatted_file(x_formatted, y_formatted, output_file_path)
        
        return True
        
    except Exception as e:
        print(f"Error processing {input_file_path}: {str(e)}")
        return False


def check_and_correct_orientation(x_values, y_values, curve_type):
    """
    Check and correct curve orientation based on electrode type.
    
    Parameters:
    - x_values: X-axis data (SOC or Capacity)
    - y_values: Y-axis data (Voltage)
    - curve_type: 'cathode', 'anode', or 'battery'
    
    Returns:
    - corrected_x, corrected_y: Arrays with proper orientation
    """
    # Create a copy to avoid modifying original data
    x_data = np.array(x_values)
    y_data = np.array(y_values)
    
    # Check current orientation of y values
    y_is_ascending = y_data[-1] > y_data[0]
    
    if curve_type in ['cathode', 'battery']:
        # Cathodes and batteries should have ascending y values (low to high voltage)
        if not y_is_ascending:
            # Reverse only y values to make y ascending
            y_data = y_data[::-1]
            return x_data, y_data, True  # Orientation was corrected
    
    elif curve_type == 'anode':
        # Anodes should have descending y values (high to low voltage)
        if y_is_ascending:
            # Reverse only y values to make y descending
            y_data = y_data[::-1]
            return x_data, y_data, True  # Orientation was corrected
    
    return x_data, y_data, False  # No correction needed


def show_curve_type_dialog():
    """
    Show a dialog to ask user what type of curves they're formatting.
    
    Returns:
    - str: 'cathode', 'anode', 'battery', 'mixed', or None if cancelled
    """
    result = [None]  # Use list to allow modification in nested function
    
    def on_selection(choice):
        result[0] = choice
        dialog.destroy()
    
    # Create dialog window
    dialog = Toplevel()
    dialog.title("Curve Type Selection")
    dialog.geometry("600x300")  # Larger default size
    dialog.minsize(500, 250)     # Minimum size
    dialog.resizable(True, True) # Allow resizing
    dialog.configure(bg="#2C2F33")
    
    # Center the dialog
    dialog.transient()
    dialog.grab_set()
    
    # Add instruction label
    label = Label(dialog, 
                 text="What type of curves are you formatting?",
                 font=("Arial", 12, "bold"),
                 bg="#2C2F33", fg="white")
    label.pack(pady=20)
    
    # Create button frame
    button_frame = Frame(dialog, bg="#2C2F33")
    button_frame.pack(pady=10)
    
    # Add buttons for each option
    buttons_info = [
        ("Cathode OCP", "cathode"),
        ("Anode OCP", "anode"), 
        ("Battery OCV", "battery"),
        ("Mixed Types", "mixed")
    ]
    
    for text, value in buttons_info:
        btn = Button(button_frame, text=text, 
                    command=lambda v=value: on_selection(v),
                    width=12, height=1,
                    font=("Arial", 10))
        btn.pack(side="left", padx=5)
    
    # Add cancel button
    cancel_btn = Button(dialog, text="Cancel", 
                       command=lambda: on_selection(None),
                       font=("Arial", 10))
    cancel_btn.pack(pady=(10, 20))
    
    # Wait for dialog to close
    dialog.wait_window()
    
    return result[0]


def process_single_file_with_orientation(input_path, output_path, curve_type, use_smoothing=True):
    """
    Process a single txt file with orientation correction and formatting.
    
    Parameters:
    - input_path: Path to input txt file
    - output_path: Path for output formatted file
    - curve_type: 'cathode', 'anode', or 'battery'
    - use_smoothing: Whether to apply derivative-based smoothing
    
    Returns:
    - bool: True if successful, False otherwise
    """
    try:
        # Parse the input file
        x_values, y_values = parse_txt_file(input_path)
        
        if len(x_values) == 0:
            return False
        
        # Fix duplicates first
        x_clean, y_clean = fix_duplicates_inplace(x_values, y_values)
        
        # Check and correct orientation based on curve type
        x_oriented, y_oriented, was_corrected = check_and_correct_orientation(x_clean, y_clean, curve_type)
        
        # Apply smoothing if requested
        if use_smoothing:
            x_final, y_final = smooth_data_via_derivative(x_oriented, y_oriented)
        else:
            # Just interpolate to standard 1001 points
            x_final = np.linspace(x_oriented.min(), x_oriented.max(), 1001)
            interp_func = interp1d(x_oriented, y_oriented, kind='linear', bounds_error=False, fill_value='extrapolate')
            y_final = interp_func(x_final)
        
        # Save the processed data
        with open(output_path, 'w') as file:
            for x, y in zip(x_final, y_final):
                file.write(f"{x} {y}\n")
        
        return True
        
    except Exception as e:
        print(f"Error processing {input_path}: {e}")
        return False


def format_folder_data():
    """
    Main function to format all txt files in a selected folder.
    Opens file dialog, processes all txt files, creates _FORMATTED folder and files.
    """
    # Open folder selection dialog
    folder_path = filedialog.askdirectory(
        title="Select folder containing txt files to format"
    )
    
    if not folder_path:
        return  # User cancelled
    
    # Get all txt files in the folder
    txt_files = [f for f in os.listdir(folder_path) if f.lower().endswith('.txt')]
    
    if not txt_files:
        messagebox.showwarning("No Files", "No .txt files found in the selected folder.")
        return
    
    # Ask user about curve type
    curve_type = show_curve_type_dialog()
    
    if curve_type is None:
        return  # User cancelled
    
    if curve_type == 'mixed':
        messagebox.showinfo("Mixed Types", 
                          "Please separate your data into different folders according to curve type:\n"
                          "- Cathode OCP files in one folder\n"
                          "- Anode OCP files in another folder\n" 
                          "- Battery OCV files in a third folder\n\n"
                          "Then run the formatter on each folder separately.")
        return
    
    # Create output folder with _FORMATTED suffix
    folder_name = os.path.basename(folder_path)
    parent_dir = os.path.dirname(folder_path)
    output_folder = os.path.join(parent_dir, f"{folder_name}_FORMATTED")
    
    # Create output directory if it doesn't exist
    os.makedirs(output_folder, exist_ok=True)
    
    # Process each txt file
    successful_files = 0
    failed_files = 0
    corrected_files = 0
    
    for txt_file in txt_files:
        input_path = os.path.join(folder_path, txt_file)
        
        # Create output filename with _FORMATTED suffix
        name_without_ext = os.path.splitext(txt_file)[0]
        output_filename = f"{name_without_ext}_FORMATTED.txt"
        output_path = os.path.join(output_folder, output_filename)
        
        # Process the file with smoothing and orientation correction
        if process_single_file_with_orientation(input_path, output_path, curve_type, use_smoothing=True):
            successful_files += 1
        else:
            failed_files += 1
    
    # Show results
    message = f"Data formatting complete!\n\n"
    message += f"Processed: {successful_files} files successfully\n"
    if failed_files > 0:
        message += f"Failed: {failed_files} files\n"
    message += f"Curve type: {curve_type.title()}\n"
    message += f"\nFormatted files saved to:\n{output_folder}"
    
    messagebox.showinfo("Formatting Complete", message)