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
        self.exclusions_from_this_run = set()

    def _calculate_risk_scores(self, custom_package_list=None):
        if custom_package_list:
            start_group("Calculating Risk Scores for Custom Package List")
            # Create scores based on pre-calculated usage, default to 1 if not found
            scores = {self._get_package_name_from_spec(pkg): self.usage_scores.get(self._get_package_name_from_spec(pkg), 1) for pkg in custom_package_list}
            print("Dynamic risk scores calculated for healing.")
            end_group()
            return scores
            
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
        if not self.requirements_path.exists():
            print(f"Warning: {self.config['REQUIREMENTS_FILE']} not found. Assuming it needs to be created.", file=sys.stderr)
            return False, []
        with open(self.requirements_path, "r") as f:
            lines = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        return all('==' in line for line in lines), lines

    def _run_bootstrap_and_validate(self, venv_dir, requirements_list):
        python_executable = str(venv_dir / "bin" / "python")
        if isinstance(requirements_list, Path) or isinstance(requirements_list, str):
            pip_command = [python_executable, "-m", "pip", "install", "-r", str(requirements_list)]
        else: # It's a list of packages
            pip_command = [python_executable, "-m", "pip", "install"] + requirements_list
            
        _, stderr_install, returncode = run_command(pip_command)
        if returncode != 0:
            return False, None, f"Failed to install dependencies. Error: {stderr_install}"

        success, metrics, validation_output = validate_changes(python_executable, group_title="Running Validation on New Baseline")
        if not success:
            return False, None, validation_output
            
        installed_packages, _, _ = run_command([python_executable, "-m", "pip", "freeze"])
        return True, {"metrics": metrics, "packages": self._prune_pip_freeze(installed_packages)}, None

    def _bootstrap_unpinned_requirements(self):
        start_group("BOOTSTRAP: Establishing a Stable Baseline")
        print("Unpinned requirements detected. Creating and validating a stable baseline...")
        venv_dir = Path("./bootstrap_venv")
        if venv_dir.exists(): shutil.rmtree(venv_dir)
        venv.create(venv_dir, with_pip=True)
        
        success, result, error_log = self._run_bootstrap_and_validate(venv_dir, self.requirements_path)
        
        if success:
            print("\nInitial baseline is valid and stable!")
            with open(self.requirements_path, "w") as f:
                f.write(result["packages"])
            start_group("View new requirements.txt content"); print(result["packages"]); end_group()
            if result["metrics"] and "not available" not in result["metrics"]:
                print(f"\n{'='*70}\n=== BOOTSTRAP SUCCESSFUL: METRICS FOR THE NEW BASELINE ===\n" + "\n".join([f"  {line}" for line in result['metrics'].split('\n')]) + f"\n{'='*70}\n")
                with open(self.config["METRICS_OUTPUT_FILE"], "w") as f:
                    f.write(result["metrics"])
            end_group()
            return

        print("\nCRITICAL: Initial baseline failed validation. Initiating Bootstrap Healing Protocol.", file=sys.stderr)
        start_group("View Initial Baseline Failure Log"); print(error_log); end_group()
        
        python_executable = str(venv_dir / "bin" / "python")
        initial_failing_packages_list, _, _ = run_command([python_executable, "-m", "pip", "freeze"])
        initial_failing_packages = self._prune_pip_freeze(initial_failing_packages_list).split('\n')

        healed_packages = self._attempt_llm_bootstrap_heal(initial_failing_packages, error_log)
        
        if not healed_packages:
            print("\nINFO: LLM healing failed. Falling back to Deterministic Downgrade Protocol.")
            healed_packages = self._attempt_deterministic_bootstrap_heal(initial_failing_packages)

        if healed_packages:
            print("\nSUCCESS: Bootstrap Healing Protocol found a stable baseline.")
            with open(self.requirements_path, "w") as f: f.write("\n".join(healed_packages))
            start_group("View Healed and Pinned requirements.txt"); print("\n".join(healed_packages)); end_group()
        else:
            sys.exit("CRITICAL ERROR: All bootstrap healing attempts failed. Cannot establish a stable baseline.")
        end_group()

    def _attempt_llm_bootstrap_heal(self, failing_packages, error_log):
        print("\nINFO: Attempting LLM-powered diagnosis for bootstrap failure.")
        original_reqs = ""
        try:
            with open(self.requirements_path, 'r') as f: original_reqs = f.read()
        except FileNotFoundError:
             print("Could not read original requirements file for LLM context.")

        # Define the example separately to ensure it is treated as a simple string.
        example_json = '{"changes": [{"package": "numpy", "version": "1.26.4"}]}'

        for attempt in range(self.config["MAX_LLM_BACKTRACK_ATTEMPTS"]):
            print(f"  LLM Heal Attempt {attempt + 1}/{self.config['MAX_LLM_BACKTRACK_ATTEMPTS']}...")
            
            # **FIXED HERE:** Construct the prompt safely using concatenation to avoid f-string parsing issues.
            prompt_base = f"""You are an expert Python dependency debugging AI. A project's validation fails with the following packages installed. Your task is to suggest a targeted downgrade of one or more packages to fix the runtime error. Your response MUST be a single, valid JSON object and nothing else.

            Original unpinned requirements:
            ---
            {original_reqs}
            ---
            Full list of failing installed packages:
            ---
            {"\n".join(failing_packages)}
            ---
            Validation error log:
            ---
            {error_log}
            ---
            Propose a change. Respond ONLY with a JSON object like this example:
            """
            prompt = prompt_base + example_json

            try:
                response = self.llm.generate_content(prompt)
                json_text = re.search(r'\{.*\}', response.text, re.DOTALL).group(0)
                suggestion = json.loads(json_text)
                changes = suggestion.get("changes", [])
                if not changes: continue

                new_packages = list(failing_packages)
                for change in changes:
                    pkg_name, new_ver = change['package'], change['version']
                    new_packages = [f"{pkg_name}=={new_ver}" if self._get_package_name_from_spec(p) == pkg_name else p for p in new_packages]

                venv_dir = Path("./bootstrap_venv");
                if venv_dir.exists(): shutil.rmtree(venv_dir)
                venv.create(venv_dir, with_pip=True)
                
                success, result, new_error_log = self._run_bootstrap_and_validate(venv_dir, new_packages)
                if success:
                    print(f"  LLM SUCCEEDED. Found a stable baseline with changes: {changes}")
                    self.exclusions_from_this_run.update(c['package'] for c in changes)
                    return result['packages'].split('\n')
                else:
                    error_log = new_error_log
            except Exception as e:
                print(f"  LLM attempt failed with an exception: {e}")
                continue
        return None
        
    def _attempt_deterministic_bootstrap_heal(self, failing_packages):
        risk_scores = self._calculate_risk_scores(custom_package_list=failing_packages)
        sorted_suspects = sorted(failing_packages, key=lambda p: risk_scores.get(self._get_package_name_from_spec(p), 0), reverse=True)

        for i, suspect_spec in enumerate(sorted_suspects):
            pkg_name = self._get_package_name_from_spec(suspect_spec)
            if '==' not in suspect_spec: continue
            current_ver = suspect_spec.split('==')[1]

            print(f"\nDeterministic Heal ({i+1}/{len(sorted_suspects)}): Testing downgrade for '{pkg_name}' (Risk Score: {risk_scores.get(pkg_name, 0):.2f})")
            
            versions = self.get_all_versions_between(pkg_name, "0.0.1", current_ver)
            if not versions: continue
            
            prev_version = versions[-1]
            
            new_packages = [f"{pkg_name}=={prev_version}" if p == suspect_spec else p for p in failing_packages]
            
            venv_dir = Path("./bootstrap_venv")
            if venv_dir.exists(): shutil.rmtree(venv_dir)
            venv.create(venv_dir, with_pip=True)

            success, result, _ = self._run_bootstrap_and_validate(venv_dir, new_packages)
            if success:
                print(f"  Deterministic Heal SUCCEEDED. Found culprit: '{pkg_name}'. Stable at version {prev_version}.")
                self.exclusions_from_this_run.add(pkg_name)
                return result['packages'].split('\n')

        return None

    def run(self):
        if os.path.exists(self.config["METRICS_OUTPUT_FILE"]): os.remove(self.config["METRICS_OUTPUT_FILE"])
        is_pinned, _ = self._get_requirements_state()
        if not is_pinned:
            self._bootstrap_unpinned_requirements()
            is_pinned, _ = self._get_requirements_state()
            if not is_pinned:
                 sys.exit("CRITICAL: Bootstrap process failed to produce a fully pinned requirements file.")

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
                if package in self.exclusions_from_this_run:
                    print(f"  Skipping '{package}' in this run's update plan due to recent bootstrap healing.")
                    continue
                if '==' not in spec: continue
                current_version = spec.split('==')[1]
                latest_version = self.get_latest_version(package)
                if latest_version and parse_version(latest_version) > parse_version(current_version):
                    packages_to_update.append((package, current_version, latest_version))
            
            if not packages_to_update:
                if pass_num == 1 and not self.exclusions_from_this_run: print("\nAll dependencies are up-to-date.")
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
            page = self.pypi.get_project_page(package_name)
            if not (page and page.packages): return None
            stable_versions = [p.version for p in page.packages if p.version and not parse_version(p.version).is_prerelease]
            if stable_versions:
                return max(stable_versions, key=parse_version)
            all_versions = [p.version for p in page.packages if p.version]
            return max(all_versions, key=parse_version) if all_versions else None
        except Exception: return None

    def _try_install_and_validate(self, package_to_update, new_version, dynamic_constraints, old_version, is_probe, changed_packages):
        venv_dir = Path("./temp_venv")
        if venv_dir.exists(): shutil.rmtree(venv_dir)
        venv.create(venv_dir, with_pip=True)
        python_executable = str(venv_dir / "bin" / "python")
        
        with open(self.requirements_path, "r") as f:
            lines = [line.strip() for line in f if line.strip()]
        
        requirements_list = [f"{package_to_update}=={new_version}" if self._get_package_name_from_spec(l) == package_to_update else l for l in lines]
        requirements_list.extend(c for c in dynamic_constraints if self._get_package_name_from_spec(c) != package_to_update)

        if not is_probe:
            start_group(f"Attempting to install {package_to_update}=={new_version}")
            print(f"\nChange analysis: Updating '{package_to_update}' from {old_version} -> {new_version}")

        _, stderr_install, returncode = run_command([python_executable, "-m", "pip", "install"] + requirements_list)
        
        if not is_probe: end_group()
        
        if returncode != 0:
            llm_summary = self._ask_llm_to_summarize_error(stderr_install)
            reason = f"Installation conflict. Summary: {llm_summary}"
            return False, reason, stderr_install

        if new_version == old_version and not changed_packages:
             print("\n--> SKIPPING validation because no effective version change has occurred.")
             return True, "Validation skipped (no change)", ""

        group_title = f"Validation for {package_to_update}=={new_version}"
        success, metrics, validation_output = validate_changes(python_executable, group_title=group_title)
        if not success:
            return False, "Validation script failed", validation_output
        return True, metrics, ""

    def attempt_update_with_healing(self, package, current_version, target_version, is_primary, dynamic_constraints, changed_packages_this_pass):
        package_label = "(Primary)" if is_primary else "(Transient)"
        success, result_data, stderr = self._try_install_and_validate(package, target_version, dynamic_constraints, old_version=current_version, is_probe=False, changed_packages=changed_packages_this_pass)
        
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
                if parse_version(candidate) <= parse_version(current_version): continue
                print(f"INFO: Attempting LLM-suggested backtrack for {package} to {candidate}")
                success, result_data, _ = self._try_install_and_validate(package, candidate, dynamic_constraints, old_version=current_version, is_probe=False, changed_packages=changed_packages_this_pass)
                if success:
                    self._handle_success(package, candidate, result_data, package_label)
                    return True, candidate, None

        print(f"INFO: LLM suggestions failed. Falling back to Binary Search backtracking.")
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
            with open(self.config["METRICS_OUTPUT_FILE"], "a") as f: f.write(metrics + "\n\n")
        else:
            print(f"\n** SUCCESS: {package} {package_label} finalized at {new_version} and passed (metrics unavailable). **\n")
        
        if not installed_packages:
            python_executable_in_venv = str(Path("./temp_venv/bin/python"))
            installed_packages, _, _ = run_command([python_executable_in_venv, "-m", "pip", "freeze"])
        with open(self.requirements_path, "w") as f: f.write(self._prune_pip_freeze(installed_packages))

    def _binary_search_backtrack(self, package, last_good_version, failed_version, dynamic_constraints, changed_packages):
        start_group(f"Binary Search Backtrack for {package}")
        
        versions = self.get_all_versions_between(package, last_good_version, failed_version)
        if not versions:
            print(f"Binary Search: No candidate versions found between {last_good_version} and {failed_version}.")
            success, metrics, _ = self._try_install_and_validate(package, last_good_version, dynamic_constraints, is_probe=True, changed_packages=changed_packages, old_version=last_good_version)
            if success:
                print(f"Binary Search SUCCESS: Re-validated last good version {last_good_version} as the only stable option.")
                end_group()
                python_executable_in_venv = str(Path("./temp_venv/bin/python"))
                installed_packages, _, _ = run_command([python_executable_in_venv, "-m", "pip", "freeze"])
                return {"version": last_good_version, "metrics": metrics, "installed_packages": installed_packages}

        low, high = 0, len(versions) - 1
        best_working_result = None
        while low <= high:
            mid = (low + high) // 2
            test_version = versions[mid]
            print(f"Binary Search: Probing version {test_version}...")
            
            success, metrics_or_reason, _ = self._try_install_and_validate(package, test_version, dynamic_constraints, is_probe=True, changed_packages=changed_packages, old_version=last_good_version)
            
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
            print(f"Binary Search SUCCESS: Found latest stable version: {best_working_result['version']}")
            return best_working_result
        print(f"Binary Search FAILED: No stable version was found for {package}.")
        return None

    def get_all_versions_between(self, package_name, start_ver_str, end_ver_str):
        try:
            page = self.pypi.get_project_page(package_name)
            if not (page and page.packages): return []
            start_v, end_v = parse_version(start_ver_str), parse_version(end_ver_str)
            candidate_versions = {parse_version(p.version) for p in page.packages if p.version and start_v <= parse_version(p.version) < end_v and not getattr(parse_version(p.version), 'is_prerelease', False)}
            return sorted([str(v) for v in candidate_versions], key=parse_version)
        except Exception: return []

    def _ask_llm_to_summarize_error(self, error_message):
        if not self.llm_available: return "(LLM unavailable)"
        prompt = f"Summarize the root cause of this Python pip install error in one concise sentence. Error Log: --- {error_message} ---"
        try:
            response = self.llm.generate_content(prompt, request_options={"timeout": 60})
            return response.text.strip().replace('\n', ' ')
        except ResourceExhausted:
            self.llm_available = False
            return "(LLM Error: API quota exhausted)"
        except Exception as e:
            return f"(LLM Error: {type(e).__name__})"
            
    def _prune_pip_freeze(self, freeze_output):
        lines = freeze_output.strip().split('\n')
        return "\n".join([line for line in lines if '==' in line and not line.startswith('-e')])

    def _ask_llm_for_root_cause(self, package, error_message):
        if not self.llm_available: return {}
        py_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        with open(self.config["REQUIREMENTS_FILE"], "r") as f:
            current_requirements = f.read()
        
        # **FIXED HERE:** JSON example is now a separate variable to avoid f-string syntax errors.
        example_json = '{"root_cause": "incompatibility", "package": "numpy", "suggested_constraint": "<2.0"}'
        prompt = f"""You are an expert Python dependency diagnostician AI. Your response MUST be a single, valid JSON object and nothing else.
        Analyze the error that occurred when updating '{package}' in a project with these requirements:
        ---
        {current_requirements}
        ---
        The error on Python {py_version} was:
        ---
        {error_message}
        ---
        Respond with a JSON object. The 'root_cause' key must be either 'self' or 'incompatibility'. If 'incompatibility', you MUST also provide the 'package' and 'suggested_constraint' keys.
        Example response: {example_json}
        """
        try:
            response = self.llm.generate_content(prompt, request_options={"timeout": 100})
            match = re.search(r'\{[\s\S]*\}', response.text)
            if not match:
                print("LLM Error: No JSON object found in the response.")
                return {}
            json_text = match.group(0)
            return json.loads(json_text)
        except ResourceExhausted:
            self.llm_available = False
            print("LLM Error: API quota exhausted for root cause analysis.")
            return {}
        except json.JSONDecodeError:
            print(f"LLM Error: Failed to decode JSON from response: {response.text}")
            return {}
        except Exception as e:
            print(f"LLM Error during root cause analysis: {type(e).__name__} - {e}")
            return {}

    def _ask_llm_for_version_candidates(self, package, failed_version):
        if not self.llm_available: return []
        
        # **FIXED HERE:** Python list example is now a separate variable.
        example_list = '["1.2.3", "1.2.2", "1.2.1"]'
        prompt = f"""You are a Python package versioning expert. Your response MUST be a single, valid Python list of strings and nothing else.
        Provide the {self.config['MAX_LLM_BACKTRACK_ATTEMPTS']} most recent, previous release versions of the python package '{package}', starting from the version just before '{failed_version}'. The list must be in descending order of version.
        Example response: {example_list}
        """
        try:
            response = self.llm.generate_content(prompt, request_options={"timeout": 60})
            match = re.search(r'\[[\s\S]*\]', response.text)
            if not match:
                print("LLM Error: No Python list found in the response.")
                return []
            list_text = match.group(0)
            return ast.literal_eval(list_text)
        except ResourceExhausted:
            self.llm_available = False
            print("LLM Error: API quota exhausted for version candidates.")
            return []
        except (ValueError, SyntaxError):
            print(f"LLM Error: Failed to parse Python list from response: {response.text}")
            return []
        except Exception as e:
            print(f"LLM Error during version candidate search: {type(e).__name__} - {e}")
            return []