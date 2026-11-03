#!/usr/bin/env python3
"""Local preview-first control panel. No permanent deletion or background scans."""
import argparse
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import webbrowser

sys.dont_write_bytecode = True
from safe_cleanup import Cleaner, SafetyError, no_links, write_json, read_json, key
from reviewed_cleanup import ReviewedCleanup
import inventory
from file_cleanup import FileCleanup

PANEL_HTML = Path(__file__).parent.parent / 'assets' / 'panel.html'


class Panel:
    def __init__(self, state):
        self.cleaner = Cleaner(state)
        self.state = self.cleaner.state
        self.token = secrets.token_hex(32)
        self.last_access = time.monotonic()
        self.scan_lock = threading.Lock()
        self.cleanup = ReviewedCleanup(state)
        self.file_cleanup = FileCleanup(state)
        self.jobs = {}
        self.jobs_lock = threading.Lock()
        self.scan_cancellations = {}

    def launch_job(self, function, kind='operation', cancel=None):
        if not self.scan_lock.acquire(blocking=False):
            raise SafetyError('已有操作正在进行，请等待完成')
        jid = key()
        self.jobs[jid] = {'state': 'running', 'message': '正在准备操作', 'kind':kind, 'started_at':time.time()}
        if cancel is not None:
            self.scan_cancellations[jid] = cancel
        def progress(message, **values):
            with self.jobs_lock:
                self.jobs[jid].update(message=message, **values)
        def run():
            try:
                value = function(progress)
                if kind == 'preview' and cancel is not None and cancel.is_set():
                    raise SafetyError('本次预览已取消；未移动文件')
                with self.jobs_lock:
                    self.jobs[jid].update(state='done', message='操作完成', result=value)
            except Exception as e:
                with self.jobs_lock:
                    detail = str(e)
                    message = detail if len(detail) < 180 and not re.search(r'[A-Za-z]:[\\/]', detail) else '操作未完成，内容继续保留。可展开详细原因。'
                    self.jobs[jid].update(state='error', message=message, detail=detail)
            finally:
                self.scan_cancellations.pop(jid, None)
                self.scan_lock.release()
        threading.Thread(target=run, daemon=True).start()
        return {'job': jid}

    def preview_job(self, function):
        cancel = threading.Event()
        def run(progress):
            def checked_progress(message, **values):
                if cancel.is_set():
                    raise SafetyError('本次预览已取消；未移动文件')
                progress(message, **values)
            return function(checked_progress)
        return self.launch_job(run, 'preview', cancel)

    def bootstrap(self):
        paths = list((self.state / 'codex-reports').glob('*.json'))
        latest=None
        expected_signature=inventory.metadata_signature(self.state)
        for file in sorted(paths,key=lambda p:p.stat().st_mtime,reverse=True):
            try:
                report=read_json(file)
                if (report.get('scope_version')==inventory.SCOPE_VERSION
                        and report.get('metadata_signature')==expected_signature
                        and [x['path'] for x in report.get('coverage',[])]==[x['path'] for x in inventory.discover(self.state)]):
                    latest=report;break
            except (OSError,ValueError):continue
        with self.jobs_lock:
            active = next((jid for jid, j in self.jobs.items() if j['state'] == 'running'), None)
        return {'latest':latest, 'active_job':active, 'candidates':latest.get('candidates',[]) if latest else [], 'cleanup_enabled':True, 'history':self.file_cleanup.history()+[dict(x,kind='legacy') for x in self.cleanup.history()],
                'manual_paused':self.cleanup.paused(), 'roots':[x['path'] for x in self.inventory(scopes=True)['scopes']]}

    def run(self, command):
        if command not in ('status', 'scan'):
            raise ValueError('面板只允许扫描和读取状态')
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name('safe_cleanup.py')),
             '--state', str(self.state), command],
            capture_output=True, text=True, encoding='utf-8', timeout=180, check=False,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or '扫描未完成，文件继续保留')
        return json.loads(result.stdout)

    def inventory(self, scopes=False, progress=None, cancel=None):
        if scopes:
            return {'scopes':inventory.discover(self.state)}
        with self.cleaner.locked():
            ctx=inventory.context(self.state)
            report=inventory.run_inventory(self.state,ctx=ctx,progress=progress,cancel=cancel)
            if report['project_count']!=len(ctx.get('projects',[])) or report['thread_count']!=len(ctx.get('threads',[])):
                raise SafetyError('项目关联信息没有完整读取，本轮不作为检查成功，请刷新项目索引')
            return report

    def server(self):
        panel = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Do not record capability URLs or local file paths in HTTP logs.

            def reply(self, status, value, html=False):
                body = value.encode('utf-8') if html else json.dumps(value, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'text/html; charset=utf-8' if html else 'application/json; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Content-Security-Policy',
                                 "default-src 'none'; connect-src 'self'; style-src 'unsafe-inline'; "
                                 f"script-src 'nonce-{panel.token}'; base-uri 'none'; frame-ancestors 'none'")
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def route(self, post=False):
                host = '127.0.0.1:' + str(self.server.server_port)
                if self.headers.get('Host') != host:
                    return self.reply(403, {'error': '只允许本机面板访问'})
                prefix = '/' + panel.token + '/'
                path = urlsplit(self.path).path
                if not path.startswith(prefix):
                    return self.reply(404, {'error': '无效的面板地址'})
                if post:
                    if not hmac.compare_digest(self.headers.get('X-Codex-Panel', ''), panel.token):
                        return self.reply(403, {'error': '缺少面板操作校验'})
                    if self.headers.get('Origin') not in (None, 'http://' + host):
                        return self.reply(403, {'error': '拒绝来自其他页面的操作'})
                route = path[len(prefix):]
                panel.last_access = time.monotonic()
                try:
                    body = None
                    if post and route.startswith(('api/cleanup-', 'api/stop', 'api/inventory')):
                        n = int(self.headers.get('Content-Length', '0'))
                        if n < 0 or n > 8192:
                            return self.reply(400, {'error':'无效的请求大小'})
                        body = json.loads(self.rfile.read(n)) if n else {}
                        if not isinstance(body, dict):
                            return self.reply(400, {'error':'无效的操作参数'})
                    if not post and route == '':
                        return self.reply(200, PANEL_HTML.read_text(encoding='utf-8').replace('__NONCE__', panel.token), html=True)
                    if post and route in ('api/inventory', 'api/inventory-start') and body:
                        return self.reply(400, {'error':'扫描范围固定为 Codex 数据目录，不能通过网页参数扩展'})
                    if not post and route == 'api/bootstrap':
                        return self.reply(200, panel.bootstrap())
                    if not post and route.startswith('api/jobs/'):
                        jid = route[len('api/jobs/'):]
                        with panel.jobs_lock:
                            result = dict(panel.jobs.get(jid, {}))
                        return self.reply(200 if result else 404, result or {'error':'操作记录已失效，请查看隔离记录'})
                    if post and route.startswith('api/cleanup-') and route not in ('api/cleanup-restore', 'api/cleanup-cancel-preview', 'api/cleanup-file-preview', 'api/cleanup-file-recycle'):
                        return self.reply(403, {'error':'旧版整目录清理接口停用；请盘点所有 Codex 项目，核实精确文件后通过当前清理清单确认处理。'})
                    if post and route == 'api/cleanup-file-preview':
                        return self.reply(200, panel.preview_job(lambda progress:panel.file_cleanup.preview(body.get('report'),body.get('ids'),progress)))
                    if post and route == 'api/cleanup-file-recycle':
                        return self.reply(200, panel.launch_job(lambda progress:panel.file_cleanup.execute(body.get('plan'),body.get('confirmation'),progress,sync_ack=body.get('sync_confirmation'))))
                    if post and route == 'api/cleanup-restore':
                        if body.get('confirmation') != 'RESTORE_RECORDED_CONTENT':
                            return self.reply(403, {'error':'请先查看恢复清单并明确确认；不会覆盖已有文件'})
                        return self.reply(200, panel.launch_job(lambda progress:(panel.file_cleanup if body.get('kind')=='files' else panel.cleanup).restore(body.get('batch'), progress)))
                    if not post and route == 'api/ping':
                        return self.reply(200, {'ready': True, 'pid': os.getpid(), 'version': 7})
                    if not post and route == 'api/status':
                        s = panel.run('status')
                        scopes = panel.inventory(scopes=True)['scopes']
                        return self.reply(200, {'roots': [x['path'] for x in scopes], 'paused': s['paused'],
                                                'approved': bool(s['approved_policy'])})
                    if post and route == 'api/scan':
                        if not panel.scan_lock.acquire(blocking=False):
                            return self.reply(409, {'error': '已有一次扫描正在进行，请稍候'})
                        try:
                            return self.reply(200, panel.inventory())
                        finally:
                            panel.scan_lock.release()
                    if post and route == 'api/inventory':
                        if not panel.scan_lock.acquire(blocking=False):
                            return self.reply(409, {'error': '已有扫描正在进行，请稍候'})
                        try:
                            return self.reply(200, panel.inventory())
                        finally:
                            panel.scan_lock.release()
                    if post and route == 'api/inventory-start':
                        cancel = threading.Event()
                        return self.reply(200, panel.launch_job(
                            lambda progress:panel.inventory(progress=progress, cancel=cancel), 'inventory', cancel))
                    if post and route in ('api/inventory-cancel', 'api/cleanup-cancel-preview'):
                        jid = body.get('job')
                        if not isinstance(jid, str):
                            return self.reply(400, {'error':'无效扫描编号'})
                        cancel = panel.scan_cancellations.get(jid)
                        expected = 'inventory' if route == 'api/inventory-cancel' else 'preview'
                        if panel.jobs.get(jid, {}).get('kind') != expected:
                            return self.reply(400, {'error':'该入口不能取消其他类型的操作'})
                        if cancel is not None:
                            cancel.set()
                        return self.reply(200, {'cancel_requested':cancel is not None})
                    if post and route == 'api/stop':
                        if panel.scan_lock.locked():
                            return self.reply(409, {'error':'操作尚未完成，请完成后再关闭面板'})
                        self.reply(200, {'stopped': True})
                        threading.Thread(target=self.server.shutdown, daemon=True).start()
                        return
                    return self.reply(404, {'error': '面板没有此操作'})
                except (RuntimeError, SafetyError, OSError, ValueError, subprocess.TimeoutExpired) as e:
                    return self.reply(500, {'error': str(e)})

            def do_GET(self):
                self.route()

            def do_POST(self):
                self.route(post=True)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        return server


