import re

with open('auto_punch_in.py', 'r') as f:
    content = f.read()

# We want to replace everything from `def _open_settings_window(self):`
# to the end of `class _SettingsButtonTarget(AppKit.NSObject): ...`

import os
