# expert_agent.py

import re
import json
import ast
from google.api_core.exceptions import ResourceExhausted

class ExpertAgent:
    """
    The "Expert" Agent (CORE). A pure reasoning engine that handles all
    interactions with the Large Language Model.
    """
    def __init__(self, llm_client):
        self.llm = llm_client
        self.llm_available = True

    def _extract_key_constraints(self, error_log: str) -> list:
        """A simple helper to find the most important lines in a pip error log."""
        key_lines = []
        # This regex looks for lines that explicitly state a requirement.
        # e.g., "pandas 2.2.0 requires numpy<2,>=1.22.4"
        pattern = re.compile(r"^\s*([a-zA-Z0-9\-_]+.* requires .*)$", re.MULTILINE)
        for match in pattern.finditer(error_log):
            key_lines.append(match.group(1).strip())
        
        # Limit to the 5 most relevant lines to keep the prompt clean.
        return key_lines[:5]

    def summarize_error(self, error_message: str) -> str:
        """Generates a concise, one-sentence summary of a pip error log."""
        if not self.llm_available: return "(LLM summary unavailable)"
        
        # --- IMPROVEMENT: Use the key constraints for a better summary ---
        key_constraints = self._extract_key_constraints(error_message)
        if key_constraints:
            context = "Key constraints found:\n" + "\n".join(key_constraints)
        else:
            context = f"Full Error Log: --- {error_message} ---"

        prompt = (
            "Summarize the root cause of the following Python dependency conflict "
            "in a single, concise sentence. Focus on the package names and versions. "
            f"Context: {context}"
        )
        try:
            response = self.llm.generate_content(prompt)
            return response.text.strip().replace('\n', ' ')
        except Exception as e:
            print(f"  -> LLM_ERROR: Could not get summary: {e}")
            return "Failed to get summary from LLM."

    # In expert_agent.py

    def propose_co_resolution(self, target_package: str, error_log: str, available_updates: dict) -> dict | None:
        """
        Analyzes a complex conflict using the full error log and proposes a 
        multi-package "moonshot" update from a list of available versions.
        """
        if not self.llm_available: return None

        update_options_str = json.dumps(available_updates, indent=2)

        prompt = f"""
        You are an expert Python dependency conflict resolver named CORE.
        Your task is to analyze a failed dependency update and determine if a
        multi-package co-resolution is a plausible solution.
        Respond ONLY with a valid JSON object with the keys "plausible" (boolean) and "proposed_plan" (a list of 'package==version' strings).

        ANALYSIS CONTEXT:
        1.  The primary goal was to update the package: '{target_package}'.
        2.  The attempt failed. The full error log from pip's resolver is below. This log contains the deep, transitive dependency conflicts.
            --- FULL ERROR LOG ---
            {error_log}
            --- END FULL ERROR LOG ---
        3.  The following packages have updates available. Your proposed plan MUST ONLY use package names and versions from this list.
            --- AVAILABLE UPDATES (ORACLE METADATA) ---
            {update_options_str}
            --- END AVAILABLE UPDATES ---

        YOUR TASK:
        Carefully analyze the FULL ERROR LOG to understand the root cause of the conflict. Based on your analysis, is it plausible that updating one or more of the packages from the AVAILABLE UPDATES list *along with* '{target_package}' would solve the conflict?
        
        If it is plausible, construct the most likely plan. The plan must include the original '{target_package}' (with its version from the AVAILABLE UPDATES list) and any other necessary packages from the list.

        If you cannot determine a plausible path from the log, or if the log indicates the conflict is with a package that cannot be updated, respond that it is not plausible.
        """

        try:
            response = self.llm.generate_content(prompt)
            match = re.search(r'\{.*\}', response.text, re.DOTALL)
            if not match:
                print(f"  -> LLM_WARNING: Co-resolution response was not valid JSON: {response.text}")
                return None
            
            plan = json.loads(match.group(0))
            if isinstance(plan.get("plausible"), bool) and isinstance(plan.get("proposed_plan"), list):
                # Final check to ensure the LLM didn't hallucinate a version or package
                for requirement in plan.get("proposed_plan", []):
                    try:
                        pkg, ver = requirement.split('==')
                        if pkg not in available_updates or ver != available_updates[pkg]:
                            print(f"  -> LLM_WARNING: Plan contains a hallucinated or incorrect version: {requirement}")
                            return {"plausible": False, "proposed_plan": []}
                    except ValueError:
                         print(f"  -> LLM_WARNING: Plan contains an invalid requirement format: {requirement}")
                         return {"plausible": False, "proposed_plan": []}
                return plan
            return None
        except (json.JSONDecodeError, AttributeError, Exception) as e:
            print(f"  -> LLM_ERROR: Could not get/parse co-resolution plan: {e}")
            return None

    def diagnose_conflict_from_log(self, error_log: str) -> list[str]:
        """
        Analyzes a pip error log and returns a structured list of conflicting package names.
        """
        if not self.llm_available:
            return []

        prompt = f"""
        You are an expert Python dependency conflict analyst. Your task is to read a pip
        error log and identify the specific, root-cause package names that are in conflict.

        Respond ONLY with a valid, clean JSON list of strings.

        EXAMPLE 1:
        Log: "ERROR: Cannot install -r file.txt (line 16) and google-api-core==1.34.0 because..."
        Response: ["google-api-core", "google-generativeai"]

        EXAMPLE 2:
        Log: "ERROR: a==1.0 requires b<2.0, but you have b==2.5"
        Response: ["a", "b"]

        Here is the error log to analyze:
        --- ERROR LOG ---
        {error_log}
        --- END ERROR LOG ---
        """
        try:
            response = self.llm.generate_content(prompt)
            match = re.search(r'\[.*\]', response.text, re.DOTALL)
            if not match:
                print(f"  -> LLM_WARNING: Diagnose conflict response was not valid JSON list: {response.text}")
                return []
            
            package_list = json.loads(match.group(0))
            if isinstance(package_list, list) and all(isinstance(p, str) for p in package_list):
                return package_list
            return []
        except (json.JSONDecodeError, AttributeError, Exception) as e:
            print(f"  -> LLM_ERROR: Could not get/parse conflict diagnosis: {e}")
            return []
        
    