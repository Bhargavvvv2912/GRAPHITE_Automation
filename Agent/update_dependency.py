import subprocess
import json
import os
import re
from datetime import datetime
from .llm_helper import summarize_changes

LOG_FILE = "logs/metrics.json"
REQUIREMENTS = "requirements.txt"

def run_command(cmd, cwd="."):
    """Run shell command and return output."""
    try:
        result = subprocess.run(
            cmd, cwd=cwd, shell=True, capture_output=True, text=True, check=True
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        return f"ERROR: {e.stderr}"

def parse_graphite_output(output):
    """Extract robustness, pixels, queries from Graphite logs."""
    metrics = {}
    match_rob = re.search(r"Final transform_robustness:\s*([\d.]+)", output)
    match_pix = re.search(r"Final number of pixels:\s*(\d+)", output)
    match_q = re.search(r"Final number of queries:\s*(\d+)", output)

    if match_rob:
        metrics["robustness"] = float(match_rob.group(1))
    if match_pix:
        metrics["pixels"] = int(match_pix.group(1))
    if match_q:
        metrics["queries"] = int(match_q.group(1))

    return metrics

def update_dependency(package, old_version, new_version, repo_path="."):
    """Update one package, run tests, log metrics + LLM analysis."""
    # Step 1: Update requirements.txt
    with open(os.path.join(repo_path, REQUIREMENTS), "r") as f:
        lines = f.readlines()
    with open(os.path.join(repo_path, REQUIREMENTS), "w") as f:
        for line in lines:
            if line.strip().startswith(package):
                f.write(f"{package}=={new_version}\n")
            else:
                f.write(line)

    # Step 2: Install updated package
    run_command(f"pip install {package}=={new_version}", cwd=repo_path)

    # Step 3: Run Graphite pipeline
    run_command("./make_output_folders.sh", cwd=repo_path)
    output = run_command(
        "python3 main.py -v 14 -t 1 --tr_lo 0.65 --tr_hi 0.85 "
        "-s score.py -n GTSRB --heatmap=Target --coarse_mode=binary -b 100 -m 100",
        cwd=repo_path,
    )

    # Step 4: Parse metrics
    metrics = parse_graphite_output(output)

    # Step 5: Call LLM helper
    llm_summary = summarize_changes(package, old_version, new_version, repo_path)

    # Step 6: Log results
    os.makedirs("logs", exist_ok=True)
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "package": package,
        "old_version": old_version,
        "new_version": new_version,
        "metrics": metrics,
        "llm_summary": llm_summary,
    }

    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            log_data = json.load(f)
    else:
        log_data = []

    log_data.append(entry)

    with open(LOG_FILE, "w") as f:
        json.dump(log_data, f, indent=2)

    # Step 7: Commit changes
    run_command("git add requirements.txt logs/metrics.json", cwd=repo_path)
    run_command(
        f'git commit -m "Update {package}: {old_version} → {new_version}"',
        cwd=repo_path,
    )

    return entry
