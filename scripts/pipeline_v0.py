"""기존 실행 명령을 유지하는 진입점. 구현: pudo/pipeline.py"""

import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from pudo.pipeline import main

    main()
