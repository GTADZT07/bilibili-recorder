import os
import re
import time
import json
import struct
import threading
import subprocess
import requests
import zlib
import brotli  # 如果服务器返回的是 Brotli 压缩

from datetime import datetime, timedelta
from pathlib import Path

# 尝试导入 websocket，不可用时退回轮询
try:
    import websocket
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False

# ========== 用户配置 ==========
room_url="https://live.bilibili.com/把我替换成直播间号比如299"                # 直播间 URL 或 房间号
save_dir=r"把我换成你要放录播的文件夹"                       # 录播文件保存目录
cookie_file=r"曲奇文件夹位置"           # 包含 SESSDATA 的 Cookie 文件
bot_token="可不填" # Telegram Bot Token
chat_id="可不填"                                    # Telegram Chat ID
prefix="【把我换成主播的名字，规范命名】_"                                # 文件名前缀，包含主播名
check_interval= 10   # 异常重试 / HTTP 轮询检测间隔（秒）
no_stream_timeout= 600  # 超过此秒数无数据判定断播结束（秒）
# ==============================

# 全局标志
stop_recording_flag = False  # 收到下播通知或超时需停止录制

# 确保保存目录存在
Path(save_dir).mkdir(parents=True, exist_ok=True)

def now_str(fmt: str = "%Y%m%d_%H%M%S") -> str:
    """获取当前时间的字符串，默认格式为年月日_时分秒，用于文件名"""
    return datetime.now().strftime(fmt)

def send_tg_message(text: str):
    """发送 Telegram 通知消息"""
    if not bot_token or not chat_id:
        return  # 未配置 Telegram 则不发送
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=5)
    except Exception as e:
        print(f"❌ Telegram 发送失败: {e}")

def get_sessdata_from_cookie() -> str:
    """从 Cookie 文件中提取 SESSDATA 值"""
    try:
        content = Path(cookie_file).read_text(encoding="utf-8")
        # 在Cookie文本中查找 SESSDATA=<值>
        m = re.search(r"SESSDATA=([^;\s]+)", content)
        if m:
            return m.group(1)
    except Exception as e:
        print(f"⚠️ 无法读取 Cookie 文件或提取 SESSDATA: {e}")
    return ""

def get_cookie_header() -> dict:
    """构造包含 SESSDATA 的请求头字典"""
    sd = get_sessdata_from_cookie()
    if sd:
        return {"Cookie": f"SESSDATA={sd}"}
    return {}

def get_live_title(real_rid: str) -> str:
    """获取当前直播间标题，并替换文件名不允许的字符"""
    headers = {"User-Agent": "Mozilla/5.0"}
    headers.update(get_cookie_header())
    try:
        resp = requests.get(
            f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={real_rid}",
            headers=headers, timeout=5
        ).json()
        if resp.get("code") == 0:
            raw_title = resp["data"].get("title", "").strip()
            # 替换文件名中的非法字符：\/:*?"<>|
            return re.sub(r'[\/\\\:\*\?"<>\|]', "_", raw_title)
    except Exception as e:
        print(f"⚠️ 获取直播标题失败：{e}")
    return ""

def get_real_room_id(rid: str) -> str:
    """将可能的短房间号转换为直播的真实房间号"""
    headers = {"User-Agent": "Mozilla/5.0"}
    headers.update(get_cookie_header())
    try:
        resp = requests.get(
            f"https://api.live.bilibili.com/room/v1/Room/room_init?id={rid}",
            headers=headers, timeout=5
        ).json()
        if resp.get("code") == 0:
            return str(resp["data"]["room_id"])
    except Exception as e:
        print(f"⚠️ 获取真实房间ID失败: {e}")
    # 请求失败则直接返回原始rid（有可能已经是真实ID）
    return rid

