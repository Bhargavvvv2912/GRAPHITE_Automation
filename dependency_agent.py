# dependency_agent.py

import os
import sys
import google.generativeai as genai
from agent_logic import DependencyAgent

# --- Configuration ---
AGENT_CONFIG = {
    "REQUIREMENTS_FILE": "requirements.txt",
    "PRIMARY_REQUIREMENTS_FILE": "primary_requirements.txt",
    "METRICS_OUTPUT_FILE": "metrics_output.txt",
    "MAX_LLM_BACKTRACK_ATTEMPTS": 3,
    "MAX_RUN_PASSES": 3,
}

if __name__ == "__main__":
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    if not GEMINI_API_KEY:
        sys.exit("Error: GEMINI_API_KEY environment variable not set.")
    
    genai.configure(api_key=GEMINI_API_KEY)
    llm_client = genai.GenerativeModel('gemini-1.5-flash')

    agent = DependencyAgent(config=AGENT_CONFIG, llm_client=llm_client)
    agent.run()