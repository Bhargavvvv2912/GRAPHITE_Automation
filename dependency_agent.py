# dependency_agent.py (The Final, Simplified, and Correct Version for requests)

import os
import sys
import google.generativeai as genai
from agent_logic import DependencyAgent
from expert_agent import ExpertAgent # Ensure expert_agent.py is in the same directory

AGENT_CONFIG = {
    "PROJECT_NAME": "requests",
    
    # This is the "Golden Record" that the agent will manage and commit.
    "REQUIREMENTS_FILE": "requirements-dev.txt",
    
    "VALIDATION_CONFIG": {
        "type": "smoke_test_with_pytest_report",
        "smoke_test_script": "validation_smoke_requests.py",
        "pytest_target": "tests",
        "project_dir": "requests_repo" 
    },
    
    # All other standard settings
    "METRICS_OUTPUT_FILE": "metrics_output.txt",
    "MAX_RUN_PASSES": 5,
    "ACCEPTABLE_FAILURE_THRESHOLD": 5
}

if __name__ == "__main__":
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    if not GEMINI_API_KEY:
        sys.exit("CRITICAL ERROR: GEMINI_API_KEY environment variable not set.")
    
    genai.configure(api_key=GEMINI_API_KEY)
    llm_client = genai.GenerativeModel('gemini-2.5-flash')

    agent = DependencyAgent(config=AGENT_CONFIG, llm_client=llm_client)
    agent.run()