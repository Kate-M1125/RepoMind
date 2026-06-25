#!/bin/bash
cd /Users/briangao/CodeSpace/RepoMind
source .venv/bin/activate
exec uvicorn backend.app:app --host 0.0.0.0 --port 8000
