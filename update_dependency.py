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
from packaging.version import parse as parse_version

# --- Configuration ---
REQUIREMENTS_FILE = "requirements.txt"
PRIMARY_REQUIREMENTS_FILE = "primary_requirements.txt" 
METRICS_OUTPUT_FILE = "metrics_output.txt"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MAX_BACKTRACK_ATTEMPTS = 3 # How many older versions LLM should suggest
MAX_RUN_PASSES = 3 # How many full loops to attempt to solve inter-dependencies

if not GEMINI_API_KEY:
    sys.exit("Error: GEMINI_API_KEY environment variable not set.")

genai.configure(api_key=GEMINI_API_KEY)
llm = genai.GenerativeModel('gemini-1.5-flash')

# --- Helper Functions ---
def start_group(title):
    print(f"\n::group::{title}")

def end_group():
    print("::endgroup::")

def run_command(command, cwd=None, python_executable=None):
    full_command = command
    if python_executable and command[0].startswith('python'):
        full_command = [python_executable] + command[1:]
    display_command = ' '.join(full_command)
    if len(display_command) > 200:
        display_command = display_command[:200] + "..."
    print(f"Running command: {display_command}")
    result = subprocess.run(full_command, capture_output=True, text=True, cwd=cwd)
    return result.stdout, result.stderr, result.returncode

def validate_changes(python_executable, group_title="Running Validation Script (main.py)"):
    start_group(group_title)
    _, stderr_sh, returncode_sh = run_command(["bash", "./make_output_folders.sh"])
    if returncode_sh != 0:
        print(f"Validation Failed: make_output_folders.sh failed.", file=sys.stderr); end_group()
        return False, None, stderr_sh
    
    validation_command = ["python3", "main.py", "-v", "14", "-t", "1", "--tr_lo", "0.65", "--tr_hi", "0.85", "-s", "score.py", "-n", "GTSRB", "--heatmap=Target", "--coarse_mode=binary", "-b", "100", "-m", "100"]
    stdout_py, stderr_py, returncode_py = run_command(validation_command, python_executable=python_executable)
    print("\n--- Captured output ---\nSTDOUT:\n---\n" + stdout_py + "\n---")
    if stderr_py: print("STDERR:\n---\n" + stderr_py + "\n---")
    
    if returncode_py != 0:
        print("Validation Failed: main.py returned a non-zero exit code.", file=sys.stderr); end_group()
        return False, None, stderr_py
    
    end_group()
    try:
        tr_score = re.search(r"Final transform_robustness:\s*([\d\.]+)", stdout_py).group(1)
        nbits = re.search(r"Final number of pixels:\s*(\d+)", stdout_py).group(1)
        queries = re.search(r"Final number of queries:\s*(\d+)", stdout_py).group(1)
        metrics_body = (f"Performance Metrics:\n- Transform Robustness: {tr_score}\n- Pixel Count: {nbits}\n- Query Count: {queries}")
        return True, metrics_body, stdout_py + stderr_py
    except (AttributeError, IndexError):
        return True, "Metrics not available for this run.", stdout_py + stderr_py

