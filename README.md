# NIRHR_FIRLR_Fusion_SR

![FIR-NIR Fusion Demo](Assets/1.jpg)

> Real-time FIR + NIR fusion RTSP demo for Raspberry Pi: grayscale global-shutter stream + thermal stream alignment, fusion, and network publishing.
>
> 面向树莓派的 FIR + NIR 实时融合 RTSP 演示：将全局快门灰度流与热成像流进行时间对齐、融合并推流。

---

## AI Assistance / AI 辅助声明

**English**

Portions of this repository (e.g., documentation, code comments, or code drafts) may have been created or refined with the assistance of generative AI tools (for example, GitHub Copilot / ChatGPT).
All AI-assisted output was reviewed, edited, and validated by the repository maintainers, who take full responsibility for the content, correctness, licensing compliance, and security of this project.

If you contribute using generative AI, please disclose it in your pull request description (tool + scope), and ensure you have the rights to submit the content.

**中文**

本仓库的部分内容（如文档、注释或代码草稿）可能在生成式 AI 工具（例如 GitHub Copilot / ChatGPT）的辅助下完成。
所有 AI 辅助产出均已由维护者进行人工审查、修改与验证；维护者对项目的内容正确性、许可合规性与安全性承担最终责任。

如果你在贡献中使用了生成式 AI，请在 PR 描述中披露（工具 + 范围），并确保你有权提交相关内容。

---

## Language / 语言切换

