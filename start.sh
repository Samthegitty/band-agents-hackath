#!/bin/bash

# 1. Dynamically generate agent_config.yaml from Railway Environment Variables
cat > agent_config.yaml << EOF
qs_orchestrator:
  agent_id: "${BAND_ORCHESTRATOR_AGENT_ID}"
  api_key:  "${BAND_ORCHESTRATOR_API_KEY}"

qs_assessment:
  agent_id: "${BAND_ASSESSMENT_AGENT_ID}"
  api_key:  "${BAND_ASSESSMENT_API_KEY}"

qs_curator:
  agent_id: "${BAND_CURATOR_AGENT_ID}"
  api_key:  "${BAND_CURATOR_API_KEY}"

qs_studyplan:
  agent_id: "${BAND_STUDYPLAN_AGENT_ID}"
  api_key:  "${BAND_STUDYPLAN_API_KEY}"
EOF

echo "✅ Generated agent_config.yaml from environment variables."

# 2. Start the agents in the background
python main.py &

# 3. Start the web server in the foreground (Railway needs this to detect the port)
python web/backend/server.py