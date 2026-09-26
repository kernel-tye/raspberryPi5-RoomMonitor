# raspberryPi5-RoomMonitor — 树莓派 5 房间监控

[English](README.en.md)

树莓派 5 + IMX500 AI 摄像头：24 小时录像，在摄像头传感器上跑人体检测，通过网页查看，录像加密后上传到百度网盘。

- 连续录像：每 5 分钟一段 MKV，画面烧录时间戳，断电也只损坏最后一段
- 人体检测直接在 IMX500 传感器上跑，树莓派 CPU 几乎不参与。检测到人后，从连续录像里剪出「出现前 30 秒～离开后 30 秒」的事件片段
- 可选门磁：Home Assistant 通过 MQTT 发出开门消息，也记为事件
- 每段录像和每个片段的 SHA256 记入 `hashes.txt`，可以证明文件事后没被改过
- 网页：实时画面、事件回放和下载、按天浏览连续录像，还能看温度、网速、TF 卡读写、内存、欠压等状态
- 百度网盘：每个文件先打成 AES-256 的 7z 包（文件名也加密）再上传，网盘只能看到加密包。上传成功且网盘上文件大小核对一致后，才删除本地文件

## 硬件

- Raspberry Pi 5（1 GB 内存也能跑，但很紧）
- Raspberry Pi AI Camera（Sony IMX500）
- 建议用官方 27 W 电源。电源不足会触发欠压和降频，网页上能看到

## 文件

| 文件 | 作用 | systemd |
|---|---|---|
| `cctv_recorder.py` | 录像、人体检测、剪事件片段、MJPEG 预览 | `cctv-recorder.service` |
| `cctv_web.py` | 网页查看器 | `cctv-web.service` |
| `cctv_baidu_sync.py` | 加密后上传百度网盘，核对后删除本地文件 | `cctv-baidu-sync.timer` → `.service` |
| `push_to_pi.py` | （Windows）把电脑上的旧视频加密后送到 Pi 排队上传 | — |
| `encrypt_local.py` | （Windows）把本地视频加密成同样格式的 7z | — |
| `cctv.env.example` | 配置示例 | — |

## 安装

以下假设用户名是 `pi`。如果不是，请把 `.service` 文件里的 `pi` / `/home/pi` 改成你的用户名和家目录。

```bash
# 1. 依赖（Raspberry Pi OS Bookworm）
sudo apt install imx500-all python3-picamera2 python3-opencv ffmpeg p7zip-full mosquitto-clients

# 2. 程序和数据目录
cp cctv_recorder.py cctv_web.py cctv_baidu_sync.py ~/
mkdir -p ~/cctv && cp cctv.env.example ~/cctv/cctv.env   # 按需修改

# 3. 网页登录密码（存 PBKDF2 哈希）
python3 ~/cctv_web.py --set-password

# 4. 7z 加密密码（丢了就再也打不开网盘上的录像）
printf '%s' '你的密码' > ~/cctv/archive_password && chmod 600 ~/cctv/archive_password

# 5. systemd
sudo cp cctv-*.service cctv-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cctv-recorder cctv-web cctv-baidu-sync.timer
```

不需要百度网盘的话，不要启用 `cctv-baidu-sync.timer`。这时录像程序会在剩余空间不足 4 GB 时自动删除最旧的连续录像，事件片段不会被自动删除。

### 百度网盘

