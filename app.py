
import os
import sqlite3
import uuid
from functools import wraps

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, session, abort
)
from werkzeug.utils import secure_filename

app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
BUCKET_NAME = os.environ.get("S3_BUCKET", "")
DB_PATH = os.environ.get("STUDENT_DB_PATH", "students.db")

ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}

app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

s3 = boto3.client("s3")


def get_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_database():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                roll_no TEXT NOT NULL UNIQUE,
                course TEXT NOT NULL,
                email TEXT NOT NULL,
                document_key TEXT
            )
        """)


def login_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return function(*args, **kwargs)
    return wrapper


def allowed_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower()
        in ALLOWED_EXTENSIONS
    )


@app.route("/health")
def health():
    return {"status": "healthy", "application": "Student Management System"}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password", "")

        if APP_PASSWORD and password == APP_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))

        flash("Incorrect password.")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    with get_db() as conn:
        students = conn.execute(
            "SELECT * FROM students ORDER BY id DESC"
        ).fetchall()

    records = []

    for student in students:
        record = dict(student)
        record["document_url"] = None

        if record["document_key"] and BUCKET_NAME:
            try:
                record["document_url"] = s3.generate_presigned_url(
                    "get_object",
                    Params={
                        "Bucket": BUCKET_NAME,
                        "Key": record["document_key"]
                    },
                    ExpiresIn=300
                )
            except (BotoCoreError, ClientError):
                pass

        records.append(record)

    return render_template("index.html", students=records)


@app.route("/add", methods=["POST"])
@login_required
def add_student():
    name = request.form.get("name", "").strip()
    roll_no = request.form.get("roll_no", "").strip()
    course = request.form.get("course", "").strip()
    email = request.form.get("email", "").strip()

    if not all([name, roll_no, course, email]):
        flash("Please fill in all student details.")
        return redirect(url_for("index"))

    document_key = None
    file = request.files.get("document")

    if file and file.filename:
        if not allowed_file(file.filename):
            flash("Only PDF, PNG, JPG and JPEG files are allowed.")
            return redirect(url_for("index"))

        if not BUCKET_NAME:
            flash("S3 bucket is not configured.")
            return redirect(url_for("index"))

        safe_name = secure_filename(file.filename)
        document_key = f"student-documents/{uuid.uuid4().hex}-{safe_name}"

        try:
            s3.upload_fileobj(
                file,
                BUCKET_NAME,
                document_key,
                ExtraArgs={
                    "ContentType": file.mimetype or
                    "application/octet-stream"
                }
            )
        except (BotoCoreError, ClientError) as error:
            app.logger.exception("S3 upload failed")
            flash("Document upload failed. Check AWS permissions.")
            return redirect(url_for("index"))

    try:
        with get_db() as conn:
            conn.execute("""
                INSERT INTO students
                (name, roll_no, course, email, document_key)
                VALUES (?, ?, ?, ?, ?)
            """, (name, roll_no, course, email, document_key))

        flash("Student added successfully!")

    except sqlite3.IntegrityError:
        flash("This roll number already exists.")

    return redirect(url_for("index"))


@app.route("/delete/<int:student_id>", methods=["POST"])
@login_required
def delete_student(student_id):
    with get_db() as conn:
        student = conn.execute(
            "SELECT document_key FROM students WHERE id = ?",
            (student_id,)
        ).fetchone()

        if not student:
            abort(404)

        if student["document_key"] and BUCKET_NAME:
            try:
                s3.delete_object(
                    Bucket=BUCKET_NAME,
                    Key=student["document_key"]
                )
            except (BotoCoreError, ClientError):
                app.logger.exception("S3 deletion failed")
                flash("Could not delete the student's S3 document.")
                return redirect(url_for("index"))

        conn.execute(
            "DELETE FROM students WHERE id = ?",
            (student_id,)
        )

    flash("Student deleted successfully!")
    return redirect(url_for("index"))


@app.errorhandler(413)
def file_too_large(error):
    flash("File is too large. Maximum size is 5 MB.")
    return redirect(url_for("index"))


if __name__ == "__main__":
    initialize_database()
    app.run(host="127.0.0.1", port=8000, debug=False)
