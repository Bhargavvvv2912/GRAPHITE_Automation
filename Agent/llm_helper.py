# agent/llm_helper.py
import os
import re
import openai

def collect_imports(repo_path="."):
    """Collect all imports in the repo"""
    imports = []
    for root, _, files in os.walk(repo_path):
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(root, f)
                try:
                    with open(path, "r", encoding="utf-8") as file:
                        for line in file:
                            if re.match(r"^\s*(import|from)\s+", line):
                                imports.append(line.strip())
                except Exception:
                    continue
    return imports

def find_relevant_files(pkg, repo_path="."):
    """Find all files that use the package name"""
    relevant = {}
    for root, _, files in os.walk(repo_path):
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(root, f)
                try:
                    with open(path, "r", encoding="utf-8") as file:
                        content = file.read()
                        if pkg in content:
                            relevant[path] = content[:800]  # only first 800 chars for brevity
                except Exception:
                    continue
    return relevant

def summarize_changes(pkg, old, new, repo_path="."):
    imports = collect_imports(repo_path)
    relevant = find_relevant_files(pkg, repo_path)

    imports_text = "\n".join(imports[:20])  # cap to avoid too long prompts
    relevant_text = "\n\n".join([f"{k}:\n{v}" for k, v in list(relevant.items())[:3]])

    prompt = f"""
    You are analyzing a research project called *Graphite*.
    Graphite uses PyTorch, Kornia, and computer vision libraries
    to evaluate robustness by running robustness tests, image transforms,
    and tracking metrics like transform_robustness, pixels, and queries.

    The dependency '{pkg}' has been updated from version {old} to {new}.

    The project currently has these imports:
    {imports_text}

    Relevant code that touches {pkg}:
    {relevant_text}

    Based on this context:
    1. Summarize potential breaking changes or API differences that could affect Graphite.
    2. Highlight any risks for reproducibility, image transforms, or robustness metrics.
    3. Suggest specific parts of the codebase that may need modification.
    """
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",  # or whichever model you configure
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400
        )
        return response["choices"][0]["message"]["content"]
    except Exception as e:
        return f"LLM request failed: {e}"
