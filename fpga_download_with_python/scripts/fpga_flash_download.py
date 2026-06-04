import configparser
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
FLOW_DIR = SCRIPT_DIR.parent
ROOT = FLOW_DIR.parent
RUNTIME_DIR = FLOW_DIR / "runtime"
CONFIG_PATH = FLOW_DIR / "config" / "device_config.ini"
SUCCESS_FLAG = ROOT / "flash_program_success.flag"
CONSOLE_LOG = ROOT / "vivado_console.log"
SERVER_TCL_PATH = SCRIPT_DIR / "vivado_persistent_server.tcl"
SERVER_DIR = RUNTIME_DIR / ".vivado_server"
SERVER_READY = SERVER_DIR / "ready.txt"

VIVADO_PORTABLE = ROOT / "xilinx" / "bin" / "vivado_lab.bat"
XILINX_DRIVES = ("C", "D", "E", "F")
XILINX_PRODUCTS = ("Vivado_Lab", "Vivado", "Vitis")
XILINX_EXECUTABLES = ("vivado_lab.bat", "vivado.bat")

RESET = "\033[0m"
COLORS = {
    "red": "\033[91m",
    "green": "\033[92m",
    "yellow": "\033[93m",
    "cyan": "\033[96m",
}

shown_friendly_error = False
active_progress = None
console_lock = threading.Lock()
ansi_enabled = True
vivado_started = False
boot_wait_reported = False
current_job_id = None


def enable_windows_ansi():
    global ansi_enabled
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            ansi_enabled = False
            return
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        ansi_enabled = False


def write(text="", color=None):
    with console_lock:
        if active_progress is not None:
            active_progress.clear_line()
        if color and ansi_enabled and os.environ.get("NO_COLOR") is None:
            print(f"{COLORS[color]}{text}{RESET}", flush=True)
        else:
            print(text, flush=True)


class ProgressBar:
    def __init__(self, label, width=28):
        self.label = label
        self.width = width
        self.index = 0
        self.running = False
        self.thread = None
        self.start_time = 0.0

    def start(self):
        global active_progress
        self.running = True
        self.start_time = time.monotonic()
        active_progress = self
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while self.running:
            self.index = (self.index + 1) % self.width
            left = max(0, self.index - 4)
            right = min(self.width, self.index + 4)
            bar = [" "] * self.width
            for pos in range(left, right):
                bar[pos] = "#"
            elapsed = int(time.monotonic() - self.start_time)
            bar_text = "".join(bar)
            with console_lock:
                print(f"\r      {self.label} [{bar_text}] {elapsed:3d}s", end="", flush=True)
            time.sleep(0.15)

    def clear_line(self):
        print("\r" + " " * (self.width + len(self.label) + 24) + "\r", end="", flush=True)

    def finish(self, message):
        global active_progress
        self.running = False
        if self.thread is not None:
            self.thread.join(timeout=1)
        elapsed = int(time.monotonic() - self.start_time)
        with console_lock:
            active_progress = None
            bar = "#" * self.width
            print(f"\r      {self.label} [{bar}] 完成，用时 {elapsed}s", flush=True)
        write(f"      当前进度：{message}", "green")


def start_progress(label):
    global active_progress
    stop_progress()
    progress = ProgressBar(label)
    active_progress = progress
    progress.start()


def progress_label():
    return active_progress.label if active_progress is not None else None


def stop_progress(message=None):
    global active_progress
    progress = active_progress
    if progress is None:
        return
    if message:
        progress.finish(message)
    else:
        progress.running = False
        if progress.thread is not None:
            progress.thread.join(timeout=1)
        with console_lock:
            active_progress = None
            progress.clear_line()


def report_boot_wait():
    global boot_wait_reported
    if boot_wait_reported:
        return
    boot_wait_reported = True
    write("      当前进度：正在等待 FPGA 启动完成并拉高 DONE 引脚...", "cyan")


