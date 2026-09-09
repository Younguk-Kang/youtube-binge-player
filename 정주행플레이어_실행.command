#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

# Check if port 54321 is already in use
if ! lsof -i:54321 > /dev/null 2>&1; then
    echo "Starting YouTube Binge Player Server..."
    nohup python3 server.py > /dev/null 2>&1 &
    sleep 1
fi

echo "Opening Player in browser..."
open "http://localhost:54321/youtube_player.html"
exit 0
