import os
import subprocess
import sys
import venv
from pathlib import Path
import ast
import shutil
import re

# This is the line that was missing
import google.generativeai as genai

from pypi_simple import PyPISimple

# --- Configuration ---
REQUIREMENTS_FILE = "requirements.txt"
METRICS_OUTPUT_FILE = "metrics_output.txt"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    print("Error: GEMINI_API_KEY environment variable not set.", file=sys.stderr)
    sys.exit(1)

genai.configure(api_key=GEMINI_API_KEY)
llm = genai.GenerativeModel('gemini-1.5-flash')

# --- Helper Functions ---
def run_command(command, cwd=None, python_executable=None):
    """Runs a command and returns the output, error, and return code."""
    full_command = command
    if python_executable and command[0].startswith('python'):
        full_command = [python_executable] + command[1:]
    
    print(f"Running command: {' '.join(full_command)}")
    result = subprocess.run(full_command, capture_output=True, text=True, cwd=cwd)
    return result.stdout, result.stderr, result.returncode

def validate_changes(python_executable):
    """
    Runs the validation process and now CAPTURES performance metrics from stdout.
    Returns a tuple: (bool: success, str: formatted_metrics or None)
    """
    print("\n--- Running Validation Step 1: Creating output folders ---")
    _, stderr_sh, returncode_sh = run_command(["bash", "./make_output_folders.sh"])
    if returncode_sh != 0:
        print("Validation Failed: The 'make_output_folders.sh' script failed.", file=sys.stderr)
        print("Error:", stderr_sh, file=sys.stderr)
        return False, None
    print("Validation Step 1 successful.")

    print("\n--- Running Validation Step 2: Executing main attack script ---")
    validation_command = [
        "python3", "main.py", "-v", "14", "-t", "1",
        "--tr_lo", "0.65", "--tr_hi", "0.85", "-s", "score.py",
        "-n", "GTSRB", "--heatmap=Target", "--coarse_mode=binary",
        "-b", "100", "-m", "100"
    ]
    
    stdout_py, stderr_py, returncode_py = run_command(validation_command, python_executable=python_executable)
    if returncode_py != 0:
        print("Validation Failed: The main.py script returned a non-zero exit code.", file=sys.stderr)
        print("Error:", stderr_py, file=sys.stderr)
        return False, None
    
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
        print("Successfully parsed metrics from validation output.")
        return True, metrics_body
    except (AttributeError, IndexError) as e:
        print("Warning: Validation script ran successfully, but failed to parse metrics from output.", file=sys.stderr)
        print(f"Parser error: {e}", file=sys.stderr)
        return True, "Metrics parsing failed."