def read_console_key():
    if os.name != "nt":
        return None
    try:
        import msvcrt

        if not msvcrt.kbhit():
            return None
        key = msvcrt.getwch()
        if key in ("\x00", "\xe0") and msvcrt.kbhit():
            msvcrt.getwch()
            return None
        return key
    except Exception:
        return None


def close_countdown(seconds):
    for remaining in range(seconds, 0, -1):
        with console_lock:
            print(
                f"\r下载成功，窗口将在 {remaining} 秒后自动关闭；按 q 退出 Vivado 常驻服务...",
                end="",
                flush=True,
            )

        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            key = read_console_key()
            if key is not None and key.lower() == "q":
                with console_lock:
                    print("\r" + " " * 86 + "\r", end="", flush=True)
                write("检测到 q，正在退出 Vivado Lab 常驻服务...", "yellow")
                stop_persistent_server()
                return
            time.sleep(0.05)

    with console_lock:
        print("\r" + " " * 86 + "\r", end="", flush=True)


def pause():
    os.system("pause")


def load_config():
    if not CONFIG_PATH.exists():
        write("错误：未找到 FPGA Flash 下载配置文件。", "red")
        write(f"缺少文件：{CONFIG_PATH}")
        write("请在 config 目录中提供 device_config.ini。")
        return None

    parser = configparser.ConfigParser()
    try:
        parser.read(CONFIG_PATH, encoding="utf-8")
    except configparser.Error as err:
        write("错误：配置文件解析失败。", "red")
        write(f"配置文件：{CONFIG_PATH}")
        write(f"原因：{err}")
        return None

    if not parser.has_section("hardware"):
        write("错误：配置文件缺少 [hardware] 节。", "red")
        write(f"配置文件：{CONFIG_PATH}")
        return None

    fpga_device = parser.get("hardware", "fpga_device", fallback="").strip()
    flash_part = parser.get("hardware", "flash_part", fallback="").strip()
    missing = []
    if not fpga_device:
        missing.append("fpga_device")
    if not flash_part:
        missing.append("flash_part")
    if missing:
        write("错误：配置文件缺少必要配置项。", "red")
        write(f"配置文件：{CONFIG_PATH}")
        write(f"缺少配置：{', '.join(missing)}")
        return None

    return {
        "fpga_device": fpga_device,
        "flash_part": flash_part,
    }


def find_vivado():
    if VIVADO_PORTABLE.exists():
        return VIVADO_PORTABLE

    for drive in XILINX_DRIVES:
        for product in XILINX_PRODUCTS:
            product_dir = Path(f"{drive}:\\") / "Xilinx" / product
            if not product_dir.exists():
                continue
            version_dirs = [path for path in product_dir.iterdir() if path.is_dir()]
            version_dirs.sort(key=lambda path: path.name, reverse=True)
            for version_dir in version_dirs:
                for executable in XILINX_EXECUTABLES:
                    candidate = version_dir / "bin" / executable
                    if candidate.exists():
                        return candidate

    return None


def is_process_alive(pid):
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
        if result.returncode == 0 and str(pid) in result.stdout:
            return True
    except Exception:
        pass

    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"if (Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue) {{ 'RUNNING' }}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
        return "RUNNING" in result.stdout
    except Exception:
        return False


def is_process_name_running(image_name):
    process_name = image_name[:-4] if image_name.lower().endswith(".exe") else image_name
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/NH"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
        if result.returncode == 0 and image_name.lower() in result.stdout.lower():
            return True
    except Exception:
        pass

    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"if (Get-Process -Name '{process_name}' -ErrorAction SilentlyContinue) {{ 'RUNNING' }}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
        return "RUNNING" in result.stdout
    except Exception:
        return False


def get_server_info():
    if not SERVER_READY.exists():
        return None, False
    try:
        expected_script_mtime = str(int(SERVER_TCL_PATH.stat().st_mtime))
        seen_script_mtime = None
        pid = None
        for line in SERVER_READY.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("pid="):
                pid = int(line.split("=", 1)[1].strip())
            elif line.startswith("script_mtime="):
                seen_script_mtime = line.split("=", 1)[1].strip()
        if pid is None or not is_process_alive(pid):
            return None, False
        return pid, seen_script_mtime == expected_script_mtime
    except Exception:
        return None, False


