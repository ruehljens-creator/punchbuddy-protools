import subprocess

def get_pt_pid_nsworkspace():
    try:
        from AppKit import NSWorkspace
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            if app.localizedName() and "Pro Tools" in app.localizedName():
                print(f"NSWorkspace found: {app.localizedName()} with PID {app.processIdentifier()}")
    except Exception as e:
        print(f"NSWorkspace failed: {e}")

def get_pt_pid_pgrep():
    try:
        out = subprocess.run(["pgrep", "-x", "Pro Tools"], capture_output=True, text=True).stdout.strip()
        print(f"pgrep -x 'Pro Tools' returned:\n{out}")
    except Exception as e:
        print(f"pgrep failed: {e}")

if __name__ == "__main__":
    get_pt_pid_nsworkspace()
    get_pt_pid_pgrep()
