import requests
import yaml
import os
import re

LOCK_FILE = "requirements.lock"
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.yaml")

def load_config():
    with open(CONFIG_FILE, "r") as f:
        return yaml.safe_load(f)

def load_locked_versions():
    """Read requirements.lock and return dict {package: version}"""
    locked = {}
    if not os.path.exists(LOCK_FILE):
        return locked

    with open(LOCK_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if "==" in line:
                pkg, ver = line.split("==")
                locked[pkg.lower()] = ver
    return locked

def fetch_latest_version(package):
    """Query PyPI API for the latest stable release of a package"""
    url = f"https://pypi.org/pypi/{package}/json"
    resp = requests.get(url)
    if resp.status_code != 200:
        return None
    data = resp.json()
    return data["info"]["version"]

def main():
    config = load_config()
    dependencies = config.get("dependencies", [])
    locked = load_locked_versions()

    outdated = []

    for pkg in dependencies:
        current = locked.get(pkg.lower())
        latest = fetch_latest_version(pkg)

        if latest is None:
            print(f"[WARN] Could not fetch version for {pkg}")
            continue

        if current != latest:
            print(f"[UPDATE] {pkg}: {current} → {latest}")
            outdated.append((pkg, current, latest))
        else:
            print(f"[OK] {pkg} is up-to-date ({current})")

    # Store results for the pipeline
    if outdated:
        with open("outdated.txt", "w") as f:
            for pkg, old, new in outdated:
                f.write(f"{pkg} {old} {new}\n")

    return outdated

if __name__ == "__main__":
    main()
