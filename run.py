#!/usr/bin/env python3
"""Entry point — start the Ghost in the Droid server (FastAPI + Uvicorn)."""
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / '.env')

import uvicorn
from gitd.app import app

if __name__ == '__main__':
    # This service is a local control surface for a user's Android devices;
    # do not expose ADB/automation endpoints to the LAN by default.
    uvicorn.run(app, host='127.0.0.1', port=5055)
