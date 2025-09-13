import os
import subprocess
import sys
import venv
from pathlib import Path
import ast
import shutil

import google.generativeai as genai
from pypi_simple import PyPISimple

# --- Configuration ---
REQUIREMENTS_FILE = "requirements.txt"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    print("Error: GEMINI_API_KEY environment variable not set.", file=sys.stderr)
    sys.exit(1)

genai.configure(api_key=GEMINI_API_KEY)
llm = genai.GenerativeModel('gemini-1.5-flash')

# (Helper functions 'run_command' and 'validate_changes' remain the same as the previous version)
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
    """Runs the specific two-step validation process for the GRAPHITE project."""
    print("\n--- Running Validation Step 1: Creating output folders ---")
    _, stderr_sh, returncode_sh = run_command(["bash", "./make_output_folders.sh"])
    if returncode_sh != 0:
        print("Validation Failed: The 'make_output_folders.sh' script failed.", file=sys.stderr)
        print("Error:", stderr_sh, file=sys.stderr)
        return False
    print("Validation Step 1 successful.")

    print("\n--- Running Validation Step 2: Executing main attack script ---")
    validation_command = [
        "python3", "main.py", "-v", "14", "-t", "1",
        "--tr_lo", "0.65", "--tr_hi", "0.85", "-s", "score.py",
        "-n", "GTSRB", "--heatmap=Target", "--coarse_mode=binary",
        "-b", "100", "-m", "100"
    ]
    
    _, stderr_py, returncode_py = run_command(validation_command, python_executable=python_executable)
    if returncode_py != 0:
        print("Validation Failed: The main.py script returned a non-zero exit code.", file=sys.stderr)
        print("Error:", stderr_py, file=sys.stderr)
        return False
    
    print("Validation Step 2 successful. All checks passed!")
    return True

# --- The Agent's Logic ---
class DependencyAgent:
    def __init__(self):
        self.pypi = PyPISimple()
        self.requirements_path = Path(REQUIREMENTS_FILE)

    def _get_requirements_state(self):
        """Checks if requirements are pinned and returns the lines."""
        if not self.requirements_path.exists():
            print(f"Error: {REQUIREMENTS_FILE} not found.", file=sys.stderr)
            sys.exit(1)
        
        with open(self.requirements_path, "r") as f:
            lines = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        
        is_pinned = all('==' in line for line in lines)
        return is_pinned, lines

    def _bootstrap_unpinned_requirements(self):
        """Creates a stable, pinned baseline from an unpinned requirements file."""
        print("Unpinned requirements detected. Attempting to create a stable baseline...")
        
        venv_dir = Path("./temp_venv")
        if venv_dir.exists(): shutil.rmtree(venv_dir)
        
        venv.create(venv_dir, with_pip=True)
        python_executable = str(venv_dir / "bin" / "python")

        print("Installing from unpinned requirements file to get latest compatible versions...")
        _, stderr, returncode = run_command([python_executable, "-m", "pip", "install", "-r", str(self.requirements_path)])

        if returncode != 0:
            print("CRITICAL ERROR: Failed to install initial set of dependencies.", file=sys.stderr)
            print("This could be due to a conflict in the latest package versions. Manual intervention is required.", file=sys.stderr)
            print("Error:", stderr, file=sys.stderr)
            sys.exit(1)
        
        print("Initial installation successful. Validating the baseline...")
        if not validate_changes(python_executable):
            print("CRITICAL ERROR: The latest compatible dependencies failed validation.", file=sys.stderr)
            print("Your project is not compatible with the newest package versions. Manual intervention is required.", file=sys.stderr)
            sys.exit(1)

        print("Validation successful! Freezing the working dependencies to requirements.txt.")
        installed_packages, _, _ = run_command([python_executable, "-m", "pip", "freeze"])
        with open(self.requirements_path, "w") as f:
            f.write(installed_packages)
        
        print("Bootstrap complete. A stable, pinned requirements.txt has been created and will be committed.")
        return

    def run(self):
        """Main execution loop for the agent."""
        is_pinned, lines = self._get_requirements_state()

        if not is_pinned:
            self._bootstrap_unpinned_requirements()
            # The agent's job is done for this run. The next run will handle updates.
            return

        # --- Normal Update Logic ---
        original_requirements = {line.split('==')[0]: line.split('==')[1] for line in lines}
        packages_to_update = []
        for package, current_version in original_requirements.items():
            print(f"Checking {package} (current: {current_version})...")
            latest_version = self.get_latest_version(package)
            if latest_version and latest_version != current_version:
                print(f"  -> Found new version for {package}: {latest_version}")
                packages_to_update.append((package, latest_version))
        
        if not packages_to_update:
            print("\nAll dependencies are up-to-date.")
            return

        for package, version in packages_to_update:
            self.attempt_update(package, version)

    # `attempt_update` and `resolve_conflict_with_llm` methods remain unchanged from the previous final version.
    def get_latest_version(self, package_name):
        """Gets the latest version of a package from PyPI."""
        try:
            package_info = self.pypi.get_project_page(package_name)
            if package_info and package_info.packages:
                versions = sorted([p.version for p in package_info.packages if p.version], reverse=True)
                return versions[0] if versions else None
        except Exception as e:
            print(f"Could not fetch package {package_name}: {e}", file=sys.stderr)
        return None
        
    def attempt_update(self, package_to_update, new_version):
        """Attempts to update a single package, resolve conflicts, and validate."""
        print(f"\n{'='*15} Attempting to update {package_to_update} to {new_version} {'='*15}")
        
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
            print("Conflict detected. Asking LLM for a solution...", file=sys.stderr)
            solution = self.resolve_conflict_with_llm(stderr, requirements_list)
            if solution:
                print("LLM proposed a solution. Retrying installation...")
                _, stderr, returncode = run_command([python_executable, "-m", "pip", "install"] + solution)
                if returncode != 0:
                    print("LLM's solution failed. Skipping this update.", file=sys.stderr)
                    print("Error:", stderr, file=sys.stderr)
                    return
            else:
                print("LLM could not find a solution. Skipping this update.", file=sys.stderr)
                return
        
        print("Installation successful. Proceeding to validation...")
        if not validate_changes(python_executable):
            print(f"Validation failed for the update of {package_to_update}. Reverting.", file=sys.stderr)
            return

        print("Validation successful! Updating requirements.txt.")
        installed_packages, _, _ = run_command([python_executable, "-m", "pip", "freeze"])
        with open(self.requirements_path, "w") as f:
            f.write(installed_packages)
        
        print(f"Successfully updated {package_to_update} and froze new requirements.\n")
    
    def resolve_conflict_with_llm(self, error_message, requirements_list):
        """Uses Gemini to find a set of compatible package versions."""
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
            if response_text.startswith("```python"):
                response_text = response_text.strip("```python\n").strip("```")

            solution_list = ast.literal_eval(response_text)

            if not isinstance(solution_list, list): return None
            
            solution_packages = {req.split('==') for req in solution_list}
            if original_packages != solution_packages: return None
            
            print("LLM provided a valid and verified solution.")
            return solution_list
        except Exception as e:
            print(f"Error parsing LLM response or communicating with API: {e}", file=sys.stderr)
            return None

if __name__ == "__main__":
    agent = DependencyAgent()
    agent.run()