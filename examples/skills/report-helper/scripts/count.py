import sys
from pathlib import Path

print(len(Path(sys.argv[1]).read_text()))
