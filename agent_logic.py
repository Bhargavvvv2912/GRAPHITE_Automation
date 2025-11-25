# agent_logic.py

import os
import sys
import venv
from pathlib import Path
import ast
import shutil
import re
import json
import subprocess
from google.api_core.exceptions import ResourceExhausted
from pypi_simple import PyPISimple
from packaging.version import parse as parse_version
from agent_utils import start_group, end_group, run_command, validate_changes
from expert_agent import ExpertAgent

class DependencyAgent:
    def __init__(self, config, llm_client):
        self.config = config
        self.expert = ExpertAgent(llm_client) 
        self.pypi = PyPISimple()
        self.requirements_path = Path(config["REQUIREMENTS_FILE"]).resolve()
        self.primary_packages = self._load_primary_packages()
        self.llm_available = True
        self.usage_scores = self._calculate_risk_scores()
        self.exclusions_from_this_run = set()
    
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

    def _calculate_update_risk_components(self, package, current_ver_str, target_ver_str):
        try:
            old_v, new_v = parse_version(current_ver_str), parse_version(target_ver_str)
            if new_v.major > old_v.major: severity_score = 10
            elif new_v.minor > old_v.minor: severity_score = 5
            else: severity_score = 1
        except Exception:
            severity_score = 5

        usage_score = self.usage_scores.get(package, 0)
        criticality_score = 1 if package in self.primary_packages else 0
        
        if hasattr(self, 'dependency_graph_metrics') and package in self.dependency_graph_metrics:
            ecosystem_score = self.dependency_graph_metrics[package].get('dependents', 0)
            depth_score = self.dependency_graph_metrics[package].get('depth', 0)
        else:
            ecosystem_score, depth_score = 0, 0
            
        return {
            "severity": severity_score, "usage": usage_score, "criticality": criticality_score,
            "ecosystem": ecosystem_score, "depth": depth_score
        }

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
            sys.exit(f"Error: {self.config['REQUIREMENTS_FILE']} not found.")
            
        with open(self.requirements_path, "r") as f:
            lines = [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]
        is_fully_pinned = all('==' in line or line.startswith('-e') for line in lines)
        return is_fully_pinned, lines

    def _bootstrap_unpinned_requirements(self):
        start_group("BOOTSTRAP: Establishing a Stable Baseline")
        print("Unpinned requirements detected. Creating and validating a stable baseline...")
        venv_dir = Path("./bootstrap_venv")
        if venv_dir.exists(): shutil.rmtree(venv_dir)
        venv.create(venv_dir, with_pip=True)
        
        success, result, error_log = self._run_bootstrap_and_validate(venv_dir, self.requirements_path)
        
        if success:
            print("\nInitial baseline is valid and stable!")
            with open(self.requirements_path, "w") as f: f.write(result["packages"])
            start_group("View new requirements.txt content"); print(result["packages"]); end_group()
            if result["metrics"] and "not available" not in result["metrics"]:
                print(f"\n{'='*70}\n=== BOOTSTRAP SUCCESSFUL: METRICS FOR THE NEW BASELINE ===\n" + "\n".join([f"  {line}" for line in result['metrics'].split('\n')]) + f"\n{'='*70}\n")
                with open(self.config["METRICS_OUTPUT_FILE"], "w") as f: f.write(result["metrics"])
            end_group()
            return

        print("\nCRITICAL: Initial baseline failed validation. Cannot proceed.", file=sys.stderr)
        start_group("View Initial Baseline Failure Log"); print(error_log); end_group()
        sys.exit("CRITICAL ERROR: Bootstrap failed. Please provide a working set of requirements.")
    
    def run(self):
        if os.path.exists(self.config["METRICS_OUTPUT_FILE"]):
            os.remove(self.config["METRICS_OUTPUT_FILE"])
        
        is_pinned, _ = self._get_requirements_state()

        if not is_pinned:
            print("INFO: Unpinned requirements detected. Bootstrapping a new baseline...")
            self._bootstrap_unpinned_requirements()
        else:
            print("INFO: Found pinned requirements. Running initial health check and regenerating lockfile...")
            start_group("Initial Baseline Health Check")
            
            venv_dir = Path("./initial_check_venv")
            if venv_dir.exists(): shutil.rmtree(venv_dir)
            venv.create(venv_dir, with_pip=True)
            
            success, result, error_log = self._run_bootstrap_and_validate(venv_dir, self.requirements_path)
            
            if not success:
                print("WARNING: Initial baseline is broken. Activating 'Unpin and Bootstrap' repair strategy.", file=sys.stderr)
                end_group()
                self._unpin_and_bootstrap()
            else:
                print("Initial baseline is valid and stable. Overwriting requirements file with clean, frozen state.")
                with open(self.requirements_path, "w") as f:
                    f.write(result["packages"])
                end_group()
        
        final_successful_updates, final_failed_updates = {}, {}
        pass_num = 0
        
        if not hasattr(self, 'dependency_graph_metrics'):
            print("INFO: Dependency graph metrics not found. Entanglement scores will be 0.")
            self.dependency_graph_metrics = {}

        while pass_num < self.config["MAX_RUN_PASSES"]:
            pass_num += 1
            start_group(f"UPDATE PASS {pass_num}/{self.config['MAX_RUN_PASSES']}")
            
            progressive_baseline_path = Path(f"./pass_{pass_num}_progressive_reqs.txt")
            shutil.copy(self.requirements_path, progressive_baseline_path)
            changed_packages_this_pass = set()

            packages_to_update = []
            with open(progressive_baseline_path, 'r') as f: lines = [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]
            for line in lines:
                if '==' not in line or line.strip().startswith('-e'): continue
                package, current_version = self._get_package_name_from_spec(line.split('==')[0]), line.split('==')[1].split(';')[0]
                latest_version = self.get_latest_version(package)
                if latest_version and parse_version(latest_version) > parse_version(current_version):
                    packages_to_update.append((package, current_version, latest_version))

            if not packages_to_update:
                print("\nNo further updates are available. The system has successfully converged.")
                if progressive_baseline_path.exists(): progressive_baseline_path.unlink()
                break 

            update_plan = []
            for pkg, cur, target in packages_to_update:
                components = self._calculate_update_risk_components(pkg, cur, target)
                update_plan.append({'pkg': pkg, 'cur': cur, 'target': target, 'components': components})
            max_ecosystem = max(p['components']['ecosystem'] for p in update_plan) if update_plan else 0
            max_depth = max(p['components']['depth'] for p in update_plan) if update_plan else 0
            W_ECOSYSTEM, W_DEPTH = 1.0, 0.5
            for p in update_plan:
                comps = p['components']; norm_ecosystem = (comps['ecosystem'] / max_ecosystem) if max_ecosystem > 0 else 0
                norm_depth = (comps['depth'] / max_depth) if max_depth > 0 else 0
                entanglement_score = (W_ECOSYSTEM * norm_ecosystem) + (W_DEPTH * norm_depth)
                p['final_score'] = (comps['severity'] * 10) + entanglement_score
                p['code_impact_score'] = comps['usage'] + (comps['criticality'] * 10)
            
            update_plan.sort(key=lambda p: (p['final_score'], p['code_impact_score']), reverse=True)
            
            total_score_sum = sum(p['final_score'] for p in update_plan) if update_plan else 0
            for p in update_plan:
                 if total_score_sum == 0: p['risk_percent_display'] = 0.0
                 else: p['risk_percent_display'] = (p['final_score'] / total_score_sum) * 100.0

            print("\nPrioritized Update Plan for this Pass (Highest Risk First):")
            print(f"{'Rank':<5} | {'Package':<30} | {'% of Total Risk':<18} | {'Change'}")
            print(f"{'-'*5} | {'-'*30} | {'-'*18} | {'-'*20}")
            for i, p in enumerate(update_plan): print(f"{i+1:<5} | {p['pkg']:<30} | {p['risk_percent_display']:<18.2f}% | {p['cur']} -> {p['target']}")
            
            for i, p_data in enumerate(update_plan):
                package, current_ver, target_ver = p_data['pkg'], p_data['cur'], p_data['target']
                print(f"\n" + "-"*80); print(f"PULSE: [PASS {pass_num} | ATTEMPT {i+1}/{len(update_plan)}] Processing '{package}'"); print(f"PULSE: Changed packages this pass so far: {changed_packages_this_pass}"); print("-"*80)
                
                success, reason_or_new_version, _ = self.attempt_update_with_healing(package, current_ver, target_ver, [], progressive_baseline_path, progressive_baseline_path)
                
                if success:
                    reached_version, is_a_real_change = "", False
                    if "skipped" in str(reason_or_new_version): reached_version, is_a_real_change = current_ver, False
                    else:
                        reached_version = reason_or_new_version
                        if current_ver != reached_version: is_a_real_change = True
                    final_successful_updates[package] = (target_ver, reached_version)
                    if is_a_real_change:
                        changed_packages_this_pass.add(package)
                        print(f"  -> SUCCESS. Locking in {package}=={reached_version} into the progressive baseline for this pass.")
                        with open(progressive_baseline_path, "r") as f: temp_lines = f.readlines()
                        with open(progressive_baseline_path, "w") as f:
                            for line in temp_lines:
                                if self._get_package_name_from_spec(line.split(';')[0]) == package:
                                    marker_part = f" ;{line.split(';')[1]}" if ';' in line else ""
                                    f.write(f"{package}=={reached_version}{marker_part}\n")
                                else: f.write(line)
                else:
                    final_failed_updates[package] = (target_ver, reason_or_new_version)
            
            end_group()

            if changed_packages_this_pass:
                print("\nPass complete with changes.")
                shutil.copy(progressive_baseline_path, self.requirements_path)
            if progressive_baseline_path.exists(): progressive_baseline_path.unlink()
            if not changed_packages_this_pass:
                print("\nNo effective version changes were possible in this pass. The system has converged.")
                break
        
        if final_successful_updates:
            self._print_final_summary(final_successful_updates, final_failed_updates)
            print("\nRun complete. The final requirements.txt is the latest provably working version.")

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

    def get_latest_version(self, package_name):
        try:
            package_info = self.pypi.get_project_page(package_name)
            if not (package_info and package_info.packages): return None
            stable_versions = [p.version for p in package_info.packages if p.version and not parse_version(p.version).is_prerelease]
            return max(stable_versions, key=parse_version) if stable_versions else max([p.version for p in package_info.packages if p.version], key=parse_version)
        except Exception: return None

    def _run_bootstrap_and_validate(self, venv_dir, requirements_source):
        python_executable = str((venv_dir / "bin" / "python").resolve())
        
        # Step 1: Install all dependencies from the requirements file.
        print(f"--> Bootstrap Step 1: Installing dependencies from '{requirements_source.name}'...")
        req_path = str(requirements_source.resolve())
        pip_command_deps = [python_executable, "-m", "pip", "install", "-r", req_path]
        _, stderr_deps, returncode_deps = run_command(pip_command_deps)
        if returncode_deps != 0:
            return False, None, f"Failed to install dependencies: {stderr_deps}"

        # Step 2: Install the project itself (if applicable).
        if self.config.get("IS_INSTALLABLE_PACKAGE", False):
            project_extras = self.config.get("PROJECT_EXTRAS", "")
            print(f"\n--> Bootstrap Step 2: Installing project from current directory ('.') in editable mode...")
            
            # FIXED: Added --no-build-isolation for legacy setup.py compatibility
            pip_command_project = [python_executable, "-m", "pip", "install", "--no-build-isolation", "-e", f".{project_extras}"]
            
            _, stderr_project, returncode_project = run_command(pip_command_project)
            if returncode_project != 0:
                return False, None, f"Failed to install project. Error: {stderr_project}"
        
        print("\n--> Bootstrap Step 3: Running validation suite...")
        success, metrics, validation_output = validate_changes(python_executable, self.config)
        if not success:
            return False, None, validation_output
            
        installed_packages_output, _, _ = run_command([python_executable, "-m", "pip", "freeze"])
        final_packages = self._prune_pip_freeze(installed_packages_output)
        return True, {"metrics": metrics, "packages": final_packages}, None
    
    def _try_install_and_validate(self, package_to_update, new_version, dynamic_constraints, baseline_reqs_path, is_probe):
        start_group(f"Probe: Install & Validate for {package_to_update}=={new_version}")
        
        venv_dir = Path("./temp_venv")
        if venv_dir.exists(): shutil.rmtree(venv_dir)
        venv.create(venv_dir, with_pip=True)
        python_executable = str((venv_dir / "bin" / "python").resolve())
        
        # Step 1: Install the proposed dependency set.
        print("--> Probe Step 1: Installing the proposed dependency set...")
        temp_reqs_path = venv_dir / "probe_requirements.txt"
        with open(baseline_reqs_path, "r") as f_read, open(temp_reqs_path, "w") as f_write:
            for line in f_read:
                line = line.strip()
                if not line or line.startswith('#'): continue
                pkg_name = self._get_package_name_from_spec(line)
                if pkg_name and pkg_name.lower() == package_to_update.lower():
                    marker_part = ""
                    if ';' in line: marker_part = " ;" + line.split(";", 1)[1]
                    f_write.write(f"{package_to_update}=={new_version}{marker_part}\n")
                else:
                    f_write.write(f"{line}\n")

        # FIXED: Added --no-build-isolation to bypass build errors during dependency install
        pip_command_core = [python_executable, "-m", "pip", "install", "--no-build-isolation", "-r", str(temp_reqs_path.resolve())]
        _, stderr_core, returncode_core = run_command(pip_command_core)
        if returncode_core != 0:
            summary = self._get_error_summary(stderr_core)
            end_group()
            return False, f"Dependency installation failed: {summary}", stderr_core
        
        if self.config.get("IS_INSTALLABLE_PACKAGE", False):
            project_extras = self.config.get("PROJECT_EXTRAS", "")
            print(f"\n--> Probe Step 2: Installing project from current directory ('.')...")
            
            # FIXED: Added --no-build-isolation
            pip_command_project = [python_executable, "-m", "pip", "install", "--no-build-isolation", f".{project_extras}"]
            
            _, stderr_project, returncode_project = run_command(pip_command_project)
            if returncode_project != 0:
                summary = self._get_error_summary(stderr_project)
                end_group()
                return False, f"Project installation failed: {summary}", stderr_project
        
        # Step 3: Run Validation.
        print("\n--> Probe Step 3: Running validation suite...")
        success, metrics, validation_output = validate_changes(python_executable, self.config)
        if not success:
            end_group()
            return False, "Validation script failed", validation_output

        print("--> SUCCESS: Probe passed.")
        end_group()
        return True, metrics, ""

    def attempt_update_with_healing(self, package, current_version, target_version, dynamic_constraints, baseline_reqs_path, progressive_baseline_path):
        print(f"\n--> Toplevel Attempt: Trying direct update to {package}=={target_version}")
        
        success, result_data, toplevel_stderr = self._try_install_and_validate(
            package, target_version, dynamic_constraints, baseline_reqs_path, is_probe=False
        )
        
        if success:
            print(f"--> Toplevel Result: Direct update SUCCEEDED.")
            return True, result_data if "skipped" in str(result_data) else target_version, None

        print(f"\n--> Toplevel Result: Direct update FAILED. Reason: '{result_data}'")
        
        # --- LEVEL 1 HEALING ---
        print("--> Action: Entering Level 1 Healing with 'Filter-Then-Scan' strategy.")
        
        # UNPACK THE NEW RETURN VALUE (healed_ver, rich_log)
        healed_version, level1_stderr = self._heal_with_filter_and_scan(package, current_version, target_version, baseline_reqs_path)
        
        if healed_version and healed_version != current_version:
             print(f"--> Healing Result: Level 1 Succeeded. Found new working version: {healed_version}")
             return True, healed_version, None

        # --- ESCALATION TO LEVEL 2 ---
        print(f"\n--> Action: Level 1 Healing failed for '{package}'. Escalating to Level 2 'Co-Resolution Expert'.")
        
        # DECISION: Use the Level 1 log if available, as it contains specific conflict details
        best_error_log = level1_stderr if level1_stderr else toplevel_stderr

        # 1. Diagnose the blockers
        print("    -> Step 1: Diagnosing blockers...")
        blockers = self.expert.diagnose_conflict_from_log(best_error_log)
        
        # 2. Feasibility Check
        print(f"    -> Step 2: Checking feasibility. Diagnosed blockers: {blockers}")
        available_updates = self.get_available_updates_from_plan()
        
        # Robust filtering: Ignore Target, Project Name, Pip, Setuptools
        project_name = self.config.get("PROJECT_NAME", "").lower().replace('_', '-')
        other_blockers = [
            b for b in blockers 
            if b.lower() != package.lower() 
            and b.lower() != project_name
            and b.lower() != "pip"
            and b.lower() != "setuptools"
        ]
        
        updatable_blockers = {
            pkg: ver for pkg, ver in available_updates.items() 
            if pkg in other_blockers
        }
        
        if not updatable_blockers:
            print("    -> Feasibility Check FAILED: No other blocking packages have available updates.")
            print("       A meaningful co-resolution is not possible at this time.")
            return True, current_version, None 

        print(f"    -> Feasibility Check PASSED. Candidates: {list(updatable_blockers.keys())}")

        # 3. GREEDY HEURISTIC ATTEMPT (Update ALL blockers)
        print(f"    -> Step 3: Attempting 'Greedy Maximization' (Update ALL blockers to latest)...")
        
        greedy_plan = [f"{package}=={target_version}"]
        for b_pkg, b_ver in updatable_blockers.items():
            if b_pkg.lower() != package.lower():
                greedy_plan.append(f"{b_pkg}=={b_ver}")
        
        print(f"       Greedy Plan: {greedy_plan}")
        probe_success = self._run_co_resolution_probe(greedy_plan, baseline_reqs_path)
        
        if probe_success:
            print("--> Greedy Maximization SUCCEEDED! (Skipped LLM)")
            with open(progressive_baseline_path, "r") as f: temp_lines = f.readlines()
            with open(progressive_baseline_path, "w") as f:
                updated_pkgs = {self._get_package_name_from_spec(p.split('==')[0]): p.split('==')[1] for p in greedy_plan}
                for line in temp_lines:
                    p_name = self._get_package_name_from_spec(line.split('==')[0] if '==' in line else "")
                    if p_name in updated_pkgs:
                        f.write(f"{p_name}=={updated_pkgs[p_name]}\n")
                    else:
                        f.write(line)
            return True, target_version, None

        print("       Greedy Plan FAILED. Proceeding to Neuro-Symbolic Refinement.")

        # 4. NEURO-SYMBOLIC LOOP (Expert Agent)
        current_versions_map = {}
        with open(baseline_reqs_path, 'r') as f:
            for line in f:
                if '==' in line:
                    p_name = self._get_package_name_from_spec(line.split('==')[0])
                    v_part = line.split('==')[1].split(';')[0].strip()
                    current_versions_map[p_name] = v_part

        history = []
        history.append((str(greedy_plan), "Greedy update failed validation."))

        for attempt in range(1, 4):
            print(f"\n    -> [Co-Resolution Attempt {attempt}/3] Requesting Expert Plan...")
            
            # Pass the Best Error Log for the first attempt, then use history
            current_error_log = best_error_log if attempt == 1 else "Previous plan failed"
            
            co_resolution_plan = self.expert.propose_co_resolution(
                target_package=package, 
                error_log=current_error_log, 
                available_updates=updatable_blockers,
                current_versions=current_versions_map,
                history=history
            )

            if not co_resolution_plan or not co_resolution_plan.get("plausible"):
                print("    -> Expert sees no plausible solution.")
                break

            plan_list = co_resolution_plan['proposed_plan']
            print(f"    -> Executing Plan: {plan_list}")
            
            probe_success = self._run_co_resolution_probe(plan_list, baseline_reqs_path)

            if probe_success:
                 print("--> Co-resolution probe SUCCEEDED!")
                 with open(progressive_baseline_path, "r") as f: temp_lines = f.readlines()
                 with open(progressive_baseline_path, "w") as f:
                     updated_pkgs = {self._get_package_name_from_spec(p.split('==')[0]): p.split('==')[1] for p in plan_list}
                     for line in temp_lines:
                         p_name = self._get_package_name_from_spec(line.split('==')[0] if '==' in line else "")
                         if p_name in updated_pkgs:
                             f.write(f"{p_name}=={updated_pkgs[p_name]}\n")
                         else:
                             f.write(line)
                 return True, target_version, None
            
            history.append((str(plan_list), "Validation Failed"))

        print(f"--> Healing Result: All attempts failed. Reverting to {current_version}.")
        return True, current_version, None
        
    def _heal_with_filter_and_scan(self, package, last_good_version, failed_version, baseline_reqs_path):
        start_group(f"Healing '{package}': Filter-Then-Scan Strategy")
        
        last_stderr = ""  # <--- Store the error log for the Expert Agent

        print("\n--- Phase 1: Filtering for compatible versions ---")
        candidate_versions = self.get_all_versions_between(package, last_good_version, failed_version)
        if not candidate_versions:
            print("No intermediate versions to test."); end_group()
            return last_good_version, ""

        fixed_constraints = []
        with open(baseline_reqs_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and self._get_package_name_from_spec(line) != package:
                    fixed_constraints.append(line)

        installable_versions = []
        venv_dir = Path("./temp_pip_check")
        if venv_dir.exists(): shutil.rmtree(venv_dir)
        venv.create(venv_dir, with_pip=True)
        python_executable = str((venv_dir / "bin" / "python").resolve())
        
        for version in reversed(candidate_versions):
            print(f"  -> Checking compatibility of {package}=={version}...")
            requirements_list_for_check = fixed_constraints + [f"{package}=={version}"]
            
            # FIXED: Added --no-build-isolation to avoid legacy build failures
            pip_command = [python_executable, "-m", "pip", "install", "--no-build-isolation", "--dry-run"] + requirements_list_for_check
            
            _, stderr, returncode = run_command(pip_command, display_command=False)

            if returncode == 0:
                print(f"     -- Compatible.")
                installable_versions.append(version)
            else:
                last_stderr = stderr  # <--- Capture the rich error log
                summary = self._get_error_summary(stderr)
                print(f"     -- Incompatible. Diagnosis: {summary}")
        
        if venv_dir.exists(): shutil.rmtree(venv_dir)

        print("\n--- Phase 2: Validating compatible versions (newest first) ---")
        if not installable_versions:
            print("Result: No compatible versions were found. Reverting to last known good version.")
            end_group()
            return last_good_version, last_stderr
        
        print(f"Found {len(installable_versions)} compatible versions to test: {installable_versions}")

        for version_to_test in installable_versions:
            success, _, _ = self._try_install_and_validate(
                package, version_to_test, [], baseline_reqs_path, is_probe=True
            )
            if success:
                print(f"\nSUCCESS: Found latest working version: {package}=={version_to_test}")
                end_group()
                return version_to_test, "" # Success = empty error log
        
        print("\nResult: No compatible version passed validation. Reverting to last known good version.")
        end_group()
        return last_good_version, last_stderr
    
    def get_all_versions_between(self, package_name, start_ver_str, end_ver_str):
        try:
            package_info = self.pypi.get_project_page(package_name)
            if not (package_info and package_info.packages): return []
            start_v, end_v = parse_version(start_ver_str), parse_version(end_ver_str)
            all_versions = {p.version for p in package_info.packages if p.version and not parse_version(p.version).is_prerelease}
            candidate_versions = [v_str for v_str in all_versions if start_v < parse_version(v_str) <= end_v]
            return sorted(candidate_versions, key=parse_version)
        except Exception: return []

    def _prune_pip_freeze(self, freeze_output):
        lines = freeze_output.strip().split('\n')
        pruned_lines = [line for line in lines if '==' in line or line.strip().startswith('-e')]
        editable_lines = sorted([line for line in pruned_lines if line.startswith('-e')])
        pinned_lines = sorted([line for line in pruned_lines if '==' in line])
        return "\n".join(editable_lines + pinned_lines)
    
    def _get_error_summary(self, error_message: str) -> str:
        return self.expert.summarize_error(error_message)
    

    def get_available_updates_from_plan(self) -> dict:
        available_updates = {}
        with open(self.requirements_path, 'r') as f:
            for line in f:
                if '==' not in line or line.startswith('#') or line.startswith('-e'):
                    continue
                try:
                    pkg_name = self._get_package_name_from_spec(line.split('==')[0])
                    current_ver = line.split('==')[1].split(';')[0].strip()
                    latest_ver = self.get_latest_version(pkg_name)
                    if latest_ver and parse_version(latest_ver) > parse_version(current_ver):
                        available_updates[pkg_name] = latest_ver
                except Exception:
                    continue
        return available_updates

    def _run_co_resolution_probe(self, proposed_plan: list, baseline_reqs_path: Path) -> bool:
        start_group(f"Co-Resolution Probe: Testing plan {proposed_plan}")
        
        venv_dir = Path("./temp_co_res_venv")
        if venv_dir.exists(): shutil.rmtree(venv_dir)
        venv.create(venv_dir, with_pip=True)
        python_executable = str((venv_dir / "bin" / "python").resolve())
        project_dir = self.config["VALIDATION_CONFIG"].get("project_dir")
        
        print("--> Stage 1: Installing the proposed co-resolution dependency set...")
        requirements_list = []
        packages_in_plan = [self._get_package_name_from_spec(p) for p in proposed_plan]

        with open(baseline_reqs_path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'): continue
                if self._get_package_name_from_spec(line) not in packages_in_plan:
                    requirements_list.append(line)
        
        requirements_list.extend(proposed_plan)
        
        # FIXED: Added --no-build-isolation
        pip_command_tools = [python_executable, "-m", "pip", "install", "--no-build-isolation"] + requirements_list
        _, stderr_install, returncode = run_command(pip_command_tools, cwd=project_dir)
        
        if returncode != 0:
            print("--> ERROR: Co-resolution installation failed.")
            summary = self.expert.summarize_error(stderr_install)
            print(f"    Diagnosis: {summary}")
            end_group()
            return False

        print("\n--> Stage 2: Building and installing the project...")
        # FIXED: Added --no-build-isolation
        build_install_command = [python_executable, "-m", "pip", "install", "--no-build-isolation", f"./{project_dir}"]
        _, stderr_build, returncode_build = run_command(build_install_command, cwd=project_dir)
        if returncode_build != 0:
            print("--> ERROR: Project build/install failed with the co-resolution set.")
            end_group()
            return False

        print("\n--> Stage 3: Running validation suite on the co-resolution set...")
        success, _, _ = validate_changes(python_executable, self.config, group_title="Validating Co-Resolution Plan")
        
        if not success:
            print("--> ERROR: Validation failed for the co-resolution set.")
            end_group()
            return False

        print("--> SUCCESS: The co-resolution plan is provably working.")
        end_group()
        return True
    
    def _unpin_and_bootstrap(self):
        start_group("REPAIR MODE: Re-bootstrapping from unpinned requirements")
        print("--> Step 1: Extracting package names from broken requirements file...")
        package_names = []
        with open(self.requirements_path, 'r') as f_read:
            for line in f_read:
                if line.strip().startswith('-e'):
                    package_names.append(line.strip())
                else:
                    pkg_name = self._get_package_name_from_spec(line)
                    if pkg_name:
                        package_names.append(pkg_name)
        
        print(f"--> Step 2: Overwriting '{self.requirements_path.name}' with unpinned packages.")
        with open(self.requirements_path, 'w') as f_write:
            f_write.write("\n".join(sorted(package_names)))
        print("\n--> Step 3: Calling bootstrap to find a new, stable, pinned baseline...")
        self._bootstrap_unpinned_requirements()
        print("\nINFO: Baseline successfully repaired.")
        end_group()