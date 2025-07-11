Python 3.8+

Streamlink

FFmpeg

pip 包：`requests`、`websocket-client`、`brotli`

包含 SESSDATA 的 Cookie 文件
  准备 Cookie 文件：
      
      1. 打开浏览器开发者工具
      - 在 Chrome/Edge 中，按 `F12` → 切到 “Application”（应用）/“Storage”（储存）→ 展开 **Cookies** → 选择 `https://www.bilibili.com`。
      
      2. 复制 SESSDATA 值
      - 在 Cookie 列表里找到键名 `SESSDATA`，双击它的 Value 列，复制完整字符串
      
      3. 新建文本文件
      - 在任意位置新建一个文本文件，命名为 `cookies.txt`
      
      4. 写入内容并保存
      - 就像这样:"SESSDATA=abcdefg123456... "

      
      
windows：
  <pre markdown> bash pip install requests websocket-client brotli streamlink  </pre>
  ffmpeg：<pre markdown> https://ffmpeg.org/download.html  </pre>


  
