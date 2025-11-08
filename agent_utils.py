# agent_utils.py

import subprocess
import re
import sys
from pathlib import Path

def start_group(title):
    """Starts a collapsible log group in GitHub Actions."""
    print(f"\n::group::{title}")

def end_group():
    """Ends a collapsible log group in GitHub Actions."""
    print("::endgroup::")

def run_command(command, cwd=None, display_command=True):
    """Runs a command and returns the output, error, and return code."""
    if display_command:
        display_str = ' '.join(command)
        if len(display_str) > 200:
            display_str = display_str[:200] + "..."
        print(f"--> Running command: '{display_str}' in CWD: '{cwd or '.'}'")
    
    result = subprocess.run(command, capture_output=True, text=True, cwd=cwd)
    return result.stdout, result.stderr, result.returncode

def _parse_pytest_summary(full_output: str) -> dict:
    """A helper function to parse pytest output, not used by this project."""
    # This is a placeholder as GRAPHITE does not use pytest.
    return {"passed": "0", "failed": "0", "errors": "0", "skipped": "0"}

def _run_smoke_test(python_executable: str, config: dict) -> tuple[bool, str, str]:
    """
    Runs the specific, multi-step validation process required for the
    GRAPHITE_Automation repository.
    """
    print("\n--- Running GRAPHITE Validation Protocol ---")
    
    # --- Step 1: Create output folders ---
    print("Step 1: Creating output folders...")
    _, stderr_sh, returncode_sh = run_command(["bash", "./make_output_folders.sh"])
    if returncode_sh != 0:
        print("Validation Failed: make_output_folders.sh failed.", file=sys.stderr)
        return False, "make_output_folders.sh failed", stderr_sh
    print("Step 1 successful.")

    # --- Step 2: Execute the main attack script ---
    print("\nStep 2: Executing main attack script (main.py)...")
    validation_command = [
        python_executable, "main.py", "-v", "14", "-t", "1",
        "--tr_lo", "0.65", "--tr_hi", "0.85", "-s", "score.py",
        "-n", "GTSRB", "--heatmap=Target", "--coarse_mode=binary",
        "-b", "100", "-m", "100"
    ]
    stdout_py, stderr_py, returncode_py = run_command(validation_command)

    print("\n--- Captured output from main.py ---")
    print(f"STDOUT:\n---\n{stdout_py}\n---")
    if stderr_py:
        print(f"STDERR:\n---\n{stderr_py}\n---")
    print("--- End of captured output ---\n")

    if returncode_py != 0:
        print("Validation Failed: main.py returned a non-zero exit code.", file=sys.stderr)
        return False, "main.py returned non-zero exit code", stderr_py
    
    # --- Step 3: Parse metrics from the output ---
    print("Step 3: Parsing performance metrics...")
    try:
        tr_score = re.search(r"Final transform_robustness:\s*([\d\.]+)", stdout_py).group(1)
        nbits = re.search(r"Final number of pixels:\s*(\d+)", stdout_py).group(1)
        queries = re.search(r"Final number of queries:\s*(\d+)", stdout_py).group(1)
        metrics_body = (
            "GRAPHITE Performance Metrics:\n"
            f"- Transform Robustness: {tr_score}\n"
            f"- Pixel Count: {nbits}\n"
            f"- Query Count: {queries}"
        )
        print("Metrics parsed successfully.")
        return True, metrics_body, stdout_py + stderr_py
    except (AttributeError, IndexError):
        print("Validation PASSED, but metrics could not be parsed from output.")
        return True, "Metrics not available for this run.", stdout_py + stderr_py

def _run_pytest_suite(python_executable: str, config: dict) -> tuple[bool, str, str]:
    """Placeholder for pytest suite, not used by this project."""
    print("\n--- Pytest Suite Skipped (Not configured for this project) ---")
    return True, "Pytest not configured.", ""

def validate_changes(python_executable: str, config: dict, group_title: str="Running Validation") -> tuple[bool, str, str]:
    """
    The main validation dispatcher. Reads the VALIDATION_CONFIG and
    orchestrates the correct validation strategy for the project.
    """
    start_group(group_title)
    
    validation_config = config.get("VALIDATION_CONFIG", {})
    validation_type = validation_config.get("type") # No default
    
    success = False
    metrics_body = "No validation performed."
    full_output = ""

    if validation_type == "script":
        success, metrics_body, full_output = _run_smoke_test(python_executable, config)
    elif validation_type == "pytest":
        success, metrics_body, full_output = _run_pytest_suite(python_executable, config)
    elif validation_type == "smoke_test_with_pytest_report":
        # This project type doesn't apply, but we handle it gracefully.
        print("WARNING: 'smoke_test_with_pytest_report' is not the ideal validation type for this project. Running smoke test only.")
        success, metrics_body, full_output = _run_smoke_test(python_executable, config)
    else:
        print(f"ERROR: Unknown or undefined validation type in AGENT_CONFIG: '{validation_type}'. Please set it to 'script'.", file=sys.stderr)
        success, metrics_body, full_output = False, f"Unknown validation type: {validation_type}", ""

    end_group()
    return success, metrics_body, full_output