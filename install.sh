#!/bin/bash

# 1. Install React/Tauri frontend dependencies
npm install

# 2. Create isolated Python environment and install backend dependencies
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cd ..

echo "Install complete. Ready to launch."