def get_server_pid():
    pid, is_current = get_server_info()
    return pid if is_current else None


def remove_stale_server_dir_if_vivado_lab_not_running():
    if not SERVER_DIR.exists():
        return

    pid, is_current = get_server_info()
    if pid is not None and is_current:
        return

    if is_process_name_running("vivado_lab.exe"):
        return

    write("检测到 Vivado Lab 未运行，正在清理上次遗留的常驻服务状态...", "yellow")
    shutil.rmtree(SERVER_DIR, ignore_errors=True)


def start_persistent_server(vivado_bin):
    SERVER_DIR.mkdir(parents=True, exist_ok=True)
    SERVER_READY.unlink(missing_ok=True)
    (SERVER_DIR / "stop.txt").unlink(missing_ok=True)
    server_log = SERVER_DIR / "server.log"

    write("正在启动 Vivado Lab 常驻服务，首次启动需要几秒钟...", "cyan")
    start_progress("启动 Vivado")
    with server_log.open("a", encoding="utf-8", newline="\n") as log_file:
        subprocess.Popen(
            [
                str(vivado_bin),
                "-mode",
                "batch",
                "-notrace",
                "-journal",
                str(SERVER_DIR / "server.jou"),
                "-log",
                str(SERVER_DIR / "server_vivado.log"),
                "-source",
                str(SERVER_TCL_PATH),
                "-tclargs",
                str(RUNTIME_DIR),
            ],
            cwd=ROOT,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        pid = get_server_pid()
        if pid is not None:
            stop_progress("Vivado Lab 常驻服务已启动。")
            return pid
        time.sleep(0.2)

    stop_progress()
    write("错误：Vivado Lab 常驻服务启动超时。", "red")
    write(f"请查看日志：{server_log}", "yellow")
    return None


def ensure_persistent_server(vivado_bin):
    pid, is_current = get_server_info()
    if pid is not None and is_current:
        write(f"复用已启动的 Vivado Lab 常驻服务，PID={pid}。", "cyan")
        return pid
    if pid is not None:
        write("检测到旧版 Vivado Lab 常驻服务，正在重启以加载最新脚本...", "yellow")
        SERVER_DIR.mkdir(parents=True, exist_ok=True)
        (SERVER_DIR / "stop.txt").write_text("stop\n", encoding="utf-8")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and is_process_alive(pid):
            time.sleep(0.2)
        SERVER_READY.unlink(missing_ok=True)
        (SERVER_DIR / "stop.txt").unlink(missing_ok=True)
    return start_persistent_server(vivado_bin)


def stop_persistent_server():
    pid, _ = get_server_info()
    if pid is None:
        write("Vivado Lab 常驻服务未运行。", "yellow")
        return 0

    SERVER_DIR.mkdir(parents=True, exist_ok=True)
    (SERVER_DIR / "stop.txt").write_text("stop\n", encoding="utf-8")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            SERVER_READY.unlink(missing_ok=True)
            write("Vivado Lab 常驻服务已停止。", "green")
            return 0
        time.sleep(0.2)

    write(f"Vivado Lab 常驻服务仍在运行，PID={pid}。", "yellow")
    return 1


def search_roots_text():
    roots = [str(VIVADO_PORTABLE)]
    for drive in XILINX_DRIVES:
        for product in XILINX_PRODUCTS:
            roots.append(str(Path(f"{drive}:\\") / "Xilinx" / product / "*" / "bin"))
    return roots


def bin_search_dirs():
    dirs = [FLOW_DIR, ROOT]
    unique_dirs = []
    for directory in dirs:
        resolved = directory.resolve()
        if resolved not in unique_dirs:
            unique_dirs.append(resolved)
    return unique_dirs


def runs_search_dirs():
    project_dir = ROOT.parent
    return sorted(
        (path.resolve() for path in project_dir.glob("*.runs") if path.is_dir()),
        key=lambda item: str(item).lower(),
    )


def bin_display_name(path):
    try:
        relative = path.relative_to(ROOT.parent)
    except ValueError:
        try:
            relative = path.relative_to(ROOT)
        except ValueError:
            return path.name
    if relative.parent == Path("."):
        return path.name
    return str(relative)


def choose_bin_file():
    bin_files = []
    seen = set()
    for directory in bin_search_dirs():
        for path in sorted(directory.glob("*.bin"), key=lambda item: item.name.lower()):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            bin_files.append(path)

    for directory in runs_search_dirs():
        for path in sorted(directory.rglob("*.bin"), key=lambda item: str(item).lower()):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            bin_files.append(path)

    if not bin_files:
        write("错误：未找到 .bin 文件。", "red")
        write("已检查入口目录、工具目录，以及上层结构中的 *.runs 子目录。")
        return None

    if len(bin_files) == 1:
        return bin_files[0]

    write("检测到多个 .bin 文件，请选择本次要烧录的文件：", "yellow")
    write()
    for index, path in enumerate(bin_files, start=1):
        write(f"  {index}. {bin_display_name(path)}")
    write()

    while True:
        choice = input(f"请输入要烧录的文件编号 [1-{len(bin_files)}]：").strip()
        write()
        if not choice:
            write("错误：未输入文件编号。", "red")
            continue
        if not choice.isdigit():
            write("错误：请输入数字编号。", "red")
            continue
        index = int(choice)
        if index < 1 or index > len(bin_files):
            write("错误：编号超出范围。", "red")
            continue
        return bin_files[index - 1]


def choose_numbered_item(title, items, prompt):
    write(title, "yellow")
    write()
    for index, item in enumerate(items, start=1):
        write(f"  {index}. {item}")
    write()

    while True:
        choice = input(f"{prompt} [1-{len(items)}]：").strip()
        write()
        if not choice:
            write("错误：未输入编号。", "red")
            continue
        if not choice.isdigit():
            write("错误：请输入数字编号。", "red")
            continue
        index = int(choice)
        if index < 1 or index > len(items):
            write("错误：编号超出范围。", "red")
            continue
        return items[index - 1]


def unique_keep_order(items):
    unique = []
    seen = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def flash_part_candidates_from_log():
    if not CONSOLE_LOG.exists():
        return []

    candidates = []
    for line in CONSOLE_LOG.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("FLASH_PART_CANDIDATE:"):
            candidate = line.split(":", 1)[1].strip()
            if candidate:
                candidates.append(candidate)
    return unique_keep_order(candidates)


def update_config_value(section, key, value):
    text = CONFIG_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    output = []
    current_section = None
    updated = False
    inserted = False

    for line in lines:
        section_match = re.match(r"\s*\[([^\]]+)\]\s*$", line)
        if section_match:
            if current_section == section and not updated:
                output.append(f"{key} = {value}")
                updated = True
                inserted = True
            current_section = section_match.group(1).strip()

        if current_section == section and re.match(rf"\s*{re.escape(key)}\s*=", line):
            output.append(re.sub(r"^(\s*[^=]+\s*=\s*).*$", rf"\g<1>{value}", line))
            updated = True
            continue

        output.append(line)

    if not updated:
        if current_section == section:
            output.append(f"{key} = {value}")
        else:
            if output and output[-1].strip():
                output.append("")
            output.append(f"[{section}]")
            output.append(f"{key} = {value}")

    newline = "\r\n" if "\r\n" in text else "\n"
    CONFIG_PATH.write_text(newline.join(output) + newline, encoding="utf-8")
    return inserted or updated


def resolve_flash_part_by_user(config):
    candidates = flash_part_candidates_from_log()
    if not candidates:
        return None

    selected = choose_numbered_item(
        f"Flash 型号 {config['flash_part']} 匹配到多个 Vivado cfgmem part，请选择本板卡实际使用的型号：",
        candidates,
        "请输入 Flash 型号编号",
    )
    update_config_value("hardware", "flash_part", selected)
    write(f"已将 Flash 型号写入配置文件：{selected}", "green")
    return selected


def show_friendly_error(title, hints):
    global shown_friendly_error
    if shown_friendly_error:
        return
    shown_friendly_error = True
    stop_progress()

    write()
    write("========================================", "red")
    write(f"烧录错误：{title}", "red")
    write("建议检查：", "yellow")
    for hint in hints:
        write(f"  - {hint}", "yellow")
    write("========================================", "red")
    write()


def handle_vivado_line(line, log_file):
    global vivado_started

    log_file.write(line + "\n")
    log_file.flush()

    if re.search(r"FPGA_ERROR_NO_DOWNLOADER|No matching targets found on connected servers", line):
        show_friendly_error(
            "未检测到 USB 下载器。",
            [
                "确认 USB 下载器已经插入电脑。",
                "确认下载器驱动已经正确安装。",
                "确认 Vivado hw_server 没有被其他程序异常占用。",
                "如果刚插入下载器，请等待几秒，或重新插拔下载器后再运行脚本。",
            ],
        )
    elif re.search(r"cannot set write enable bit|block\(s\) protected|Flash Programming Unsuccessful", line, re.I):
        show_friendly_error(
            "Flash 写入失败，可能处于写保护或无法置位写使能。",
            [
                "确认 Flash 芯片没有被硬件写保护。",
                "确认开发板供电稳定，重新上电后再试。",
                "确认选择的 Flash 型号和板卡实际器件一致。",
                "如果刚失败过，请重新插拔下载器或重启常驻服务后再试。",
            ],
        )
    elif re.search(r"FPGA_ERROR_TARGET_NOT_OPEN|Check cable connectivity and that the target board is powered up", line):
        show_friendly_error(
            "下载器已连接，但板卡未上电或 JTAG 目标无法打开。",
            [
                "确认 FPGA 开发板电源已经打开。",
                "确认下载器到开发板的 JTAG 线缆连接牢靠。",
                "确认板卡 JTAG 模式/拨码开关设置正确。",
                "重新给开发板上电后再运行脚本。",
            ],
        )
    elif re.search(r"FPGA_ERROR_NO_FPGA_DEVICE|No devices detected on target", line):
        show_friendly_error(
            "已找到下载器，但未检测到 FPGA 芯片。",
            [
                "确认 FPGA 开发板已经上电。",
                "确认下载器到 FPGA 的 JTAG 连接没有松动或接反。",
                "重新插拔下载器或重新给开发板上电后再试。",
            ],
        )
    elif re.search(r"cannot open|could not open|failed to open", line, re.I) and re.search(
        r"hw_target|target", line, re.I
    ):
        show_friendly_error(
            "打开 JTAG 目标失败。",
            [
                "确认没有其他 Vivado/Vivado Lab 正在占用下载器。",
                "确认 hw_server 工作正常。",
                "重新插拔下载器后再运行脚本。",
            ],
        )
    elif "VIVADO_PERSISTENT_JOB_START" in line or "VIVADO_PERSISTENT_JOB_DONE" in line:
        return
    elif "STEP 1" in line:
        if not vivado_started:
            vivado_started = True
        write()
        write("[步骤 1/3] 正在加载 Flash 桥接程序到 FPGA，请保持 JTAG 连接稳定...", "cyan")
    elif "STEP 2" in line:
        write()
        write("[步骤 2/3] 正在擦除、写入并校验 Flash，耗时通常最长，请耐心等待...", "cyan")
    elif "STEP 3" in line:
        write()
        write("[步骤 3/3] 正在从 Flash 启动 FPGA，并检查 DONE 引脚状态...", "cyan")
    elif line.startswith("Mfg ID"):
        write("      已识别到 Flash 存储器，开始执行擦写流程。", "cyan")
    elif "TCL_PROGRESS_FLASH_START" in line:
        write("      当前进度：正在擦除、写入并校验 Flash 数据...", "cyan")
        start_progress("Flash 编程")
    elif "TCL_PROGRESS_FLASH_DONE" in line:
        stop_progress("Flash 擦除、写入和校验完成。")
    elif "TCL_PROGRESS_BOOT_START" in line:
        report_boot_wait()
    elif "TCL_PROGRESS_BOOT_DONE" in line:
        return
    elif "Performing Erase Operation" in line:
        write("      当前进度：正在擦除 Flash 内容...", "cyan")
        if progress_label() != "Flash 编程":
            start_progress("擦除 Flash")
    elif "Erase Operation successful" in line:
        if progress_label() == "擦除 Flash":
            stop_progress("Flash 擦除完成。")
        else:
            write("      当前进度：Flash 擦除完成。", "green")
    elif "Performing Program and Verify Operations" in line:
        write("      当前进度：正在写入并校验 Flash 数据...", "cyan")
        if progress_label() != "Flash 编程":
            start_progress("写入/校验")
    elif "Program/Verify Operation successful" in line:
        if progress_label() == "写入/校验":
            stop_progress("Flash 写入和校验完成。")
        else:
            write("      当前进度：Flash 写入和校验完成。", "green")
    elif "Flash programming completed successfully" in line:
        write("      当前进度：Vivado 已确认 Flash 编程成功。", "green")
    elif re.search(r"Will wait up to .* booting to complete", line):
        report_boot_wait()
    elif "Done pin status: HIGH" in line:
        write("      当前进度：DONE 引脚已为高电平，FPGA 已从 Flash 启动。", "green")
    elif "FLASH_PROGRAM_SUCCESS" in line:
        write("      当前进度：烧录流程完成，正在退出 Vivado。", "green")
    elif re.search(r"^(ERROR:|FATAL:)|\bFAIL(?:ED|URE)?\b", line):
        stop_progress()
        if shown_friendly_error and "failed due to earlier errors" in line:
            return
        write(line, "red")
    elif re.search(r"WARNING|CRITICAL", line):
        write(line, "yellow")
    elif re.search(r"SUCCESS|completed successfully", line):
        write(line, "green")
    else:
        return


def tail_job_log(job_log, done_file, log_file):
    position = 0
    while True:
        if job_log.exists():
            with job_log.open("r", encoding="utf-8", errors="replace") as source:
                source.seek(position)
                for raw_line in source:
                    handle_vivado_line(raw_line.rstrip("\r\n"), log_file)
                position = source.tell()

        if done_file.exists():
            if job_log.exists():
                with job_log.open("r", encoding="utf-8", errors="replace") as source:
                    source.seek(position)
                    for raw_line in source:
                        handle_vivado_line(raw_line.rstrip("\r\n"), log_file)
                    position = source.tell()
            try:
                return int(done_file.read_text(encoding="utf-8", errors="ignore").strip() or "1")
            except ValueError:
                return 1

        time.sleep(0.1)


def run_vivado(vivado_bin, bin_file, config):
    global vivado_started, boot_wait_reported, current_job_id
    vivado_started = False
    boot_wait_reported = False
    current_job_id = None
    SUCCESS_FLAG.unlink(missing_ok=True)
    CONSOLE_LOG.unlink(missing_ok=True)

    with CONSOLE_LOG.open("w", encoding="utf-8", newline="\n") as log_file:
        try:
            pid = ensure_persistent_server(vivado_bin)
            if pid is None:
                return 1

            job_id = datetime.now().strftime("job_%Y%m%d_%H%M%S_%f")
            current_job_id = job_id
            request_file = SERVER_DIR / f"{job_id}.req"
            job_log = SERVER_DIR / f"{job_id}.log"
            done_file = SERVER_DIR / f"{job_id}.done"

            request_file.write_text(
                "\n".join(
                    [
                        f"job_id={job_id}",
                        f"bin_file={bin_file}",
                        f"success_flag={SUCCESS_FLAG}",
                        f"fpga_device={config['fpga_device']}",
                        f"flash_part={config['flash_part']}",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            write("已提交烧录任务，正在等待 Vivado Lab 执行...", "cyan")
            return tail_job_log(job_log, done_file, log_file)
        finally:
            stop_progress()


def is_success(vivado_exit):
    if SUCCESS_FLAG.exists():
        return True

    if CONSOLE_LOG.exists():
        text = CONSOLE_LOG.read_text(encoding="utf-8", errors="ignore")
        if "FLASH_PROGRAM_SUCCESS" in text:
            return True
        if "Flash programming completed successfully" in text and "Done pin status: HIGH" in text:
            return True

    return vivado_exit == 0 and SUCCESS_FLAG.exists()


def unlink_quietly(path):
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def cleanup_server_history(success):
    if not SERVER_DIR.exists():
        return

    for path in SERVER_DIR.glob("job_*.req"):
        unlink_quietly(path)

    for path in SERVER_DIR.glob("job_*.done"):
        if not success and current_job_id and path.stem == current_job_id:
            continue
        unlink_quietly(path)

    for path in SERVER_DIR.glob("job_*.log"):
        if not success and current_job_id and path.stem == current_job_id:
            continue
        unlink_quietly(path)


def cleanup(success):
    if success:
        write()
        write("正在清理临时文件...")
        for path in ROOT.glob("*.log"):
            unlink_quietly(path)
        for path in ROOT.glob("*.jou"):
            unlink_quietly(path)
        unlink_quietly(SUCCESS_FLAG)
    else:
        write()
        write("编程失败，已保留 vivado.log、vivado.jou 和 vivado_console.log。")

    for pattern in ("*.dmp", "vivado_pid*.str"):
        for path in ROOT.glob(pattern):
            unlink_quietly(path)
    shutil.rmtree(ROOT / ".Xil", ignore_errors=True)
    cleanup_server_history(success)


def main():
    os.chdir(ROOT)
    if os.name == "nt":
        os.system("chcp 65001 >nul")
        enable_windows_ansi()

    config = load_config()
    if config is None:
        pause()
        return 1

    if "--self-test" in sys.argv:
        write("Python 运行环境检查通过。", "green")
        return 0

    if "--stop-server" in sys.argv:
        return stop_persistent_server()

    remove_stale_server_dir_if_vivado_lab_not_running()

    vivado_bin = find_vivado()
    if vivado_bin is None:
        write("错误：未找到 Vivado Lab。", "red")
        write("已搜索以下位置：", "yellow")
        for root in search_roots_text():
            write(f"  - {root}", "yellow")
        write("请确保已安装 Vivado Lab/Vivado/Vitis，或将便携包放入 xilinx 文件夹。")
        pause()
        return 1
    write(f"已找到 Vivado 工具：{vivado_bin}", "cyan")

    bin_file = choose_bin_file()
    if bin_file is None:
        pause()
        return 1

    write(f"找到比特流文件：{bin_display_name(bin_file)}", "cyan")
    write(f"FPGA 器件型号：{config['fpga_device']}", "cyan")
    write(f"Flash 型号：{config['flash_part']}", "cyan")
    write("开始 FPGA Flash 编程...", "cyan")
    write()

    vivado_exit = run_vivado(vivado_bin, bin_file, config)
    success = is_success(vivado_exit)
    if not success:
        selected_flash_part = resolve_flash_part_by_user(config)
        if selected_flash_part is not None:
            config = load_config()
            if config is None:
                pause()
                return 1
            write("已更新配置，正在使用所选 Flash 型号重新提交烧录任务...", "cyan")
            write(f"Flash 型号：{config['flash_part']}", "cyan")
            write()
            vivado_exit = run_vivado(vivado_bin, bin_file, config)
            success = is_success(vivado_exit)

    write()
    if success:
        write("========================================", "green")
        write("        Flash 编程已完成！", "green")
        write("========================================", "green")
        if vivado_exit != 0:
            write(f"提示：Vivado 返回码为 {vivado_exit}，但日志显示 Flash 编程和启动均已成功。", "yellow")
    else:
        write("========================================", "red")
        write("           编程失败！", "red")
        write("========================================", "red")
        write()
        write("请查看 vivado.log 或 vivado_console.log 了解详情。", "yellow")

    cleanup(success)
    write()
    if success:
        close_countdown(10)
    else:
        pause()
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
