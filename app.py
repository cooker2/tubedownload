import os
import asyncio
import urllib.parse
import time
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

main_loop = asyncio.get_event_loop()

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
app.mount("/downloads", StaticFiles(directory="downloads"), name="downloads")

class VideoRequest(BaseModel):
    url: str

DOWNLOAD_DIR = './downloads'
MAX_CONCURRENT_PER_USER = 1

progress_lock = Lock()
active_connections = defaultdict(dict)
task_queues = defaultdict(asyncio.Queue)
active_tasks = defaultdict(set)
download_links = defaultdict(dict)
download_progress = defaultdict(dict)
user_workers = set()

def extract_video_id(url: str):
    parsed = urlparse(url)
    return parse_qs(parsed.query).get("v", [""])[0]

def get_video_filename(url, format_id):
    with YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
        info = ydl.extract_info(url, download=False)
        return f"{info['id']}_{format_id}.mp4"

def make_progress_hook(user_id, video_id):
    def hook(d):
        try:
            status = d.get('status', '')
            if status == 'downloading' or status == 'finished':
                progress = build_progress_data(d)

                # 保存到全局变量
                download_progress[user_id][video_id] = progress

                # 推送到WebSocket
                websocket = active_connections.get(user_id, {}).get(video_id)
                if websocket:
                    if main_loop.is_running():
                        main_loop.call_soon_threadsafe(
                            asyncio.create_task, websocket.send_json(progress)
                        )
        except Exception as e:
            print(f"[HOOK ERROR] {e}")
    return hook

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
    encoded_file_name = urllib.parse.quote(file_name)

    def _upload():
        try:
            print(f"[UPLOAD] 上传到R2: {file_path}")
            r2_client.upload_file(
                file_path,
                "videodown",
                f"your/folder/{encoded_file_name}",
                ExtraArgs={'ACL': 'public-read'}
            )
            os.remove(file_path)
            return f"{BASE_PUBLIC_R2_URL}/your/folder/{encoded_file_name}"
        except Exception as e:
            print(f"[UPLOAD ERROR] {e}")
            return None


    # 用线程池跑上传，避免卡主事件循环
    loop = asyncio.get_running_loop()
    file_url = await loop.run_in_executor(None, _upload)
    return file_url


def download_in_thread(url, format_id, user_id, video_id, download_path):
    try:
        # 先解析视频信息，判断format是否有音频
        with YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats', [])
            has_audio = format_has_audio(formats, format_id)

        # 准备hook
        hook = make_progress_hook(user_id, video_id)

        # 根据是否有音频，决定格式
        selected_format = f"{format_id}" if has_audio else f"{format_id}+bestaudio/best"

        ydl_opts = {
            'outtmpl': download_path,
            'format': selected_format,
            'noplaylist': True,
            'quiet': False,
            'progress_hooks': [hook],
            'merge_output_format': 'mp4',   # 最终统一合成mp4格式
            # 新增的容错配置
            'retries': 10,
            'fragment_retries': 10,
            'timeout': 30,
            'fragment_timeout': 30,

             'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
        
             },
    # 新增：限速
            'limit_rate': '2M',
        }

        print(f"[下载准备] 选择的format: {selected_format}")

        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        return True
    except Exception as e:
        print(f"下载异常: {e}")
        return False


 # 根据 format_id 检查是否有声音       
def format_has_audio(formats, format_id):
    for f in formats:
        if f.get('format_id') == format_id:
            return f.get('acodec') != 'none'
    return False

async def download_video(url, format_id, user_id):
    video_id = extract_video_id(url)
    file_name = get_video_filename(url, format_id)
    download_path = os.path.join(DOWNLOAD_DIR, file_name)

    # 确保下载目录存在
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    try:
        print(f"[后台] 开始下载任务: user_id={user_id}, video_id={video_id}, format_id={format_id}")

        # 在独立线程中执行下载（避免阻塞主事件循环）
        with concurrent.futures.ThreadPoolExecutor() as executor:
            success = await asyncio.get_event_loop().run_in_executor(
                executor, download_in_thread, url, format_id, user_id, video_id, download_path
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
                            asyncio.create_task, websocket.send_json({
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
    while True:
        url, format_id = await queue.get()
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

    # ✅ 新增：检查 format 的文件大小
    with YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
        info = ydl.extract_info(url, download=False)
        formats = info.get("formats", [])
        selected_format = next((f for f in formats if f.get("format_id") == format_id), None)

        if not selected_format:
            return JSONResponse({"status": "error", "message": "找不到对应的format_id"})

        filesize = selected_format.get("filesize") or selected_format.get("filesize_approx") or 0
        filesize_mb = filesize / 1024 / 1024

        # ✅ 如果超过200MB，拒绝下载
        if filesize and filesize_mb > 200:
            return JSONResponse({
                "status": "error",
                "message": f"该格式文件大小为 {filesize_mb:.2f}MB，已超过200MB限制，无法下载。"
            })

    # ✅ 正常排队流程
    if user_id not in task_queues:
        task_queues[user_id] = asyncio.Queue()
    if user_id not in user_workers:
        user_workers.add(user_id)
        asyncio.create_task(process_user_queue(user_id))

    existing_url = download_links[user_id].get((video_id, format_id))
    if existing_url:
        return JSONResponse({"status": "done", "download_url": existing_url})

    if len(active_tasks[user_id]) >= MAX_CONCURRENT_PER_USER:
        return JSONResponse({"status": "error", "message": "已达最大并发限制。"})

    active_tasks[user_id].add((url, format_id))
    await task_queues[user_id].put((url, format_id, limit_rate))

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
