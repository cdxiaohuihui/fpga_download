import os
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import fpga_flash_download


def main():
    os.chdir(fpga_flash_download.ROOT)
    if os.name == "nt":
        os.system("chcp 65001 >nul")
        fpga_flash_download.enable_windows_ansi()
    return fpga_flash_download.stop_persistent_server()


if __name__ == "__main__":
    raise SystemExit(main())
