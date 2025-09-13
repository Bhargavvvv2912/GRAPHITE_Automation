import os
import subprocess
from .update_dependency import update_dependency
from .check_updates import main as check_updates_main
from .llm_helper import summarize_changes

OUTDATED_FILE = "outdated.txt"

def run_check_updates():
    """Run the script that checks for outdated packages"""
    code = subprocess.run("python3 agent/check_updates.py", shell=True)
    return code.returncode == 0

def read_outdated_list():
    """Return a list of outdated packages [(pkg, old, new)]"""
    if not os.path.exists(OUTDATED_FILE):
        return []
    with open(OUTDATED_FILE, "r") as f:
        lines = f.readlines()
    outdated = []
    for line in lines:
        parts = line.strip().split()
        if len(parts) == 3:
            outdated.append((parts[0], parts[1], parts[2]))
    return outdated

def main():
    print("Running dependency check...")
    success = run_check_updates()
    if not success:
        print(" check_updates.py failed.")
        return

    outdated = read_outdated_list()
    if not outdated:
        print("All dependencies are up-to-date.")
        return

    print(f"Found {len(outdated)} outdated packages. Updating one by one...")

    for pkg, old, new in outdated:
        print(f" Updating {pkg}: {old} → {new}")
        try:
            entry = update_dependency(pkg, old, new, repo_path=".")
            print(f"{pkg} updated successfully. Metrics: {entry['metrics']}")
        except Exception as e:
            print(f"Failed to update {pkg}: {e}")
            # optionally break the loop or continue to next dependency
            continue

    print("All updates processed.")

if __name__ == "__main__":
    main()
