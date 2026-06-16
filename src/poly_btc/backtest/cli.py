"""Launcher: `poly-btc-app` runs the Streamlit UI bound to 0.0.0.0:8501."""
from __future__ import annotations

import os
import sys

from streamlit.web import cli as stcli


def run_streamlit() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    app_path = os.path.join(here, "app.py")
    sys.argv = [
        "streamlit", "run", app_path,
        "--server.address=0.0.0.0",
        f"--server.port={os.environ.get('STREAMLIT_PORT', '8501')}",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
    ]
    sys.exit(stcli.main())
