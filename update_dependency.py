import os
import subprocess
import sys
import venv
from pathlib import Path
import ast
import shutil
import re
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted
from pypi_simple import PyPISimple

# --- Configuration ---
REQUIREMENTS_FILE = "requirements.txt"
PRIMARY_REQUIREMENTS_FILE = "primary_requirements.txt" 
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
    Runs the validation process and captures performance metrics if they exist.
    Success is determined by exit code, not by presence of metrics.
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
    except (AttributeError, IndexError):
        print("Warning: Validation script ran successfully, but metrics were not found in the output.", file=sys.stderr)
        return True, "Metrics not available for this run."

# --- The Agent's Logic ---
class DependencyAgent:
    def __init__(self):
        self.pypi = PyPISimple()
        self.requirements_path = Path(REQUIREMENTS_FILE)
        self.primary_packages = self._load_primary_packages()
        self.llm_available = True

    def _load_primary_packages(self):
        primary_path = Path(PRIMARY_REQUIREMENTS_FILE)
        if not primary_path.exists():
            return set()
        with open(primary_path, "r") as f:
            return {self._get_package_name_from_spec(line.strip()) for line in f if line.strip() and not line.startswith('#')}

    def _get_package_name_from_spec(self, spec_line):
        match = re.match(r'([a-zA-Z0-9\-_]+)', spec_line)
        return match.group(1) if match else None

    def _get_requirements_state(self):
        if not self.requirements_path.exists():
            sys.exit(f"Error: {REQUIREMENTS_FILE} not found.")
        with open(self.requirements_path, "r") as f:
            lines = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        is_pinned = all('==' in line for line in lines)
        return is_pinned, lines

    def _bootstrap_unpinned_requirements(self):
        print("Unpinned requirements detected. Creating a stable baseline...")
        venv_dir = Path("./temp_venv")
        if venv_dir.exists(): shutil.rmtree(venv_dir)
        venv.create(venv_dir, with_pip=True)
        python_executable = str(venv_dir / "bin" / "python")
        _, stderr, returncode = run_command([python_executable, "-m", "pip", "install", "-r", str(self.requirements_path)])
        if returncode != 0:
            sys.exit("CRITICAL ERROR: Failed to install initial set of dependencies.")
        success, metrics = validate_changes(python_executable)
        if not success:
            sys.exit("CRITICAL ERROR: Initial dependencies failed validation.")
        installed_packages, _, _ = run_command([python_executable, "-m", "pip", "freeze"])
        with open(self.requirements_path, "w") as f:
            f.write(installed_packages)
        if metrics:
            with open(METRICS_OUTPUT_FILE, "w") as f:
                f.write(metrics)

    def run(self):
        if os.path.exists(METRICS_OUTPUT_FILE): os.remove(METRICS_OUTPUT_FILE)
        is_pinned, lines = self._get_requirements_state()
        if not is_pinned:
            self._bootstrap_unpinned_requirements()
            return

        original_requirements = {self._get_package_name_from_spec(line): line for line in lines}
        packages_to_update = []
        for package, spec in original_requirements.items():
            if '==' not in spec: continue
            current_version = spec.split('==')[1]
            latest_version = self.get_latest_version(package)
            if latest_version and latest_version != current_version:
                packages_to_update.append((package, latest_version))
        
        if not packages_to_update:
            print("\nAll dependencies are up-to-date.")
            return

        successful_updates = []
        failed_updates = []
        for package, version in packages_to_update:
            is_primary = package in self.primary_packages
            if self.attempt_update(package, version, is_primary):
                successful_updates.append(f"{package}=={version}")
            else:
                failed_updates.append(f"{package} (target: {version})")
        
        if successful_updates or failed_updates:
            print("\n" + "#"*70); print("### UPDATE RUN SUMMARY ###")
            if successful_updates:
                print("\n[SUCCESS] The following packages were successfully updated:")
                for pkg in successful_updates: print(f"- {pkg}")
            if failed_updates:
                print("\n[FAILURE] The following packages had updates available but FAILED:")
                for pkg in failed_updates: print(f"- {pkg}")
            print("#"*70 + "\n")

        if successful_updates:
            print("\n" + "#"*70); print("### FINAL SYSTEM HEALTH CHECK ON COMBINED UPDATES ###")
            print("This step validates the combination of all successful updates from this run."); print("#"*70 + "\n")
            venv_dir = Path("./final_venv")
            if venv_dir.exists(): shutil.rmtree(venv_dir)
            venv.create(venv_dir, with_pip=True)
            python_executable = str(venv_dir / "bin" / "python")
            _, stderr, returncode = run_command([python_executable, "-m", "pip", "install", "-r", str(self.requirements_path)])
            if returncode != 0:
                print("CRITICAL ERROR: Final installation of combined dependencies failed!", file=sys.stderr); return
            success, metrics = validate_changes(python_executable)
            if success and metrics:
                print("\n" + "="*70); print("=== FINAL METRICS FOR THE FULLY UPDATED ENVIRONMENT ===")
                indented_metrics = "\n".join([f"  {line}" for line in metrics.split('\n')])
                print(indented_metrics); print("="*70)
            elif success:
                print("\n" + "="*70); print("=== Final validation passed, but metrics were not available in output. ==="); print("="*70)
            else:
                print("\n" + "!"*70); print("!!! CRITICAL ERROR: Final validation of combined dependencies failed! !!!"); print("!"*70)

    def get_latest_version(self, package_name):
        try:
            package_info = self.pypi.get_project_page(package_name)
            if package_info and package_info.packages:
                versions = sorted([p.version for p in package_info.packages if p.version], reverse=True)
                return versions[0] if versions else None
        except Exception: return None
        
    def attempt_update(self, package_to_update, new_version, is_primary):
        package_label = "(Primary)" if is_primary else "(Transient)"
        print(f"\n{'='*20} Attempting to update {package_to_update} {package_label} to {new_version} {'='*20}")
        venv_dir = Path("./temp_venv")
        if venv_dir.exists(): shutil.rmtree(venv_dir)
        venv.create(venv_dir, with_pip=True)
        python_executable = str(venv_dir / "bin" / "python")
        with open(self.requirements_path, "r") as f:
             lines = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        
        requirements_list = []
        for line in lines:
            pkg_name = self._get_package_name_from_spec(line)
            if pkg_name == package_to_update:
                requirements_list.append(f"{package_to_update}=={new_version}")
            else:
                requirements_list.append(line)
        
        _, stderr, returncode = run_command([python_executable, "-m", "pip", "install"] + requirements_list)
        if returncode != 0:
            if self.llm_available:
                solution = self.resolve_conflict_with_llm(stderr, requirements_list)
                if solution:
                    _, stderr, returncode = run_command([python_executable, "-m", "pip", "install"] + solution)
                    if returncode != 0: return False
                else: return False
            else:
                print("Initial install failed and LLM is unavailable due to quota. Skipping.", file=sys.stderr)
                return False
        
        success, metrics = validate_changes(python_executable)
        if not success: return False

        if metrics:
            print(f"\n** SUCCESS: {package_to_update} {package_label} was updated to {new_version} and passed validation. **")
            indented_metrics = "\n".join([f"  {line}" for line in metrics.split('\n')])
            print(indented_metrics + "\n")
            with open(METRICS_OUTPUT_FILE, "w") as f: f.write(metrics)
        else:
            print(f"\n** SUCCESS: {package_to_update} {package_label} updated and passed validation, but metrics were not in output. **\n")

        installed_packages, _, _ = run_command([python_executable, "-m", "pip", "freeze"])
        with open(self.requirements_path, "w") as f: f.write(installed_packages)
        return True

    def resolve_conflict_with_llm(self, error_message, requirements_list):
        py_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        original_packages = {self._get_package_name_from_spec(req) for req in requirements_list}
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
        2. The new list MUST include every package from the original list.
        3. Your response MUST be ONLY a Python list of strings in the format 'package_name==version'.
        Example response: ['numpy<2.0', 'pandas==2.2.0', 'scipy==1.11.4']
        """
        try:
            print(f"Sending prompt to LLM for conflict resolution (context: Python {py_version})...")
            response = llm.generate_content(prompt)
            response_text = response.text.strip()
            match = re.search(r'(\[.*?\])', response_text, re.DOTALL)
            if not match: return None
            list_string = match.group(1)
            solution_list = ast.literal_eval(list_string)
            if not isinstance(solution_list, list): return None
            solution_packages = {self._get_package_name_from_spec(req) for req in solution_list}
            if original_packages != solution_packages: return None
            return solution_list
        except ResourceExhausted:
            print("\n!!! WARNING: LLM daily quota has been exceeded. The agent will continue without conflict resolution. !!!\n")
            self.llm_available = False
            return None
        except Exception as e:
            print(f"Error parsing LLM response or communicating with API: {e}", file=sys.stderr)
            return None

if __name__ == "__main__":
    agent = DependencyAgent()
    agent.run()