# --- The Agent's Logic ---
class DependencyAgent:
    def __init__(self):
        self.pypi = PyPISimple()
        self.requirements_path = Path(REQUIREMENTS_FILE)

    def _get_requirements_state(self):
        """Checks if requirements are pinned and returns the lines."""
        if not self.requirements_path.exists():
            print(f"Error: {REQUIREMENTS_FILE} not found.", file=sys.stderr); sys.exit(1)
        with open(self.requirements_path, "r") as f:
            lines = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        is_pinned = all('==' in line for line in lines)
        return is_pinned, lines

    def _bootstrap_unpinned_requirements(self):
        """Creates a stable, pinned baseline from an unpinned requirements file."""
        print("Unpinned requirements detected. Attempting to create a stable baseline...")
        venv_dir = Path("./temp_venv");
        if venv_dir.exists(): shutil.rmtree(venv_dir)
        venv.create(venv_dir, with_pip=True)
        python_executable = str(venv_dir / "bin" / "python")
        print("Installing from unpinned requirements file to get latest compatible versions...")
        _, stderr, returncode = run_command([python_executable, "-m", "pip", "install", "-r", str(self.requirements_path)])
        if returncode != 0:
            print("CRITICAL ERROR: Failed to install initial set of dependencies.", file=sys.stderr); sys.exit(1)
        
        print("Initial installation successful. Validating the baseline...")
        success, metrics = validate_changes(python_executable)
        if not success:
            print("CRITICAL ERROR: The latest compatible dependencies failed validation.", file=sys.stderr); sys.exit(1)

        print("Validation successful! Freezing the working dependencies to requirements.txt.")
        installed_packages, _, _ = run_command([python_executable, "-m", "pip", "freeze"])
        with open(self.requirements_path, "w") as f: f.write(installed_packages)
        with open(METRICS_OUTPUT_FILE, "w") as f: f.write(metrics)
        print("Bootstrap complete. A stable, pinned requirements.txt has been created.")

    def run(self):
        """Main execution loop for the agent."""
        if os.path.exists(METRICS_OUTPUT_FILE): os.remove(METRICS_OUTPUT_FILE)
        is_pinned, lines = self._get_requirements_state()
        if not is_pinned:
            self._bootstrap_unpinned_requirements()
            return

        original_requirements = {line.split('==')[0]: line.split('==')[1] for line in lines}
        packages_to_update = []
        for package, current_version in original_requirements.items():
            latest_version = self.get_latest_version(package)
            if latest_version and latest_version != current_version:
                packages_to_update.append((package, latest_version))
        
        if not packages_to_update:
            print("\nAll dependencies are up-to-date.")
            return

        for package, version in packages_to_update:
            self.attempt_update(package, version)
            
    def get_latest_version(self, package_name):
        """Gets the latest version of a package from PyPI."""
        try:
            package_info = self.pypi.get_project_page(package_name)
            if package_info and package_info.packages:
                versions = sorted([p.version for p in package_info.packages if p.version], reverse=True)
                return versions[0] if versions else None
        except Exception: return None
        
    def attempt_update(self, package_to_update, new_version):
        """Attempts to update a single package, resolve conflicts, and validate."""
        print(f"\n{'='*20} Attempting to update {package_to_update} to {new_version} {'='*20}")
        venv_dir = Path("./temp_venv")
        if venv_dir.exists(): shutil.rmtree(venv_dir)
        venv.create(venv_dir, with_pip=True)
        python_executable = str(venv_dir / "bin" / "python")
        with open(self.requirements_path, "r") as f:
             lines = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        updated_reqs = {line.split('==')[0]: line.split('==')[1] for line in lines}
        updated_reqs[package_to_update] = new_version
        requirements_list = [f"{p}=={v}" for p, v in updated_reqs.items()]
        
        _, stderr, returncode = run_command([python_executable, "-m", "pip", "install"] + requirements_list)
        if returncode != 0:
            solution = self.resolve_conflict_with_llm(stderr, requirements_list)
            if solution:
                _, stderr, returncode = run_command([python_executable, "-m", "pip", "install"] + solution)
                if returncode != 0:
                    print("LLM's solution failed. Skipping this update.", file=sys.stderr); return
            else:
                print("LLM could not find a solution. Skipping this update.", file=sys.stderr); return
        
        print("Installation successful. Proceeding to validation...")
        success, metrics = validate_changes(python_executable)
        if not success:
            print(f"Validation failed for the update of {package_to_update}. Reverting.", file=sys.stderr)
            return

        print("\n" + "*"*60)
        print(f"** SUCCESS: {package_to_update} was updated to {new_version} and passed validation. **")
        indented_metrics = "\n".join([f"  {line}" for line in metrics.split('\n')])
        print(indented_metrics)
        print("*"*60 + "\n")

        print(f"Freezing new state to {self.requirements_path}...")
        installed_packages, _, _ = run_command([python_executable, "-m", "pip", "freeze"])
        with open(self.requirements_path, "w") as f: f.write(installed_packages)
        with open(METRICS_OUTPUT_FILE, "w") as f: f.write(metrics)

    def resolve_conflict_with_llm(self, error_message, requirements_list):
        py_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        original_packages = {req.split('==')[0] for req in requirements_list}
        prompt = f"""
        I am an automated script trying to resolve a Python dependency conflict for a project running on Python {py_version}. My attempt to install the following packages failed:
        {requirements_list}

        The exact error message from pip was:
        ---
        {error_message}
        ---
        Your task is to provide a new, corrected list of packages that resolves this conflict.
        Constraints:
        1. The new list MUST be compatible with Python {py_version}.
        2. The new list MUST include every package from the original list. Do not omit any.
        3. Your response MUST be ONLY a Python list of strings in the format 'package_name==version'.
        Example response: ['numpy==1.26.4', 'pandas==2.2.0', 'scipy==1.11.4']
        """
        try:
            print(f"Sending prompt to LLM for conflict resolution (context: Python {py_version})...")
            response = llm.generate_content(prompt)
            response_text = response.text.strip()
            
            # Use regex to find the list within the text, making it robust to extra text from the LLM.
            match = re.search(r'(\[.*?\])', response_text, re.DOTALL)
            if not match:
                print(f"LLM Error: Could not find a list in the response text.", file=sys.stderr)
                return None

            list_string = match.group(1)
            solution_list = ast.literal_eval(list_string)

            if not isinstance(solution_list, list):
                print(f"LLM Error: Parsed structure was not a list.", file=sys.stderr)
                return None
            
            # Verify the solution contains the same packages
            solution_packages = {req.split('==')[0] for req in solution_list}
            if original_packages != solution_packages:
                print(f"LLM Error: Solution did not contain the correct set of packages.", file=sys.stderr)
                return None
            
            print("LLM provided a valid and verified solution.")
            return solution_list
        except Exception as e:
            print(f"Error parsing LLM response or communicating with API: {e}", file=sys.stderr)
            return None

if __name__ == "__main__":
    agent = DependencyAgent()
    agent.run()