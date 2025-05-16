import os
import asyncio
import urllib.parse
import time
import re
import concurrent.futures
from threading import Lock
from collections import defaultdict
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from yt_dlp import YoutubeDL
import boto3
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from subprocess import Popen
from typing import Dict, Tuple
import botocore.config
from boto3.s3.transfer import TransferConfig
import contextlib
import io
import sys


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

class YTDLPLogger:
    def debug(self, msg):
        print(f"[yt-dlp DEBUG] {msg}")

    def info(self, msg):
        print(f"[yt-dlp INFO] {msg}")

    def warning(self, msg):
        print(f"[yt-dlp WARN] {msg}")

    def error(self, msg):
        print(f"[yt-dlp ERROR] {msg}")

main_loop = asyncio.get_event_loop()
download_procs: Dict[Tuple[str,str,str], Popen] = {}
BASE_DIR    = Path(__file__).parent
DOWNLOAD_DIR= BASE_DIR / "downloads"
COOKIE_DIR  = BASE_DIR / "cookies"

# 在挂载前创建
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(COOKIE_DIR,   exist_ok=True)


# 配置


BASE_PUBLIC_R2_URL = 'https://pub-97c6ef35e0ee4959894afa0e3d88607f.r2.dev'

r2_client = boto3.client(
    's3',
    endpoint_url='https://10e344035791e1b2db58758de93abac4.r2.cloudflarestorage.com',
    aws_access_key_id='888250588e84e29be21bfe081c6b0bcc',
    aws_secret_access_key='bd627abb9505a0531f84813cea8105184cf265e673c3b9020913d2ac220c936d'
)

app = FastAPI()
app.mount("/downloads",  StaticFiles(directory=str(DOWNLOAD_DIR)), name="downloads")

class VideoRequest(BaseModel):
    url: str

MAX_CONCURRENT_PER_USER = 1

progress_lock = Lock()
active_connections = defaultdict(dict)
task_queues = defaultdict(asyncio.Queue)
active_tasks = defaultdict(set)
download_links = defaultdict(dict)
download_progress = defaultdict(dict)
user_workers = set()
cancel_flags = defaultdict(dict)  # 添加取消标志字典

def extract_video_id(url: str):
    parsed = urlparse(url)
    return parse_qs(parsed.query).get("v", [""])[0]

def get_video_filename(url, format_id):
    with YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
        info = ydl.extract_info(url, download=False)
        return f"{info['id']}_{format_id}.mp4"



def build_progress_data(d):
    downloaded = d.get('downloaded_bytes', 0)
    total = d.get('total_bytes', 0) or d.get('total_bytes_estimate', 0)
    percent = f"{(downloaded/total)*100:.1f}%" if total else "0.0%"
    speed_str = f"{d.get('speed', 0)/1024:.1f}KB/s" if d.get('speed') else "-"
    eta_str = f"{d.get('eta', 0)}s" if d.get('eta') else "-"

    return {
        'status': d.get('status', ''),
        'percent': percent,
        'downloaded': f"{downloaded / 1024 / 1024:.2f}MB",
        'total': f"{total / 1024 / 1024:.2f}MB" if total else "未知",
        'speed': speed_str,
        'eta': eta_str,
        'timestamp': time.time()
    }

async def upload_to_r2(file_name):
    file_path = os.path.join(DOWNLOAD_DIR, file_name)
    encoded_file_name = urllib.parse.quote(file_name, safe='')
    key = f"your/folder/{file_name}"
    public_url = f"{BASE_PUBLIC_R2_URL}/your/folder/{encoded_file_name}"

    def _upload():
        try:
            print(f"[UPLOAD] 准备上传文件到 R2: {file_path}")
            config = botocore.config.Config(connect_timeout=10, read_timeout=60)
            transfer_cfg = TransferConfig(multipart_threshold=100 * 1024 * 1024)  # 超过100MB才分块上传

            r2_client.upload_file(
                file_path,
                "videodown",
                key,
                Config=transfer_cfg
            )
            os.remove(file_path)
            print(f"[UPLOAD ✅] 上传成功: {public_url}")
            return public_url
        except Exception as e:
            print(f"[UPLOAD ❌] 上传失败: {e}")
            return None

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _upload)

