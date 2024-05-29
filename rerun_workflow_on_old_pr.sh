#!/bin/bash

# Get the personal access token from the environment variable
GITHUB_TOKEN="YOUR_CLASSIC_PERSONAL_ACCESS_TOKEN"
# $ADABOT_GITHUB_ACCESS_TOKEN"

# workflow id
WORKFLOW_ID="1388947"

# Construct the URL for the API endpoint
URL="https://api.github.com/repos/adafruit/Adafruit_MLX90393_Library/actions/workflows/$WORKFLOW_ID/dispatches"

# Construct the JSON payload for the request
PAYLOAD=$(cat <<EOF
{
  "ref": "pulls/24/head"
}
EOF
)

# Send the POST request to trigger the workflow run
curl -X POST \
     -H "Authorization: token $GITHUB_TOKEN" \
     -d "$PAYLOAD" \
     "$URL"

# Check the response status code
if [ $? -eq 0 ]; then
  echo "Workflow run triggered successfully."
else
  echo "Failed to trigger workflow run. Status code: $?"
fi