# --- The Agent's Logic ---
class DependencyAgent:
    def __init__(self):
        self.pypi = PyPISimple()
        self.requirements_path = Path(REQUIREMENTS_FILE)
        self.primary_packages = self._load_primary_packages()
        self.llm_available = True

    def _get_package_name_from_spec(self, spec_line):
        match = re.match(r'([a-zA-Z0-9\-_]+)', spec_line)
        return match.group(1) if match else None

    def _load_primary_packages(self):
        primary_path = Path(PRIMARY_REQUIREMENTS_FILE)
        if not primary_path.exists(): return set()
        with open(primary_path, "r") as f:
            return {self._get_package_name_from_spec(line.strip()) for line in f if line.strip() and not line.startswith('#')}

    def _get_requirements_state(self):
        if not self.requirements_path.exists(): sys.exit(f"Error: {REQUIREMENTS_FILE} not found.")
        with open(self.requirements_path, "r") as f:
            lines = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        return all('==' in line for line in lines), lines

    def _bootstrap_unpinned_requirements(self):
        print("Unpinned requirements detected. Creating a stable baseline...")
        venv_dir = Path("./temp_venv");
        if venv_dir.exists(): shutil.rmtree(venv_dir)
        venv.create(venv_dir, with_pip=True)
        python_executable = str(venv_dir / "bin" / "python")
        _, stderr, returncode = run_command([python_executable, "-m", "pip", "install", "-r", str(self.requirements_path)])
        if returncode != 0:
            sys.exit("CRITICAL ERROR: Failed to install initial set of dependencies.")
        success, metrics, _ = validate_changes(python_executable, group_title="Running Validation on New Baseline")
        if not success:
            sys.exit("CRITICAL ERROR: Initial dependencies failed validation.")
        if metrics and "not available" not in metrics:
            print(f"\n{'='*70}\n=== BOOTSTRAP SUCCESSFUL: METRICS FOR THE NEW BASELINE ENVIRONMENT ===\n" + "\n".join([f"  {line}" for line in metrics.split('\n')]) + f"\n{'='*70}\n")
        installed_packages, _, _ = run_command([python_executable, "-m", "pip", "freeze"])
        with open(self.requirements_path, "w") as f: f.write(installed_packages)
        if metrics:
            with open(METRICS_OUTPUT_FILE, "w") as f: f.write(metrics)

    def run(self):
        if os.path.exists(METRICS_OUTPUT_FILE): os.remove(METRICS_OUTPUT_FILE)
        is_pinned, _ = self._get_requirements_state()
        if not is_pinned:
            self._bootstrap_unpinned_requirements()
            return

        final_successful_updates = {}
        final_failed_updates = {}
        for pass_num in range(1, MAX_RUN_PASSES + 1):
            print(f"\n{'#'*30} STARTING UPDATE PASS {pass_num}/{MAX_RUN_PASSES} {'#'*30}\n")
            
            _, lines = self._get_requirements_state()
            original_requirements = {self._get_package_name_from_spec(line): line for line in lines}
            packages_to_update = []
            for package, spec in original_requirements.items():
                if '==' not in spec: continue
                current_version = spec.split('==')[1]
                latest_version = self.get_latest_version(package)
                if latest_version and parse_version(latest_version) > parse_version(current_version):
                    packages_to_update.append((package, latest_version))
            
            if not packages_to_update:
                if pass_num == 1: print("\nAll dependencies are up-to-date.")
                else: print("\nNo further updates possible. System has converged to a stable state.")
                break

            updates_made_this_pass = False
            for package, version in packages_to_update:
                is_primary = self._get_package_name_from_spec(package) in self.primary_packages
                success, reason = self.attempt_update_with_healing(package, version, is_primary)
                if success:
                    updates_made_this_pass = True
                    final_successful_updates[package] = reason
                    if package in final_failed_updates: del final_failed_updates[package]
                else:
                    final_failed_updates[package] = reason
            
            if not updates_made_this_pass:
                print("\nNo successful updates in this pass. System is stable.")
                break

        if final_successful_updates or final_failed_updates:
            print("\n" + "#"*70); print("### OVERALL UPDATE RUN SUMMARY ###")
            if final_successful_updates:
                print("\n[SUCCESS] The following packages were successfully updated:")
                for pkg, reason in final_successful_updates.items(): print(f"- {pkg}: {reason}")
            if final_failed_updates:
                print("\n[FAILURE] The following packages had updates available but could not be installed:")
                for pkg, reason in final_failed_updates.items(): print(f"- {pkg}: {reason}")
            print("#"*70 + "\n")

        if final_successful_updates:
            print("\n" + "#"*70); print("### FINAL SYSTEM HEALTH CHECK ON COMBINED UPDATES ###")
            print("This step validates the combination of all successful updates from this run."); print("#"*70 + "\n")
            venv_dir = Path("./final_venv")
            if venv_dir.exists(): shutil.rmtree(venv_dir)
            venv.create(venv_dir, with_pip=True)
            python_executable = str(venv_dir / "bin" / "python")
            _, stderr, returncode = run_command([python_executable, "-m", "pip", "install", "-r", str(self.requirements_path)])
            if returncode != 0: print("CRITICAL ERROR: Final installation of combined dependencies failed!", file=sys.stderr); return
            success, metrics, _ = validate_changes(python_executable, group_title="Final System Health Check")
            if success and metrics and "not available" not in metrics:
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
            if not (package_info and package_info.packages): return None
            stable_versions = [p.version for p in package_info.packages if p.version and not parse_version(p.version).is_prerelease]
            if stable_versions: return max(stable_versions, key=parse_version)
            all_versions = [p.version for p in package_info.packages if p.version]
            return max(all_versions, key=parse_version) if all_versions else None
        except Exception: return None

    def _try_install_and_validate(self, package_to_update, new_version):
        venv_dir = Path("./temp_venv")
        if venv_dir.exists(): shutil.rmtree(venv_dir)
        venv.create(venv_dir, with_pip=True)
        python_executable = str(venv_dir / "bin" / "python")
        with open(self.requirements_path, "r") as f:
            lines = [line.strip() for line in f if line.strip()]
        
        requirements_list = [f"{package_to_update}=={new_version}" if self._get_package_name_from_spec(l) == package_to_update else l for l in lines]
        
        _, stderr, returncode = run_command([python_executable, "-m", "pip", "install"] + requirements_list)
        if returncode != 0:
            if not self.llm_available: return False, "Installation conflict and LLM unavailable", stderr
            solution = self.resolve_conflict_with_llm(stderr, requirements_list)
            if solution:
                _, stderr, returncode = run_command([python_executable, "-m", "pip", "install"] + solution)
                if returncode != 0: return False, "LLM solution failed to install", stderr
            else: return False, "LLM could not find a solution", stderr
        
        success, metrics, validation_output = validate_changes(python_executable)
        if not success: return False, "Validation script failed", validation_output
        return True, metrics, ""

    def attempt_update_with_healing(self, package, target_version, is_primary):
        package_label = "(Primary)" if is_primary else "(Transient)"
        print(f"\n{'='*20} Attempting to update {package} {package_label} to {target_version} {'='*20}")
        
        success, result_data, stderr = self._try_install_and_validate(package, target_version)
        
        if success:
            metrics = result_data
            if metrics and "not available" not in metrics:
                print(f"\n** SUCCESS: {package} {package_label} updated to {target_version} and passed validation. **")
                print("\n".join([f"  {line}" for line in metrics.split('\n')]) + "\n")
                with open(METRICS_OUTPUT_FILE, "w") as f: f.write(metrics)
            else:
                print(f"\n** SUCCESS: {package} {package_label} updated to {target_version} and passed validation (metrics unavailable). **\n")

            installed_packages, _, _ = run_command([Path("./temp_venv/bin/python"), "-m", "pip", "freeze"])
            with open(self.requirements_path, "w") as f: f.write(installed_packages)
            return True, f"Updated to {target_version}"

        # --- ENTER HEALING MODE ---
        print(f"INFO: Initial update for {package} to {target_version} failed. Reason: {result_data}. Entering healing mode.")
        
        # 1. LLM-Guided Backtracking
        version_candidates = self._ask_llm_for_version_candidates(package, target_version, stderr)
        if version_candidates:
            for candidate in version_candidates:
                print(f"INFO: Attempting LLM-suggested backtrack version for {package}: {candidate}")
                success, result_data, _ = self._try_install_and_validate(package, candidate)
                if success:
                    print(f"INFO: LLM-suggested version {candidate} for {package} passed validation!")
                    # Handle success same as above...
                    return True, f"Success after backtracking to LLM suggestion {candidate}"

        # 2. Binary Search would go here as a final fallback.
        print(f"INFO: LLM suggestions failed for {package}. A more robust solution would implement Binary Search here.")
        
        return False, f"All backtracking attempts for {package} failed."

    def resolve_conflict_with_llm(self, error_message, requirements_list):
        py_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        original_packages = {self._get_package_name_from_spec(req) for req in requirements_list}
        prompt = f"""I am an automated script trying to resolve a Python dependency conflict for a project running on Python {py_version}. My attempt to install failed. The error was:
---
{error_message}
---
The packages I tried to install are: {requirements_list}.
Provide a new, corrected list of packages that resolves this conflict. The new list MUST include all original packages. Your response format must be ONLY a Python list of strings. Example: ['numpy<2.0', 'pandas==2.2.0']"""
        try:
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
            print("\n!!! WARNING: LLM daily quota exceeded. The agent will continue without conflict resolution. !!!\n"); self.llm_available = False; return None
        except Exception as e:
            print(f"Error parsing LLM response or communicating with API: {e}", file=sys.stderr); return None

    def _ask_llm_for_version_candidates(self, package, failed_version, error_message):
        if not self.llm_available: return []
        py_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        prompt = f"""I am trying to find a working version of the package '{package}'. Version '{failed_version}' failed to validate on Python {py_version} with the error:
---
{error_message}
---
Based on this error, give me a Python list of the {MAX_BACKTRACK_ATTEMPTS} most recent, previous versions that are most likely to be stable and compatible, in descending order. Your response MUST be ONLY a Python list of strings. Example: ['4.8.1.78', '4.8.0.76']"""
        try:
            response = llm.generate_content(prompt)
            response_text = response.text.strip()
            match = re.search(r'(\[.*?\])', response_text, re.DOTALL)
            if not match: return []
            list_string = match.group(1)
            solution_list = ast.literal_eval(list_string)
            if not isinstance(solution_list, list): return []
            return solution_list
        except Exception:
            return []

if __name__ == "__main__":
    agent = DependencyAgent()
    agent.run()