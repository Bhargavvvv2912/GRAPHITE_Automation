# validation_graphite.py

import subprocess
import re
import sys
from pathlib import Path

def run_command(command, cwd=None):
    """A simple helper to run a command and return its output and exit code."""
    display_command = ' '.join(command)
    print(f"--> Running validation step: '{display_command}' in CWD: '{cwd or '.'}'")
    result = subprocess.run(command, capture_output=True, text=True, cwd=cwd)
    return result.stdout, result.stderr, result.returncode

def main():
    """
    Runs the specific, multi-step validation protocol required for the
    GRAPHITE_Automation repository. Exits with 0 on success, 1 on failure.
    """
    print("--- Starting GRAPHITE Validation Protocol ---")
    
    try:
        # --- Step 1: Create output folders ---
        print("\nStep 1: Creating output folders...")
        _, stderr_sh, returncode_sh = run_command(["bash", "./make_output_folders.sh"])
        if returncode_sh != 0:
            raise RuntimeError(f"make_output_folders.sh failed with stderr:\n{stderr_sh}")
        print("Step 1 successful.")

        # --- Step 2: Execute the main attack script ---
        # We use sys.executable to ensure we use the python from the correct virtual environment.
        python_executable = sys.executable
        print(f"\nStep 2: Executing main attack script (main.py) using {python_executable}...")
        validation_command = [
            python_executable, "main.py", "-v", "14", "-t", "1",
            "--tr_lo", "0.65", "--tr_hi", "0.85", "-s", "score.py",
            "-n", "GTSRB", "--heatmap=Target", "--coarse_mode=binary",
            "-b", "100", "-m", "100"
        ]
        stdout_py, stderr_py, returncode_py = run_command(validation_command)

        if returncode_py != 0:
            raise RuntimeError(f"main.py returned a non-zero exit code with stderr:\n{stderr_py}")
        
        # We print the full stdout so the agent can capture it for metrics.
        print("\n--- Main.py Full Output ---")
        print(stdout_py)
        print("--- End of Main.py Output ---")
        
        print("\n--- GRAPHITE Validation Protocol: PASSED ---")
        sys.exit(0)

    except Exception as e:
        print(f"\n--- GRAPHITE Validation Protocol: FAILED ---", file=sys.stderr)
        print(f"Error during validation: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()