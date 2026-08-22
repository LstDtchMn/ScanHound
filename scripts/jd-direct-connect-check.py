"""Does JDownloader accept a DIRECT LAN connection from the container?

WHY
---
ScanHound reaches JDownloader through MyJDownloader's cloud at
api.jdownloader.org -- container -> internet -> another continent -> back down
to a JDownloader on the same Windows host. That path produced 11 outages in 27
hours (13 read-timeouts, 5 "Network is unreachable", 2 dropped sessions).

JDownloader's own settings show Direct Connect Mode = "Only allow direct
connections from lan", and its ACTUAL listener is on the port reported as
`Last Local Port` (61133 at the time of writing), NOT the `Manual Local Port`
of 3129. Probing 3128/3129 is what made this look disabled for most of a day.

Confirmed reachable from inside the container:
    host.docker.internal:61133  OPEN
    192.168.1.170:61133         OPEN

This script establishes whether myjdapi can actually USE that path.

WHAT IT DOES
------------
  1. reads ScanHound's existing JD credentials from its own config
  2. logs in to the MyJD cloud (needed once, for auth + endpoint discovery)
  3. calls enable_direct_connection() and reports the endpoints JD advertises
  4. makes ONE harmless read-only query and times it

WHAT IT DOES NOT DO
-------------------
  * does NOT add, start, stop or modify any download
  * does NOT write ScanHound's config or database
  * does NOT change any JDownloader setting
  * prints NO credential values

Note on `direct_connect()`: myjdapi has a separate `direct_connect(ip, port)`
that sets connection type "remoteapi". That is the DEPRECATED JD RemoteAPI on
3128, a different protocol, and not what we want. The correct path is a normal
`connect()` followed by `enable_direct_connection()`.

USAGE (run on the server; it executes inside the scanhound container)

    python scripts/jd-direct-connect-check.py
"""
import json
import os
import subprocess
import sys

CONTAINER = "scanhound"

PROBE = r'''
import json, sys, time
sys.path.insert(0, "/app")

cfg = json.load(open("/data/.config/scanhound/config.json"))
email = cfg.get("jd_email") or ""
password = cfg.get("jd_password") or ""
device_name = cfg.get("jd_device") or ""
if not email or not password:
    print("FAIL no MyJDownloader credentials in config"); raise SystemExit(1)

import myjdapi
jd = myjdapi.Myjdapi()
jd.set_app_key("ScanHound-DirectConnectCheck")

# Same timeout ScanHound now uses, so this is not a more forgiving test.
try:
    jd._Myjdapi__timeout = int(cfg.get("jd_api_timeout_seconds") or 20)
except Exception:
    pass

t0 = time.monotonic()
jd.connect(email, password)
jd.update_devices()
print("OK  cloud login            %.2fs   connection_type=%s"
      % (time.monotonic() - t0, jd.get_connection_type()))

device = jd.get_device(device_name) if device_name else jd.list_devices()[0]
print("OK  device                 %s" % device_name)

# --- baseline: one query over the CLOUD -----------------------------------
t0 = time.monotonic()
try:
    device.downloadcontroller.get_current_state()
    cloud_ms = (time.monotonic() - t0) * 1000
    print("OK  query via CLOUD        %.0f ms" % cloud_ms)
except Exception as exc:
    cloud_ms = None
    print("--  query via CLOUD        FAILED %s" % str(exc)[:60])

# --- enable direct, and see what JD advertises -----------------------------
device.enable_direct_connection()
infos = getattr(device, "_Jddevice__direct_connection_info", None)
if not infos:
    print("FAIL JDownloader advertised NO direct endpoints.")
    print("     Direct Connect Mode may be off, or JD cannot see the container")
    print("     as being on the LAN. Nothing changes in ScanHound.")
    raise SystemExit(2)

print("OK  direct endpoints advertised by JDownloader:")
for entry in infos:
    conn = entry.get("conn", entry)
    print("      %s:%s" % (conn.get("ip"), conn.get("port")))

# --- the same query again, now that direct is enabled ----------------------
t0 = time.monotonic()
try:
    device.downloadcontroller.get_current_state()
    direct_ms = (time.monotonic() - t0) * 1000
    print("OK  query with DIRECT on   %.0f ms" % direct_ms)
except Exception as exc:
    print("FAIL query with DIRECT on  %s" % str(exc)[:70])
    print("     JD advertised an endpoint but the call did not succeed through")
    print("     it. Do NOT wire this into ScanHound.")
    raise SystemExit(3)

print()
if cloud_ms and direct_ms < cloud_ms * 0.6:
    print("VERDICT  direct connection WORKS and is materially faster")
    print("         (%.0f ms vs %.0f ms). Worth wiring in." % (direct_ms, cloud_ms))
else:
    print("VERDICT  direct connection works, but is not obviously faster")
    print("         (%.0f ms vs %s ms). Still worth wiring in: the point is that"
          % (direct_ms, "%.0f" % cloud_ms if cloud_ms else "n/a"))
    print("         it removes the internet from the path, not that it is quick.")
print()
print("NOTE     the cloud is still needed ONCE per process, to authenticate and")
print("         to discover these endpoints. What changes is that each GRAB no")
print("         longer crosses the internet.")
'''


def main():
    print(__doc__.split("USAGE")[0].rstrip())
    print("=" * 66)
    print()

    probe = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         ".jd_direct_probe.py")
    with open(probe, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(PROBE)
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    try:
        subprocess.run(["docker", "cp", probe,
                        "%s:/tmp/jd_direct_probe.py" % CONTAINER],
                       capture_output=True, env=env)
    finally:
        os.remove(probe)

    r = subprocess.run(
        ["docker", "exec", "-w", "/app", CONTAINER,
         "python", "/tmp/jd_direct_probe.py"],
        capture_output=True, text=True, env=env)
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        print("  " + line)
    if r.returncode != 0:
        print()
        print("  Exit %d -- direct connection is NOT usable as configured." % r.returncode)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