def ping(record):
    port, token = record.get('port'), record.get('token')
    if not isinstance(port, int) or not 1 <= port <= 65535 or not re.fullmatch('[a-f0-9]{64}', str(token)):
        return None
    url = f'http://127.0.0.1:{port}/{token}/'
    try:
        with urlopen(url + 'api/ping', timeout=1) as r:
            data = json.load(r)
        if data.get('ready') and data.get('pid') == record.get('pid') and data.get('version') == 7:
            return url
    except (OSError, ValueError):
        pass
    return None


def serve(state, idle_minutes):
    panel = Panel(state)
    panel.cleaner.cfg()  # Existing configuration required; never initialize or authorize here.
    server = panel.server()
    record = panel.state / 'panel.json'
    write_json(record, {'pid': os.getpid(), 'port': server.server_port, 'token': panel.token})
    done = threading.Event()
    def expire():
        while not done.wait(5):
            if time.monotonic() - panel.last_access > idle_minutes * 60 and not panel.scan_lock.locked():
                server.shutdown()
                return
    threading.Thread(target=expire, daemon=True).start()
    try:
        server.serve_forever(poll_interval=.25)
    finally:
        done.set()
        server.server_close()


def start(state):
    cleaner = Cleaner(state)
    if not cleaner.config_file.exists():
        with cleaner.locked():
            if not cleaner.config_file.exists():
                cleaner.init([])  # Tool-owned empty configuration, no cleanup authorization.
    cleaner.cfg()
    record = no_links(cleaner.state / 'panel.json')
    if record.exists():
        try:
            url = ping(read_json(record))
            if url:
                return url
        except (OSError, ValueError):
            pass
    log = no_links(cleaner.state / 'panel-service.log')
    with log.open('ab') as f:
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), '--serve', '--state', str(cleaner.state)],
            stdin=subprocess.DEVNULL, stdout=f, stderr=f,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
        )
    for _ in range(50):
        if child.poll() is not None:
            raise RuntimeError('面板启动失败，请检查 panel-service.log')
        if record.exists():
            data = read_json(record)
            if data.get('pid') == child.pid:
                url = ping(data)
                if url:
                    return url
        time.sleep(.1)
    raise RuntimeError('面板尚未就绪，请稍后重试')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state', default=str(Path(os.environ['LOCALAPPDATA']) / 'CodexSafeCleanup'))
    p.add_argument('--serve', action='store_true')
    p.add_argument('--open', action='store_true', help='在系统浏览器打开；Codex 内使用 open_in_codex')
    p.add_argument('--idle-minutes', type=int, default=30)
    args = p.parse_args()
    if args.serve:
        serve(args.state, args.idle_minutes)
    else:
        url = start(args.state)
        if args.open:
            webbrowser.open(url)
        print(json.dumps({'url': url, 'requires_cleanup_confirmation': True, 'mode':'codex_only_scan'}))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as e:
        sys.stderr.write(str(e) + '\n')
        raise SystemExit(2)
