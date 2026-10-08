"""最小本机网页 GUI：不安装前端依赖，不开放外网监听。"""

import argparse
import json
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

from ui_runtime import InterfaceController


PAGE = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ai-agent-lab GUI</title>
<style>body{font:18px sans-serif;max-width:850px;margin:30px auto;padding:12px}
input{width:70%;font:inherit}button{font:inherit}pre{white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid #bbb;padding:12px;max-height:260px;overflow:auto}</style>
<h1>ai-agent-lab 最小 GUI</h1><p id="mode"></p><p id="status">等待连接</p>
<form id="form"><input id="task" placeholder="37 + 58 / 查看当前项目目录" aria-label="输入任务">
<button id="send">提交</button></form><p>界面只提交任务和显示事件；不要输入密钥或私人资料。</p>
<h2>最终结果</h2><pre id="answer">尚无结果</pre><h2>状态与工具事件</h2><pre id="events"></pre>
<script>
const token=location.hash.slice(1); history.replaceState(null,'',location.pathname);
const headers={'X-Lab-Token':token,'Content-Type':'application/json'};
let submitting=false;
const task=document.getElementById('task'),send=document.getElementById('send');
async function refresh(){
 try{
  const r=await fetch('/events',{headers});if(!r.ok)throw Error('请使用终端输出的完整本机链接打开');
  const s=await r.json();document.getElementById('mode').textContent=s.demo?'本地模拟模型 · 无 API · 费用 0 元':'真实模型 · 本进程累计预算 1 元';
  const states=s.events.filter(e=>e.kind==='status');document.getElementById('status').textContent=states.at(-1)?.text||'就绪';
  document.getElementById('events').textContent=s.events.filter(e=>e.kind!=='status').map(e=>'['+e.kind+'] '+e.text).join('\n');
  const finals=s.events.filter(e=>e.kind==='final');document.getElementById('answer').textContent=finals.at(-1)?.text||'尚无结果';
  send.disabled=s.busy||submitting;task.disabled=s.busy;
 }catch(e){document.getElementById('status').textContent=e.message;send.disabled=true;}
}
document.getElementById('form').onsubmit=async e=>{
 e.preventDefault();if(submitting)return;submitting=true;send.disabled=true;
 try{const r=await fetch('/task',{method:'POST',headers,body:JSON.stringify({task:task.value})});
 const s=await r.json();if(!r.ok)document.getElementById('status').textContent=s.error;else task.value='';}
 finally{submitting=false;await refresh();}
};refresh();setInterval(refresh,500);
</script></html>'''


def create_server(controller, port=0):
    token = secrets.token_urlsafe(24)  # 仅本机界面令牌，不是 API Key。

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # 不记录链接中的令牌，也不记录用户任务。

        def reply(self, code, payload, html=False):
            body = (payload if html else json.dumps(payload, ensure_ascii=False)).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            host = f"127.0.0.1:{self.server.server_port}"
            return (self.headers.get("Host") == host
                    and self.headers.get("Origin", "http://" + host) == "http://" + host
                    and secrets.compare_digest(self.headers.get("X-Lab-Token", ""), token))

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/":
                self.reply(200, PAGE, html=True)
            elif path == "/events" and self.authorized():
                self.reply(200, controller.snapshot())
            else:
                self.reply(403, {"error": "拒绝访问：请使用本机完整链接。"})

        def do_POST(self):
            if urlsplit(self.path).path != "/task" or not self.authorized():
                self.reply(403, {"error": "拒绝访问"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 8192:
                    raise ValueError
                self.connection.settimeout(3)
                body = json.loads(self.rfile.read(size))
                accepted, note = controller.submit(body.get("task"))
            except (ValueError, TypeError, AttributeError, OSError):
                self.reply(400, {"error": "请求须为含 task 文本的 JSON，且不超过 8192 字节。"})
                return
            self.reply(202 if accepted else 409, {"ok": accepted, "error": "" if accepted else note})

    server = HTTPServer(("127.0.0.1", port), Handler)
    return server, f"http://127.0.0.1:{server.server_port}/#{token}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    controller = server = None
    try:
        controller = InterfaceController(demo=True)
        server, url = create_server(controller)
        print("本机 GUI 链接（只在本机打开）：", url, flush=True)
        print("Ctrl+C 停止服务；关闭浏览器标签不会停止服务。本节 GUI 仅本地模拟，无 API。", flush=True)
        server.serve_forever(poll_interval=0.2)
    except ValueError as error:
        print("配置错误：", error)
        return 1
    except KeyboardInterrupt:
        print("GUI 服务已停止。")
    finally:
        if server:
            server.server_close()
        if controller:
            controller.close()
            print("累计用量/预算：", json.dumps(controller.runtime.usage(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
