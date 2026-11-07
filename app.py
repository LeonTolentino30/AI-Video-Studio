import os
import sqlite3
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv
from runwayml import RunwayML


load_dotenv()

MOCK_MODE = os.getenv("MOCK_MODE", "true").lower() == "true"

app = Flask(__name__)

@app.route("/config", methods=["GET"])
def get_config():
    return jsonify({
        "mode": "demo" if MOCK_MODE else "real",
        "real_generation_enabled": not MOCK_MODE
    })


def init_database():
    connection = sqlite3.connect("videos.db")

    connection.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT UNIQUE,
            prompt TEXT NOT NULL,
            video_url TEXT,
            status TEXT NOT NULL,
            mode TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    connection.commit()
    connection.close()


init_database()


api_key = os.getenv("RUNWAYML_API_SECRET")

if not api_key:
    raise RuntimeError("Runway API key is missing!")

client = RunwayML(api_key=api_key)


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/generate", methods=["POST"])
def generate_video():
    data = request.get_json(silent=True) or {}
    prompt = data.get("prompt", "")

    if not isinstance(prompt, str) or not prompt.strip():
        return jsonify({"error": "Please enter a prompt"}), 400

    if len(prompt) > 1000:
        return jsonify({"error": "Prompt is too long"}), 400
        
    if MOCK_MODE:
        import uuid

        task_id = str(uuid.uuid4())

        video_url = (
            "https://interactive-examples.mdn.mozilla.net/"
            "media/cc0-videos/flower.mp4"
        )

        connection = sqlite3.connect("videos.db")

        connection.execute("""
            INSERT INTO videos
            (task_id, prompt, video_url, status, mode)
            VALUES (?, ?, ?, ?, ?)
        """, (
            task_id,
            prompt,
            video_url,
            "SUCCEEDED",
            "mock"
        ))

        connection.commit()
        connection.close()

        return jsonify({
            "task_id": task_id,
            "status": "submitted",
            "mode": "mock"
        })



    try:
        
        task = client.text_to_video.create(
            model="gen4.5",
            prompt_text=prompt,
            ratio="720:1280",
            duration=5
        )

        connection = sqlite3.connect("videos.db")

        connection.execute("""
            INSERT INTO videos (task_id, prompt, video_url, status, mode)
            VALUES (?, ?, ?, ?, ?)
        """, (task.id, prompt, None, "PENDING", "real"))

        connection.commit()
        connection.close()

        return jsonify({
            "task_id": task.id,
            "status": "submitted",
            "mode": "real"
        })


    except Exception:
        app.logger.exception("Runway generation failed")
        return jsonify({
            "error": "Video generation request failed"
        }), 502


@app.route("/status/<task_id>", methods=["GET"])
def check_video_status(task_id):

    
    if MOCK_MODE:
        return jsonify({
            "task_id": task_id,
            "status": "SUCCEEDED",
            "video_url": (
                "https://interactive-examples.mdn.mozilla.net/"
                "media/cc0-videos/flower.mp4"
            ),
            "mode": "mock"
        })

    try:
        task = client.tasks.retrieve(task_id)

        result = {
            "task_id": task.id,
            "status": task.status
        }
        if task.status in ("SUCCEEDED", "FAILED"):
            video_url = None

            if task.status == "SUCCEEDED" and task.output:
                video_url = task.output[0]

            connection = sqlite3.connect("videos.db")

            connection.execute("""
                UPDATE videos
                SET status = ?, video_url = ?
                WHERE task_id = ? AND mode = ?
            """, (task.status, video_url, task.id, "real"))

            connection.commit()
            connection.close()


        if task.status == "SUCCEEDED":
            result["video_url"] = (
                task.output[0] if task.output else None
            )

       
        elif task.status == "FAILED":
            result["error"] = "Video generation failed"
            result["failure"] = getattr(task, "failure", None)
            result["failure_code"] = getattr(task, "failure_code", None)


        return jsonify(result)

    except Exception:
        app.logger.exception("Runway status check failed")
        return jsonify({
            "error": "Unable to check video status"
        }), 502


@app.route("/gallery", methods=["GET"])
def get_gallery():
    try:
        connection = sqlite3.connect("videos.db")
        connection.row_factory = sqlite3.Row

        videos = connection.execute("""
            SELECT id, task_id, prompt, video_url,
                   status, mode, created_at
            FROM videos
            ORDER BY id DESC
        """).fetchall()

        connection.close()

        return jsonify({
            "videos": [dict(video) for video in videos]
        })

    except Exception:
        app.logger.exception("Gallery loading failed")

        return jsonify({
            "error": "Unable to load gallery"
        }), 500
@app.route("/gallery/<int:video_id>", methods=["DELETE"])
def delete_video(video_id):
    try:
        connection = sqlite3.connect("videos.db")

        cursor = connection.execute(
            "DELETE FROM videos WHERE id = ?",
            (video_id,)
        )

        connection.commit()

        deleted = cursor.rowcount
        connection.close()

        if deleted == 0:
            return jsonify({
                "error": "Video not found"
            }), 404

        return jsonify({
            "message": "Video removed from gallery",
            "video_id": video_id
        })

    except Exception:
        app.logger.exception("Video deletion failed")

        return jsonify({
            "error": "Unable to delete video"
        }), 500


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False
    )



