#!/bin/bash
# Setup script cho AI20K project

set -e

echo "=== AI20K Project Setup ==="

# Check Python version
python3 -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ required'"
echo "Python version OK"

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install the core runtime and development/test tooling. AI model dependencies are
# intentionally opt-in; see docs/setup_pc.md.
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt

# Create .env if not exists
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env — please edit with your API keys"
fi

# Create data directories
mkdir -p data/handbooks

echo "Setup complete! Run: python run.py"
echo "For STT/TTS/model setup, continue with docs/setup_pc.md"
