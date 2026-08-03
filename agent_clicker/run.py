"""Convenience launcher. Run: python run.py"""
import os
import socket

from app.server import app

PHONE_BRIDGE_PORT = int(os.environ.get("AIOS_PHONE_BRIDGE_PORT", "5000"))

if __name__ == "__main__":
    # Recovery belongs to the backend owner, never to utility/test imports of
    # code_jobs. Import-time recovery can misclassify a live job owned by this
    # process as orphaned.
    import code_jobs
    code_jobs.recover_interrupted()
    try:
        lan_ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        lan_ip = "your-computer-ip"
    print(f"Agent Clicker -> http://127.0.0.1:{PHONE_BRIDGE_PORT}")
    print(f"Phone (LAN)  -> http://{lan_ip}:{PHONE_BRIDGE_PORT}/phone")
    print(f"Phone (away) -> set app to http://YOUR_PUBLIC_IP:{PHONE_BRIDGE_PORT}")
    app.run(host="0.0.0.0", port=PHONE_BRIDGE_PORT, debug=False, threaded=True)
