import os
from flask import Flask, request, jsonify
import logging
from yt_dlp import YoutubeDL

# 设置日志级别为 DEBUG，方便排查问题
logging.basicConfig(level=logging.DEBUG)

app = Flask(__name__)

@app.route("/")
def home():
    logging.debug("Home route accessed")
    return "YouTube Downloader API is running"

@app.route("/get_video_info", methods=["POST"])
def get_video_info():
    data = request.json
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
                        "format_id": f["format_id"],
                        "ext": f["ext"],
                        "resolution": f.get("resolution") or f"{f.get('width')}x{f.get('height')}",
                        "filesize": f.get("filesize"),
                        "url": f["url"],
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
