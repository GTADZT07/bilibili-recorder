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

# 全局事件与标志
start_record_event   = threading.Event()  # 收到开播通知
stop_recording_flag  = False              # 收到下播通知或超时需停止录制

# 确保保存目录存在
Path(save_dir).mkdir(parents=True, exist_ok=True)

def now_str(fmt="%Y%m%d_%H%M%S"):
    return datetime.now().strftime(fmt)

def send_tg_message(text: str):
    """Telegram 通知（未配置或失败时安全跳过，不影响录播）"""
    # 如果没填 bot_token 或 chat_id，就跳过而不抛错
    if not bot_token or not chat_id:
        print("⚠️ 未配置 Telegram，跳过通知")
        return

    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=5)
    except Exception as e:
        print(f"❌ Telegram 发送失败: {e}")
def get_sessdata_from_cookie() -> str:
    """从 Cookie 文件中提取 SESSDATA"""
    try:
        content = Path(cookie_file).read_text(encoding="utf-8")
        m = re.search(r"SESSDATA=([^;\s]+)", content)
        if m:
            return m.group(1)
    except Exception as e:
        print(f"⚠️ 读取 Cookie 失败: {e}")
    return ""

def get_cookie_header() -> dict:
    sd = get_sessdata_from_cookie()
    if sd:
        return {"Cookie": f"SESSDATA={sd}"}
    return {}
def get_live_title(real_rid: str) -> str:
    """拉取当前直播标题，替换掉文件名非法字符"""
    headers = {"User-Agent": "Mozilla/5.0"}
    headers.update(get_cookie_header())
    try:
        r = requests.get(
            f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={real_rid}",
            headers=headers, timeout=5
        ).json()
        if r.get("code") == 0:
            raw = r["data"].get("title", "").strip()
            # 把 / \ : * ? " < > | 都替换成下划线
            return re.sub(r'[\/\\\:\*\?"<>\|]', "_", raw)
    except Exception as e:
        print(f"⚠️ 获取标题失败：{e}")
    return ""
def get_real_room_id(rid: str) -> str:
    """将短号转换为真实房间号"""
    headers = {"User-Agent":"Mozilla/5.0"} 
    headers.update(get_cookie_header())
    try:
        r = requests.get(
            f"https://api.live.bilibili.com/room/v1/Room/room_init?id={rid}",
            headers=headers, timeout=5
        ).json()
        if r.get("code")==0:
            return str(r["data"]["room_id"])
    except Exception as e:
        print(f"⚠️ 获取真实房间ID失败: {e}")
    return rid

def get_danmu_server_info(rid: str):
    """获取 DanMu WebSocket 信息"""
    headers = {"User-Agent":"Mozilla/5.0"}
    headers.update(get_cookie_header())
    try:
        r = requests.get(
            f"https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo?id={rid}",
            headers=headers, timeout=5
        ).json()
        if r.get("code")==0:
            data = r["data"]
            host = data["host_list"][0]["host"]
            token = data["token"]
            port = data["host_list"][0].get("wss_port", 443)
            return f"wss://{host}:{port}/sub", token
    except Exception as e:
        print(f"⚠️ 获取弹幕服务器信息失败: {e}")
    return None, None

def wait_for_live(real_rid: str) -> bool:
    """
    等待开播：优先 WebSocket，失败后退回 HTTP 轮询。
    开播时返回 True。
    """
    # 先检查当前状态
    def http_check():
        headers = {"User-Agent":"Mozilla/5.0"}
        headers.update(get_cookie_header())
        try:
            r = requests.get(
                f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={real_rid}",
                headers=headers, timeout=5
            ).json()
            return r.get("data",{}).get("live_status",0)!=0
        except:
            return False

    if WS_AVAILABLE:
        wss_url, token = get_danmu_server_info(real_rid)
        if wss_url and token:
            print(f"📺 使用 WebSocket 监听开播：{room_url}")
            try:
                ws = websocket.create_connection(wss_url, timeout=10)
                # send auth
                auth = {
                    "uid":0,"roomid":int(real_rid),
                    "protover":2,"platform":"web","type":2,"key":token
                }
                body = json.dumps(auth).encode()
                head = struct.pack(">IHHII",16+len(body),16,1,7,1)
                ws.send(head+body)
                # 开启心跳
                def hb():
                    pkt = struct.pack(">IHHII",16,16,1,2,1)
                    while True:
                        try: ws.send(pkt)
                        except: break
                        time.sleep(30)
                threading.Thread(target=hb,daemon=True).start()

                # 等消息
                while True:
                    msg = ws.recv()
                    if not msg: break
                    # 解析包头
                    if isinstance(msg,bytes) and len(msg)>=16:
                        op = struct.unpack(">I",msg[8:12])[0]
                        if op==5:
                            # 解压或直接 JSON
                            ver = struct.unpack(">H",msg[6:8])[0]
                            body = msg[16:]
                            if ver==2:
                                try: body = zlib.decompress(body)
                                except: pass
                            for sub in parse_ws_slices(body):
                                cmd = sub.get("cmd","")
                                if cmd=="LIVE":
                                    print("📢 WebSocket 检测到开播！")
                                    ws.close()
                                    return True
                                
                                elif cmd == "DANMU_MSG":
                                    # sub["info"] 是个列表，结构是 [弹幕文本, 用户信息, …]
                                    danmu_text = sub["info"][1][1]
                                    user      = sub["info"][2][1]
                                    print(f"[弹幕] {user}: {danmu_text}")
                                # 你还可以捕获其他事件： SEND_GIFT、INTERACT_WORD 等
                        elif op==8:
                            # auth ok
                            pass
                ws.close()
            except Exception as e:
                print(f"❌ WebSocket 监听异常：{e}")

    # WebSocket 失败，退 HTTP 轮询
    print(f"📡 HTTP 轮询等待开播：{room_url}")
    while True:
        if http_check():
            print("📢 HTTP 检测到开播！")
            return True
        time.sleep(check_interval)

