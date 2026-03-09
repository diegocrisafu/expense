"""Launch Roger's dashboard standalone."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from polymarket_scanner.dashboard import start_web_dashboard
import time

PORT = 8080
print(f"🤖 Roger the Polymarket Bot — Dashboard")
print(f"   Open http://localhost:{PORT} in your browser\n")
start_web_dashboard(port=PORT, db_path="polymarket_scanner.db")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\nRoger says goodbye! 👋")
