#!/usr/bin/env bash

echo "Initializing the environment..."

python -m venv .venv
source .venv/bin/activate

echo "Installing required packages..."

pip install -qr requirements.txt

echo "Done."

echo "Starting the application..."

source ./run.sh
