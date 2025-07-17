# 更新日志

2025年7月18日
- .ts 片段合并时重复引用问题，避免合并出错
- .ass 文件时间戳精度提升到三位
- 优化弹幕监听线程，异常时自动重连，只有主流程停止时才退出

---

# 功能预览

- 自动监测 B 站直播开播／下播（WebSocket + HTTP 轮询）  
- 分段录制（Streamlink 拉流 + 断线重连），无数据 10 分钟 停止  
- TS 片段合并（FFmpeg），保留源文件  
- 弹幕提取，输出 ASS，自动生成无弹幕 & 带弹幕 MP4  
- 可选 Telegram 通知（开始／结束），留空可跳过  
- 零改动启动：只需在脚本顶部设置房间号、保存目录、Cookie（SESSDATA）和可选 Bot 配置  
- 跨平台：Windows / macOS / Linux，纯 Python + FFmpeg + Streamlink

---

## 前置条件

- Python 3.8+  
- Streamlink  
- FFmpeg  
- 包含 `SESSDATA` 的 Cookie 文件

---

## 准备 Cookie 文件

1. 打开浏览器开发者工具  
   - 在 Chrome/Edge 中按 `F12` → 切到 **Application** / **Storage** → 展开 **Cookies** → 选择 `https://www.bilibili.com`  
2. 复制 `SESSDATA` 值  
   - 在 Cookie 列表里找到键名 `SESSDATA`，双击它的 **Value** 列，复制完整字符串  
3. 新建文本文件  
   - 在任意位置新建一个文本文件，命名为 `cookies.txt`  
4. 写入内容并保存  
   ```text
   SESSDATA=abcdefg123456...

      
      
## 安装依赖
windows：
  <pre markdown> bash pip install requests websocket-client brotli streamlink  </pre>
 

Linux/macOS:
      <pre markdown>pip3 install requests websocket-client brotli streamlink</pre>  
     
## 安装FFmpeg
 <pre markdown>https://ffmpeg.org/download.html</pre>  

## 启动脚本
新建bat文件（win）：
      <pre markdown>@echo off
      python "%~dp0recorder_id.py"
      pause
      </pre>

新建.sh文件（macos/linux）：
        <pre markdown>python3 recorder_id.py</pre>
        执行一下权限命令并启动（macos/linux）：<pre markdown>chmod +x start.sh<br/>./start.sh```</pre>
