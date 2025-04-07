import os
from flask import Flask, request, jsonify
import logging
from yt_dlp import YoutubeDL

# 设置日志级别为 DEBUG，方便排查问题
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)

@app.route("/")
def home():
    logging.debug("Home route accessed")
    return "YouTube Downloader API is running"

@app.route("/get_video_info", methods=["POST"])
def get_video_info():
    # 检查请求是否为 JSON 格式
    if not request.is_json:
        logging.error("Request is not in JSON format")
        return jsonify({"error": "Request must be in JSON format"}), 400

    data = request.get_json()
    url = data.get("url")
    if not url:
        logging.error("URL is missing in the request")
        return jsonify({"error": "URL is required"}), 400

    try:
        logging.info(f"Fetching video info for URL: {url}")
        ydl_opts = {"skip_download": True, "quiet": True, "format": "best"}
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get("formats", [])
            video_data = {
                "title": info.get("title"),
                "thumbnail": info.get("thumbnail"),
                "formats": [
                    {
                        "format_id": f.get("format_id", "N/A"),
                        "ext": f.get("ext", "N/A"),
                        "resolution": f.get("resolution") or f"{f.get('width', 'N/A')}x{f.get('height', 'N/A')}",
                        "filesize": f.get("filesize", "N/A"),
                        "url": f.get("url", "N/A"),
                    }
                    for f in formats if f.get("url")
                ],
            }
            logging.info("Video info fetched successfully")
            return jsonify(video_data)
    except Exception as e:
        logging.error(f"Error while fetching video info: {str(e)}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    logging.info(f"Starting Flask server on port {port}")
    app.run(debug=True, host="0.0.0.0", port=port)