用的是 [BaiduPCS-Go](https://github.com/qjfoidnh/BaiduPCS-Go)，放在 `~/bin/BaiduPCS-Go`。BDUSS 和 STOKEN 从浏览器里 `pan.baidu.com` 的 cookie 中取：

```bash
~/bin/BaiduPCS-Go login -bduss="..." -stoken="..."
# 在海外时，默认的 pcs.baidu.com 会不断断开重来，换成下面这个节点
~/bin/BaiduPCS-Go config set -pcs_addr c.pcs.baidu.com -fix_pcs_addr=true
```

## 配置

所有设置都写在 `~/cctv/cctv.env`（环境变量），每项都可以不写，默认值见 [`cctv.env.example`](cctv.env.example)。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CCTV_HOME` | `~/cctv` | 数据目录 |
| `CCTV_WEB_LISTEN` | `127.0.0.1:8090` | 网页监听的地址。建议填 Tailscale 等内网地址，**不要暴露到公网** |
| `CCTV_MQTT_TOPIC` | `cctv/door` | 门磁的 MQTT topic（本机 mosquitto），收到 `START` 就算开门 |
| `CCTV_BPCS` | `~/bin/BaiduPCS-Go` | BaiduPCS-Go 程序路径 |
| `CCTV_REMOTE` | `/房间监控` | 网盘上的目录 |
| `CCTV_OLD_DIR` | `~/ai_cam_records_private` | 旧门磁录像目录（可选），最后才上传 |

录像参数（分辨率、帧率、检测阈值、事件前后时长等）是 `cctv_recorder.py` 开头的常量。

## 工作方式

### 录像 `cctv_recorder.py`

- 分辨率 1280×960、15 fps，H.264 编码（Pi 5 没有硬件编码器，用 libx264，qp 27）。房间没人活动时码率很低，有人活动时约 1.3 Mbps。连续录像每天约 5 GB。
- 人体检测：`ssd_mobilenetv2_fpnlite_320x320_pp`，COCO 里的 person 类，分数 ≥ 0.55，且最近 5 帧里至少 3 帧检测到人。
- 事件片段存到 `events/*.mp4`，单段最长 10 分钟，记录在 `events.jsonl`。
- 在 `127.0.0.1:8000/stream.mjpg` 提供 MJPEG 实时画面，给网页和 Home Assistant 用。
- 每分钟写一次 `heartbeat`。30 秒没有画面或 ffmpeg 退出时，程序自己退出，由 systemd 重启。

### 网页 `cctv_web.py`

- 登录：密码存 PBKDF2 哈希，会话 cookie 用 HMAC 签名，7 天有效。同一 IP 输错 5 次锁定 10 分钟。
- 「树莓派」一栏每 3 秒刷新一次：CPU 和摄像头温度、风扇转速、网速、TF 卡读写、CPU、内存和 swap、待上传数量、欠压和降频状态。
- 「百度限速 500KB/s」勾选框：写入或删除 `~/cctv/upload_limit`，同步程序每上传一个文件前会读一次。

### 百度同步 `cctv_baidu_sync.py`

- 上传顺序：日志 → 开门事件 → 有人事件（新的优先）→ 连续录像（旧的优先）→ 旧门磁录像 → 收件箱 `~/cctv/inbox/*.7z`。
- 每轮最多运行 5 分钟，结束 30 秒后开始下一轮，每轮重新排优先级，所以新发生的事件总能优先上传。
- 删除本地文件前要满足两个条件：文件已存在满 24 小时，并且网盘上 `.7z` 的大小和上传时逐字节一致。收件箱里的包本来就只是中转，核对一致后立即删除。
  百度上传时会逐块校验 MD5，但它返回的整个文件的 MD5 不可靠；下载回来比对又太慢。同一个文件用同一个密码打包，7z 输出的大小是确定的，所以用「上传成功 + 大小一致」来验证。
- 网盘目录：`events/`、`segments/YYYY-MM-DD/`、`logs/`、`旧门磁录像/`。
- 打开加密包：电脑用 7-Zip，安卓用 ZArchiver，iOS 用 iZip。

### 电脑 → Pi：`push_to_pi.py`（Windows）

把电脑上的旧视频交给 Pi 排队上传。对每个文件依次：本机加密并测试能否解开 → 用 `pscp` 传到 Pi 的 `inbox/*.7z.part` → 两边 SHA256 一致后改名为 `.7z` → 删除本机原文件。Pi 的收件箱最多同时放 14 GB，并始终保留至少 6 GB 剩余空间，所以录像程序不会因为它而删录像。脚本可以随时中断再重新运行。

```powershell
$env:CCTV_PI_HOST="pi@100.x.y.z"; $env:CCTV_PI_HOSTKEY="SHA256:..."
$env:CCTV_SSH_PASSWORD="..."; $env:CCTV_ZIP_PASSWORD="..."
python push_to_pi.py <目录>
```

需要安装 PuTTY（`plink`、`pscp`）和 7-Zip。

## 注意

- 摄像头同一时间只能被一个进程占用。
- Pi 5 只有 1 GB 内存时本来就会用到 swap，不要再加吃内存的程序。
- 网页只应该在内网或 Tailscale 里访问。
- 请遵守当地关于录像和隐私的法律。

## 许可证

[MIT](LICENSE)