def get_danmu_server_info(rid: str):
    """获取 B站弹幕服务器的 WebSocket 接入点和鉴权token"""
    headers = {"User-Agent": "Mozilla/5.0"}
    headers.update(get_cookie_header())
    try:
        resp = requests.get(
            f"https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo?id={rid}",
            headers=headers, timeout=5
        ).json()
        if resp.get("code") == 0:
            data = resp["data"]
            host = data["host_list"][0]["host"]
            token = data["token"]
            port = data["host_list"][0].get("wss_port", 443)
            # 返回 WebSocket 接入URL 和认证需要的 token
            return f"wss://{host}:{port}/sub", token
    except Exception as e:
        print(f"⚠️ 获取弹幕服务器信息失败: {e}")
    return None, None

def wait_for_live(real_rid: str) -> bool:
    """
    等待直播开播：优先使用 WebSocket 弹幕连接等待“LIVE”信号，失败则使用 HTTP 轮询。
    当检测到开播时返回 True。
    """
    # 定义HTTP轮询检查直播开播状态的函数
    def http_check():
        headers = {"User-Agent": "Mozilla/5.0"}
        headers.update(get_cookie_header())
        try:
            resp = requests.get(
                f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={real_rid}",
                headers=headers, timeout=5
            ).json()
            # live_status 返回 1 表示正在直播，2 表示轮播，0 表示未开播
            return resp.get("data", {}).get("live_status", 0) != 0
        except Exception:
            return False

    # 优先尝试 WebSocket 接口监听直播状态
    if WS_AVAILABLE:
        wss_url, token = get_danmu_server_info(real_rid)
        if wss_url and token:
            print(f"📺 使用 WebSocket 监听开播：{room_url}")
            try:
                ws = websocket.create_connection(wss_url, timeout=10)
                # 发送认证包
                auth_params = {
                    "uid": 0,
                    "roomid": int(real_rid),
                    "protover": 2,
                    "platform": "web",
                    "type": 2,
                    "key": token
                }
                body = json.dumps(auth_params).encode()
                # 构造弹幕协议头部：长度、头部长度、协议版本、操作码、序列
                header = struct.pack(">IHHII", 16 + len(body), 16, 1, 7, 1)
                ws.send(header + body)
                # 开启心跳线程，每30秒发送心跳包保持连接
                def send_heartbeats():
                    packet = struct.pack(">IHHII", 16, 16, 1, 2, 1)
                    while True:
                        try:
                            ws.send(packet)
                        except Exception:
                            break
                        time.sleep(30)
                threading.Thread(target=send_heartbeats, daemon=True).start()

                # 等待服务端消息
                while True:
                    msg = ws.recv()
                    if not msg:
                        break  # 连接关闭
                    if isinstance(msg, bytes) and len(msg) >= 16:
                        # 从消息字节中提取操作码
                        op = struct.unpack(">I", msg[8:12])[0]
                        if op == 5:  # 普通数据包 (命令包)
                            ver = struct.unpack(">H", msg[6:8])[0]
                            data = msg[16:]
                            # 压缩的弹幕数据需解压
                            if ver == 2:
                                try:
                                    data = zlib.decompress(data)
                                except Exception:
                                    pass
                            # 解析可能包含多条信息的数据
                            for sub_json in parse_ws_slices(data):
                                cmd = sub_json.get("cmd", "")
                                if cmd == "LIVE":
                                    print("📢 WebSocket 检测到开播！")
                                    ws.close()
                                    return True  # 收到开播消息
                                elif cmd == "DANMU_MSG":
                                    # 处理实时收到的弹幕消息（此处仅打印，录制线程会另外处理）
                                    danmu_text = sub_json["info"][1][1]
                                    user = sub_json["info"][2][1]
                                    print(f"[弹幕] {user}: {danmu_text}")
                                # 可以扩展处理其他消息类型：如 SEND_GIFT、INTERACT_WORD 等
                        elif op == 8:
                            # 操作码8：进入房间/认证成功的确认包
                            # 不做处理，继续等待“LIVE”指令
                            pass
                ws.close()
            except Exception as e:
                print(f"❌ WebSocket 监听异常：{e}")

    # 如果 WebSocket 检测不可用或发生异常，使用 HTTP 接口轮询直播状态
    print(f"📡 HTTP 轮询等待开播：{room_url}")
    while True:
        if http_check():
            print("📢 HTTP 检测到开播！")
            return True
        time.sleep(check_interval)

