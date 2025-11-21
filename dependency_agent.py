# dependency_agent.py (The Final, Correctly Configured Version for GRAPHITE)

import os
import sys
import google.generativeai as genai

# Import the two agents that form our final architecture
from agent_logic import DependencyAgent
from expert_agent import ExpertAgent

# --- This is the definitive, simplified, and correct configuration for the GRAPHITE project ---
AGENT_CONFIG = {
    # A unique name for the project.
    "PROJECT_NAME": "graphite",

    # This is the "Golden Record" lock file that the agent will manage.
    "REQUIREMENTS_FILE": "requirements.txt",
    
    "METRICS_OUTPUT_FILE": "metrics_output.txt",
    "PRIMARY_REQUIREMENTS_FILE": "primary_requirements.txt",

    # This configuration tells our universal agent_utils.py how to validate this specific project.
    "VALIDATION_CONFIG": {
        "type": "script",
        # It correctly points to our dedicated validation script for this project.
        "smoke_test_script": "validation_graphite.py",
    },
    
    # All other standard settings
    "MAX_RUN_PASSES": 3,
}

if __name__ == "__main__":
    # --- This is the standard, reusable loader logic for our multi-agent system ---
    
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    if not GEMINI_API_KEY:
        sys.exit("CRITICAL ERROR: GEMINI_API_KEY environment variable not set.")
    
    genai.configure(api_key=GEMINI_API_KEY)
    llm_client = genai.GenerativeModel('gemini-2.5-flash')

    # Initialize the Manager (AURA) by passing it the config and the LLM client.
    # The agent's __init__ method will handle creating its own Expert assistant.
    agent = DependencyAgent(config=AGENT_CONFIG, llm_client=llm_client)
    agent.run()