class SilentLogger:
    def debug(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): print(f"[yt-dlp ERROR] {msg}")


def download_in_thread(user_id: str, video_id: str, url: str, format_id: str):
    try:
        if user_id not in cancel_flags:
            cancel_flags[user_id] = {}
        cancel_flags[user_id][(video_id, format_id)] = False

        with YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get("title", "video")
            title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_', '.'))
            title = title[:50]
            file_name = f"{video_id}_{title}.mp4"

        output_path = os.path.join(DOWNLOAD_DIR, file_name)
        websocket = active_connections.get(user_id, {}).get(video_id)

        def yt_hook(d):
            if cancel_flags.get(user_id, {}).get((video_id, format_id)):
                raise Exception("Cancelled by user")
            if d.get("status") == "downloading":
                progress = build_progress_data(d)
                if websocket:
                    asyncio.run_coroutine_threadsafe(websocket.send_json(progress), main_loop)

        ydl_opts = {
            "format": f"{format_id}+bestaudio/best",
            "merge_output_format": "mp4",
            "outtmpl": output_path,
            "progress_hooks": [yt_hook],
            "logger": YTDLPLogger(), 
            "verbose": True,
            "verbose": True,
            "retries": 10,
            "fragment_retries": 10,
            "concurrent_fragment_downloads": 1
            }

        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        return True

    except Exception as e:
        print(f"[Download Error] {e}")
        return False

    finally:
        if user_id in cancel_flags and (video_id, format_id) in cancel_flags[user_id]:
            del cancel_flags[user_id][(video_id, format_id)]

 # 根据 format_id 检查是否有声音       
def format_has_audio(formats, format_id):
    for f in formats:
        if f.get('format_id') == format_id:
            return f.get('acodec') != 'none'
    return False

async def download_video(url, format_id, user_id):
    video_id = extract_video_id(url)
    print(f"[后台] ✅ 进入 download_video(): user_id={user_id}, video_id={video_id}, format_id={format_id}")
    # 获取视频信息以获取标题
    with YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
        info = ydl.extract_info(url, download=False)
        title = info.get('title', 'video')
        # 清理文件名中的非法字符
        title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_', '.'))
        # 限制标题长度
        title = title[:50]
        file_name = f"{video_id}_{title}.mp4"
    
    download_path = os.path.join(DOWNLOAD_DIR, file_name)

    # 确保下载目录存在
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    try:
        print(f"[后台] 开始下载任务: user_id={user_id}, video_id={video_id}, format_id={format_id}")

        # 在独立线程中执行下载（避免阻塞主事件循环）
        with concurrent.futures.ThreadPoolExecutor() as executor:
            success = await asyncio.get_event_loop().run_in_executor(
                executor, download_in_thread, user_id, video_id, url, format_id
            )

        if success and os.path.exists(download_path):
            print(f"[后台] 下载完成，准备上传到 R2: {file_name}")

                # 推送 preparing 状态到 WebSocket
            websocket = active_connections.get(user_id, {}).get(video_id)
            if websocket:
                if main_loop.is_running():
                    main_loop.call_soon_threadsafe(
                        asyncio.create_task, websocket.send_json({
                            "status": "preparing",
                            "message": "Preparing download file...",
                            "timestamp": time.time()
                        })
                    )
                        
            # 上传到 R2
            file_url = await upload_to_r2(file_name)
            if file_url:
                download_links[user_id][(video_id, format_id)] = file_url
                print(f"[后台] 上传成功，文件链接: {file_url}")

                # 推送 "上传成功" 信息到 WebSocket（可选，增强体验）
                websocket = active_connections.get(user_id, {}).get(video_id)
                if websocket:
                    if main_loop.is_running():
                       main_loop.call_soon_threadsafe(
                           asyncio.create_task,
                           websocket.send_json({
                               "status": "uploaded",
                               "download_url": file_url,
                               "timestamp": time.time()
                          })
                     )
            else:
                print(f"[后台] ❌ 上传失败: {file_name}")
        else:
            print(f"[后台] ❌ 下载失败或文件不存在: {download_path}")

    except Exception as e:
        print(f"[后台] ❌ download_video异常: {str(e)}")
    finally:
        # 下载任务完成，无论成功失败，都移除任务标记
        active_tasks[user_id].discard((url, format_id))