- [English](#english)
- [中文](#中文)

---

## English

### 1. What this repo does (updated)

Current core script is **`fusion_RSTP.py`** (latest version), used to:

1. Capture GS grayscale stream from `libcamerasrc`.
2. Capture thermal stream from `v4l2src` (`/dev/video*`, default `/dev/video42`).
3. Normalize thermal intensity using percentile clipping (`p_low`, `p_high`).
4. Time-match thermal frames to GS frames with nearest timestamp and `delta-ms` threshold.
5. Fuse and publish one RTSP stream via `GstRtspServer` (`rtsp://<ip>:<port><path>`).

Also included:
- `RTSP.py`: GTK-based dual-stream RTSP GUI (`/gs` + `/thermal`) for parameter tuning demos.
- `gs_demo.py`: local GS preview/recording tool (60 FPS target, snapshot + bitrate hotkeys).
- `GUI.py`: lightweight Tkinter control-panel prototype for interface experiments.

---

### 2. Dependency installation

#### 2.1 System packages

```bash
sudo apt update
sudo apt install -y \
  python3-pip python3-opencv python3-gi python3-gi-cairo \
  gir1.2-gstreamer-1.0 gir1.2-gst-rtsp-server-1.0 \
  gstreamer1.0-tools gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly gstreamer1.0-libav \
  ffmpeg
```

> If you need `--edge-mode guided` or `--edge-mode joint-bilateral`, install OpenCV with `ximgproc` support (opencv-contrib build).

#### 2.2 Python packages

```bash
python3 -m pip install --upgrade pip
python3 -m pip install numpy
```

---

### 3. Quick start (fusion RTSP)

```bash
python3 fusion_RSTP.py \
  --port 8554 \
  --path /fusion \
  --gs-w 1280 --gs-h 720 --gs-fps 30 \
  --th-dev /dev/video42
```

Open with VLC/ffplay:

- `rtsp://<PI_IP>:8554/fusion`

Script startup logs show:
- RTSP URL
- guided/joint-bilateral capability status
- active edge mode

---

### 4. Key CLI options (`fusion_RSTP.py`)

| Option | Default | Meaning |
|---|---:|---|
| `--port` | `8554` | RTSP port |
| `--path` | `/fusion` | RTSP mount path |
| `--gs-w` / `--gs-h` | `1280` / `720` | GS output resolution |
| `--gs-fps` | `30` | GS capture/output FPS |
| `--th-dev` | `/dev/video42` | Thermal device |
| `--alpha` | `0.35` | Fusion alpha |
| `--delta-ms` | `40.0` | Max timestamp mismatch for thermal refresh |
| `--th-buf-len` | `16` | Thermal ring buffer length |
| `--p-low` / `--p-high` | `2.0` / `98.0` | Thermal percentile normalization |
| `--colormap` | `inferno` | Thermal colormap (`jet/turbo/inferno/magma/plasma/viridis`) |
| `--blend-mode` | `add` | Blend mode (`add` or `mix`) |
| `--edge-mode` | `none` | Edge refine mode (`none/guided/joint-bilateral`) |
| `--gf-radius` / `--gf-eps` | `4` / `1e-3` | Guided filter parameters |
| `--jb-d` / `--jb-sigma-color` / `--jb-sigma-space` | `5` / `25.0` / `7.0` | Joint bilateral parameters |
| `--bitrate-kbps` | `4000` | x264 bitrate |

---

### 5. Fusion pipeline details

- **GS ingest pipeline**: `libcamerasrc -> videobalance saturation=0 -> videoconvert -> appsink`
- **Thermal ingest pipeline**: `v4l2src -> videoconvert -> appsink`
- Thermal frames are converted to grayscale and normalized by percentile stretch.
- Fusion state keeps a thermal deque and picks nearest frame by timestamp.
- If no valid new thermal frame arrives within `delta-ms`, last valid overlay is held.
- Output is encoded by `x264enc` and sent via `rtph264pay` through RTSP.

---

### 6. Example commands

**Low-latency default**
```bash
python3 fusion_RSTP.py --port 8554 --path /fusion
```

**Sharper boundaries (if ximgproc available)**
```bash
python3 fusion_RSTP.py --edge-mode guided --gf-radius 4 --gf-eps 1e-3
```

**Joint bilateral thermal refinement**
```bash
python3 fusion_RSTP.py --edge-mode joint-bilateral --jb-d 5 --jb-sigma-color 25 --jb-sigma-space 7
```

---

### 7. Troubleshooting

1. **`ModuleNotFoundError: gi`**
   - Reinstall `python3-gi`, `gir1.2-gstreamer-1.0`, `gir1.2-gst-rtsp-server-1.0`.
2. **RTSP connected but black/no frame**
   - Check `libcamerasrc` and thermal `/dev/video*` separately with `gst-launch-1.0`.
3. **`guided` / `joint-bilateral` not effective**
   - Your OpenCV likely has no `ximgproc`; script falls back to `none` automatically.
4. **High latency**
   - Lower `--gs-w/--gs-h`, reduce `--bitrate-kbps`, and ensure Pi thermal throttling is not happening.

---

### 8. Other scripts in this repository

- **`RTSP.py` (legacy but useful in teaching/lab demos)**
  - Starts a GTK UI to publish **two RTSP streams** at once (`GS` and `Thermal`).
  - Supports runtime adjustment of width/height/FPS/bitrate and video-device refresh.
  - Typical launch:

    ```bash
    python3 RTSP.py --port 8554 --gs-path /gs --th-path /thermal
    ```

- **`gs_demo.py` (single-camera local validation)**
  - Uses Picamera2 + OpenCV for local preview, OSD, and optional recording.
  - Keyboard shortcuts: `Q/Esc` quit, `R` record toggle, `S` snapshot, `+/-` bitrate adjust.
  - Typical launch:

    ```bash
    python3 gs_demo.py
    ```

- **`GUI.py` (UI prototype)**
  - Tkinter-based control-panel mockup for power state, view selection, and basic operator flow.
  - Useful as a starting point for building a richer integrated front-end.

---

## 中文

### 1. 项目功能（已按最新版更新）

当前核心脚本是 **`fusion_RSTP.py`**，用于：

1. 通过 `libcamerasrc` 采集全局快门灰度视频；
2. 通过 `v4l2src` 采集热成像视频（默认 `/dev/video42`）；
3. 按百分位（`p_low/p_high`）对热图做归一化；
4. 依据时间戳就近匹配热帧与 GS 帧，并用 `delta-ms` 控制最大容差；
5. 将融合结果编码后通过 RTSP 单路输出。

仓库中其它脚本：
- `RTSP.py`：基于 GTK 的双路 RTSP 图形界面（`/gs` + `/thermal`），适合调参演示。
- `gs_demo.py`：GS 本地预览/录制工具（目标 60 FPS，支持截图与码率热键）。
- `GUI.py`：轻量级 Tkinter 控制面板原型，用于界面流程验证。

---

### 2. 依赖安装

#### 2.1 系统依赖

```bash
sudo apt update
sudo apt install -y \
  python3-pip python3-opencv python3-gi python3-gi-cairo \
  gir1.2-gstreamer-1.0 gir1.2-gst-rtsp-server-1.0 \
  gstreamer1.0-tools gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly gstreamer1.0-libav \
  ffmpeg
```

> 若要启用 `--edge-mode guided` 或 `--edge-mode joint-bilateral`，OpenCV 需包含 `ximgproc`（通常来自 opencv-contrib）。

#### 2.2 Python 依赖

```bash
python3 -m pip install --upgrade pip
python3 -m pip install numpy
```

---

### 3. 快速开始（融合 RTSP）

```bash
python3 fusion_RSTP.py \
  --port 8554 \
  --path /fusion \
  --gs-w 1280 --gs-h 720 --gs-fps 30 \
  --th-dev /dev/video42
```

播放器地址（VLC/ffplay）：

- `rtsp://<树莓派IP>:8554/fusion`

启动后会打印：
- 融合流 URL
- guided/joint-bilateral 可用性
- 实际生效的 edge mode

---

### 4. 关键参数（`fusion_RSTP.py`）

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `--port` | `8554` | RTSP 端口 |
| `--path` | `/fusion` | RTSP 挂载路径 |
| `--gs-w` / `--gs-h` | `1280` / `720` | GS 输出分辨率 |
| `--gs-fps` | `30` | GS 采集/输出帧率 |
| `--th-dev` | `/dev/video42` | 热成像设备 |
| `--alpha` | `0.35` | 融合系数 |
| `--delta-ms` | `40.0` | 热帧刷新允许的最大时间差 |
| `--th-buf-len` | `16` | 热帧缓存队列长度 |
| `--p-low` / `--p-high` | `2.0` / `98.0` | 热图归一化百分位 |
| `--colormap` | `inferno` | 伪彩（`jet/turbo/inferno/magma/plasma/viridis`） |
| `--blend-mode` | `add` | 融合模式（`add`/`mix`） |
| `--edge-mode` | `none` | 边缘优化（`none/guided/joint-bilateral`） |
| `--gf-radius` / `--gf-eps` | `4` / `1e-3` | guidedFilter 参数 |
| `--jb-d` / `--jb-sigma-color` / `--jb-sigma-space` | `5` / `25.0` / `7.0` | joint bilateral 参数 |
| `--bitrate-kbps` | `4000` | x264 码率 |

---

### 5. 融合流程说明

- **GS 输入链路**：`libcamerasrc -> videobalance saturation=0 -> videoconvert -> appsink`
- **热成像输入链路**：`v4l2src -> videoconvert -> appsink`
- 热成像先转灰度，再做百分位拉伸。
- 通过时间戳在热帧缓存中选择“最近邻”进行融合。
- 若当前时刻无新热帧满足 `delta-ms`，则保持上一帧有效热图。
- 输出侧用 `x264enc` 编码，通过 RTSP 推流。

---

### 6. 使用示例

**默认低时延**
```bash
python3 fusion_RSTP.py --port 8554 --path /fusion
```

**边缘更贴合（需 ximgproc）**
```bash
python3 fusion_RSTP.py --edge-mode guided --gf-radius 4 --gf-eps 1e-3
```

**联合双边细化**
```bash
python3 fusion_RSTP.py --edge-mode joint-bilateral --jb-d 5 --jb-sigma-color 25 --jb-sigma-space 7
```

---

### 7. 常见问题

1. **`ModuleNotFoundError: gi`**
   - 重新安装 `python3-gi` 与相关 `gir1.2-*` 包。
2. **RTSP 可连接但无画面**
   - 分别用 `gst-launch-1.0` 验证 `libcamerasrc` 与热成像设备。
3. **guided/joint-bilateral 没效果**
   - OpenCV 可能不含 `ximgproc`，脚本会自动回退到 `none`。
4. **时延偏高**
   - 降低分辨率/码率，检查树莓派是否热降频。

---

### 8. 仓库内其他脚本说明

- **`RTSP.py`（旧版但教学/实验常用）**
  - 启动 GTK 界面，同时发布两路 RTSP（`GS` 与 `Thermal`）。
  - 支持在界面中调整分辨率、帧率、码率，并刷新视频设备列表。
  - 常用启动命令：

    ```bash
    python3 RTSP.py --port 8554 --gs-path /gs --th-path /thermal
    ```

- **`gs_demo.py`（单相机本地验证）**
  - 基于 Picamera2 + OpenCV，提供本地预览、OSD 与录制。
  - 快捷键：`Q/Esc` 退出，`R` 录制开关，`S` 截图，`+/-` 调整码率。
  - 常用启动命令：

    ```bash
    python3 gs_demo.py
    ```

- **`GUI.py`（界面原型）**
  - 基于 Tkinter 的控制面板草图，包含电源状态、视图切换等基本交互。
  - 可作为后续整合式上位机界面的起点。

---

### 9. 第三方代码说明

`Thirdparty/v4l2lepton_by_groupgets_modified/` 目录提供了 Lepton 相关工具，可用于构建热成像输入链路。
