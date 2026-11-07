import os
import sqlite3
from pathlib import Path
from flask import Flask, request, jsonify, render_template
from werkzeug.utils import secure_filename
import cv2
from character_swap.service import CharacterSwapService
from character_swap.store import JobStore
from character_swap.web import register_job_lookup


def create_app(config=None, *, load_environment=None, runway_client=None, character_swap_service=None):
    """Initialize explicitly; offline callers inject configuration, paths, and a client.

    Module import does not load dotenv, open databases, or construct providers.
    Environment-free creation does not read credential environment variables.
    """
    supplied = dict(config or {})
    testing = supplied.get("TESTING", False)
    if load_environment is None:
        load_environment = not testing
    if testing:
        if load_environment:
            raise ValueError("Offline tests must disable environment loading")
        if runway_client is None:
            raise ValueError("Offline tests require an injected Runway client")
        if not supplied.get("DATABASE_PATH") or not supplied.get("UPLOAD_FOLDER"):
            raise ValueError("Offline tests require explicit database and upload paths")
        for name in ("DATABASE_PATH", "CHARACTER_SWAP_DATABASE_PATH"):
            if name in supplied and Path(supplied[name]).resolve() == Path("videos.db").resolve():
                raise ValueError("Offline tests cannot use the production database")
        if supplied.get("MOCK_MODE", True) is not True:
            raise ValueError("Offline tests require MOCK_MODE=True")
        if character_swap_service is not None and (
            character_swap_service.policy.mock_mode is not True
            or character_swap_service.policy.spending_enabled is not False
        ):
            raise ValueError("Offline tests require a disabled Character Swap service")
    if not load_environment and runway_client is None:
        raise ValueError("Environment-free initialization requires an injected Runway client")
    if load_environment:
        from dotenv import load_dotenv
        load_dotenv()




    app = Flask(__name__)
    app.config.update(
        MOCK_MODE=os.getenv("MOCK_MODE", "true").lower() == "true" if load_environment else True,
        SECRET_KEY=os.getenv("FLASK_SECRET_KEY") if load_environment else None,
        CHARACTER_SWAP_HTTPS=(os.getenv("CHARACTER_SWAP_HTTPS", "true").lower() != "false") if load_environment else True,
        DATABASE_PATH="videos.db", UPLOAD_FOLDER="uploads",
        MAX_CONTENT_LENGTH=200 * 1024 * 1024,
    )
    app.config.update(supplied)
    MOCK_MODE = app.config["MOCK_MODE"]
    DATABASE_PATH = str(app.config["DATABASE_PATH"])


    def local_character_swap_service():
        # Reuse the ignored local database with a separate Character Swap table.
        # No providers are registered; the service's default spending policy is off.
        if "character_swap_local_service" not in app.extensions:
            app.extensions["character_swap_local_service"] = character_swap_service if character_swap_service is not None else CharacterSwapService(
                JobStore(app.config.get("CHARACTER_SWAP_DATABASE_PATH", DATABASE_PATH)), {}, {}
            )
        return app.extensions["character_swap_local_service"]


    register_job_lookup(app, local_character_swap_service)

    # Character Swap upload settings
    UPLOAD_FOLDER = Path(app.config["UPLOAD_FOLDER"])
    UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)



    ALLOWED_VIDEO_EXTENSIONS = {".mp4"}
    ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}


    @app.route("/config", methods=["GET"])
    def get_config():
        return jsonify({
            "mode": "demo" if MOCK_MODE else "real",
            "real_generation_enabled": not MOCK_MODE
        })


    def init_database():
        connection = sqlite3.connect(DATABASE_PATH)

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
    local_character_swap_service()


    if runway_client is not None:
        client = runway_client
    else:
        from runwayml import RunwayML
        api_key = os.getenv("RUNWAYML_API_SECRET") if load_environment else None
        if not api_key:
            raise RuntimeError("Runway API key is missing!")
        client = RunwayML(api_key=api_key)
    app.extensions["runway_client"] = client


    @app.route("/")

    def home():
        return render_template("index.html")


    @app.route("/character-swap")
    def character_swap():
        return render_template("character_swap.html")


    @app.route("/character-swap/upload", methods=["POST"])
    def upload_character_swap():
        import uuid

        if "video" not in request.files or "photo" not in request.files:
            return jsonify({"error": "Upload a video and reference photo"}), 400

    
        video = request.files["video"]
        photo = request.files["photo"]
        prompt = request.form.get("prompt", "").strip()

        if not video.filename or not photo.filename:
            return jsonify({"error": "Both files are required"}), 400

        if not prompt:
            return jsonify({"error": "Please describe the video edit"}), 400

        if len(prompt) > 1000:
            return jsonify({"error": "Prompt must be 1000 characters or less"}), 400


        video_extension = Path(secure_filename(video.filename)).suffix.lower()
        photo_extension = Path(secure_filename(photo.filename)).suffix.lower()

        if video_extension not in ALLOWED_VIDEO_EXTENSIONS:
            return jsonify({"error": "Video must be MP4"}), 400

        if photo_extension not in ALLOWED_IMAGE_EXTENSIONS:
            return jsonify({"error": "Photo must be JPG or PNG"}), 400

        upload_id = uuid.uuid4().hex

        video_path = UPLOAD_FOLDER / f"{upload_id}_video{video_extension}"
        photo_path = UPLOAD_FOLDER / f"{upload_id}_photo{photo_extension}"

   
        try:
            video.save(video_path)
            photo.save(photo_path)

            capture = cv2.VideoCapture(str(video_path))

            try:
                if not capture.isOpened():
                    raise ValueError("Unable to read the uploaded video")

                fps = capture.get(cv2.CAP_PROP_FPS)
                frame_count = capture.get(cv2.CAP_PROP_FRAME_COUNT)

                if fps <= 0 or frame_count <= 0:
                    raise ValueError("Unable to determine video duration")

                duration = frame_count / fps

                if duration < 2 or duration > 30:
                    raise ValueError(
                        "Video must be between 2 and 30 seconds"
                    )
            finally:
                capture.release()

        except (OSError, ValueError) as error:
            video_path.unlink(missing_ok=True)
            photo_path.unlink(missing_ok=True)

            return jsonify({"error": str(error)}), 400

   
    
    
        return jsonify({
            "message": "Files uploaded successfully",
            "upload_id": upload_id,
            "duration": round(duration, 2),
            "prompt": prompt,
            "status": "ready",
            "video_filename": video_path.name,
            "photo_filename": photo_path.name
        }), 201


    @app.route("/character-swap/prepare", methods=["POST"])
    def prepare_character_swap():
        if MOCK_MODE:
            return jsonify({
                "error": "Character Swap preparation is disabled in demo mode. No external upload request was sent.",
                "mode": "demo"
            }), 403

        import re

        data = request.get_json(silent=True) or {}
        upload_id = data.get("upload_id", "")

        if not isinstance(upload_id, str) or not re.fullmatch(
            r"[0-9a-f]{32}", upload_id
        ):
            return jsonify({"error": "Invalid upload ID"}), 400

        videos = list(UPLOAD_FOLDER.glob(f"{upload_id}_video.mp4"))
        photos = [
            path for extension in (".jpg", ".jpeg", ".png")
            for path in UPLOAD_FOLDER.glob(f"{upload_id}_photo{extension}")
        ]

        if len(videos) != 1 or len(photos) != 1:
            return jsonify({"error": "Uploaded files not found"}), 404

        try:
            video_upload = client.uploads.create_ephemeral(file=videos[0])
            photo_upload = client.uploads.create_ephemeral(file=photos[0])

            return jsonify({
                "message": "Files prepared for Runway",
                "video_uri": video_upload.uri,
                "photo_uri": photo_upload.uri,
                "status": "prepared"
            })


        except Exception:
            app.logger.exception("Runway asset upload failed")
            return jsonify({
                "error": "Unable to upload assets to Runway"
            }), 502





    @app.route("/character-swap/generate", methods=["POST"])
    def generate_character_swap():
        if MOCK_MODE:
            return jsonify({
                "error": "Real Character Swap generation is disabled in demo mode. No generation request was sent.",
                "mode": "demo"
            }), 403

        data = request.get_json(silent=True) or {}

        video_uri = data.get("video_uri")
        photo_uri = data.get("photo_uri")
        prompt = data.get("prompt", "")

        if not all([
            isinstance(video_uri, str) and video_uri.startswith("runway://"),
            isinstance(photo_uri, str) and photo_uri.startswith("runway://"),
            isinstance(prompt, str) and prompt.strip()
        ]):
            return jsonify({"error": "Missing or invalid generation inputs"}), 400

        if len(prompt) > 1000:
            return jsonify({"error": "Prompt is too long"}), 400

        try:
            task = client.video_to_video.create(
                model="kling_4",
                video_uri=video_uri,
                prompt_text=prompt,
                references=[
                    {"uri": photo_uri}
                ],
                ratio="auto_720p"
            )

            return jsonify({
                "task_id": task.id,
                "status": "submitted",
                "model": "kling_4"
            }), 202

        except Exception:
            app.logger.exception("Character Swap generation failed")
            return jsonify({
                "error": "Unable to start Character Swap generation"
            }), 502



    @app.route("/character-swap/status/<task_id>", methods=["GET"])
    def check_character_swap_status(task_id):
        try:
            task = client.tasks.retrieve(task_id)

            result = {
                "task_id": task.id,
                "status": task.status
            }

            if task.status == "SUCCEEDED":
                result["video_url"] = (
                    task.output[0] if task.output else None
                )

        
            elif task.status == "FAILED":
                result["error"] = "Character Swap generation failed"
                result["failure_code"] = getattr(
                    task, "failure_code", None
                )
                result["failure"] = getattr(
                    task, "failure", None
                )
                result["failure_details"] = getattr(
                    task, "failure_details", None
                )


            return jsonify(result)

        except Exception:
            app.logger.exception("Character Swap status check failed")

            return jsonify({
                "error": "Unable to check Character Swap status"
            }), 502


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

            connection = sqlite3.connect(DATABASE_PATH)

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

            connection = sqlite3.connect(DATABASE_PATH)

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

                connection = sqlite3.connect(DATABASE_PATH)

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
            connection = sqlite3.connect(DATABASE_PATH)
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
            connection = sqlite3.connect(DATABASE_PATH)

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


    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=5000, debug=False)