async def process_user_queue(user_id):
    queue = task_queues[user_id]
    print(f"[QUEUE] 🎯 启动处理器: {user_id}")
    while True:
        url, format_id = await queue.get()
        print(f"[QUEUE] 🟢 Worker 正在处理任务: url={url}, format_id={format_id}, user_id={user_id}",flush=True)
        await download_video(url, format_id, user_id)
        queue.task_done()

@app.get("/")
async def home():
    html_path = Path(__file__).parent / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse("前端页面未找到", status_code=404)

@app.get("/queue_download")
async def queue_download(
    request: Request, 
    url: str = Query(...), 
    format_id: str = Query(...),
    limit_rate: str = Query("2M")
):
    user_id = request.cookies.get("user_id", "debug_user")
    video_id = extract_video_id(url)

    print(f"[QUEUE] 收到下载请求: user_id={user_id}, video_id={video_id}, format_id={format_id}",flush=True)

    try:
        with YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get("formats", [])
            selected_format = next((f for f in formats if f.get("format_id") == format_id), None)

            if not selected_format:
                print(f"[QUEUE] ❌ 找不到格式: {format_id}")
                return JSONResponse({"status": "error", "message": "找不到对应的format_id"})

            filesize = selected_format.get("filesize") or selected_format.get("filesize_approx") or 0
            filesize_mb = filesize / 1024 / 1024

            print(f"[QUEUE] 检测到文件大小: {filesize_mb:.2f}MB")

            if filesize and filesize_mb > 300:
                print(f"[QUEUE] ❌ 文件过大: {filesize_mb:.2f}MB，拒绝下载")
                return JSONResponse({
                    "status": "error",
                    "message": f"该格式文件大小为 {filesize_mb:.2f}MB，已超过300MB限制，无法下载。"
                })

    except Exception as e:
        print(f"[QUEUE] ❌ 获取视频信息失败: {e}")
        return JSONResponse({"status": "error", "message": "视频信息获取失败"})

    print(f"[QUEUE] ✅ 通过大小检查")

    if user_id not in task_queues:
        task_queues[user_id] = asyncio.Queue()
    if user_id not in user_workers:
        print(f"[QUEUE] 启动用户下载队列处理器: {user_id}")
        user_workers.add(user_id)
        asyncio.create_task(process_user_queue(user_id))

    existing_url = download_links[user_id].get((video_id, format_id))
    if existing_url:
        print(f"[QUEUE] ✅ 已经下载过，返回现有链接")
        return JSONResponse({"status": "done", "download_url": existing_url})

    if len(active_tasks[user_id]) >= MAX_CONCURRENT_PER_USER:
        print(f"[QUEUE] ❌ 并发限制，当前活跃任务数: {len(active_tasks[user_id])}")
        return JSONResponse({"status": "error", "message": "已达最大并发限制。"})

    print(f"[QUEUE] 加入 active_tasks: {(url, format_id)}")
    active_tasks[user_id].add((url, format_id))

    await task_queues[user_id].put((url, format_id))
    print(f"[QUEUE] 放入任务队列: user_id={user_id}, 队列长度={task_queues[user_id].qsize()}")

    return JSONResponse({"status": "queued"})



@app.websocket("/ws/progress")
async def websocket_progress(websocket: WebSocket, user_id: str, video_id: str):
    await websocket.accept()
    if user_id not in active_connections:
        active_connections[user_id] = {}
    active_connections[user_id][video_id] = websocket
    try:
        while True:
            await asyncio.sleep(60)
    except WebSocketDisconnect:
        if user_id in active_connections and video_id in active_connections[user_id]:
            del active_connections[user_id][video_id]

