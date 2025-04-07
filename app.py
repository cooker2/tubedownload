from flask import Flask, request, jsonify, render_template
from yt_dlp import YoutubeDL
import os
import traceback
import logging

# 初始化日志配置
logging.basicConfig(level=logging.DEBUG)

app = Flask(__name__)

# 首页路由
@app.route("/")
def index():
    logging.info("Accessed Home Page")
    return render_template("index.html")

# 获取视频信息接口
@app.route("/get_video_info", methods=["POST"])
def get_video_info():
    try:
        # 请求接收日志
        logging.debug("Received a request to /get_video_info")
        data = request.get_json()
        logging.debug(f"Request data: {data}")

        if not data:
            logging.error("No data received")
            return jsonify({"error": "No data received"}), 400

        url = data.get("url")
        if not url:
            logging.error("No URL provided in the request")
            return jsonify({"error": "URL is required"}), 400

        # 打印获取的视频 URL
        logging.info(f"Received URL: {url}")

        try:
            # 配置 yt-dlp
            ydl_opts = {"skip_download": True, "quiet": False, "format": "best"}
            logging.debug(f"yt-dlp options: {ydl_opts}")

            # 使用 yt-dlp 提取视频信息
            with YoutubeDL(ydl_opts) as ydl:
                logging.info("Starting video information extraction")
                info = ydl.extract_info(url, download=False)
                logging.debug(f"Extracted info: {info}")

                formats = info.get("formats", [])
                video_data = {
                    "title": info.get("title", "Unknown Title"),
                    "thumbnail": info.get("thumbnail", ""),
                    "formats": [
                        {
                            "format_id": f.get("format_id", "unknown"),
                            "ext": f.get("ext", "unknown"),
                            "resolution": f.get("resolution") or f"{f.get('width')}x{f.get('height')}",
                            "filesize": f.get("filesize", "unknown"),
                            "url": f.get("url", "unknown"),
                        }
                        for f in formats if f.get("url")
                    ],
                }
                logging.info(f"Video data prepared: {video_data['title']}")
                return jsonify(video_data)
        except Exception as e:
            logging.error("Error during video extraction", exc_info=True)
            return jsonify({"error": str(e), "details": traceback.format_exc()}), 500

    except Exception as e:
        logging.error("General Error in /get_video_info", exc_info=True)
        return jsonify({"error": str(e), "details": traceback.format_exc()}), 500


