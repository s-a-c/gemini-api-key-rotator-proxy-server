#!/bin/bash

# --- CONFIGURATION ---
PROXY_PORT=8888
PROXY_HOST="127.0.0.1"
# Ensure the proxy script and keys file exist in the current directory
PROXY_SCRIPT="main.py"
KEYS_FILE="api_keys.txt"

echo "🚀 Starting Antigravity Proxy Environment..."

# 1. Check for API keys
if [ ! -f "$KEYS_FILE" ]; then
    echo "❌ Error: $KEYS_FILE not found. Please create it and add your Gemini keys."
    exit 1
fi

# 2. Start the Proxy Server in the background
# We use nohup or & to keep it running while the script continues
python3 -m uvicorn main:APP --host $PROXY_HOST --port $PROXY_PORT > proxy.log 2>&1 &
PROXY_PID=$!

# Give the proxy a moment to initialize
sleep 2

# 3. Verify Proxy is up
if ps -p $PROXY_PID > /dev/null; then
    echo "✅ Proxy started successfully (PID: $PROXY_PID) on http://$PROXY_HOST:$PROXY_PORT"
else
    echo "❌ Proxy failed to start. Check proxy.log for details."
    exit 1
fi

# 4. Export Antigravity Environment Variables
# We point the IDE to our local proxy instead of Google's direct URL
export GEMINI_API_BASE_URL="http://$PROXY_HOST:$PROXY_PORT/v1beta"
export GEMINI_API_KEY="proxy_managed_session" # The proxy ignores this and uses the pool

echo "🛠️ Environment variables set. Launching Antigravity IDE..."

# 5. Launch Antigravity (replace 'antigravity' with your actual IDE binary command)
antigravity

# 6. Cleanup: When you close Antigravity, kill the proxy server
echo "Shutting down proxy..."
kill $PROXY_PID
