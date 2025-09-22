# agent_utils.py

import subprocess
import re
import sys

def start_group(title):
    """Starts a collapsible log group in GitHub Actions."""
    print(f"\n::group::{title}")

def end_group():
    """Ends a collapsible log group in GitHub Actions."""
    print("::endgroup::")

def run_command(command, cwd=None, python_executable=None):
    """Runs a command and returns the output, error, and return code."""
    full_command = command
    if python_executable and command[0].startswith('python'):
        full_command = [python_executable] + command[1:]
    
    display_command = ' '.join(full_command)
    if len(display_command) > 200:
        display_command = display_command[:200] + "..."
    print(f"Running command: {display_command}")
    
    result = subprocess.run(full_command, capture_output=True, text=True, cwd=cwd)
    return result.stdout, result.stderr, result.returncode

def validate_changes(python_executable, group_title="Running Validation Script"):
    """
    Runs the validation process inside a collapsible group and captures metrics.
    Returns a tuple of (success, metrics_string, full_output).
    """
    start_group(group_title)
    
    print("\n--- Running Validation Step 1: Creating output folders ---")
    _, stderr_sh, returncode_sh = run_command(["bash", "./make_output_folders.sh"])
    if returncode_sh != 0:
        print("Validation Failed: make_output_folders.sh failed.", file=sys.stderr)
        end_group()
        return False, None, stderr_sh
    print("Validation Step 1 successful.")

    print("\n--- Running Validation Step 2: Executing main attack script ---")
    validation_command = [
        "python3", "main.py", "-v", "14", "-t", "1",
        "--tr_lo", "0.65", "--tr_hi", "0.85", "-s", "score.py",
        "-n", "GTSRB", "--heatmap=Target", "--coarse_mode=binary",
        "-b", "100", "-m", "100"
    ]
    stdout_py, stderr_py, returncode_py = run_command(validation_command, python_executable=python_executable)

    print("\n--- Captured output from main.py ---")
    print(f"STDOUT:\n---\n{stdout_py}\n---")
    if stderr_py:
        print(f"STDERR:\n---\n{stderr_py}\n---")
    print("--- End of captured output ---\n")

    if returncode_py != 0:
        print("Validation Failed: main.py returned a non-zero exit code.", file=sys.stderr)
        end_group()
        return False, None, stderr_py
    
    end_group()

    try:
        tr_score = re.search(r"Final transform_robustness:\s*([\d\.]+)", stdout_py).group(1)
        nbits = re.search(r"Final number of pixels:\s*(\d+)", stdout_py).group(1)
        queries = re.search(r"Final number of queries:\s*(\d+)", stdout_py).group(1)
        metrics_body = (
            "Performance Metrics:\n"
            f"- Transform Robustness: {tr_score}\n"
            f"- Pixel Count: {nbits}\n"
            f"- Query Count: {queries}"
        )
        return True, metrics_body, stdout_py + stderr_py
    except (AttributeError, IndexError):
        return True, "Metrics not available for this run.", stdout_py + stderr_py