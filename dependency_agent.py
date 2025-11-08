# dependency_agent.py (The Final, Correctly Configured Version for requests)

import os
import sys
import google.generativeai as genai
from agent_logic import DependencyAgent

AGENT_CONFIG = {
    # 1. Point to the correct, non-generated requirements file for requests
    "REQUIREMENTS_FILE": "requirements-dev.txt",
    
    # 2. Use the simple, robust "script" validation for this baseline
    "VALIDATION_CONFIG": {
        "type": "smoke_test_with_pytest_report",
        "smoke_test_script": "validation_smoke_requests.py",
        "pytest_target": "tests",
        "project_dir": "requests" 
    },
    
    # 3. All other standard settings
    "PRIMARY_REQUIREMENTS_FILE": "primary_requirements.txt",
    "METRICS_OUTPUT_FILE": "metrics_output.txt",
    "MAX_LLM_BACKTRACK_ATTEMPTS": 3,
    "MAX_RUN_PASSES": 5,
    "ACCEPTABLE_FAILURE_THRESHOLD": 5
}

if __name__ == "__main__":
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    if not GEMINI_API_KEY:
        GEMINI_API_KEY='AIzaSyDbXGtg7X-Acn1nGABx7yHk5cGRtzy_dIY'
    
    genai.configure(api_key=GEMINI_API_KEY)
    llm_client = genai.GenerativeModel('gemini-2.5-flash')

    agent = DependencyAgent(config=AGENT_CONFIG, llm_client=llm_client)
    agent.run()




