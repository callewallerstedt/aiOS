"""Convenience launcher. Run: python run.py"""
import socket

from app.server import app

if __name__ == "__main__":
    try:
        lan_ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        lan_ip = "your-computer-ip"
    print("Agent Clicker -> http://127.0.0.1:5000")
    print(f"Phone -> http://{lan_ip}:5000/phone")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
