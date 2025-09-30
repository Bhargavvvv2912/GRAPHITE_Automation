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
        self.usage_scores = self._calculate_risk_scores()

    def _calculate_risk_scores(self):
        start_group("Analyzing Codebase for Update Risk")
        scores = {}
        repo_root = Path('.')
        for py_file in repo_root.rglob('*.py'):
            if any(part in str(py_file) for part in ['temp_venv', 'final_venv', 'bootstrap_venv', 'agent_logic.py', 'agent_utils.py', 'dependency_agent.py']):
                continue
            try:
                with open(py_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    tree = ast.parse(content)
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Import):
                            for alias in node.names:
                                module_name = self._get_package_name_from_spec(alias.name)
                                scores[module_name] = scores.get(module_name, 0) + 1
                        elif isinstance(node, ast.ImportFrom) and node.module:
                            module_name = self._get_package_name_from_spec(node.module)
                            scores[module_name] = scores.get(module_name, 0) + 1
            except Exception: continue
        
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
        print("Unpinned requirements detected. Creating a stable baseline...")
        venv_dir = Path("./bootstrap_venv");
        if venv_dir.exists(): shutil.rmtree(venv_dir)
        venv.create(venv_dir, with_pip=True)
        python_executable = str(venv_dir / "bin" / "python")
        _, stderr, returncode = run_command([python_executable, "-m", "pip", "install", "-r", str(self.requirements_path)])
        if returncode != 0: sys.exit(f"CRITICAL ERROR: Failed to install initial set of dependencies. Error: {stderr}")
        success, metrics, _ = validate_changes(python_executable, group_title="Running Validation on New Baseline")
        if not success: sys.exit("CRITICAL ERROR: Initial dependencies failed validation.")
        if metrics and "not available" not in metrics:
            print(f"\n{'='*70}\n=== BOOTSTRAP SUCCESSFUL: METRICS FOR THE NEW BASELINE ===\n" + "\n".join([f"  {line}" for line in metrics.split('\n')]) + f"\n{'='*70}\n")
        installed_packages, _, _ = run_command([python_executable, "-m", "pip", "freeze"])
        with open(self.requirements_path, "w") as f: f.write(self._prune_pip_freeze(installed_packages))
        start_group("View new requirements.txt content"); print(installed_packages); end_group()
        if metrics:
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
        pass_num = 0
        
        while pass_num < self.config["MAX_RUN_PASSES"]:
            pass_num += 1
            start_group(f"UPDATE PASS {pass_num}/{self.config['MAX_RUN_PASSES']} (Constraints: {dynamic_constraints})")
            
            changed_packages_this_pass = set()
            
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
            
            packages_to_update.sort(key=lambda p: self._calculate_update_risk(p[0], p[1], p[2]), reverse=True)
            print("\nPrioritized Update Plan for this Pass:")
            total_updates_in_plan = len(packages_to_update)
            for i, (pkg, _, target_ver) in enumerate(packages_to_update):
                score = self._calculate_update_risk(pkg, _, target_ver)
                print(f"  {i+1}/{total_updates_in_plan}: {pkg} (Risk Score: {score:.2f}) -> {target_ver}")

            learned_a_new_constraint = False
            for i, (package, current_ver, target_ver) in enumerate(packages_to_update):
                print(f"\n" + "-"*80); print(f"PULSE: [PASS {pass_num} | ATTEMPT {i+1}/{total_updates_in_plan}] Processing '{package}'"); print(f"PULSE: Changed packages this pass so far: {changed_packages_this_pass}"); print("-"*80)
                is_primary = self._get_package_name_from_spec(package) in self.primary_packages
                success, reason, learned_constraint = self.attempt_update_with_healing(package, current_ver, target_ver, is_primary, dynamic_constraints, changed_packages_this_pass)
                
                if success:
                    final_successful_updates[package] = (target_ver, reason)
                    if package in final_failed_updates: del final_failed_updates[package]
                    if current_ver != reason:
                        changed_packages_this_pass.add(package)
                else:
                    final_failed_updates[package] = (target_ver, reason)
                    if learned_constraint and learned_constraint not in dynamic_constraints:
                        print(f"DIAGNOSIS: Learned new global constraint '{learned_constraint}' from failure of {package}.")
                        dynamic_constraints.append(learned_constraint)
                        learned_a_new_constraint = True
                        break
            
            end_group()
            if learned_a_new_constraint:
                print("ACTION: Restarting update pass to apply newly learned global constraint.")
                continue
            
            if not changed_packages_this_pass:
                print("\nNo effective version changes were made in this pass. System is stable.")
                break
        
        self._print_final_summary(final_successful_updates, final_failed_updates)
        if final_successful_updates:
            self._run_final_health_check()

    def _calculate_update_risk(self, package, current_ver, target_ver):
        usage = self.usage_scores.get(package, 0)
        is_primary = 1 if package in self.primary_packages else 0
        try:
            old_v, new_v = parse_version(current_ver), parse_version(target_ver)
            if new_v.major > old_v.major: semver_severity = 3
            elif new_v.minor > old_v.minor: semver_severity = 2
            else: semver_severity = 1
        except: semver_severity = 1
        return (usage * 5.0) + (is_primary * 3.0) + (semver_severity * 2.0)

    def _print_final_summary(self, successful, failed):
        print("\n" + "#"*70); print("### OVERALL UPDATE RUN SUMMARY ###")
        if successful:
            print("\n[SUCCESS] The following packages were successfully updated:")
            print(f"{'Package':<30} | {'Target Version':<20} | {'Reached Version':<20}")
            print(f"{'-'*30} | {'-'*20} | {'-'*20}")
            for pkg, (target_ver, version) in successful.items(): print(f"{pkg:<30} | {target_ver:<20} | {version:<20}")
        if failed:
            print("\n[FAILURE] Updates were attempted but FAILED for:")
            print(f"{'Package':<30} | {'Target Version':<20} | {'Reason for Failure'}")
            print(f"{'-'*30} | {'-'*20} | {'-'*40}")
            for pkg, (target_ver, reason) in failed.items(): print(f"{pkg:<30} | {target_ver:<20} | {reason}")
        print("#"*70 + "\n")

    def _run_final_health_check(self):
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

    def _try_install_and_validate(self, package_to_update, new_version, dynamic_constraints, old_version='N/A', is_probe=False, changed_packages=None):
        venv_dir = Path("./temp_venv")
        if venv_dir.exists(): shutil.rmtree(venv_dir)
        venv.create(venv_dir, with_pip=True)
        python_executable = str(venv_dir / "bin" / "python")
        with open(self.requirements_path, "r") as f:
             lines = [line.strip() for line in f if line.strip()]
        
        requirements_list = [f"{package_to_update}=={new_version}" if self._get_package_name_from_spec(l) == package_to_update else l for l in lines]
        requirements_list.extend(dynamic_constraints)
        
        if not is_probe:
            start_group(f"Attempting to install {package_to_update}=={new_version}")
            print(f"\nChange analysis: Updating '{package_to_update}' from {old_version} -> {new_version}")
        
        _, stderr_install, returncode = run_command([python_executable, "-m", "pip", "install"] + requirements_list)
        
        if not is_probe: end_group()
        
        if returncode != 0:
            llm_summary = self._ask_llm_to_summarize_error(stderr_install)
            reason = f"Installation conflict. Summary: {llm_summary}"
            return False, reason, stderr_install
        
        is_upgrade = new_version != old_version
        if not is_upgrade and (changed_packages is not None and not changed_packages):
             print("\n--> STEP 2: SKIPPING validation because no effective change has occurred.")
             return True, "Validation skipped (no change)", ""

        group_title = f"Validation for {package_to_update}=={new_version}"
        success, metrics, validation_output = validate_changes(python_executable, group_title=group_title)
        if not success:
            return False, "Validation script failed", validation_output
        return True, metrics, ""

    def attempt_update_with_healing(self, package, current_version, target_version, is_primary, dynamic_constraints, changed_packages_this_pass):
        package_label = "(Primary)" if is_primary else "(Transient)"
        success, result_data, stderr = self._try_install_and_validate(package, target_version, dynamic_constraints, old_version=current_version, changed_packages=changed_packages_this_pass)
        
        if success:
            self._handle_success(package, target_version, result_data, package_label)
            return True, target_version, None

        print(f"\nINFO: Initial update for '{package}' failed. Reason: '{result_data}'")
        start_group("View Full Error Log for Initial Failure"); print(stderr); end_group()
        print("INFO: Entering unified healing mode.")
        
        root_cause = self._ask_llm_for_root_cause(package, stderr)
        if root_cause and root_cause.get("package") != package:
            constraint = f"{root_cause.get('package')}{root_cause.get('suggested_constraint')}"
            return False, f"Diagnosed incompatibility with {root_cause.get('package')}", constraint

        version_candidates = self._ask_llm_for_version_candidates(package, target_version)
        if version_candidates:
            for candidate in version_candidates:
                if parse_version(candidate) < parse_version(current_version): continue
                print(f"INFO: Attempting LLM-suggested backtrack for {package} to {candidate}")
                success, result_data, _ = self._try_install_and_validate(package, candidate, dynamic_constraints, old_version=current_version, changed_packages=changed_packages_this_pass)
                if success:
                    self._handle_success(package, candidate, result_data, package_label)
                    return True, candidate, None

        print(f"INFO: LLM suggestions failed. Falling back to Classic Binary Search.")
        success_package = self._binary_search_backtrack(package, current_version, target_version, dynamic_constraints, changed_packages_this_pass)
        if success_package:
            found_version = success_package["version"]
            self._handle_success(package, found_version, success_package["metrics"], package_label, installed_packages=success_package["installed_packages"])
            return True, found_version, None

        return False, "All backtracking attempts failed.", None
    
    def _handle_success(self, package, new_version, metrics, package_label, installed_packages=None):
        if metrics and "not available" not in metrics:
            print(f"\n** SUCCESS: {package} {package_label} finalized at {new_version} and passed validation. **")
            print("\n".join([f"  {line}" for line in metrics.split('\n')]) + "\n")
            with open(self.config["METRICS_OUTPUT_FILE"], "w") as f: f.write(metrics)
        else:
            print(f"\n** SUCCESS: {package} {package_label} finalized at {new_version} and passed validation (metrics unavailable). **\n")
        
        if not installed_packages:
            python_executable_in_venv = str(Path("./temp_venv/bin/python"))
            installed_packages, _, _ = run_command([python_executable_in_venv, "-m", "pip", "freeze"])
        with open(self.requirements_path, "w") as f: f.write(self._prune_pip_freeze(installed_packages))

    def _binary_search_backtrack(self, package, last_good_version, failed_version, dynamic_constraints, changed_packages):
        start_group(f"Binary Search Backtrack for {package}")
        
        versions = self.get_all_versions_between(package, last_good_version, failed_version)
        if not versions:
            end_group(); print(f"Binary Search FAILED: No versions found between {last_good_version} and {failed_version}."); return None

        low, high = 0, len(versions) - 1
        best_working_result = None
        while low <= high:
            mid = (low + high) // 2
            test_version = versions[mid]
            
            print(f"Binary Search: Probing version {test_version}...")
            success, metrics_or_reason, _ = self._try_install_and_validate(package, test_version, dynamic_constraints, old_version=last_good_version, is_probe=True, changed_packages=changed_packages)
            
            if success:
                print(f"Binary Search: Version {test_version} PASSED probe.")
                python_executable_in_venv = str(Path("./temp_venv/bin/python"))
                installed_packages, _, _ = run_command([python_executable_in_venv, "-m", "pip", "freeze"])
                best_working_result = {"version": test_version, "metrics": metrics_or_reason, "installed_packages": installed_packages}
                low = mid + 1
            else:
                print(f"Binary Search: Version {test_version} FAILED probe. Reason: {metrics_or_reason}.")
                high = mid - 1
        
        end_group()
        if best_working_result:
            print(f"Binary Search SUCCESS: Found latest stable version to be {best_working_result['version']}")
            return best_working_result
        print(f"Binary Search FAILED: No stable version was found for {package}.")
        return None

    def get_all_versions_between(self, package_name, start_ver_str, end_ver_str):
        try:
            package_info = self.pypi.get_project_page(package_name)
            if not (package_info and package_info.packages): return []
            start_v = parse_version(start_ver_str)
            end_v = parse_version(end_ver_str)
            candidate_versions = [v for p in package_info.packages if p.version and start_v < (v := parse_version(p.version)) < end_v]
            return sorted([str(v) for v in set(candidate_versions)], key=parse_version, reverse=True)
        except Exception:
            return []

    def _ask_llm_to_summarize_error(self, error_message):
        if not self.llm_available: return "(LLM unavailable due to quota)"
        prompt = f"The following is a Python pip install error log. Please summarize the root cause of the conflict in a single, concise sentence. Error Log: --- {error_message} ---"
        try:
            response = self.llm.generate_content(prompt)
            return response.text.strip().replace('\n', ' ')
        except Exception:
            return "Failed to get summary from LLM."
            
    def _prune_pip_freeze(self, freeze_output):
        lines = freeze_output.strip().split('\n')
        return "\n".join([line for line in lines if '==' in line and not line.startswith('-e')])

    def _ask_llm_for_root_cause(self, package, error_message):
        if not self.llm_available: return {}
        py_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        with open(self.config["REQUIREMENTS_FILE"], "r") as f:
            current_requirements = f.read()
        prompt = f"""You are an expert Python dependency diagnostician AI. Analyze the error that occurred when updating '{package}' in a project with these requirements:
---
{current_requirements}
---
The error on Python {py_version} was:
---
{error_message}
---
Respond in JSON. Is the root_cause 'self' or 'incompatibility'? If incompatibility, name the 'package' and 'suggested_constraint'. Example: {{"root_cause": "incompatibility", "package": "numpy", "suggested_constraint": "<2.0"}}"""
        try:
            response = self.llm.generate_content(prompt)
            json_text = re.search(r'\{.*\}', response.text, re.DOTALL).group(0)
            return json.loads(json_text)
        except Exception: return {}

    def _ask_llm_for_version_candidates(self, package, failed_version):
        if not self.llm_available: return []
        prompt = f"Give a Python list of the {self.config['MAX_LLM_BACKTRACK_ATTEMPTS']} most recent, previous release versions of the python package '{package}', starting from the version just before '{failed_version}'. The list must be in descending order. Respond ONLY with the list. An example could be only '[2.2.3, 2.2.2, 2.1.0]'"
        try:
            response = self.llm.generate_content(prompt)
            match = re.search(r'(\[.*?\])', response.text, re.DOTALL)
            if not match: return []
            return ast.literal_eval(match.group(1))
        except ResourceExhausted:
            self.llm_available = False
            return []
        except Exception: return []