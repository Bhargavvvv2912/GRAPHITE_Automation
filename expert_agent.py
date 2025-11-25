# expert_agent.py

import re
import json
from google.api_core.exceptions import ResourceExhausted

class ExpertAgent:
    def __init__(self, llm_client):
        self.llm = llm_client
        self.llm_available = True

    def _clean_json_response(self, text: str) -> str:
        """Sanitizes LLM output."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z]*\n", "", cleaned)
            cleaned = re.sub(r"\n```$", "", cleaned)
        return cleaned.strip()

    def _extract_constraint_details(self, error_log: str) -> list:
        """
        Extracts the full constraint strings with versions.
        Example output: ['langchain-core<0.2', 'langchain==0.1.0']
        """
        constraints = []
        # Pattern: Package name followed by version operator and version number
        # Captures the whole string like "numpy<2.0" or "pandas==1.5.3"
        pattern = re.compile(r"([a-zA-Z0-9\-_]+(?:==|>=|<=|~=|!=|<|>)[0-9\.]+)")
        
        for match in pattern.finditer(error_log):
            c = match.group(1)
            # Filter out noise
            if not any(x in c for x in ['python', 'setup', 'pip']):
                constraints.append(c)
        
        return list(set(constraints))

    def summarize_error(self, error_message: str) -> str:
        """
        Generates a summary.
        GUARANTEE: If the LLM is vague, we append the raw extracted version constraints.
        """
        # 1. Deterministic Extraction of Versions
        raw_constraints = self._extract_constraint_details(error_message)
        constraints_str = ", ".join(raw_constraints[:5]) # Top 5 to avoid clutter

        if not self.llm_available:
            return f"Dependency Conflict. Constraints: {constraints_str}"

        # 2. LLM Summary
        prompt = (
            "Summarize the root cause of this dependency conflict in one sentence. "
            f"Context: {error_message[:2000]}"
        )
        try:
            llm_summary = self.llm.generate_content(prompt).text.strip().replace('\n', ' ')
            
            # 3. The "Truth Enforcement" Append
            # If the LLM didn't mention specific numbers, we force them in.
            return f"{llm_summary} [Identified Constraints: {constraints_str}]"
        except:
            return f"Dependency Conflict. [Identified Constraints: {constraints_str}]"

    def diagnose_conflict_from_log(self, error_log: str) -> list[str]:
        """
        Extracts ALL conflicting package names using "Scorched Earth" Regex.
        PURE DETERMINISTIC: No LLM fallback.
        """
        found_packages = set()
        
        # 1. Standard: "pkg==1.0", "pkg>=1.0"
        pattern_std = re.compile(r"(?P<name>[a-zA-Z0-9\-_]+)(?:==|>=|<=|~=|!=|<|>)")
        
        # 2. Loose: "pkg 1.0" (Space separated)
        pattern_space = re.compile(r"(?P<name>[a-zA-Z0-9\-_]+)\s+\d+(?:\.\d+)+")
        
        # 3. Parentheses: "pkg (1.0)" - Common in some pip versions
        pattern_paren = re.compile(r"(?P<name>[a-zA-Z0-9\-_]+)\s*\(\d+(?:\.\d+)+\)")

        for pat in [pattern_std, pattern_space, pattern_paren]:
            for match in pat.finditer(error_log):
                name = match.group('name').lower()
                if self._is_valid_package_name(name): found_packages.add(name)

        # 4. Contextual Search: Grab words near "conflict", "requirement", "depends"
        #    This catches packages listed in sentences like "Conflict between A, B, and C"
        context_keywords = [
            r"conflict(?:s)?\s+(?:between|among|with|in)\s+((?:[a-zA-Z0-9\-_]+(?:,?\s+and\s+|,?\s*)?)+)",
            r"requirement\s+((?:[a-zA-Z0-9\-_]+)+)",
        ]
        
        for keyword_pat in context_keywords:
            for match in re.finditer(keyword_pat, error_log, re.IGNORECASE):
                # The capture group contains the list of packages (e.g. "pkg-a, pkg-b, and pkg-c")
                raw_list = match.group(1)
                tokens = re.split(r'[,\s]+', raw_list)
                for t in tokens:
                    clean_t = t.strip("`'").lower()
                    if self._is_valid_package_name(clean_t):
                        found_packages.add(clean_t)

        if '-' in found_packages: found_packages.remove('-')
        
        return list(found_packages)

    def _is_valid_package_name(self, name: str) -> bool:
        noise = {'python', 'pip', 'setuptools', 'wheel', 'setup', 'dependencies', 
                 'versions', 'requirement', 'conflict', 'between', 'and', 'the', 'version', 'package'}
        return name and len(name) > 1 and name not in noise

    def propose_co_resolution(
        self, target_package: str, error_log: str, available_updates: dict,
        current_versions: dict = None, history: list = None
    ) -> dict | None:
        """
        Iterative Co-Resolution Planner with Chain-of-Thought Reasoning.
        """
        if not self.llm_available: return None

        floor_constraints = json.dumps(current_versions, indent=2) if current_versions else "{}"
        ceiling_constraints = json.dumps(available_updates, indent=2)

        history_text = ""
        if history:
            history_text = "--- PREVIOUS FAILED ATTEMPTS ---\n"
            for i, (attempt_plan, failure_reason) in enumerate(history):
                history_text += f"Attempt {i+1} Plan: {attempt_plan}\nResult: FAILED. Reason: {failure_reason}\n\n"

        # --- THE UPGRADED "CHAIN OF THOUGHT" PROMPT ---
        prompt = f"""
        You are CORE (Constraint Optimization & Resolution Expert).
        You are solving a hard dependency deadlock for '{target_package}'.

        CONTEXT:
        1. Target Package: {target_package}
        2. Current Versions (Floor): {floor_constraints}
        3. Available Updates (Ceiling): {ceiling_constraints}

        ERROR LOG:
        {error_log}

        {history_text}

        YOUR MISSION:
        You must find a combination of versions that satisfies the conflict.
        
        INSTRUCTIONS - THINK STEP-BY-STEP:
        1. ANALYZE CONSTRAINTS: Look at the error log. Identify exactly which package is demanding which version (e.g. "PkgA requires PkgB < 2.0").
        2. COMPARE WITH OPTIONS: Look at the 'Available Updates'.
           - If the log says "Requires B < 2.0" and Available Updates has "B": "3.0", you MUST NOT use the new B. You must hold B back.
           - If the log says "Requires C >= 1.5" and Available Updates has "C": "1.8", you SHOULD use the new C.
        3. FORMULATE PLAN: Construct a list of 'package==version' strings.
           - You can pick the "Ceiling" version (Update).
           - You can pick the "Floor" version (Hold Back).
           - You CANNOT invent versions not in Floor or Ceiling.

        RESPONSE FORMAT:
        You must output your reasoning first, followed by the JSON block.
        
        Reasoning: [Your step-by-step logic here]
        ```json
        {{
            "plausible": true,
            "proposed_plan": ["package==version", ...]
        }}
        ```
        """

        try:
            response = self.llm.generate_content(prompt)
            # We now need to extract the JSON from the potential text block
            text = response.text
            
            # Robust Extraction: Find the last JSON code block or the first JSON object
            json_match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
            if not json_match:
                # Fallback: look for just braces
                json_match = re.search(r'(\{.*\})', text, re.DOTALL)
            
            if not json_match: return None
            
            plan_json_str = json_match.group(1)
            plan = json.loads(plan_json_str)
            
            # Validation Logic (Same as before)
            if plan.get("plausible") and isinstance(plan.get("proposed_plan"), list):
                valid_plan = []
                for requirement in plan.get("proposed_plan", []):
                    try:
                        pkg, ver = requirement.split('==')
                        if (pkg in available_updates and available_updates[pkg] == ver) or \
                           (current_versions and pkg in current_versions and current_versions[pkg] == ver):
                            valid_plan.append(requirement)
                    except ValueError: continue
                
                if not valid_plan: return {"plausible": False, "proposed_plan": []}
                plan["proposed_plan"] = valid_plan
                return plan
            return None
        except Exception as e:
            print(f"  -> LLM_ERROR: {e}")
            return None