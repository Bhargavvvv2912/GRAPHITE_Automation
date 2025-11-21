# validation_graphite.py (The Final, Correct, and Robust Version)

import subprocess
import re
import sys
from pathlib import Path

# --- THE DEFINITIVE FIX: ANCHOR ALL PATHS TO THE SCRIPT'S LOCATION ---
# This assumes that 'validation_graphite.py' and 'make_output_folders.sh'
# are in the same root directory of your agent's repository.
SCRIPT_DIR = Path(__file__).parent.resolve()
# --- END OF FIX ---

def run_command(command, cwd=None):
    """A simple helper to run a command and return its output and exit code."""
    display_command = ' '.join(command)
    print(f"--> Running validation step: '{display_command}' in CWD: '{cwd or '.'}'")
    result = subprocess.run(command, capture_output=True, text=True, cwd=cwd)
    return result.stdout, result.stderr, result.returncode

def main():
    """
    Runs the specific, multi-step validation protocol for GRAPHITE.
    """
    print("--- Starting GRAPHITE Validation Protocol ---")
    
    try:
        # --- Step 1: Create output folders ---
        print("\nStep 1: Creating output folders...")
        
        # --- THE DEFINITIVE FIX: USE THE ABSOLUTE PATH ---
        # Construct the absolute path to the shell script.
        shell_script_path = str(SCRIPT_DIR / "make_output_folders.sh")
        # Run the command. CWD is now irrelevant for this script.
        _, stderr_sh, returncode_sh = run_command(["bash", shell_script_path])
        # --- END OF FIX ---

        if returncode_sh != 0:
            raise RuntimeError(f"make_output_folders.sh failed with stderr:\n{stderr_sh}")
        print("Step 1 successful.")

        # --- Step 2: Execute the main attack script ---
        python_executable = sys.executable
        print(f"\nStep 2: Executing main attack script (main.py) using {python_executable}...")
        
        # This command is correctly run from inside the 'graphite_repo',
        # so it can find 'main.py'.
        validation_command = [
            python_executable, "main.py", "-v", "14", "-t", "1",
            "--tr_lo", "0.65", "--tr_hi", "0.85", "-s", "score.py",
            "-n", "GTSRB", "--heatmap=Target", "--coarse_mode=binary",
            "-b", "100", "-m", "100"
        ]
        stdout_py, stderr_py, returncode_py = run_command(validation_command)

        if returncode_py != 0:
            raise RuntimeError(f"main.py returned a non-zero exit code with stderr:\n{stderr_py}")
        
        print("Validation PASSED.")
        print("\n--- Main.py Full Output ---")
        print(stdout_py)
        print("--- End of Main.py Output ---")
        sys.exit(0)

    except Exception as e:
        print(f"\n--- GRAPHITE Validation Protocol: FAILED ---", file=sys.stderr)
        print(f"Error during validation: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()