import subprocess
import os
import re
import json
from datetime import datetime
from .llm_helper import summarize_changes

LOCK_FILE = "requirements.lock"
OUTDATED_FILE = "outdated.txt"
LOGS_DIR = "logs"
METRICS_FILE = os.path.join(LOGS_DIR, "metrics.json")

def read_outdated():
    if not os.path.exists(OUTDATED_FILE):
        return None
    with open(OUTDATED_FILE, "r") as f:
        lines = f.readlines()
    if not lines:
        return None
    pkg, old, new = lines[0].strip().split()
    return pkg, old, new

def update_lock(pkg, new_version):
    updated_lines = []
    with open(LOCK_FILE, "r") as f:
        for line in f:
            if line.lower().startswith(pkg.lower() + "=="):
                updated_lines.append(f"{pkg}=={new_version}\n")
            else:
                updated_lines.append(line)
    with open(LOCK_FILE, "w") as f:
        f.writelines(updated_lines)

def run_command(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr

def parse_metrics(output: str):
    metrics = {}
    m1 = re.search(r"Final transform_robustness:\s*([0-9.]+)", output)
    m2 = re.search(r"Final number of pixels:\s*(\d+)", output)
    m3 = re.search(r"Final number of queries:\s*(\d+)", output)
    if m1: metrics["robustness"] = float(m1.group(1))
    if m2: metrics["pixels"] = int(m2.group(1))
    if m3: metrics["queries"] = int(m3.group(1))
    return metrics

def save_metrics(pkg, old, new, metrics, llm_summary):
    os.makedirs(LOGS_DIR, exist_ok=True)
    entry = {
        "timestamp": datetime.now().isoformat(),
        "package": pkg,
        "old_version": old,
        "new_version": new,
        "metrics": metrics,
        "llm_summary": llm_summary
    }
    if os.path.exists(METRICS_FILE):
        with open(METRICS_FILE, "r") as f:
            data = json.load(f)
    else:
        data = []
    data.append(entry)
    with open(METRICS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def run_tests():
    code, out1 = run_command("./make_output_folders.sh")
    if code != 0:
        return False, out1

    cmd = "python3 main.py -v 14 -t 1 --tr_lo 0.65 --tr_hi 0.85 -s score.py -n GTSRB --heatmap=Target --coarse_mode=binary -b 100 -m 100"
    code, out2 = run_command(cmd)
    if code != 0:
        return False, out2

    os.makedirs(LOGS_DIR, exist_ok=True)
    log_path = os.path.join(LOGS_DIR, "last_run.log")
    with open(log_path, "w") as f:
        f.write(out1 + "\n" + out2)

    if "Attack Completed." not in out2:
        return False, out2

    return True, out2

def git_commit(pkg, old, new):
    run_command("git config user.name 'autoupdate-bot'")
    run_command("git config user.email 'autoupdate-bot@example.com'")
    run_command("git add requirements.lock logs/")
    run_command(f'git commit -m "Update {pkg}: {old} → {new}"')

def main():
    outdated = read_outdated()
    if not outdated:
        print(" No outdated dependencies left")
        return
    pkg, old, new = outdated
    print(f" Updating {pkg}: {old} → {new}")

    #  Call LLM with repo context (imports + relevant files)
    llm_summary = summarize_changes(pkg, old, new, repo_path=".")
    print(f"LLM Summary for {pkg}:\n{llm_summary}")

    # Update lock file
    update_lock(pkg, new)

    # Install new version
    code, _ = run_command(f"pip install {pkg}=={new}")
    if code != 0:
        print(f" Failed to install {pkg}=={new}, reverting")
        update_lock(pkg, old)
        return

    # Run smoke tests
    success, output = run_tests()
    if success:
        metrics = parse_metrics(output)
        print(f" Tests passed with {pkg}=={new}, metrics={metrics}")
        save_metrics(pkg, old, new, metrics, llm_summary)
        git_commit(pkg, old, new)
    else:
        print(f" Tests failed with {pkg}=={new}, reverting")
        update_lock(pkg, old)
        run_command(f"pip install {pkg}=={old}")

if __name__ == "__main__":
    main()

