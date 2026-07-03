"""EXE entry point — launches the desktop workbench directly, no CLI parsing."""
from visual_agent.workbench_app import launch_desktop_app
import sys

if __name__ == "__main__":
    sys.exit(launch_desktop_app())