def parse_ws_slices(blob: bytes) -> list:
    """解析 WebSocket 数据包，提取可能包含的多条JSON消息"""
    results = []
    offset = 0
    # 按照弹幕协议逐段解析
    while offset + 16 <= len(blob):
        # 数据包长度、头部长度、版本、操作码
        packet_len = int.from_bytes(blob[offset:offset+4], "big")
        header_len = int.from_bytes(blob[offset+4:offset+6], "big")
        ver = int.from_bytes(blob[offset+6:offset+8], "big")
        op = int.from_bytes(blob[offset+8:offset+12], "big")
        body = blob[offset + header_len: offset + packet_len]
        if op == 5:  # 弹幕数据
            if ver in (2, 3):
                try:
                    if ver == 2:
                        body = zlib.decompress(body)
                    else:
                        body = brotli.decompress(body)
                except Exception:
                    pass
                # 递归解析解压后的数据
                results.extend(parse_ws_slices(body))
            else:
                try:
                    results.append(json.loads(body.decode("utf-8", errors="ignore")))
                except Exception:
                    pass
        # 移动偏移量到下一个包起始
        offset += packet_len
    return results

def danmu_listener(real_rid: str, danmaku_path: Path, start_time: datetime, stop_event: threading.Event):
    """独立线程：连接弹幕服务器抓取弹幕，自动重连并写入ASS弹幕文件"""
    # 如果弹幕ASS文件不存在，先写入ASS文件头
    if not danmaku_path.exists():
        danmaku_path.write_text(
            "[Script Info]\n"
            "ScriptType: v4.00+\n\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: default,Arial,36,&H00FFFFFF,&H0000FFFF,&H00000000,&H00000000,"
            "0,0,0,0,100,100,0,0,1,2,0,7,10,10,10,1\n\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n",
            encoding="utf-8"
        )
    while not stop_event.is_set():
        wss_url, token = get_danmu_server_info(real_rid)
        if not wss_url:
            time.sleep(check_interval)
            continue
        try:
            ws = websocket.create_connection(wss_url, timeout=10)
            # 发送认证包加入房间
            auth = {"uid": 0, "roomid": int(real_rid), "protover": 2, "platform": "web", "type": 2, "key": token}
            body = json.dumps(auth).encode()
            header = struct.pack(">IHHII", 16 + len(body), 16, 1, 7, 1)
            ws.send(header + body)

            # 开启心跳线程保持弹幕连接
            def send_heartbeats():
                packet = struct.pack(">IHHII", 16, 16, 1, 2, 1)
                while True:
                    try:
                        ws.send(packet)
                    except Exception:
                        break
                    time.sleep(30)
            threading.Thread(target=send_heartbeats, daemon=True).start()

            # 开始接收弹幕消息
            while not stop_event.is_set():
                msg = ws.recv()
                if not msg:
                    break
                # 弹幕服务器可能将多条弹幕打包在一起发送，逐条解析
                for sub_json in parse_ws_slices(msg if isinstance(msg, bytes) else msg.encode()):
                    if sub_json.get("cmd") == "DANMU_MSG":
                        text = sub_json["info"][1][1]  # 弹幕文本内容
                        # 计算弹幕出现的相对时间（从录制开始算起）
                        elapsed = datetime.now() - start_time
                        mm, ss = divmod(elapsed.seconds, 60)
                        ccc = int(elapsed.microseconds / 1000)
                        start_ts = f"0:{mm:02d}:{ss:02d}.{ccc:03d}"
                        end_time = elapsed + timedelta(seconds=5)
                        emm, ess = divmod(end_time.seconds, 60)
                        eccc = int(end_time.microseconds / 1000)
                        end_ts = f"0:{emm:02d}:{ess:02d}.{eccc:03d}"
                        # 将弹幕作为一行字幕写入 ASS 文件（5秒显示时间）
                        line = f"Dialogue: 0,{start_ts},{end_ts},default,,0,0,0,,{text}\n"
                        # 追加写入弹幕 ASS 文件
                        with danmaku_path.open("a", encoding="utf-8") as f:
                            f.write(line)
            ws.close()
        except Exception as e:
            print(f"⚠️ 弹幕监听异常，将在5秒后重连: {e}")
            time.sleep(5)
            # 不 set stop_event，允许自动重连
        finally:
            print("🛑 弹幕监听线程停止")
    # 只在 stop_event.set() 被主流程调用时才彻底退出

