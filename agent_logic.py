# agent_logic.py

import os
import sys
import venv
from pathlib import Path
import ast
import shutil
import re
import json
from google.api_core.exceptions import ResourceExhausted
from pypi_simple import PyPISimple
from packaging.version import parse as parse_version
from agent_utils import start_group, end_group, run_command, validate_changes

class DependencyAgent:
    def __init__(self, config, llm_client):
        self.config = config
        self.llm = llm_client
        self.pypi = PyPISimple()
        self.requirements_path = Path(config["REQUIREMENTS_FILE"])
        self.primary_packages = self._load_primary_packages()
        self.llm_available = True
        self.usage_scores = self._calculate_usage_scores()

    def _calculate_usage_scores(self):
        start_group("Analyzing Codebase for Import Usage")
        scores = {}
        repo_root = Path('.')
        print("Scanning all .py files for import statements...")
        for py_file in repo_root.rglob('*.py'):
            # Avoid scanning the agent's own venvs and code
            if any(part in str(py_file) for part in ['temp_venv', 'final_venv', 'bootstrap_venv', 'agent_logic.py', 'agent_utils.py', 'dependency_agent.py']):
                continue
            try:
                with open(py_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    tree = ast.parse(content)
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Import):
                            for alias in node.names:
                                module_name = alias.name.split('.')[0]
                                scores[module_name] = scores.get(module_name, 0) + 1
                        elif isinstance(node, ast.ImportFrom):
                            if node.module:
                                module_name = node.module.split('.')[0]
                                scores[module_name] = scores.get(module_name, 0) + 1
            except Exception:
                continue # Ignore files that cannot be parsed
        
        normalized_scores = {name.replace('_', '-'): score for name, score in scores.items()}
        print("Usage scores calculated.")
        end_group()
        return normalized_scores

    def _get_package_name_from_spec(self, spec_line):
        match = re.match(r'([a-zA-Z0-9\-_]+)', spec_line)
        return match.group(1) if match else None

    def _load_primary_packages(self):
        primary_path = Path(self.config["PRIMARY_REQUIREMENTS_FILE"])
        if not primary_path.exists(): return set()
        with open(primary_path, "r") as f:
            return {self._get_package_name_from_spec(line.strip()) for line in f if line.strip() and not line.startswith('#')}

    def _get_requirements_state(self):
        if not self.requirements_path.exists(): sys.exit(f"Error: {self.config['REQUIREMENTS_FILE']} not found.")
        with open(self.requirements_path, "r") as f:
            lines = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        return all('==' in line for line in lines), lines

    def _bootstrap_unpinned_requirements(self):
        print("Unpinned requirements detected. Attempting to create a stable baseline...")
        venv_dir = Path("./bootstrap_venv");
        if venv_dir.exists(): shutil.rmtree(venv_dir)
        venv.create(venv_dir, with_pip=True)
        python_executable = str(venv_dir / "bin" / "python")
        
        print("\nStep 1: Installing the latest compatible versions...")
        _, stderr, returncode = run_command([python_executable, "-m", "pip", "install", "-r", str(self.requirements_path)])
        if returncode != 0: sys.exit(f"CRITICAL ERROR: Failed to install initial set of dependencies. Error: {stderr}")
        
        print("\nStep 2: Validating the new baseline environment...")
        success, metrics, _ = validate_changes(python_executable, group_title="Running Validation on New Baseline")
        if not success: sys.exit("CRITICAL ERROR: Initial dependencies failed validation.")
        
        print("\nStep 3: Freezing the validated environment to requirements.txt...")
        installed_packages, stderr_freeze, returncode_freeze = run_command([python_executable, "-m", "pip", "freeze"])
        if returncode_freeze != 0: sys.exit(f"CRITICAL ERROR: Failed to 'pip freeze' the new environment. Error: {stderr_freeze}")
        
        with open(self.requirements_path, "w") as f: f.write(installed_packages)
        print("\nSuccessfully created and froze a new, stable requirements.txt.")
        start_group("View new requirements.txt content"); print(installed_packages); end_group()

        if metrics and "not available" not in metrics:
            print(f"\n{'='*70}\n=== BOOTSTRAP SUCCESSFUL: METRICS FOR THE NEW BASELINE ===\n" + "\n".join([f"  {line}" for line in metrics.split('\n')]) + f"\n{'='*70}\n")
            with open(self.config["METRICS_OUTPUT_FILE"], "w") as f: f.write(metrics)

    def run(self):
        if os.path.exists(self.config["METRICS_OUTPUT_FILE"]): os.remove(self.config["METRICS_OUTPUT_FILE"])
        is_pinned, _ = self._get_requirements_state()
        if not is_pinned:
            self._bootstrap_unpinned_requirements()
            return

        dynamic_constraints = []
        final_successful_updates = {}
        final_failed_updates = {}
        
        for pass_num in range(1, self.config["MAX_RUN_PASSES"] + 1):
            start_group(f"UPDATE PASS {pass_num}/{self.config['MAX_RUN_PASSES']} (Constraints: {dynamic_constraints})")
            
            _, lines = self._get_requirements_state()
            all_reqs = list(set(lines + dynamic_constraints))
            original_requirements = {self._get_package_name_from_spec(line): line for line in all_reqs}
            
            packages_to_update = []
            for package, spec in original_requirements.items():
                if '==' not in spec: continue
                current_version = spec.split('==')[1]
                latest_version = self.get_latest_version(package)
                if latest_version and parse_version(latest_version) > parse_version(current_version):
                    packages_to_update.append((package, current_version, latest_version))
            
            if not packages_to_update:
                if pass_num == 1: print("\nAll dependencies are up-to-date.")
                else: print("\nNo further updates possible. System has converged.")
                end_group()
                break
            
            packages_to_update.sort(key=lambda p: self.usage_scores.get(p[0], 0), reverse=True)
            print("\nPrioritized Update Plan for this Pass:")
            for pkg, _, target_ver in packages_to_update:
                score = self.usage_scores.get(pkg, 0)
                print(f"- {pkg} (Usage Score: {score}) -> {target_ver}")

            updates_made_this_pass = False
            learned_a_new_constraint = False
            for package, current_ver, target_ver in packages_to_update:
                is_primary = self._get_package_name_from_spec(package) in self.primary_packages
                success, reason, learned_constraint = self.attempt_update_with_healing(package, current_ver, target_ver, is_primary, dynamic_constraints)
                
                if success:
                    updates_made_this_pass = True
                    final_successful_updates[package] = reason
                    if package in final_failed_updates: del final_failed_updates[package]
                else:
                    final_failed_updates[package] = reason
                    if learned_constraint and learned_constraint not in dynamic_constraints:
                        print(f"DIAGNOSIS: Learned new global constraint '{learned_constraint}' from failure of {package}.")
                        dynamic_constraints.append(learned_constraint)
                        learned_a_new_constraint = True
                        break
            
            end_group()
            if learned_a_new_constraint:
                print("ACTION: Restarting update pass to apply newly learned global constraint.")
                continue
            
            if not updates_made_this_pass:
                print("\nNo successful updates in this pass. System is stable.")
                break

        if final_successful_updates or final_failed_updates:
            print("\n" + "#"*70); print("### OVERALL UPDATE RUN SUMMARY ###")
            if final_successful_updates:
                print("\n[SUCCESS] The following packages were updated:")
                for pkg, reason in final_successful_updates.items(): print(f"- {pkg}: {reason}")
            if final_failed_updates:
                print("\n[FAILURE] Updates were attempted but FAILED for:")
                for pkg, reason in final_failed_updates.items(): print(f"- {pkg}: {reason}")
            print("#"*70 + "\n")

        if final_successful_updates:
            print("\n" + "#"*70); print("### FINAL SYSTEM HEALTH CHECK ###"); print("#"*70 + "\n")
            venv_dir = Path("./final_venv")
            if venv_dir.exists(): shutil.rmtree(venv_dir)
            venv.create(venv_dir, with_pip=True)
            python_executable = str(venv_dir / "bin" / "python")
            _, stderr, returncode = run_command([python_executable, "-m", "pip", "install", "-r", str(self.requirements_path)])
            if returncode != 0:
                print("CRITICAL ERROR: Final installation of combined dependencies failed!", file=sys.stderr); return
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
            return max(stable_versions, key=parse_version) if stable_versions else max([p.version for p in package_info.packages if p.version], key=parse_version)
        except Exception: return None

    # In agent_logic.py

    def _try_install_and_validate(self, package_to_update, new_version, dynamic_constraints):
        """
        The definitive version with clear, explicit logging for every step.
        This function attempts a full install-and-validate cycle for a single target version.
        """
        venv_dir = Path("./temp_venv")
        if venv_dir.exists(): shutil.rmtree(venv_dir)
        venv.create(venv_dir, with_pip=True)
        python_executable = str(venv_dir / "bin" / "python")
        
        with open(self.requirements_path, "r") as f:
             lines = [line.strip() for line in f if line.strip()]
        
        requirements_list = [f"{package_to_update}=={new_version}" if self._get_package_name_from_spec(l) == package_to_update else l for l in lines]
        requirements_list.extend(dynamic_constraints)
        
        # --- First Installation Attempt ---
        print(f"\n--> STEP 1: Attempting to install environment with {package_to_update}=={new_version}")
        _, stderr_install, returncode = run_command([python_executable, "-m", "pip", "install"] + requirements_list)
        
        if returncode != 0:
            print(f"--> STEP 1 FAILED: The initial installation attempt resulted in a dependency conflict.")
            
            if not self.llm_available: 
                return False, "Installation conflict (LLM unavailable)", stderr_install
            
            # --- LLM Conflict Resolution Attempt ---
            print("--> STEP 1.1: Asking LLM for a conflict resolution plan.")
            solution = self.resolve_conflict_with_llm(stderr_install, requirements_list)
            
            if solution:
                start_group("LLM's Proposed Solution")
                print("The LLM suggested the following installation plan to resolve the conflict:")
                for line in solution: print(f"- {line}")
                end_group()
                
                print("\n--> STEP 1.2: Attempting to install the LLM's proposed solution.")
                _, stderr_llm, returncode_llm = run_command([python_executable, "-m", "pip", "install"] + solution)
                if returncode_llm != 0: 
                    print("--> STEP 1.2 FAILED: The LLM's proposed solution also failed to install.")
                    start_group("Reason for LLM Solution Failure")
                    print(stderr_llm)
                    end_group()
                    return False, "LLM solution failed to install", stderr_llm
            else:
                print("--> STEP 1.1 FAILED: The LLM could not find a solution for the conflict.")
                return False, "LLM could not find a solution", stderr_install
        
        print(f"--> STEP 1 SUCCESS: Successfully installed the environment.")
        
        # --- Validation Step ---
        print(f"\n--> STEP 2: Running validation suite.")
        success, metrics, validation_output = validate_changes(python_executable, group_title=f"Validation for {package_to_update}=={new_version}")
        
        if not success:
            print(f"--> STEP 2 FAILED: The validation script failed.")
            return False, "Validation script failed", validation_output

        print(f"--> STEP 2 SUCCESS: Validation passed.")
        return True, metrics, ""

    # In agent_logic.py, replace the entire old function with this one.

    def attempt_update_with_healing(self, package, current_version, target_version, is_primary, dynamic_constraints):
        """
        The definitive, final version with a UNIFIED failure response.
        It no longer tries to diagnose the root cause and instead always
        triggers the full backtracking process for the target package.
        """
        package_label = "(Primary)" if is_primary else "(Transient)"
        
        # This first attempt is still the "happy path".
        success, result_data, stderr = self._try_install_and_validate(package, target_version, dynamic_constraints)
        
        if success:
            metrics = result_data
            if metrics and "not available" not in metrics:
                print(f"\n** SUCCESS: {package} {package_label} updated to {target_version} and passed validation. **")
                print("\n".join([f"  {line}" for line in metrics.split('\n')]) + "\n")
                with open(self.config["METRICS_OUTPUT_FILE"], "w") as f: f.write(metrics)
            
            installed_packages, _, _ = run_command([str(Path("./temp_venv/bin/python")), "-m", "pip", "freeze"])
            with open(self.requirements_path, "w") as f: f.write(installed_packages)
            # On success, we no longer return a learned constraint (it's always None).
            return True, f"Updated to {target_version}", None

        # --- HEALING MODE (TRIGGERED BY *ANY* FAILURE) ---
        print(f"INFO: Initial update for {package} to {target_version} failed. Reason: '{result_data}'. Entering unified healing mode.")
        
        # THE ROOT CAUSE ANALYSIS STEP IS NOW COMPLETELY REMOVED.
        # We proceed directly to backtracking the target package.
        
        version_candidates = self._ask_llm_for_version_candidates(package, target_version, stderr)
        if version_candidates:
            for candidate in version_candidates:
                print(f"INFO: Attempting LLM-suggested backtrack for {package} to {candidate}")
                # We use the simple helper here, as we only care about pass/fail for now.
                success, _, _ = self._try_install_and_validate(package, candidate, dynamic_constraints)
                if success:
                    # If an LLM guess works, we must re-run the success logic to capture metrics and freeze the state.
                    print(f"INFO: LLM-suggested version {candidate} passed validation. Finalizing state.")
                    # We call the main helper again to ensure a clean final state and metrics capture.
                    final_success, final_metrics, _ = self._try_install_and_validate(package, candidate, dynamic_constraints)
                    if final_success:
                        # Full success block handling
                        if final_metrics and "not available" not in final_metrics:
                           print(f"\n** SUCCESS: {package} {package_label} backtracked to {candidate} and passed validation. **")
                           print("\n".join([f"  {line}" for line in final_metrics.split('\n')]) + "\n")
                           with open(self.config["METRICS_OUTPUT_FILE"], "w") as f: f.write(final_metrics)
                        
                        installed_packages, _, _ = run_command([str(Path("./temp_venv/bin/python")), "-m", "pip", "freeze"])
                        with open(self.requirements_path, "w") as f: f.write(installed_packages)
                        return True, f"Backtracked to LLM suggestion {candidate}", None

        print(f"INFO: LLM suggestions failed or were not attempted. Falling back to Binary Search.")
        found_version = self._binary_search_backtrack(package, current_version, target_version, dynamic_constraints)
        
        if found_version:
            # Similar to above, we must run a final validation on the version found by the search.
            print(f"INFO: Binary search found stable version {found_version}. Finalizing state.")
            final_success, final_metrics, _ = self._try_install_and_validate(package, found_version, dynamic_constraints)
            if final_success:
                if final_metrics and "not available" not in final_metrics:
                    print(f"\n** SUCCESS: {package} {package_label} backtracked to {found_version} and passed validation. **")
                    print("\n".join([f"  {line}" for line in final_metrics.split('\n')]) + "\n")
                    with open(self.config["METRICS_OUTPUT_FILE"], "w") as f: f.write(final_metrics)
                
                installed_packages, _, _ = run_command([str(Path("./temp_venv/bin/python")), "-m", "pip", "freeze"])
                with open(self.requirements_path, "w") as f: f.write(installed_packages)

                return True, f"Backtracked to stable version {found_version}", None

        # If we get here, absolutely every attempt has failed.
        return False, "All backtracking attempts failed.", None

    def _binary_search_backtrack(self, package, last_good_version, failed_version, dynamic_constraints):
        start_group(f"Binary Search Backtrack for {package}")
        
        versions = self.get_all_versions_between(package, last_good_version, failed_version)
        if not versions:
            end_group(); return None

        low, high = 0, len(versions) - 1
        best_working_version = None
        while low <= high:
            mid = (low + high) // 2
            test_version = versions[mid]
            print(f"Binary Search: Testing version {test_version}...")
            
            success, _, _ = self._try_install_and_validate(package, test_version, dynamic_constraints)
            
            if success:
                print(f"Binary Search: Version {test_version} PASSED validation.")
                best_working_version = test_version
                low = mid + 1
            else:
                print(f"Binary Search: Version {test_version} FAILED validation.")
                high = mid - 1
        
        end_group()
        if best_working_version:
            print(f"Binary Search SUCCESS: Found latest stable version to be {best_working_version}")
            return best_working_version
        return None

    def get_all_versions_between(self, package_name, start_ver_str, end_ver_str):
        try:
            package_info = self.pypi.get_project_page(package_name)
            if not (package_info and package_info.packages): return []
            start_v = parse_version(start_ver_str)
            end_v = parse_version(end_ver_str)
            candidate_versions = []
            for p in package_info.packages:
                if p.version:
                    try:
                        v = parse_version(p.version)
                        if start_v <= v < end_v: candidate_versions.append(v)
                    except: continue
            return sorted([str(v) for v in set(candidate_versions)], key=parse_version)
        except Exception:
            return []
            
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
            print(f"Sending prompt to LLM for conflict resolution (context: Python {py_version})...")
            response = self.llm.generate_content(prompt)
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
            self.llm_available = False
            return None
        except Exception:
            return None

    def _ask_llm_for_root_cause(self, package, error_message):
        if not self.llm_available: return {}
        prompt = f"Analyze the Python error that occurred when updating '{package}'. Error: --- {error_message} --- Respond in JSON with 'root_cause': ('self' or 'incompatibility'), and if 'incompatibility', also 'package': 'package_name' and 'suggested_constraint': '<version'."
        try:
            response = self.llm.generate_content(prompt)
            json_text = re.search(r'\{.*\}', response.text, re.DOTALL).group(0)
            return json.loads(json_text)
        except Exception: return {}

    def _ask_llm_for_version_candidates(self, package, failed_version, error_message):
        if not self.llm_available: return []
        prompt = f"The python package '{package}' version '{failed_version}' failed validation on Python 3.9. Error: ---{error_message}--- Based on this, give a Python list of the {self.config['MAX_LLM_BACKTRACK_ATTEMPTS']} most recent, previous versions that are most likely to be stable, in descending order. Respond ONLY with the list."
        try:
            response = self.llm.generate_content(prompt)
            match = re.search(r'(\[.*?\])', response.text, re.DOTALL)
            if not match: return []
            return ast.literal_eval(match.group(1))
        except Exception: return []