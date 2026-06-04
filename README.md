# FPGA Flash Download Tools

这是一个面向 Windows 环境的 FPGA Flash 下载工具包，用于通过 Xilinx Vivado/Vivado Lab 硬件管理器将 `.bin` 程序烧录到板卡 Flash 中。

工具封装了 Python 主流程、Vivado Tcl 下载流程和双击可运行的批处理入口，适合在工程交付、生产调试或现场下载时使用。

## 功能特点

- 双击批处理即可启动下载流程。
- 随包携带 Python 运行环境，无需用户单独安装 Python。
- 自动查找待烧录的 `.bin` 文件。
- 自动检测并复用 Vivado Lab 常驻服务，减少重复启动 Vivado 的等待时间。
- 根据配置文件匹配 FPGA 器件型号和 Flash cfgmem 型号。
- 支持 Flash 桥接、擦除、写入、校验、从 Flash 启动和 DONE 状态检查。
- 失败时保留日志，便于定位下载器、板卡供电、器件型号或 Vivado 配置问题。

## 目录结构

```text
tools/
├── README.md
└── fpga_download_with_python/
    ├── 下载FPGA程序.bat
    ├── 退出vivado进程.bat
    ├── 使用说明.md
    ├── config/
    │   └── device_config.ini
    ├── scripts/
    │   ├── fpga_flash_download.py
    │   ├── stop_vivado_server.py
    │   └── vivado_persistent_server.tcl
    └── runtime/
        └── python/
```

## 运行环境

- Windows 系统。
- 已安装 Xilinx Vivado Lab、Vivado 或 Vitis。
- FPGA 板卡已上电。
- USB/JTAG 下载器已连接。
- `config/device_config.ini` 中的 FPGA 和 Flash 型号与实际板卡一致。

脚本会优先查找以下 Vivado 入口：

- `tools/xilinx/bin/vivado_lab.bat`
- `C:\Xilinx`、`D:\Xilinx`、`E:\Xilinx`、`F:\Xilinx` 下的 Vivado Lab、Vivado 或 Vitis 安装目录

## 快速开始

1. 将整个文件夹放入工程文件夹tools文件夹中；

2. 进入 `fpga_download_with_python/config/device_config.ini`，确认硬件配置：
   
   ```ini
   [hardware]
   fpga_device = xc7a100t
   flash_part = is25lp128f
   ```

3. 将需要烧录的 `.bin` 文件放到 `fpga_download_with_python/` 目录，也就是和 `下载FPGA程序.bat` 同一层。
   脚本也会自动搜索上层工程中的 `*.runs` 目录。

4. 双击运行：
   
   ```text
   fpga_download_with_python/下载FPGA程序.bat
   ```

5. 如果检测到多个 `.bin` 文件，按终端提示输入编号并回车。

6. 等待脚本完成下载。成功后窗口会倒计时自动关闭。

## 配置说明

配置文件位于：

```text
fpga_download_with_python/config/device_config.ini
```

字段说明：

- `fpga_device`：FPGA 器件型号。可填写基础型号，例如 `xc7a100t`，脚本会自动匹配 Vivado 硬件管理器中的 `xc7a100t_0` 等实际设备名。
- `flash_part`：Flash 器件型号。可填写基础型号，例如 `is25lp128f`，脚本会自动匹配 Vivado `get_cfgmem_parts` 中的完整 cfgmem 名称，例如 `is25lp128f-spi-x1_x2_x4`。

如果一个 Flash 基础型号匹配到多个 Vivado cfgmem part，脚本会优先选择 `-spi-x1_x2_x4`。仍然无法唯一确定时，会提示用户选择，并将选择结果写回配置文件。

## 常用操作

### 下载 FPGA 程序

双击：

```text
fpga_download_with_python/下载FPGA程序.bat
```

### 手动退出 Vivado 常驻服务

双击：

```text
fpga_download_with_python/退出vivado进程.bat
```

下载成功倒计时期间，也可以按 `q` 退出 Vivado Lab 常驻服务。

## 日志与排查

下载失败时，脚本会保留日志文件，优先查看：

- `vivado_console.log`
- `vivado.log`
- `vivado.jou`
- `fpga_download_with_python/runtime/.vivado_server/server_vivado.log`

常见检查项：

- USB/JTAG 下载器是否插入。
- FPGA 板卡是否上电。
- 是否有其他 Vivado/Vivado Lab 占用了下载器。
- `device_config.ini` 中的 FPGA 型号是否匹配实际板卡。
- `device_config.ini` 中的 Flash 型号是否匹配实际板卡。
- Vivado/Vivado Lab 是否支持所配置的 Flash cfgmem part。

## 注意事项

- 烧录过程中不要拔掉 JTAG 线缆、下载器或板卡电源。
- 更换板卡、下载器或 Flash 型号后，建议重新检查配置文件。
- `runtime/python/` 是随包运行环境，通常不需要手动修改。

## 进一步说明

更详细的中文使用步骤见：

```text
fpga_download_with_python/使用说明.md
```

# fpga_download
