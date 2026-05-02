"""
passenger_wsgi.py – Hostinger Passenger/WSGI entry point.

Hostinger's Python app manager looks for a file called passenger_wsgi.py
in the application root and expects a variable named `application`.

Setup steps on Hostinger hPanel:
  1. Go to hPanel → Hosting → your domain → Python.
  2. Create a Python app, set:
       Python version : 3.12
       Application root: /home/<user>/solix_performance
       Application URL : your domain or subdomain
       Application startup file: passenger_wsgi.py
       Application Entry point: application
  3. Open the virtualenv terminal and run:
       pip install -r requirements.txt
  4. Restart the app from hPanel.
"""
import sys
import os
from pathlib import Path

# Ensure the app directory is on sys.path.
APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from dotenv import load_dotenv
load_dotenv(APP_DIR / ".env")

from app import create_app

application = create_app()