def record_stream(real_rid: str):
    """开始录制直播流：网络断开自动重连；下播或超时停止录制"""
    global stop_recording_flag
    stop_recording_flag = False

    # 获取当前直播标题用于文件名（可选）
    raw_title = get_live_title(real_rid)
    if not raw_title:
        # 如果没有获取到标题，就使用 prefix（去掉末尾下划线）代替
        raw_title = prefix.rstrip("_")
    # 准备本次录制文件的前缀（包含主播名、直播标题、日期）
    date_str = datetime.now().strftime("%m月%d号")
    session_prefix = f"{prefix}{raw_title}_{date_str}_"

    # 获取 SESSDATA（如有）用于 streamlink 请求
    sess = get_sessdata_from_cookie()
    cookie_args = ["--http-cookie", f"SESSDATA={sess}"] if sess else []

    # 为本次直播创建独立的存储文件夹（使用当前时间命名）
    ts_dir = Path(save_dir) / now_str()
    ts_dir.mkdir(parents=True, exist_ok=True)
    danmaku_file = ts_dir / "danmaku.ass"  # 弹幕文件路径
    start_time = datetime.now()

    # 创建 stop_event
    danmu_stop_event = threading.Event()  # 创建 stop_event

    # 启动弹幕监听线程（守护线程，在后台记录弹幕）
    thread = threading.Thread(
        target=danmu_listener,
        args=(real_rid, danmaku_file, start_time, danmu_stop_event),  # 传递正确的 stop_event
        daemon=True
    )
    thread.start()

    print(f"🟢 弹幕监听线程已启动，弹幕输出文件: {danmaku_file}")

    parts = []           # 保存本次所有录制的 ts 分段文件路径
    last_data_time = time.time()  # 记录上次成功写入数据的时间，用于超时判断

    # 发送 Telegram 开始录制通知
    send_tg_message(f"🟢 {session_prefix} 开始录制，时间：{now_str('%H:%M:%S')}")

    # 循环录制，自动重连
    while True:
        # 为新的片段生成文件名（当前时间为文件名）
        ts_filename = ts_dir / f"{now_str()}.ts"
        # 调用 streamlink 获取直播流，保存到文件
        cmd = ["streamlink"] + cookie_args + [
            "--retry-streams", "5", "--retry-max", "3",  # 尝试获取流的重试次数
            f"https://live.bilibili.com/{real_rid}", "best", "-o", str(ts_filename)
        ]
        for attempt in range(1, 4):
            res = subprocess.run(cmd)
            if res.returncode == 0:
                break
            else:
                send_tg_message(f"❌ 第{attempt}次拉流失败，错误码{res.returncode}")
                if attempt < 3:
                    time.sleep(5)
        else:
            send_tg_message("❌ 连续3次拉流失败，跳过本段")

        # 只在文件有效且未被添加时 append
        if ts_filename.exists() and ts_filename.stat().st_size > 1_048_576:  # >1MB视为有效片段
            if ts_filename not in parts:
                parts.append(ts_filename)
            last_data_time = time.time()

        # 若收到停止标志（来自外部下播通知），跳出循环结束录制
        if stop_recording_flag:
            break
        # 若超过设定时间无有效数据，则认为直播已结束，下播
        if time.time() - last_data_time > no_stream_timeout:
            print("🛑 长时间无数据，判断主播已下播，结束录制。")
            stop_recording_flag = True
            break

    # 录制结束，发送下播通知
    send_tg_message(f"🔴 {session_prefix} 检测到下播，停止录制，时间：{now_str('%H:%M:%S')}")
    danmu_stop_event.set()  # 停止弹幕监听

    # 合并所有录制的 ts 文件
    if parts:
        # 1) 生成清单文件（去重）
        unique_parts = []
        seen = set()
        for seg in parts:
            if seg not in seen:
                unique_parts.append(seg)
                seen.add(seg)
        list_file = ts_dir / "files.txt"
        with open(list_file, "w", encoding="utf-8") as lf:
            for seg in unique_parts:
                lf.write(f"file '{seg.as_posix()}'\n")

        # merged_ts 提前定义
        merged_ts = Path(save_dir) / f"{session_prefix}{now_str()}_ts.ts"
        def try_concat(retries=2):
            cmd = [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", str(list_file), "-c", "copy", str(merged_ts)
            ]
            for i in range(1, retries+1):
                res = subprocess.run(cmd)
                if res.returncode == 0:
                    send_tg_message(f"✅ 合并成功（第{i}次）")
                    return True
                else:
                    send_tg_message(f"❌ 合并失败（第{i}次），错误码 {res.returncode}")
                    if i < retries:
                        time.sleep(5)
            send_tg_message("❌ FFmpeg 合并最终失败")
            return False

        # 3) 执行合并，失败就退出
           # 3) 将弹幕嵌入到视频中
        if not try_concat():
            return  # 如果合并失败，则直接退出

    # 4) 生成无弹幕版本的视频
    no_danmu_video = Path(save_dir) / f"{session_prefix}{now_str()}_no_danmu.mp4"
    cmd_no_danmu = [
        "ffmpeg", "-i", str(merged_ts), "-c:v", "libx264", "-c:a", "aac", "-strict", "experimental", str(no_danmu_video)
    ]
    res_no_danmu = subprocess.run(cmd_no_danmu)
    if res_no_danmu.returncode == 0:
        send_tg_message(f"✅ 无弹幕视频生成成功：{no_danmu_video}")
    else:
        send_tg_message(f"❌ 无弹幕视频生成失败，错误码 {res_no_danmu.returncode}")
    
    
    if danmaku_file.exists():
        final_video = Path(save_dir) / f"{session_prefix}{now_str()}_with_danmu.mp4"
        cmd_danmu = [
            "ffmpeg", "-i", str(merged_ts), "-i", str(danmaku_file), "-c:v", "libx264", "-c:a", "aac",
            "-c:s", "mov_text", "-strict", "experimental", str(final_video)
        ]
        res = subprocess.run(cmd_danmu)
        if res.returncode == 0:
            print(f"✅ 弹幕视频生成成功：{final_video}")
        else:
            print(f"❌ 弹幕视频生成失败，错误码 {res.returncode}")

    

        



if __name__ == "__main__":
    # 提取真实房间号（处理短号情况）
    room_id_str = room_url.rstrip("/").split("/")[-1]
    real_rid = get_real_room_id(room_id_str)

    # 主循环：等待开播 -> 录制 -> 结束后继续等待下一次开播
    while True:
        try:
            if wait_for_live(real_rid):
                record_stream(real_rid)
        except Exception as e:
            print(f"❗ 主循环异常: {e}")
        # 等待一段时间再进行下一轮检测，防止过于频繁
        time.sleep(check_interval)