@app.post("/fetch_formats")
async def fetch_formats(video: VideoRequest):
    try:
        with YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(video.url, download=False)
            formats = info.get("formats", [])
            
            # ✅ 只要有视频轨就保留（acodec可以是none）
            filtered_formats = [
                f for f in formats if f.get("vcodec") != "none"
            ]

            return {
                "status": "success",
                "formats": [
                    {
                        "format_id": f.get("format_id"),
                        "resolution": f.get("resolution") or f"{f.get('height', '未知')}p",
                        "ext": f.get("ext", "未知"),
                        "format_note": f.get("format_note", "无"),
                        "vcodec": f.get("vcodec", "N/A"),
                        "acodec": f.get("acodec", "N/A"),
                        "filesize": f.get("filesize") or f.get("filesize_approx") or 0
                    } for f in filtered_formats
                ],
                "title": info.get("title", "未知视频"),
                "thumbnail": info.get("thumbnail", ""),
                "duration": info.get("duration_string", "未知")
            }
    except Exception as e:
        return {"status": "error", "message": f"解析失败: {str(e)}"}

@app.post("/cancel_download")
async def cancel_download(request: Request, url: str = Query(...), format_id: str = Query(...)):
    user_id = request.cookies.get("user_id", "debug_user")
    video_id = extract_video_id(url)

    cancel_flags[user_id][(video_id, format_id)] = True

    # 从活动任务中移除
    for task in list(active_tasks[user_id]):
        if extract_video_id(task[0]) == video_id and task[1] == format_id:
            active_tasks[user_id].discard(task)

    # ✅ WebSocket 发送
    websocket = active_connections.get(user_id, {}).get(video_id)
    if websocket:
        try:
            await websocket.send_json({
                "status": "cancelled",
                "message": "Download cancelled by user",
                "timestamp": time.time()
            })
            print(f"[CANCEL] ✅ 已推送取消消息: user={user_id}, video={video_id}")
        except Exception as e:
            print(f"[CANCEL] ❌ 发送失败: {e}")
    else:
        print(f"[CANCEL] ⚠️ 无 WebSocket 连接: user={user_id}, video={video_id}")



@app.get("/status")
async def get_status(request: Request, video_id: str = Query(...), format_id: str = Query(...)):
    user_id = request.cookies.get("user_id", "debug_user")

    file_url = None
    if user_id in download_links:
        for (vid, fid), url in download_links[user_id].items():
            if vid == video_id and fid == format_id:
                file_url = url
                break

    if file_url:
        return {"status": "done", "download_url": file_url}
    else:
        return {"status": "downloading"}

# 所有用户任务的实时状态
@app.get("/admin/tasks")
async def get_all_tasks():
    all_tasks = []

    for user_id, tasks in active_tasks.items():
        for (url, format_id) in tasks:
            video_id = extract_video_id(url)
            progress = download_progress.get(user_id, {}).get(video_id, {})
            all_tasks.append({
                "user_id": user_id,
                "video_id": video_id,
                "url": url,
                "format_id": format_id,
                "status": progress.get('status', 'queued'),
                "percent": progress.get('percent', '0%'),
                "downloaded": progress.get('downloaded', '0MB'),
                "total": progress.get('total', '未知'),
                "speed": progress.get('speed', '-'),
                "eta": progress.get('eta', '-')
            })
    
    return {"tasks": all_tasks}

@app.get("/admin")
async def admin_panel():
    html_path = Path(__file__).parent / "admin.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse("Admin 页面未找到", status_code=404)


@app.on_event("startup")
async def start_server():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    print("服务启动成功")

if __name__ == "__main__":
    import os, uvicorn
    # 从环境变量读取 PORT，默认 8000
    port = int(os.environ.get("PORT", 8000))
    # 绑定所有接口，监听该端口
    uvicorn.run(app, host="0.0.0.0", port=port)