def parse_ws_slices(blob: bytes):
    """提取 WebSocket 多包 JSON"""
    out=[]
    offset=0
    while offset+16<=len(blob):
        plen = int.from_bytes(blob[offset:offset+4],"big")
        hlen = int.from_bytes(blob[offset+4:offset+6],"big")
        ver  = int.from_bytes(blob[offset+6:offset+8],"big")
        op   = int.from_bytes(blob[offset+8:offset+12],"big")
        body = blob[offset+hlen:offset+plen]
        if op==5:
            if ver in (2,3):
                try:
                    if ver==2: body = zlib.decompress(body)
                    else: import brotli; body = brotli.decompress(body)
                except: pass
                out.extend(parse_ws_slices(body))
            else:
                try:
                    out.append(json.loads(body.decode("utf-8",errors="ignore")))
                except:
                    pass
        offset += plen
    return out

def danmu_listener(real_rid: str, danmaku_file: Path, start_time: datetime):
    """专门负责长连接拿弹幕，遇断自动重连"""
    print("🔔 danmu_listener 启动，开始订阅弹幕")
        # ASS 文件头，只写一次
    if not danmaku_file.exists():
        danmaku_file.write_text(
            "[Script Info]\n"
            "ScriptType: v4.00+\n"
            "\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: default,Arial,36,&H00FFFFFF,&H0000FFFF,&H00000000,&H00000000,"
            "0,0,0,0,100,100,0,0,1,2,0,7,10,10,10,1\n\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        , encoding="utf-8")
    while True:
        wss_url, token = get_danmu_server_info(real_rid)
        if not wss_url:
            time.sleep(check_interval)
            continue

        try:
            ws = websocket.create_connection(wss_url, timeout=10)
            # auth 包
            auth = {"uid":0,"roomid":int(real_rid),"protover":2,"platform":"web","type":2,"key":token}
            body = json.dumps(auth).encode()
            head = struct.pack(">IHHII", 16+len(body),16,1,7,1)
            ws.send(head+body)
            
            # 心跳
            def hb():
                pkt = struct.pack(">IHHII",16,16,1,2,1)
                while True:
                    try: ws.send(pkt)
                    except: break
                    time.sleep(30)
            threading.Thread(target=hb, daemon=True).start()
                        # —— 在这里开始接收消息 —— #
            while True:
                msg = ws.recv()
                if not msg:
                    break
                # 假设 msg 已经是 bytes，需要解包并解析
                for sub in parse_ws_slices(msg):
                    if sub.get("cmd") == "DANMU_MSG":
                        print("📨 收到弹幕包：", sub) 
                        text = sub["info"][1][1]
                        # 计算相对时间
                        delta = datetime.now() - start_time
                        mm, ss = divmod(delta.seconds, 60)
                        cc = int(delta.microseconds/10000)
                        start_ts = f"0:{mm:02d}:{ss:02d}.{cc:02d}"
                        end_delta = delta + timedelta(seconds=5)
                        emm, ess = divmod(end_delta.seconds, 60)
                        ecc = int(end_delta.microseconds/10000)
                        end_ts = f"0:{emm:02d}:{ess:02d}.{ecc:02d}"
                        # ASS 一行
                        line = (f"Dialogue: 0,{start_ts},{end_ts},default,"
                                f"*,0,0,0,,{text}\n")
                        danmaku_file.open("a", encoding="utf-8").write(line)
            ws.close()

        except Exception as e:
            print("弹幕通道异常，5秒后重连：", e)
            time.sleep(5)

def record_stream(real_rid: str):
    """开始录制——遇断自动重连；断播超时或下播通知则结束"""
    global stop_recording_flag
    stop_recording_flag = False
    
    # 如果 WebSocket 可用且已获取 host/token，可再单独监听 PREPARING 触发下播
    # 这里略，可自行扩展 on_message 逻辑。
    raw_title = get_live_title(real_rid)
    if not raw_title:
        # 去掉尾部下划线和方括号
        raw_title = prefix.rstrip("_").strip("【】")
    # —— 3. 再拼上几月几号 —— #
    date_str = datetime.now().strftime("%m月%d号")
    # —— 4. 最终前缀：prefix+标题+日期+下划线 —— #
    session_prefix = f"{prefix}{raw_title}_{date_str}_"
    # 读取 cookie
    sess = get_sessdata_from_cookie()
    cookie_args = ["--http-cookie",f"SESSDATA={sess}"] if sess else []

    # 组目录、文件名
    ts_dir = Path(save_dir) / now_str()
    ts_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    danmaku_file = ts_dir / "danmaku.ass"
    start_time = datetime.now()
    threading.Thread(
        target=danmu_listener,
        args=(real_rid, danmaku_file, start_time),
        daemon=True
    ).start()
    print(f"🟢 已启动弹幕监听线程，输出文件：{danmaku_file}")
    parts=[]
    last_data = time.time()

    send_tg_message(f"🟢{session_prefix}开始录制：{now_str('%H:%M:%S')}")

    
    while True:
        # 录一段到 ts_dir
        fn = ts_dir / f"{now_str()}.ts"
        cmd = ["streamlink"] + cookie_args + [
            "--retry-streams","5","--retry-max","3",
            f"https://live.bilibili.com/{real_rid}",
            "best","-o", str(fn)
        ]
        subprocess.run(cmd)
        if fn.exists() and fn.stat().st_size>1_048_576:
            parts.append(fn)
            last_data = time.time()
        # 若收到外部停止标志，也跳出
        if stop_recording_flag:
            break
        # 超时检测：超 no_stream_timeout 秒没数据，判断为下播
        if time.time()-last_data > no_stream_timeout:
            print("🛑 超时未检测到数据，结束录制。")
            break     
    
    # 下播通知
    send_tg_message(f"🔴{session_prefix}检测到下播：{now_str('%H:%M:%S')}")

    # 合并
    if parts:
        listf = ts_dir / "files.txt"
        with open(listf,"w",encoding="utf-8") as f:
            for p in parts:
                f.write(f"file '{p.as_posix()}'\n")
        out_ts = Path(save_dir)/ f"{session_prefix}{now_str()}_ts.ts"
        subprocess.run([
            "ffmpeg","-f","concat","-safe","0",
            "-i", str(listf), "-c","copy", str(out_ts)
        ])
        send_tg_message(f"🎬{session_prefix}录制完成：{out_ts.name}\n🕒 时长：{str(timedelta(seconds=int(time.time()-last_data)))}")

        no_dm_mp4 = Path(save_dir) / f"{session_prefix}{now_str()}_nodm.mp4"
        subprocess.run([
            "ffmpeg", "-y",
            "-i", str(out_ts),
            "-c:v", "copy", "-c:a", "copy",
            str(no_dm_mp4)
        ])
        send_tg_message(f"🎥{session_prefix}无弹幕版本已生成：{no_dm_mp4.name}")
        # 加弹幕版
        danmaku_file = ts_dir / "danmaku.ass"
        if danmaku_file.exists() and danmaku_file.stat().st_size > 0:
            dm_mp4 = Path(save_dir) / f"{session_prefix}{now_str()}_withdm.mp4"
            subprocess.run([
                "ffmpeg", "-y",
                "-i", str(out_ts),
                "-vf", f"subtitles={danmaku_file.as_posix()}",
                "-c:a", "copy",
                str(dm_mp4)
            ])
            send_tg_message(f"🎉{session_prefix}有弹幕版本已生成：{dm_mp4.name}")
        else:
            send_tg_message(f"⚠️ 未抓到任何弹幕，跳过有弹幕版生成")
    else:
        print("⚠️ 本次未录到任何数据。")
        send_tg_message(f"⚠️{session_prefix}未录到任何内容")

if __name__ == "__main__":
    # 初始化真实房间号
    short = room_url.rstrip("/").split("/")[-1]
    real_rid = get_real_room_id(short)

    # 主循环：等开播 -> 录制 -> 循环
    while True:
        try:
            if wait_for_live(real_rid):
                record_stream(real_rid)
        except Exception as e:
            print(f"❗ 主流程异常: {e}")
        time.sleep(check_interval)
