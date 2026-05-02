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

APP_DIR = Path(__file__).resolve().parent

# Activate the local .venv if present (Hostinger shared hosting without hPanel Python app)
_venv_activate = APP_DIR / ".venv" / "bin" / "activate_this.py"
if _venv_activate.exists():
    exec(open(_venv_activate).read(), {"__file__": str(_venv_activate)})
else:
    # Ensure site-packages from local .venv are on sys.path
    import glob
    for _sp in glob.glob(str(APP_DIR / ".venv" / "lib" / "python*" / "site-packages")):
        if _sp not in sys.path:
            sys.path.insert(0, _sp)

# Ensure the app directory is on sys.path.
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from dotenv import load_dotenv
load_dotenv(APP_DIR / ".env")

from app import create_app

application = create_app()
