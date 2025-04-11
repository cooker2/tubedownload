import os
import asyncio
import urllib.parse
from collections import defaultdict
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from yt_dlp import YoutubeDL
from urllib.parse import urlparse, parse_qs, unquote

os.makedirs("downloads", exist_ok=True)
app = FastAPI()
app.mount("/downloads", StaticFiles(directory="downloads"), name="downloads")

class VideoRequest(BaseModel):
    url: str

task_queues = defaultdict(asyncio.Queue)
active_tasks = defaultdict(set)
download_progress = {}

MAX_CONCURRENT_PER_USER = 3

def get_user_id(request: Request = None):
    return "default_user"

def extract_video_id(url: str):
    try:
        decoded_url = unquote(url)
        query = parse_qs(urlparse(decoded_url).query)
        return query.get("v", [""])[0]
    except Exception as e:
        print(f"[ERROR] extract_video_id error: {e}")
        return ""

def filter_formats(formats):
    result = []
    seen = set()
    for f in formats:
        if f.get("vcodec") == "none" or f.get("acodec") == "none":
            continue
        fid = f.get("format_id")
        if fid and fid not in seen:
            seen.add(fid)
            result.append(f)
    return result

@app.get("/", response_class=HTMLResponse)
async def home():
    try:
        with open("index.html", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except Exception as e:
        return HTMLResponse(f"加载主页失败：{str(e)}", status_code=500)

@app.post("/fetch_formats")
async def fetch_formats(video: VideoRequest):
    try:
        print(f"[DEBUG] 正在解析: {video.url}")
        with YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(video.url, download=False)
            formats = filter_formats(info.get("formats", []))
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
                    } for f in formats
                ],
                "title": info.get("title", "未知视频"),
                "thumbnail": info.get("thumbnail", ""),
                "duration": info.get("duration_string", "未知")
            }
    except Exception as e:
        print(f"[ERROR] 解析失败: {e}")
        return {"status": "error", "message": "Failed to parse"}

@app.get("/queue_download")
async def queue_download(url: str = Query(...), format_id: str = Query(...)):
    user_id = get_user_id()
    queue = task_queues[user_id]
    video_id = extract_video_id(url)
    task = (video_id, format_id)

    if len(active_tasks[user_id]) >= MAX_CONCURRENT_PER_USER:
        return JSONResponse({"status": "error", "message": "您已同时下载 3 个任务，请稍候再试。"})

    if task in active_tasks[user_id]:
        return JSONResponse({"status": "error", "message": "该任务正在进行中。"})

    active_tasks[user_id].add(task)
    await queue.put((url, format_id))
    print(f"[DEBUG] 加入下载队列: {video_id} [{format_id}]")
    return JSONResponse({"status": "queued", "message": "任务已加入队列"})

@app.get("/status")
async def get_status(url: str = Query(...), format_id: str = Query(...)):
    user_id = get_user_id()
    video_id = extract_video_id(url)
    task = (video_id, format_id)

    if task in active_tasks[user_id]:
        return {"status": "downloading"}

    # 检查文件是否存在并完成（排除.part文件）
    for filename in os.listdir("downloads"):
        if video_id in filename and not filename.endswith(".part"):
            return {"status": "done", "filename": filename}

    return {"status": "not_found"}

@app.get("/progress")
async def get_progress(url: str = Query(...)):
    video_id = extract_video_id(url)
    return download_progress.get(video_id, {
        "percent": "0%",
        "speed": "",
        "downloaded": "",
        "total": "",
        "eta": ""
    })

# 后台下载处理逻辑
async def process_user_queue(user_id):
    queue = task_queues[user_id]
    while True:
        url, format_id = await queue.get()
        video_id = extract_video_id(url)
        decoded_url = unquote(url)
        print(f"[DEBUG] 开始下载任务: {decoded_url} [{format_id}]")
        asyncio.create_task(run_download_in_background(video_id, decoded_url, format_id, user_id))
        queue.task_done()

async def run_download_in_background(video_id, url, format_id, user_id):
    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                percent = d["downloaded_bytes"] / total * 100
                speed = d.get("speed", 0)
                eta = d.get("eta", 0)
                downloaded = d["downloaded_bytes"] / 1024 / 1024
                total_mb = total / 1024 / 1024

                download_progress[video_id] = {
                    "percent": f"{percent:.1f}%",
                    "speed": f"{speed/1024:.1f} KB/s" if speed else "",
                    "downloaded": f"{downloaded:.1f} MB",
                    "total": f"{total_mb:.1f} MB",
                    "eta": f"{int(eta)}s" if eta else "--"
                }
        elif d["status"] == "finished":
            download_progress[video_id] = {
                "percent": "100%",
                "speed": "",
                "downloaded": "",
                "total": "",
                "eta": ""
            }
            print(f"[DEBUG] {video_id} 下载完成 ✅")

    def blocking_download():
        try:
            ydl_opts = {
                "outtmpl": "./downloads/%(id)s_%(title).50s.%(ext)s",
                "format": f"{format_id}+bestaudio",
                "merge_output_format": "mp4",
                "noplaylist": True,
                "quiet": False,
                "progress_hooks": [progress_hook],
            }
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception as e:
            print(f"[ERROR] 下载失败: {e}")
        finally:
            task = (video_id, format_id)
            active_tasks[user_id].discard(task)
            print(f"[DEBUG] 已清除活跃任务: {task}")

    await asyncio.to_thread(blocking_download)

@app.on_event("startup")
async def start_workers():
    user_id = get_user_id()
    print("[DEBUG] 启动下载监听线程")
    asyncio.create_task(process_user_queue(user_id))
