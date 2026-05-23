"""Convenience launcher. Run: python run.py"""
from app.server import app

if __name__ == "__main__":
    print("Agent Clicker -> http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
