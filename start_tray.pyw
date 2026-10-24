import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
os.chdir(root)
sys.path.insert(0, str(root))

from tray_app import main

raise SystemExit(main())
