"""Zeeva Clinic Attendance & HR MVP — Python/Tkinter + SQLite."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import string
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

PRIMARY = "#6AAFAA"
PRIMARY_DARK = "#3E7774"
SECONDARY = "#B4B4B4"
BACKGROUND = "#F6F8F8"
PANEL = "#FFFFFF"
TEXT = "#263238"
MUTED = "#657477"
WARNING = "#B7791F"

FACE_MATCH_THRESHOLD = 60  # LBPH confidence: lower = better match. Tightened for stricter identity checks.
FACE_MATCH_FRAMES_REQUIRED = 5  # Consecutive verified frames needed before attendance is saved.
FACE_SCAN_TIMEOUT_SECONDS = 25
FACE_LIVENESS_TURN_RATIO = 0.16  # Minimum horizontal bbox-center shift (fraction of frame width) counted as a head turn.
FACE_REGISTRATION_ANGLES = (
    ("STRAIGHT", "Look straight at the camera"),
    ("LEFT", "Turn your head slightly left"),
    ("RIGHT", "Turn your head slightly right"),
    ("UP", "Tilt your chin up slightly"),
    ("DOWN", "Tilt your chin down slightly"),
)
FACE_REGISTRATION_SAMPLES_PER_ANGLE = 4


def database_path() -> Path:
    folder = Path(__file__).resolve().parent / "data"
    folder.mkdir(exist_ok=True)
    return folder / "zeeva_clinic.db"


def data_dir() -> Path:
    folder = Path(__file__).resolve().parent / "data"
    folder.mkdir(exist_ok=True)
    return folder


def face_dir_for(employee_id: int) -> Path:
    """Face samples are stored under the STABLE database employee id, never the
    Login ID, so changing a Login ID can never disconnect or lose face data."""
    folder = data_dir() / "faces" / str(employee_id)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def face_model_path() -> Path:
    return data_dir() / "face_model.yml"


def face_labels_path() -> Path:
    return data_dir() / "face_labels.json"


def normalise_phone(value: str) -> str:
    """Strips spaces, +, -, and brackets so phone numbers can be compared as digits only."""
    return re.sub(r"[^0-9]", "", value or "")


def migrate_face_folders(con) -> None:
    """One-time migration: older installs stored face samples under
    data/faces/<employee_code>/. Move any such folders to data/faces/<employee_id>/
    so Login ID changes can never orphan face data. Never deletes data on failure."""
    faces_root = data_dir() / "faces"
    if not faces_root.exists():
        return
    code_to_id = {row["employee_code"]: row["id"] for row in con.execute("SELECT id, employee_code FROM employees")}
    moved_any = False
    for folder in list(faces_root.iterdir()):
        if not folder.is_dir() or folder.name.isdigit():
            continue  # already id-keyed, or not a recognised employee folder
        employee_id = code_to_id.get(folder.name)
        if employee_id is None:
            continue  # unknown code — leave untouched rather than guess
        target = faces_root / str(employee_id)
        if target.exists() and any(target.iterdir()):
            continue  # target already has data; don't risk overwriting it
        target.mkdir(parents=True, exist_ok=True)
        for sample in folder.glob("*.png"):
            shutil.move(str(sample), str(target / sample.name))
        try:
            folder.rmdir()
        except OSError:
            pass  # non-empty (unexpected file types) — leave the old folder in place
        moved_any = True
    if moved_any:
        train_face_model()


def _face_libs():
    """Lazy import of the optional face-recognition dependencies, so the rest
    of the app still runs fine if these haven't been installed yet."""
    try:
        import cv2
        from PIL import Image, ImageTk
        return cv2, Image, ImageTk
    except ImportError:
        return None, None, None


def train_face_model() -> bool:
    """Rebuilds the face-recognition model from every registered employee's saved
    face samples under data/faces/<employee_id>/. Returns True if a model was built."""
    cv2, _, _ = _face_libs()
    if cv2 is None:
        return False
    faces_root = data_dir() / "faces"
    samples, labels, label_map = [], [], {}
    if faces_root.exists():
        for i, folder in enumerate(sorted((p for p in faces_root.iterdir() if p.is_dir()), key=lambda p: p.name)):
            images = list(folder.glob("*.png"))
            if not images:
                continue
            label_map[str(i)] = folder.name  # folder.name is the employee's stable database id
            for img_path in images:
                img = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
                if img is not None:
                    samples.append(img); labels.append(i)
    if not samples:
        for path in (face_model_path(), face_labels_path()):
            if path.exists(): path.unlink()
        return False
    import numpy as np
    recognizer = cv2.face.LBPHFaceRecognizer_create()
    recognizer.train(samples, np.array(labels))
    recognizer.save(str(face_model_path()))
    face_labels_path().write_text(json.dumps(label_map))
    return True


def face_model_available() -> bool:
    """Fails safe: True only if a model and its labels both exist and can actually be loaded."""
    cv2, _, _ = _face_libs()
    if cv2 is None or not face_model_path().exists() or not face_labels_path().exists():
        return False
    try:
        label_map = json.loads(face_labels_path().read_text())
        if not isinstance(label_map, dict) or not label_map:
            return False
        recognizer = cv2.face.LBPHFaceRecognizer_create()
        recognizer.read(str(face_model_path()))
    except Exception:
        return False
    return True


def verify_face(employee_id: int, gray_face) -> tuple[bool, float]:
    """Checks a captured face crop against ONE specific employee's registered face —
    this answers "does this face belong to this signed-in account", not "whose face is this".
    Returns (matched, confidence). Lower confidence means a closer match. Fails safe (no match)
    on any missing/corrupted model rather than raising."""
    cv2, _, _ = _face_libs()
    if not face_model_available():
        return False, 999.0
    try:
        label_map = json.loads(face_labels_path().read_text())
        recognizer = cv2.face.LBPHFaceRecognizer_create()
        recognizer.read(str(face_model_path()))
        predicted_label, confidence = recognizer.predict(gray_face)
    except Exception:
        return False, 999.0
    predicted_employee_id = label_map.get(str(predicted_label))
    return (predicted_employee_id == str(employee_id) and confidence <= FACE_MATCH_THRESHOLD), confidence


def verify_face_anonymous(gray_face) -> tuple[bool, float, int | None]:
    """Recognizes ANY registered employee from a face crop (without pre-login).
    Used for quick clock-in/out from the login screen. Returns (matched, confidence, employee_id).
    If no match found or model unavailable, returns (False, 999.0, None)."""
    cv2, _, _ = _face_libs()
    if not face_model_available():
        return False, 999.0, None
    try:
        label_map = json.loads(face_labels_path().read_text())
        recognizer = cv2.face.LBPHFaceRecognizer_create()
        recognizer.read(str(face_model_path()))
        predicted_label, confidence = recognizer.predict(gray_face)
    except Exception:
        return False, 999.0, None
    predicted_employee_id_str = label_map.get(str(predicted_label))
    if predicted_employee_id_str and confidence <= FACE_MATCH_THRESHOLD:
        return True, confidence, int(predicted_employee_id_str)
    return False, confidence, None


def single_face_crop(cv2, gray_frame, size=200):
    """Returns a crop ONLY when exactly one sufficiently large face is visible.

    Selecting the largest/closest face when several are in frame was unsafe: someone
    else could be recorded alongside (or instead of) the signed-in employee. If zero or
    more than one face is detected, the scan must reject the frame outright.
    """
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = cascade.detectMultiScale(gray_frame, 1.2, 5, minSize=(120, 120))
    if len(faces) != 1:
        return None, None, len(faces)
    x, y, w, h = faces[0]
    crop = cv2.resize(gray_frame[y:y + h, x:x + w], (size, size))
    return crop, (x, y, w, h), 1


def temporary_password() -> str:
    """A random, one-time temporary password — never derived from any predictable pattern.
    Mixed case + digits, easy enough to read aloud/type, hard enough to not be guessable."""
    alphabet = string.ascii_uppercase + string.ascii_lowercase + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(10))
    return f"Zv-{body}"


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, expected = stored.split("$", 1)
        actual = password_hash(password, bytes.fromhex(salt_hex)).split("$", 1)[1]
        return hmac.compare_digest(actual, expected)
    except (ValueError, AttributeError):
        return False


@contextmanager
def db():
    connection = sqlite3.connect(database_path())
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS departments (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS shifts (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, start_time TEXT NOT NULL, end_time TEXT NOT NULL, required_minutes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('ADMIN','HR','MANAGER','STAFF')), active INTEGER NOT NULL DEFAULT 1, must_change_password INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS employees (id INTEGER PRIMARY KEY, employee_code TEXT NOT NULL UNIQUE, user_id INTEGER UNIQUE REFERENCES users(id), name TEXT NOT NULL, department_id INTEGER REFERENCES departments(id), designation TEXT, joining_date TEXT, basic_salary REAL NOT NULL DEFAULT 0, required_minutes INTEGER NOT NULL DEFAULT 480, shift_id INTEGER REFERENCES shifts(id), leave_balance REAL NOT NULL DEFAULT 0, monthly_paid_leaves REAL NOT NULL DEFAULT 1, overtime_eligible INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, email TEXT, phone TEXT, date_of_birth TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS attendance (id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id), work_date TEXT NOT NULL, clock_in TEXT, clock_out TEXT, required_minutes INTEGER NOT NULL, actual_minutes INTEGER, overtime_minutes INTEGER NOT NULL DEFAULT 0, late_minutes INTEGER NOT NULL DEFAULT 0, early_leave_minutes INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'ABSENT', remarks TEXT, verification_method TEXT, verification_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(employee_id, work_date));
CREATE TABLE IF NOT EXISTS attendance_overrides (id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id), work_date TEXT NOT NULL, required_minutes INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(employee_id, work_date));
CREATE TABLE IF NOT EXISTS attendance_corrections (id INTEGER PRIMARY KEY, attendance_id INTEGER NOT NULL REFERENCES attendance(id), requested_by INTEGER REFERENCES users(id), requested_clock_in TEXT, requested_clock_out TEXT, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', reviewed_by INTEGER REFERENCES users(id), reviewed_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS audit_logs (id INTEGER PRIMARY KEY, entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL, field_name TEXT NOT NULL, old_value TEXT, new_value TEXT, changed_by INTEGER REFERENCES users(id), reason TEXT NOT NULL, changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS leave_types (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, paid INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS leave_requests (id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id), leave_type_id INTEGER REFERENCES leave_types(id), start_date TEXT NOT NULL, end_date TEXT NOT NULL, reason TEXT, status TEXT NOT NULL DEFAULT 'PENDING', reviewed_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS overtime_records (id INTEGER PRIMARY KEY, attendance_id INTEGER NOT NULL REFERENCES attendance(id), potential_minutes INTEGER NOT NULL, approved_minutes INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'PENDING', reviewed_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS holidays (id INTEGER PRIMARY KEY, holiday_date TEXT NOT NULL UNIQUE, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payroll (id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id), month INTEGER NOT NULL, year INTEGER NOT NULL, basic_salary REAL NOT NULL, working_days REAL NOT NULL, present_days REAL NOT NULL, leave_days REAL NOT NULL, unpaid_leave_days REAL NOT NULL, leave_deduction REAL NOT NULL, approved_overtime_minutes INTEGER NOT NULL, overtime_amount REAL NOT NULL, tds REAL NOT NULL, professional_tax REAL NOT NULL, other_deductions REAL NOT NULL, gross_salary REAL NOT NULL, net_salary REAL NOT NULL, status TEXT NOT NULL DEFAULT 'DRAFT', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(employee_id, month, year));
CREATE TABLE IF NOT EXISTS system_settings (setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_attendance_date ON attendance(work_date);
CREATE INDEX IF NOT EXISTS idx_attendance_employee_date ON attendance(employee_id, work_date);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_logs(entity_type, entity_id);
"""


def migrate_schema(con) -> None:
    """Adds newer columns to an existing database without wiping existing data."""
    existing = {row["name"] for row in con.execute("PRAGMA table_info(employees)")}
    if "date_of_birth" not in existing:
        con.execute("ALTER TABLE employees ADD COLUMN date_of_birth TEXT")
    if "monthly_paid_leaves" not in existing:
        con.execute("ALTER TABLE employees ADD COLUMN monthly_paid_leaves REAL NOT NULL DEFAULT 1")
    attendance_cols = {row["name"] for row in con.execute("PRAGMA table_info(attendance)")}
    if "verification_method" not in attendance_cols:
        con.execute("ALTER TABLE attendance ADD COLUMN verification_method TEXT")
    if "verification_at" not in attendance_cols:
        con.execute("ALTER TABLE attendance ADD COLUMN verification_at TEXT")
    migrate_face_folders(con)


def generate_employee_code(con) -> str:
    max_num = 1024
    for row in con.execute("SELECT employee_code FROM employees"):
        code = row["employee_code"]
        if code.startswith("ZV-") and code[3:].isdigit():
            max_num = max(max_num, int(code[3:]))
    return f"ZV-{max_num + 1}"


def initialise_database() -> None:
    with db() as con:
        con.executescript(SCHEMA)
        migrate_schema(con)
        con.execute("INSERT OR IGNORE INTO departments(name) VALUES ('IT'), ('Nursing'), ('Medical'), ('Reception'), ('Administration')")
        con.execute("INSERT OR IGNORE INTO shifts(name, start_time, end_time, required_minutes) VALUES ('General', '10:00', '19:00', 480), ('Morning', '08:00', '17:00', 480), ('Doctor', '11:00', '20:00', 540)")
        con.execute("INSERT OR IGNORE INTO system_settings(setting_key, setting_value) VALUES ('payroll_working_days_base', '26')")
        if not con.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            admin = con.execute("INSERT INTO users(username,password_hash,role,must_change_password) VALUES(?,?,?,1)", ("admin", password_hash("ChangeMe123!"), "ADMIN")).lastrowid
            staff = con.execute("INSERT INTO users(username,password_hash,role,must_change_password) VALUES(?,?,?,1)", ("ZV-1024", password_hash("StaffDemo123!"), "STAFF")).lastrowid
            dept = con.execute("SELECT id FROM departments WHERE name='IT'").fetchone()[0]
            shift = con.execute("SELECT id FROM shifts WHERE name='General'").fetchone()[0]
            con.execute("INSERT INTO employees(employee_code,user_id,name,department_id,designation,basic_salary,required_minutes,shift_id,leave_balance,overtime_eligible) VALUES(?,?,?,?,?,?,?,?,?,?)", ("ZV-1024", staff, "Dhaval", dept, "IT Executive", 30000, 480, shift, 5, 1))


@dataclass
class Session:
    user_id: int
    username: str
    role: str
    employee_id: int | None
    name: str


class ClinicApp(tk.Tk):
    def __init__(self):
        super().__init__()
        initialise_database()
        self.session: Session | None = None
        self.title("Zeeva Clinic | Staff Attendance & HR")
        self.geometry("1200x760")
        self.minsize(1000, 650)
        self.configure(bg=BACKGROUND)
        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self.style.configure("Treeview", rowheight=32, font=("Segoe UI", 10), background=PANEL, fieldbackground=PANEL, foreground=TEXT)
        self.style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"), background="#E8F1F0", foreground=TEXT)
        self.show_login()

    def clear(self):
        for child in self.winfo_children(): child.destroy()

    def show_login(self):
        self.clear()
        card = tk.Frame(self, bg=PANEL, padx=55, pady=45, highlightbackground="#D7E3E1", highlightthickness=1)
        card.place(relx=.5, rely=.5, anchor="center", width=470)
        tk.Label(card, text="ZEEVA", fg=PRIMARY_DARK, bg=PANEL, font=("Segoe UI", 27, "bold")).pack()
        tk.Label(card, text="CLINIC", fg=MUTED, bg=PANEL, font=("Segoe UI", 10, "bold")).pack(pady=(0, 28))
        tk.Label(card, text="Staff Attendance", fg=TEXT, bg=PANEL, font=("Segoe UI", 19, "bold")).pack(anchor="w")
        tk.Label(card, text="Sign in to securely access your workspace", fg=MUTED, bg=PANEL, font=("Segoe UI", 10)).pack(anchor="w", pady=(5, 24))
        self.login_username = self.labeled_entry(card, "Employee ID / Username")
        self.login_password = self.labeled_entry(card, "Password", show="•")
        self.login_password.bind("<Return>", lambda _: self.authenticate())
        
        # Button row for SIGN IN and SCAN FACE
        button_row = tk.Frame(card, bg=PANEL)
        button_row.pack(fill="x", pady=(24, 0))
        tk.Button(button_row, text="SIGN IN", command=self.authenticate, bg=PRIMARY_DARK, fg="white", activebackground=PRIMARY, relief="flat", cursor="hand2", font=("Segoe UI", 10, "bold"), pady=12).pack(side="left", fill="x", expand=True)
        tk.Button(button_row, text="SCAN FACE", command=self.quick_face_clock, bg=PRIMARY, fg="white", activebackground=PRIMARY_DARK, relief="flat", cursor="hand2", font=("Segoe UI", 10, "bold"), pady=12).pack(side="left", fill="x", expand=True, padx=(8, 0))
        
        tk.Label(card, text="Your attendance data is private and protected.", fg=MUTED, bg=PANEL, font=("Segoe UI", 9)).pack(pady=(18, 0))

    def labeled_entry(self, parent, label, show=None):
        tk.Label(parent, text=label, bg=PANEL, fg=TEXT, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(12, 5))
        entry = tk.Entry(parent, show=show, font=("Segoe UI", 12), relief="solid", bd=1, highlightthickness=1, highlightbackground="#D7E3E1")
        entry.pack(fill="x", ipady=9)
        return entry

    def authenticate(self):
        username, password = self.login_username.get().strip(), self.login_password.get()
        with db() as con:
            user = con.execute("SELECT u.*, e.id employee_id, e.name FROM users u LEFT JOIN employees e ON e.user_id=u.id WHERE u.username=? AND u.active=1", (username,)).fetchone()
        if not user or not verify_password(password, user["password_hash"]):
            messagebox.showerror("Sign in failed", "Invalid Employee ID or password.")
            return
        self.session = Session(user["id"], user["username"], user["role"], user["employee_id"], user["name"] or user["username"])
        if user["must_change_password"]:
            self.force_password_change_dialog()
            return
        self.show_shell("Dashboard")

    def force_password_change_dialog(self):
        """Blocks access until a temporary password is replaced. Cancelling signs the user back out."""
        dialog = tk.Toplevel(self); dialog.title("Set a new password"); dialog.configure(bg=PANEL); dialog.resizable(False, False)
        dialog.grab_set(); dialog.protocol("WM_DELETE_WINDOW", lambda: None)
        wrap = tk.Frame(dialog, bg=PANEL, padx=30, pady=25); wrap.pack()
        tk.Label(wrap, text="Choose a new password", bg=PANEL, fg=TEXT, font=("Segoe UI", 15, "bold")).pack(anchor="w")
        tk.Label(wrap, text="You're signed in with a temporary password. Set your own before continuing.", bg=PANEL, fg=MUTED, wraplength=320, justify="left").pack(anchor="w", pady=(3, 16))
        new_pw = self.labeled_entry(wrap, "New password (min 8 characters)", show="•")
        confirm_pw = self.labeled_entry(wrap, "Confirm new password", show="•")

        def save():
            pw, confirm = new_pw.get(), confirm_pw.get()
            if len(pw) < 8:
                messagebox.showerror("Too short", "Use at least 8 characters.", parent=dialog); return
            if pw != confirm:
                messagebox.showerror("Doesn't match", "Both passwords must match.", parent=dialog); return
            with db() as con:
                con.execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?", (password_hash(pw), self.session.user_id))
                con.execute("INSERT INTO audit_logs(entity_type,entity_id,field_name,old_value,new_value,changed_by,reason) VALUES('user',?,?,?,?,?,?)", (self.session.user_id, "password_changed", "", "self-service", self.session.user_id, "First-login password change"))
            dialog.destroy()
            self.show_shell("Dashboard")
        tk.Button(wrap, text="SAVE & CONTINUE", command=save, bg=PRIMARY_DARK, fg="white", relief="flat", cursor="hand2", font=("Segoe UI", 10, "bold"), pady=11).pack(fill="x", pady=(16, 0))
        tk.Button(wrap, text="SIGN OUT", command=lambda: (dialog.destroy(), self.show_login()), bg="#E2E8E7", fg=TEXT, relief="flat", cursor="hand2", font=("Segoe UI", 9, "bold"), pady=9).pack(fill="x", pady=(8, 0))

    def quick_face_clock(self):
        """Quick clock-in/out from login screen using face scan alone (no username/password needed).
        Recognizes the employee from their registered face and automatically clocks them in or out."""
        cv2, Image, ImageTk = _face_libs()
        if cv2 is None:
            messagebox.showerror("Face recognition unavailable", "Face recognition is unavailable. Please use regular login instead.")
            return
        if not face_model_available():
            messagebox.showerror("Face model not ready", "Face registration model not available. Please ask your Manager/Admin to register faces first.")
            return

        try:
            capture = cv2.VideoCapture(0)
            camera_ok = capture.isOpened()
        except Exception:
            camera_ok = False
        if not camera_ok:
            messagebox.showerror("Camera not found", "Couldn't open the webcam. Check that it's connected and not in use by another app.")
            try: capture.release()
            except Exception: pass
            return

        dialog = tk.Toplevel(self); dialog.title("Quick Clock: Scan Face"); dialog.configure(bg=PANEL); dialog.resizable(False, False); dialog.grab_set()
        tk.Label(dialog, text="Position your face in the camera", bg=PANEL, fg=TEXT, font=("Segoe UI", 14, "bold")).pack(padx=24, pady=(20, 4))
        hint = tk.Label(dialog, text="Looking for your registered face...", bg=PANEL, fg=MUTED); hint.pack(padx=24)
        video_label = tk.Label(dialog, bg="#000000"); video_label.pack(padx=24, pady=14)
        progress = tk.Label(dialog, text="", bg=PANEL, fg=PRIMARY_DARK, font=("Segoe UI", 10, "bold")); progress.pack(padx=24, pady=(0, 6))

        state = {
            "running": True, "phase": "CENTER1", "matched_frames": 0,
            "turn_direction": None, "elapsed_ticks": 0, "recognized_employee_id": None,
        }
        deadline_ticks = int(FACE_SCAN_TIMEOUT_SECONDS * 1000 / 30)

        def reset_progress(reason_text):
            state["phase"] = "CENTER1"; state["matched_frames"] = 0; state["turn_direction"] = None
            hint.configure(text=reason_text)
            progress.configure(text="")

        def cleanup():
            state["running"] = False
            try: capture.release()
            except Exception: pass

        def close():
            cleanup()
            if dialog.winfo_exists(): dialog.destroy()
        dialog.protocol("WM_DELETE_WINDOW", close)
        tk.Button(dialog, text="CANCEL", command=close, bg="#E2E8E7", fg=TEXT, relief="flat", cursor="hand2", font=("Segoe UI", 9, "bold"), padx=16, pady=8).pack(pady=(0, 18))

        def timeout_fail():
            cleanup()
            if dialog.winfo_exists(): dialog.destroy()
            messagebox.showerror("Face verification failed", "Could not recognize your face. Please use regular login instead.")

        def succeed_clock_in_out(employee_id, employee_name):
            """Record attendance for the recognized employee without logging in."""
            cleanup()
            if dialog.winfo_exists(): dialog.destroy()
            
            with db() as con:
                today_str = date.today().isoformat()
                existing = con.execute(
                    "SELECT id, clock_in, clock_out FROM attendance WHERE employee_id=? AND attendance_date=?",
                    (employee_id, today_str)
                ).fetchone()
                
                if existing and existing["clock_out"]:
                    # Already clocked out today
                    messagebox.showinfo("Already clocked out", f"Welcome {employee_name}! You already clocked out today at {existing['clock_out']}.\n\nPlease sign in to view your details.")
                    self.show_login()
                    return
                
                if existing and existing["clock_in"]:
                    # Clock them out
                    action = "CLOCK OUT"
                    now = datetime.now()
                    con.execute("UPDATE attendance SET clock_out=? WHERE id=?", (now.isoformat(), existing["id"]))
                    con.execute("INSERT INTO audit_logs(entity_type,entity_id,field_name,old_value,new_value,changed_by,reason) VALUES('attendance',?,?,?,?,?,?)", 
                                (existing["id"], "clock_out", "", now.isoformat(), employee_id, "Face-scan clock out from login screen"))
                    clock_out_time = now.strftime("%I:%M %p")
                else:
                    # Clock them in
                    action = "CLOCK IN"
                    now = datetime.now()
                    attendance_id = con.execute(
                        "INSERT INTO attendance(employee_id,attendance_date,clock_in,verification_method) VALUES(?,?,?,?)",
                        (employee_id, today_str, now.isoformat(), "FACE")
                    ).lastrowid
                    con.execute("INSERT INTO audit_logs(entity_type,entity_id,field_name,old_value,new_value,changed_by,reason) VALUES('attendance',?,?,?,?,?,?)", 
                                (attendance_id, "clock_in", "", now.isoformat(), employee_id, "Face-scan clock in from login screen"))
                    clock_out_time = now.strftime("%I:%M %p")
            
            # Show success message
            result_msg = f"✓ {action} Recorded\n\nName: {employee_name}\nTime: {clock_out_time}\n\nWould you like to sign in to view your dashboard?"
            response = messagebox.askyesno("Attendance Recorded", result_msg)
            if response:
                self.show_login()
            else:
                self.show_login()

        def step():
            if not state["running"]: return
            state["elapsed_ticks"] += 1
            if state["elapsed_ticks"] > deadline_ticks:
                timeout_fail(); return
            try:
                ok, frame = capture.read()
            except Exception:
                ok, frame = False, None
            if not ok or frame is None:
                hint.configure(text="No face detected")
                dialog.after(30, step); return

            try:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                crop, box, face_count = single_face_crop(cv2, gray)
                frame_w = gray.shape[1]
                display = frame.copy()
            except Exception:
                dialog.after(30, step); return

            if face_count == 0:
                reset_progress("No face detected")
            elif face_count > 1:
                reset_progress("Multiple faces detected — only one person in frame, please")
            else:
                x, y, w, h = box
                cv2.rectangle(display, (x, y), (x + w, y + h), (150, 150, 220), 2)
                center_ratio = (x + w / 2) / frame_w
                offset = center_ratio - 0.5

                if state["phase"] in ("CENTER1", "CENTER2"):
                    if abs(offset) > 0.22:
                        hint.configure(text="Face the camera straight on")
                    else:
                        matched, _confidence, employee_id = verify_face_anonymous(crop)
                        if not matched:
                            reset_progress("Face not recognized")
                        else:
                            state["recognized_employee_id"] = employee_id
                            state["matched_frames"] += 1
                            cv2.rectangle(display, (x, y), (x + w, y + h), (100, 200, 100), 2)
                            hint.configure(text=f"Identity verified {state['matched_frames']}/{FACE_MATCH_FRAMES_REQUIRED}")
                            if state["phase"] == "CENTER1" and state["matched_frames"] >= 2:
                                state["phase"] = "TURN"
                                hint.configure(text="Now slowly turn your head — left or right")
                            elif state["phase"] == "CENTER2" and state["matched_frames"] >= FACE_MATCH_FRAMES_REQUIRED:
                                progress.configure(text="Verification successful")
                                with db() as con:
                                    emp = con.execute("SELECT name FROM employees WHERE id=?", (state["recognized_employee_id"],)).fetchone()
                                succeed_clock_in_out(state["recognized_employee_id"], emp["name"] if emp else "Staff")
                                return
                elif state["phase"] == "TURN":
                    if offset < -FACE_LIVENESS_TURN_RATIO:
                        state["turn_direction"] = "left"
                    elif offset > FACE_LIVENESS_TURN_RATIO:
                        state["turn_direction"] = "right"
                    if state["turn_direction"]:
                        hint.configure(text="Good — now return to center")
                        state["phase"] = "CENTER2"
                    else:
                        hint.configure(text="Slowly turn your head — left or right")
                progress.configure(text=f"{state['matched_frames']}/{FACE_MATCH_FRAMES_REQUIRED} verified")

            rgb = cv2.cvtColor(display, cv2.COLOR_BGR2RGB)
            photo = ImageTk.PhotoImage(Image.fromarray(rgb).resize((480, 360)))
            video_label.configure(image=photo); video_label.image = photo
            dialog.after(30, step)

        dialog.after(200, step)

    def show_shell(self, page):
        self.clear()
        sidebar = tk.Frame(self, bg="#234543", width=230)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="ZEEVA\nCLINIC", justify="left", bg="#234543", fg="white", font=("Segoe UI", 20, "bold"), padx=25, pady=32).pack(anchor="w")
        menus = ["Dashboard", "My Attendance", "My Leave", "My Salary Slips", "My Requests"] if self.session.role == "STAFF" else ["Dashboard", "Attendance", "Employees", "Leave", "Overtime", "Reports"]
        if self.session.role in ("HR", "ADMIN"): menus += ["Payroll", "Salary Slips", "Settings"]
        for item in menus:
            tk.Button(sidebar, text=item, command=lambda x=item: self.show_shell(x), anchor="w", padx=25, bg="#234543" if item != page else PRIMARY_DARK, fg="white", activebackground=PRIMARY_DARK, activeforeground="white", relief="flat", font=("Segoe UI", 10), pady=11).pack(fill="x")
        tk.Button(sidebar, text="Sign out", command=self.show_login, anchor="w", padx=25, bg="#234543", fg="#DDE9E8", relief="flat", font=("Segoe UI", 10), pady=11).pack(side="bottom", fill="x")
        main = tk.Frame(self, bg=BACKGROUND, padx=35, pady=28)
        main.pack(side="left", fill="both", expand=True)
        tk.Label(main, text=page, bg=BACKGROUND, fg=TEXT, font=("Segoe UI", 23, "bold")).pack(anchor="w")
        tk.Label(main, text=f"{self.session.name}  ·  {self.session.role.title()}  ·  {datetime.now():%d %B %Y}", bg=BACKGROUND, fg=MUTED, font=("Segoe UI", 10)).pack(anchor="w", pady=(3, 22))
        if self.session.role in ("ADMIN", "HR", "MANAGER"): self.birthday_banner(main)
        if page == "Dashboard": self.dashboard(main)
        elif page in ("Attendance", "My Attendance"): self.attendance_page(main, page == "Attendance")
        elif page == "Employees": self.employees_page(main)
        else: self.placeholder(main, page)

    def birthday_banner(self, parent):
        with db() as con:
            rows = con.execute("SELECT name, date_of_birth FROM employees WHERE active=1 AND date_of_birth IS NOT NULL AND date_of_birth != ''").fetchall()
        today, todays, upcoming = date.today(), [], []
        for r in rows:
            try:
                dob = date.fromisoformat(r["date_of_birth"])
                next_birthday = dob.replace(year=today.year)
            except ValueError:
                continue
            except Exception:
                continue
            if next_birthday < today:
                try: next_birthday = next_birthday.replace(year=today.year + 1)
                except ValueError: continue
            delta = (next_birthday - today).days
            if delta == 0: todays.append(r["name"])
            elif 0 < delta <= 3: upcoming.append((r["name"], delta))
        if todays:
            tk.Label(parent, text="\U0001F382 Birthday today: " + ", ".join(todays), bg="#FBE4E1", fg="#B03A2E", font=("Segoe UI", 10, "bold"), anchor="w", padx=16, pady=9).pack(fill="x", pady=(0, 10))
        if upcoming:
            text = ", ".join(f"{n} (in {d} day{'s' if d != 1 else ''})" for n, d in upcoming)
            tk.Label(parent, text="\U0001F388 Coming up: " + text, bg="#FFF3DD", fg=WARNING, font=("Segoe UI", 9, "bold"), anchor="w", padx=16, pady=7).pack(fill="x", pady=(0, 10))

    def card(self, parent, title, value, subtitle=""):
        frame = tk.Frame(parent, bg=PANEL, padx=18, pady=16, highlightbackground="#E2E8E7", highlightthickness=1)
        tk.Label(frame, text=title.upper(), bg=PANEL, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        tk.Label(frame, text=value, bg=PANEL, fg=PRIMARY_DARK, font=("Segoe UI", 22, "bold")).pack(anchor="w", pady=(8, 2))
        tk.Label(frame, text=subtitle, bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w")
        return frame

    def dashboard(self, parent):
        if self.session.role == "STAFF": self.staff_dashboard(parent); return
        today = date.today().isoformat()
        with db() as con:
            rows = con.execute("SELECT a.*, e.name FROM attendance a JOIN employees e ON e.id=a.employee_id WHERE a.work_date=?", (today,)).fetchall()
            active = con.execute("SELECT COUNT(*) c FROM employees WHERE active=1").fetchone()["c"]
        present = sum(1 for r in rows if r["clock_in"])
        working = sum(1 for r in rows if r["clock_in"] and not r["clock_out"])
        late = sum(1 for r in rows if r["late_minutes"] > 0)
        grid = tk.Frame(parent, bg=BACKGROUND); grid.pack(fill="x")
        for i, (title, value, sub) in enumerate((("Present", present, "clocked in today"), ("Absent", max(active-present, 0), "active staff"), ("Late", late, "late arrivals"), ("Currently inside", working, "not clocked out"))):
            grid.columnconfigure(i, weight=1); self.card(grid, title, str(value), sub).grid(row=0, column=i, padx=(0 if i == 0 else 10, 0), sticky="ew")
        tk.Label(parent, text="Today's attendance", bg=BACKGROUND, fg=TEXT, font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(28, 10))
        self.attendance_table(parent, rows, editable=True)

    def staff_dashboard(self, parent):
        record = self.today_record()
        status = "Not clocked in" if not record or not record["clock_in"] else ("Working" if not record["clock_out"] else "Present")
        hours = self.minutes_label(record["actual_minutes"]) if record and record["actual_minutes"] is not None else "—"
        grid = tk.Frame(parent, bg=BACKGROUND); grid.pack(fill="x")
        for i, (t, v, s) in enumerate((("Today", status, "attendance status"), ("Clock in", self.time_label(record["clock_in"]) if record else "—", "today"), ("Working", hours, "completed time"), ("Required", self.minutes_label(record["required_minutes"]) if record else "8h 00m", "daily hours"))):
            grid.columnconfigure(i, weight=1); self.card(grid, t, v, s).grid(row=0, column=i, padx=(0 if i == 0 else 10, 0), sticky="ew")
        controls = tk.Frame(parent, bg=BACKGROUND); controls.pack(fill="x", pady=28)
        tk.Button(controls, text="SCAN FACE & CLOCK IN", command=lambda: self.face_clock_dialog("IN"), bg=PRIMARY_DARK, fg="white", relief="flat", font=("Segoe UI", 10, "bold"), padx=24, pady=12).pack(side="left")
        tk.Button(controls, text="SCAN FACE & CLOCK OUT", command=lambda: self.face_clock_dialog("OUT"), bg="#526465", fg="white", relief="flat", font=("Segoe UI", 10, "bold"), padx=24, pady=12).pack(side="left", padx=10)
        tk.Label(parent, text="My recent attendance", bg=BACKGROUND, fg=TEXT, font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(5, 10))
        with db() as con:
            rows = con.execute("SELECT a.*, e.name FROM attendance a JOIN employees e ON e.id=a.employee_id WHERE a.employee_id=? ORDER BY work_date DESC LIMIT 12", (self.session.employee_id,)).fetchall()
        self.attendance_table(parent, rows, editable=False)

    def today_record(self):
        with db() as con:
            return con.execute("SELECT * FROM attendance WHERE employee_id=? AND work_date=?", (self.session.employee_id, date.today().isoformat())).fetchone()

    def clock(self, action, verification_method="FACE"):
        if not self.session.employee_id: return
        now = datetime.now(); today = now.date().isoformat(); stamp = now.isoformat(timespec="seconds")
        try:
            with db() as con:
                employee = con.execute("SELECT e.*, s.start_time, s.end_time FROM employees e LEFT JOIN shifts s ON s.id=e.shift_id WHERE e.id=?", (self.session.employee_id,)).fetchone()
                record = con.execute("SELECT * FROM attendance WHERE employee_id=? AND work_date=?", (employee["id"], today)).fetchone()
                if action == "IN":
                    if record and record["clock_in"]:
                        messagebox.showinfo("Already clocked in", f"You are already marked IN at {self.time_label(record['clock_in'])}."); return
                    calc = self.compute_attendance(stamp, None, employee["start_time"], employee["end_time"], employee["required_minutes"], employee["overtime_eligible"])
                    if record:
                        con.execute("UPDATE attendance SET clock_in=?,status=?,late_minutes=?,verification_method=?,verification_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                    (stamp, calc["status"], calc["late_minutes"], verification_method, stamp, record["id"]))
                    else:
                        con.execute("INSERT INTO attendance(employee_id,work_date,clock_in,required_minutes,late_minutes,status,verification_method,verification_at) VALUES(?,?,?,?,?,?,?,?)",
                                    (employee["id"], today, stamp, employee["required_minutes"], calc["late_minutes"], calc["status"], verification_method, stamp))
                    messagebox.showinfo("Attendance recorded", f"Attendance recorded successfully.\n\nEmployee: {employee['name']}\nDate: {now:%d %B %Y}\nIN Time: {now:%I:%M %p}")
                else:
                    if not record or not record["clock_in"]:
                        messagebox.showerror("Clock Out unavailable", "You cannot Clock Out because today's Clock In is missing."); return
                    if record["clock_out"]:
                        messagebox.showinfo("Already clocked out", f"You have already clocked OUT at {self.time_label(record['clock_out'])}."); return
                    calc = self.compute_attendance(record["clock_in"], stamp, employee["start_time"], employee["end_time"], record["required_minutes"], employee["overtime_eligible"])
                    con.execute("UPDATE attendance SET clock_out=?,actual_minutes=?,overtime_minutes=?,early_leave_minutes=?,status=?,verification_method=?,verification_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                (stamp, calc["actual_minutes"], calc["overtime_minutes"], calc["early_leave_minutes"], calc["status"], verification_method, stamp, record["id"]))
                    messagebox.showinfo("Clock Out recorded", f"Clock Out recorded successfully.\n\nEmployee: {employee['name']}\nOUT Time: {now:%I:%M %p}")
        except sqlite3.IntegrityError:
            messagebox.showerror("Attendance not saved", "Today's attendance already has a record for this account. Please refresh and try again.")
            return
        except sqlite3.Error:
            messagebox.showerror("Attendance not saved", "A database error occurred and attendance was NOT recorded. Please try again or contact your Manager/Admin.")
            return
        self.show_shell("Dashboard")

    def register_face_dialog(self, employee_id, employee_name):
        if self.session.role not in ("MANAGER", "ADMIN"):
            messagebox.showerror("Not allowed", "Only a Manager or Admin can register a staff member's face."); return
        cv2, Image, ImageTk = _face_libs()
        if cv2 is None:
            messagebox.showerror("Face recognition not installed", "Install the required packages first, then restart the app:\n\npy -3 -m pip install opencv-contrib-python pillow")
            return
        if not messagebox.askyesno("Consent required", f"Face data is biometric information. Confirm {employee_name} has agreed to register their face for attendance before continuing."):
            return
        capture = cv2.VideoCapture(0)
        if not capture.isOpened():
            messagebox.showerror("Camera not found", "Could not open a webcam. Check that one is connected and not in use by another app.")
            return

        dialog = tk.Toplevel(self); dialog.title(f"Register Face — {employee_name}"); dialog.configure(bg=PANEL); dialog.resizable(False, False); dialog.grab_set()
        tk.Label(dialog, text=f"Register {employee_name}'s face", bg=PANEL, fg=TEXT, font=("Segoe UI", 14, "bold")).pack(padx=24, pady=(20, 4))
        hint = tk.Label(dialog, text=FACE_REGISTRATION_ANGLES[0][1], bg=PANEL, fg=MUTED); hint.pack(padx=24)
        video_label = tk.Label(dialog, bg="#000000"); video_label.pack(padx=24, pady=14)

        # Capture into a NEW temp folder first; only swap it in for the real folder once every
        # angle has succeeded, so a failed/cancelled registration never touches existing face data.
        target_folder = face_dir_for(employee_id)
        temp_folder = data_dir() / "faces" / f"_pending_{employee_id}"
        if temp_folder.exists(): shutil.rmtree(temp_folder, ignore_errors=True)
        temp_folder.mkdir(parents=True, exist_ok=True)
        had_existing = any(target_folder.glob("*.png"))

        state = {"angle_index": 0, "samples_this_angle": 0, "cooldown": 0, "running": True, "shot": 0}

        def cleanup(release_camera=True):
            state["running"] = False
            if release_camera:
                try: capture.release()
                except Exception: pass
            shutil.rmtree(temp_folder, ignore_errors=True)

        def close():
            cleanup()
            if dialog.winfo_exists(): dialog.destroy()
        dialog.protocol("WM_DELETE_WINDOW", close)
        tk.Button(dialog, text="CANCEL", command=close, bg="#E2E8E7", fg=TEXT, relief="flat", cursor="hand2", font=("Segoe UI", 9, "bold"), padx=16, pady=8).pack(pady=(0, 18))

        def finish():
            state["running"] = False
            try: capture.release()
            except Exception: pass
            
            # Show SAVE / RETAKE dialog instead of auto-saving
            def save_confirmed():
                # Swap the fully-captured temp folder in for the real one; old data is preserved
                # right up until this point, so a mid-registration failure never loses anything.
                if target_folder.exists(): shutil.rmtree(target_folder, ignore_errors=True)
                shutil.move(str(temp_folder), str(target_folder))
                built = train_face_model()
                if dialog.winfo_exists(): dialog.destroy()
                if built:
                    messagebox.showinfo("Face registered", f"{employee_name}'s face was registered successfully. They can now use face-scan clock in.")
                else:
                    messagebox.showwarning("Registration incomplete", "Samples were saved but the recognition model could not be built. Try registering again.")
            
            def retake():
                # Go back to angle 1 and recapture
                state["angle_index"] = 0
                state["samples_this_angle"] = 0
                state["shot"] = 0
                state["running"] = True
                shutil.rmtree(temp_folder, ignore_errors=True)
                temp_folder.mkdir(parents=True, exist_ok=True)
                try:
                    capture2 = cv2.VideoCapture(0)
                    if capture2.isOpened():
                        # Replace old capture with new one
                        capture.release()
                        capture = capture2
                        hint.configure(text=FACE_REGISTRATION_ANGLES[0][1])
                        dialog.after(200, step)
                    else:
                        messagebox.showerror("Camera error", "Could not reopen camera for retake.")
                        capture2.release()
                except Exception as e:
                    messagebox.showerror("Error", f"Could not retake: {e}")
            
            # Hide video and show confirmation buttons
            video_label.pack_forget()
            hint.configure(text="✓ All angles captured!\n\nDo you want to save this face registration?")
            
            # Replace CANCEL button with SAVE/RETAKE buttons
            for widget in dialog.winfo_children():
                if isinstance(widget, tk.Button) and widget.cget("text") == "CANCEL":
                    widget.pack_forget()
            
            button_frame = tk.Frame(dialog, bg=PANEL)
            button_frame.pack(pady=(0, 18), padx=24, fill="x")
            tk.Button(button_frame, text="RETAKE", command=retake, bg="#E2E8E7", fg=TEXT, relief="flat", cursor="hand2", font=("Segoe UI", 9, "bold"), padx=16, pady=8).pack(side="left", padx=(0, 8))
            tk.Button(button_frame, text="SAVE", command=save_confirmed, bg=PRIMARY_DARK, fg="white", relief="flat", cursor="hand2", font=("Segoe UI", 9, "bold"), padx=16, pady=8).pack(side="left")

        def step():
            if not state["running"]: return
            try:
                ok, frame = capture.read()
            except Exception:
                ok, frame = False, None
            if ok and frame is not None:
                try:
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    crop, box, face_count = single_face_crop(cv2, gray)
                    display = frame.copy()
                    if box:
                        x, y, w, h = box
                        cv2.rectangle(display, (x, y), (x + w, y + h), (100, 180, 100), 2)
                    rgb = cv2.cvtColor(display, cv2.COLOR_BGR2RGB)
                    photo = ImageTk.PhotoImage(Image.fromarray(rgb).resize((480, 360)))
                    video_label.configure(image=photo); video_label.image = photo
                except Exception:
                    dialog.after(30, step); return

                angle_key, angle_prompt = FACE_REGISTRATION_ANGLES[state["angle_index"]]
                if face_count == 0:
                    hint.configure(text=f"{angle_prompt} — no face detected")
                elif face_count > 1:
                    hint.configure(text="Multiple faces detected — only one person in frame, please")
                elif state["cooldown"] > 0:
                    state["cooldown"] -= 1
                    hint.configure(text=f"{angle_prompt} — hold still ({state['samples_this_angle']}/{FACE_REGISTRATION_SAMPLES_PER_ANGLE})")
                elif crop is not None:
                    state["shot"] += 1
                    cv2.imwrite(str(temp_folder / f"{angle_key}_{state['shot']}.png"), crop)
                    state["samples_this_angle"] += 1
                    state["cooldown"] = 10
                    if state["samples_this_angle"] >= FACE_REGISTRATION_SAMPLES_PER_ANGLE:
                        state["angle_index"] += 1
                        state["samples_this_angle"] = 0
                        if state["angle_index"] >= len(FACE_REGISTRATION_ANGLES):
                            finish(); return
                        hint.configure(text=FACE_REGISTRATION_ANGLES[state["angle_index"]][1])
                    else:
                        hint.configure(text=f"{angle_prompt} — captured {state['samples_this_angle']}/{FACE_REGISTRATION_SAMPLES_PER_ANGLE}")
            else:
                hint.configure(text="Camera frame unavailable — check the webcam connection")
            dialog.after(30, step)

        dialog.after(200, step)

    def face_clock_dialog(self, action):
        """Mandatory face-scan clock in/out. There is deliberately no password or manager
        bypass here: a missing registration, unavailable camera, multiple faces, wrong face,
        or a failed liveness check all mean attendance is simply NOT recorded."""
        if not self.session.employee_id:
            return
        employee_id = self.session.employee_id
        with db() as con:
            employee = con.execute("SELECT name FROM employees WHERE id=?", (employee_id,)).fetchone()
        employee_name = employee["name"]

        cv2, Image, ImageTk = _face_libs()
        if cv2 is None:
            messagebox.showerror("Face recognition unavailable", "Face recognition is unavailable. Please contact the Manager/Admin.")
            return
        if not face_model_available() or not any(face_dir_for(employee_id).glob("*.png")):
            messagebox.showerror("Face not registered", "Face scan isn't set up for your account yet. Ask your Manager/Admin to register it from Employees. Attendance was NOT recorded.")
            return

        try:
            capture = cv2.VideoCapture(0)
            camera_ok = capture.isOpened()
        except Exception:
            camera_ok = False
        if not camera_ok:
            messagebox.showerror("Camera not found", "Couldn't open the webcam. Check that it's connected and not in use by another app. Attendance was NOT recorded.")
            try: capture.release()
            except Exception: pass
            return

        dialog = tk.Toplevel(self); dialog.title(f"Face Scan — {action.title()}"); dialog.configure(bg=PANEL); dialog.resizable(False, False); dialog.grab_set()
        tk.Label(dialog, text=f"Confirm it's you, {employee_name}", bg=PANEL, fg=TEXT, font=("Segoe UI", 14, "bold")).pack(padx=24, pady=(20, 4))
        hint = tk.Label(dialog, text="Position your face in the camera", bg=PANEL, fg=MUTED); hint.pack(padx=24)
        video_label = tk.Label(dialog, bg="#000000"); video_label.pack(padx=24, pady=14)
        progress = tk.Label(dialog, text="", bg=PANEL, fg=PRIMARY_DARK, font=("Segoe UI", 10, "bold")); progress.pack(padx=24, pady=(0, 6))

        # phase machine: CENTER1 (2 matched+centred frames) -> TURN (a genuine head turn,
        # weak but real anti-static-photo check) -> CENTER2 (remaining matched frames up to
        # FACE_MATCH_FRAMES_REQUIRED). ANY failure resets the whole sequence to zero.
        state = {
            "running": True, "phase": "CENTER1", "matched_frames": 0,
            "turn_direction": None, "elapsed_ticks": 0,
        }
        deadline_ticks = int(FACE_SCAN_TIMEOUT_SECONDS * 1000 / 30)

        def reset_progress(reason_text):
            state["phase"] = "CENTER1"; state["matched_frames"] = 0; state["turn_direction"] = None
            hint.configure(text=reason_text)
            progress.configure(text="")

        def cleanup():
            state["running"] = False
            try: capture.release()
            except Exception: pass

        def close():
            cleanup()
            if dialog.winfo_exists(): dialog.destroy()
        dialog.protocol("WM_DELETE_WINDOW", close)
        tk.Button(dialog, text="CANCEL", command=close, bg="#E2E8E7", fg=TEXT, relief="flat", cursor="hand2", font=("Segoe UI", 9, "bold"), padx=16, pady=8).pack(pady=(0, 18))

        def timeout_fail():
            cleanup()
            if dialog.winfo_exists(): dialog.destroy()
            messagebox.showerror("Verification failed", "Face verification failed (timed out). Attendance was NOT recorded.")

        def succeed():
            cleanup()
            if dialog.winfo_exists(): dialog.destroy()
            self.clock(action, verification_method="FACE")

        def step():
            if not state["running"]: return
            state["elapsed_ticks"] += 1
            if state["elapsed_ticks"] > deadline_ticks:
                timeout_fail(); return
            try:
                ok, frame = capture.read()
            except Exception:
                ok, frame = False, None
            if not ok or frame is None:
                hint.configure(text="No face detected")
                dialog.after(30, step); return

            try:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                crop, box, face_count = single_face_crop(cv2, gray)
                frame_w = gray.shape[1]
                display = frame.copy()
            except Exception:
                dialog.after(30, step); return

            if face_count == 0:
                reset_progress("No face detected")
            elif face_count > 1:
                reset_progress("Multiple faces detected — only one person in frame, please")
            else:
                x, y, w, h = box
                cv2.rectangle(display, (x, y), (x + w, y + h), (150, 150, 220), 2)
                center_ratio = (x + w / 2) / frame_w
                offset = center_ratio - 0.5

                if state["phase"] in ("CENTER1", "CENTER2"):
                    if abs(offset) > 0.22:
                        hint.configure(text="Face the camera straight on")
                    else:
                        matched, _confidence = verify_face(employee_id, crop)
                        if not matched:
                            reset_progress("Face mismatch")
                        else:
                            state["matched_frames"] += 1
                            cv2.rectangle(display, (x, y), (x + w, y + h), (100, 200, 100), 2)
                            hint.configure(text=f"Identity verified {state['matched_frames']}/{FACE_MATCH_FRAMES_REQUIRED}")
                            if state["phase"] == "CENTER1" and state["matched_frames"] >= 2:
                                state["phase"] = "TURN"
                                hint.configure(text="Now slowly turn your head — left or right")
                            elif state["phase"] == "CENTER2" and state["matched_frames"] >= FACE_MATCH_FRAMES_REQUIRED:
                                progress.configure(text="Verification successful")
                                succeed(); return
                elif state["phase"] == "TURN":
                    if offset < -FACE_LIVENESS_TURN_RATIO:
                        state["turn_direction"] = "left"
                    elif offset > FACE_LIVENESS_TURN_RATIO:
                        state["turn_direction"] = "right"
                    if state["turn_direction"]:
                        hint.configure(text="Good — now return to center")
                        state["phase"] = "CENTER2"
                    else:
                        hint.configure(text="Slowly turn your head — left or right")
                progress.configure(text=f"{state['matched_frames']}/{FACE_MATCH_FRAMES_REQUIRED} verified")

            rgb = cv2.cvtColor(display, cv2.COLOR_BGR2RGB)
            photo = ImageTk.PhotoImage(Image.fromarray(rgb).resize((480, 360)))
            video_label.configure(image=photo); video_label.image = photo
            dialog.after(30, step)

        dialog.after(200, step)

    @staticmethod
    def compute_attendance(clock_in_iso, clock_out_iso, shift_start, shift_end, required_minutes, overtime_eligible):
        """The ONE place attendance numbers are derived, so a manager edit to Clock In,
        Clock Out, or shift always recalculates late/early/overtime/actual consistently —
        never leaving stale values behind from a previous edit."""
        result = {"actual_minutes": None, "late_minutes": 0, "early_leave_minutes": 0, "overtime_minutes": 0, "status": "ABSENT"}
        if not clock_in_iso:
            return result
        clock_in = datetime.fromisoformat(clock_in_iso)
        if shift_start:
            late = max(0, ClinicApp.time_difference_minutes(shift_start, clock_in.strftime("%H:%M")))
            result["late_minutes"] = late
        result["status"] = "WORKING"
        if clock_out_iso:
            clock_out = datetime.fromisoformat(clock_out_iso)
            actual = int((clock_out - clock_in).total_seconds() // 60)
            result["actual_minutes"] = max(0, actual)
            if overtime_eligible:
                result["overtime_minutes"] = max(0, actual - required_minutes)
            if shift_end:
                result["early_leave_minutes"] = max(0, ClinicApp.time_difference_minutes(clock_out.strftime("%H:%M"), shift_end))
            result["status"] = "PRESENT"
        return result

    @staticmethod
    def time_difference_minutes(start, end):
        a = datetime.strptime(start, "%H:%M"); b = datetime.strptime(end, "%H:%M")
        return int((b-a).total_seconds() // 60)
    @staticmethod
    def minutes_label(minutes): return f"{minutes // 60}h {minutes % 60:02d}m" if minutes is not None else "—"
    @staticmethod
    def time_label(value): return datetime.fromisoformat(value).strftime("%I:%M %p") if value else "—"

    def attendance_page(self, parent, all_staff):
        with db() as con:
            if all_staff:
                rows = con.execute("SELECT a.*, e.name, e.employee_code, d.name department FROM attendance a JOIN employees e ON e.id=a.employee_id LEFT JOIN departments d ON d.id=e.department_id ORDER BY a.work_date DESC LIMIT 100").fetchall()
            else:
                rows = con.execute("SELECT a.*, e.name, e.employee_code, d.name department FROM attendance a JOIN employees e ON e.id=a.employee_id LEFT JOIN departments d ON d.id=e.department_id WHERE a.employee_id=? ORDER BY a.work_date DESC", (self.session.employee_id,)).fetchall()
        self.attendance_table(parent, rows, editable=all_staff)

    def attendance_table(self, parent, rows, editable):
        box = tk.Frame(parent, bg=PANEL); box.pack(fill="both", expand=True)
        cols = ("date", "employee", "department", "clock_in", "clock_out", "hours", "status")
        tree = ttk.Treeview(box, columns=cols, show="headings")
        for c, label, width in zip(cols, ("Date","Employee","Department","IN","OUT","Hours","Status"), (100,180,130,100,100,100,110)):
            tree.heading(c, text=label); tree.column(c, width=width, anchor="center")
        for r in rows:
            tree.insert("", "end", iid=str(r["id"]), values=(r["work_date"], r["name"], r["department"] if "department" in r.keys() else "", self.time_label(r["clock_in"]), self.time_label(r["clock_out"]), self.minutes_label(r["actual_minutes"]), r["status"]))
        tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(box, orient="vertical", command=tree.yview); scroll.pack(side="right", fill="y"); tree.configure(yscrollcommand=scroll.set)
        if editable:
            tk.Button(parent, text="EDIT SELECTED ATTENDANCE", command=lambda: self.edit_attendance(tree), bg=PRIMARY_DARK, fg="white", relief="flat", font=("Segoe UI", 9, "bold"), padx=18, pady=10).pack(anchor="e", pady=(12, 0))

    def edit_attendance(self, tree):
        if self.session.role not in ("MANAGER", "ADMIN", "HR"):
            messagebox.showerror("Not allowed", "Only a Manager, HR, or Admin can correct attendance."); return
        selected = tree.selection()
        if not selected: messagebox.showwarning("Select attendance", "Select an attendance record first."); return
        attendance_id = int(selected[0])
        with db() as con:
            record = con.execute("SELECT * FROM attendance WHERE id=?", (attendance_id,)).fetchone()
            employee = con.execute("SELECT e.*, s.start_time, s.end_time FROM employees e LEFT JOIN shifts s ON s.id=e.shift_id WHERE e.id=?", (record["employee_id"],)).fetchone()
        dialog = tk.Toplevel(self); dialog.title("Correct attendance"); dialog.configure(bg=PANEL); dialog.resizable(False, False); dialog.grab_set()
        frame = tk.Frame(dialog, bg=PANEL, padx=30, pady=25); frame.pack()
        tk.Label(frame, text="Edit attendance", bg=PANEL, fg=TEXT, font=("Segoe UI", 16, "bold")).pack(anchor="w")
        tk.Label(frame, text="A reason is required and every change is audited. Late, early-leave and overtime are recalculated automatically.", bg=PANEL, fg=MUTED, wraplength=320, justify="left").pack(anchor="w", pady=(3, 18))
        clock_in = self.labeled_entry(frame, "Clock In (YYYY-MM-DDTHH:MM:SS)"); clock_in.insert(0, record["clock_in"] or "")
        clock_out = self.labeled_entry(frame, "Clock Out (YYYY-MM-DDTHH:MM:SS)"); clock_out.insert(0, record["clock_out"] or "")
        reason = self.labeled_entry(frame, "Reason")
        def save():
            if not reason.get().strip(): messagebox.showerror("Reason required", "Enter a reason for this correction.", parent=dialog); return
            try:
                new_in, new_out = clock_in.get().strip() or None, clock_out.get().strip() or None
                if new_in: datetime.fromisoformat(new_in)
                if new_out: datetime.fromisoformat(new_out)
                if new_in and new_out and datetime.fromisoformat(new_out) < datetime.fromisoformat(new_in):
                    messagebox.showerror("Invalid times", "Clock Out cannot be before Clock In.", parent=dialog); return
            except ValueError: messagebox.showerror("Invalid time", "Use YYYY-MM-DDTHH:MM:SS.", parent=dialog); return
            try:
                with db() as con:
                    old_in, old_out = record["clock_in"], record["clock_out"]
                    calc = self.compute_attendance(new_in, new_out, employee["start_time"], employee["end_time"], record["required_minutes"], employee["overtime_eligible"])
                    con.execute("UPDATE attendance SET clock_in=?,clock_out=?,actual_minutes=?,late_minutes=?,early_leave_minutes=?,overtime_minutes=?,status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                (new_in, new_out, calc["actual_minutes"], calc["late_minutes"], calc["early_leave_minutes"], calc["overtime_minutes"], calc["status"], attendance_id))
                    for field, old, new in (("clock_in",old_in,new_in),("clock_out",old_out,new_out)):
                        if old != new: con.execute("INSERT INTO audit_logs(entity_type,entity_id,field_name,old_value,new_value,changed_by,reason) VALUES('attendance',?,?,?,?,?,?)", (attendance_id,field,old,new,self.session.user_id,reason.get().strip()))
            except sqlite3.Error:
                messagebox.showerror("Not saved", "A database error occurred; the correction was NOT saved.", parent=dialog); return
            dialog.destroy(); self.show_shell("Attendance")
        tk.Button(frame, text="SAVE CORRECTION", command=save, bg=PRIMARY_DARK, fg="white", relief="flat", font=("Segoe UI", 10, "bold"), padx=18, pady=10).pack(anchor="e", pady=(22, 0))

    def employees_page(self, parent):
        can_manage = self.session.role in ("MANAGER", "ADMIN")
        if can_manage:
            top = tk.Frame(parent, bg=BACKGROUND); top.pack(fill="x", pady=(0, 14))
            tk.Label(top, text="Double-click a row to edit that staff member.", bg=BACKGROUND, fg=MUTED, font=("Segoe UI", 9)).pack(side="left")
            tk.Button(top, text="IMPORT PHONE-NAMED FACE PHOTOS", command=self.import_face_photos_dialog, bg="#E2E8E7", fg=TEXT, relief="flat", cursor="hand2", font=("Segoe UI", 9, "bold"), padx=14, pady=9).pack(side="right", padx=(10, 0))
            tk.Button(top, text="+ ADD STAFF", command=self.add_staff_dialog, bg=PRIMARY_DARK, fg="white", relief="flat", cursor="hand2", font=("Segoe UI", 9, "bold"), padx=16, pady=9).pack(side="right")
        with db() as con:
            rows = con.execute("SELECT e.*, d.name department, s.name shift_name, u.active user_active, u.must_change_password FROM employees e LEFT JOIN departments d ON d.id=e.department_id LEFT JOIN shifts s ON s.id=e.shift_id LEFT JOIN users u ON u.id=e.user_id WHERE e.active=1 ORDER BY e.name").fetchall()
        box = tk.Frame(parent, bg=PANEL); box.pack(fill="both", expand=True)
        cols = ("name", "code", "phone", "department", "shift", "account", "password", "face")
        headers = ("Name", "Login ID", "Phone", "Department", "Shift", "Account", "Password", "Face")
        widths = (160, 90, 105, 130, 120, 80, 90, 60)
        tree = ttk.Treeview(box, columns=cols, show="headings")
        for c, label, w in zip(cols, headers, widths):
            tree.heading(c, text=label); tree.column(c, width=w, anchor="center")
        for r in rows:
            face_registered = any(face_dir_for(r["id"]).glob("*.png"))
            tree.insert("", "end", iid=str(r["id"]), values=(
                r["name"], r["employee_code"], r["phone"] or "—", r["department"] or "—", r["shift_name"] or "—",
                "Active" if r["user_active"] else "Inactive",
                "Temporary" if r["must_change_password"] else "Set",
                "✓" if face_registered else "✗",
            ))
        tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(box, orient="vertical", command=tree.yview); scroll.pack(side="right", fill="y"); tree.configure(yscrollcommand=scroll.set)
        if can_manage:
            tree.bind("<Double-1>", lambda e: self.edit_staff_dialog(int(tree.focus())) if tree.focus() else None)

    def import_face_photos_dialog(self):
        if self.session.role not in ("MANAGER", "ADMIN"):
            messagebox.showerror("Not allowed", "Only a Manager or Admin can import face photos."); return
        cv2, Image, ImageTk = _face_libs()
        if cv2 is None:
            messagebox.showerror("Face recognition not installed", "Install the required packages first, then restart the app:\n\npy -3 -m pip install opencv-contrib-python pillow")
            return
        folder = filedialog.askdirectory(title="Select a folder of phone-number-named staff photos")
        if not folder: return
        folder = Path(folder)
        with db() as con:
            employees = con.execute("SELECT id, name, phone FROM employees WHERE active=1 AND phone IS NOT NULL AND phone != ''").fetchall()
        by_phone = {}
        for e in employees:
            key = normalise_phone(e["phone"])
            by_phone.setdefault(key, []).append(e)

        imported, skipped = 0, []
        for img_path in sorted(list(folder.glob("*.jpg")) + list(folder.glob("*.jpeg")) + list(folder.glob("*.png"))):
            phone_key = normalise_phone(img_path.stem)
            if not phone_key:
                skipped.append((img_path.name, "Invalid filename")); continue
            matches = by_phone.get(phone_key, [])
            if not matches:
                skipped.append((img_path.name, "Unknown phone")); continue
            if len(matches) > 1:
                skipped.append((img_path.name, "Duplicate phone")); continue
            try:
                img = cv2.imread(str(img_path))
                if img is None:
                    skipped.append((img_path.name, "Invalid image")); continue
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                crop, box, face_count = single_face_crop(cv2, gray)
            except Exception:
                skipped.append((img_path.name, "Invalid image")); continue
            if face_count == 0:
                skipped.append((img_path.name, "No face")); continue
            if face_count > 1:
                skipped.append((img_path.name, "Multiple faces")); continue
            employee = matches[0]
            target = face_dir_for(employee["id"])
            existing = list(target.glob("*.png"))
            next_index = len(existing)
            cv2.imwrite(str(target / f"IMPORTED_{next_index}.png"), crop)
            with db() as con:
                con.execute("INSERT INTO audit_logs(entity_type,entity_id,field_name,old_value,new_value,changed_by,reason) VALUES('employee',?,?,?,?,?,?)", (employee["id"], "face_imported", "", img_path.name, self.session.user_id, "Face photo imported by phone number"))
            imported += 1

        if imported:
            train_face_model()

        reasons = {}
        for _name, reason in skipped:
            reasons[reason] = reasons.get(reason, 0) + 1
        summary_lines = [f"Imported: {imported}", f"Skipped: {len(skipped)}"]
        if reasons:
            summary_lines.append("")
            summary_lines.append("Reasons:")
            summary_lines += [f"- {reason}: {count}" for reason, count in reasons.items()]
        messagebox.showinfo("Import complete", "\n".join(summary_lines))
        self.show_shell("Employees")


    def add_staff_dialog(self):
        if self.session.role not in ("MANAGER", "ADMIN"):
            messagebox.showerror("Not allowed", "Only a Manager or Admin can add staff."); return
        with db() as con:
            departments = con.execute("SELECT id, name FROM departments WHERE active=1 ORDER BY name").fetchall()
            shifts = con.execute("SELECT id, name, start_time, end_time, required_minutes FROM shifts ORDER BY name").fetchall()
        dialog = tk.Toplevel(self); dialog.title("Add Staff"); dialog.configure(bg=PANEL); dialog.resizable(False, False); dialog.grab_set()
        wrap = tk.Frame(dialog, bg=PANEL, padx=30, pady=25); wrap.pack()
        tk.Label(wrap, text="Add new staff", bg=PANEL, fg=TEXT, font=("Segoe UI", 16, "bold")).pack(anchor="w")
        tk.Label(wrap, text="This creates a login account and employee record together.", bg=PANEL, fg=MUTED).pack(anchor="w", pady=(3, 16))

        columns = tk.Frame(wrap, bg=PANEL); columns.pack()
        left = tk.Frame(columns, bg=PANEL); left.pack(side="left", padx=(0, 24))
        right = tk.Frame(columns, bg=PANEL); right.pack(side="left")

        name = self.labeled_entry(left, "Staff Name *")
        phone = self.labeled_entry(left, "Phone Number *")
        dob = self.labeled_entry(left, "Birthday (YYYY-MM-DD)")
        designation = self.labeled_entry(left, "Designation")
        joining = self.labeled_entry(left, "Joining Date (YYYY-MM-DD)"); joining.insert(0, date.today().isoformat())

        tk.Label(right, text="Department * (pick or type a new one)", bg=PANEL, fg=TEXT, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(12, 5))
        dept_names = [d["name"] for d in departments]
        dept_box = ttk.Combobox(right, values=dept_names, font=("Segoe UI", 11)); dept_box.pack(fill="x", ipady=4)
        if dept_names: dept_box.current(0)

        tk.Label(right, text="Shift Name * (pick or type a new one)", bg=PANEL, fg=TEXT, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(12, 5))
        shift_names = [s["name"] for s in shifts]
        shift_box = ttk.Combobox(right, values=shift_names, font=("Segoe UI", 11)); shift_box.pack(fill="x", ipady=4)
        if shift_names: shift_box.current(0)

        time_row = tk.Frame(right, bg=PANEL); time_row.pack(fill="x", pady=(10, 0))
        start_col = tk.Frame(time_row, bg=PANEL); start_col.pack(side="left", padx=(0, 10))
        end_col = tk.Frame(time_row, bg=PANEL); end_col.pack(side="left")
        tk.Label(start_col, text="Start (HH:MM)", bg=PANEL, fg=TEXT, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        start_time = tk.Entry(start_col, font=("Segoe UI", 11), relief="solid", bd=1, width=10); start_time.pack(ipady=6)
        tk.Label(end_col, text="End (HH:MM)", bg=PANEL, fg=TEXT, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        end_time = tk.Entry(end_col, font=("Segoe UI", 11), relief="solid", bd=1, width=10); end_time.pack(ipady=6)
        tk.Label(right, text="Only needed when typing a brand-new shift name — picking an\nexisting one fills these in for you.", bg=PANEL, fg=MUTED, font=("Segoe UI", 8), justify="left").pack(anchor="w", pady=(4, 0))

        def fill_shift_times(event=None):
            match = next((s for s in shifts if s["name"].lower() == shift_box.get().strip().lower()), None)
            start_time.delete(0, "end"); end_time.delete(0, "end")
            if match:
                start_time.insert(0, match["start_time"]); end_time.insert(0, match["end_time"])
        shift_box.bind("<<ComboboxSelected>>", fill_shift_times)
        shift_box.bind("<FocusOut>", fill_shift_times)
        fill_shift_times()

        salary = self.labeled_entry(right, "Monthly Salary (₹) *")
        monthly_leave = self.labeled_entry(right, "Paid Leaves per Month"); monthly_leave.insert(0, "1")
        opening_leave = self.labeled_entry(right, "Opening Leave Balance"); opening_leave.insert(0, "0")

        def valid_clock(value):
            try:
                datetime.strptime(value, "%H:%M"); return True
            except ValueError:
                return False

        def save():
            dept_name_val = dept_box.get().strip()
            shift_name_val = shift_box.get().strip()
            if not name.get().strip() or not phone.get().strip() or not dept_name_val or not shift_name_val:
                messagebox.showerror("Missing details", "Name, phone, department and shift are required.", parent=dialog); return
            try:
                salary_val = float(salary.get().strip())
            except ValueError:
                messagebox.showerror("Invalid salary", "Enter a numeric salary.", parent=dialog); return
            try:
                monthly_leave_val = float(monthly_leave.get().strip() or "1")
                opening_leave_val = float(opening_leave.get().strip() or "0")
            except ValueError:
                messagebox.showerror("Invalid leave value", "Leave values must be numeric.", parent=dialog); return
            dob_val = dob.get().strip()
            if dob_val:
                try: date.fromisoformat(dob_val)
                except ValueError: messagebox.showerror("Invalid date", "Birthday must be in YYYY-MM-DD format.", parent=dialog); return
            join_val = joining.get().strip() or date.today().isoformat()
            try: date.fromisoformat(join_val)
            except ValueError: messagebox.showerror("Invalid date", "Joining date must be in YYYY-MM-DD format.", parent=dialog); return

            existing_shift = next((s for s in shifts if s["name"].lower() == shift_name_val.lower()), None)
            start_val, end_val = start_time.get().strip(), end_time.get().strip()
            if not existing_shift:
                if not valid_clock(start_val) or not valid_clock(end_val):
                    messagebox.showerror("Shift timing needed", "Enter a valid Start and End time (HH:MM) to create a new shift.", parent=dialog); return

            temp_password = temporary_password()
            staff_name = name.get().strip()
            with db() as con:
                dept_row = con.execute("SELECT id FROM departments WHERE lower(name)=lower(?)", (dept_name_val,)).fetchone()
                dept_id = dept_row["id"] if dept_row else con.execute("INSERT INTO departments(name) VALUES (?)", (dept_name_val,)).lastrowid

                shift_row = con.execute("SELECT id, start_time, end_time, required_minutes FROM shifts WHERE lower(name)=lower(?)", (shift_name_val,)).fetchone()
                if shift_row:
                    shift_id, required_minutes = shift_row["id"], shift_row["required_minutes"]
                    if valid_clock(start_val) and valid_clock(end_val) and (start_val != shift_row["start_time"] or end_val != shift_row["end_time"]):
                        required_minutes = self.time_difference_minutes(start_val, end_val)
                        if required_minutes <= 0: required_minutes += 24 * 60
                        con.execute("UPDATE shifts SET start_time=?,end_time=?,required_minutes=? WHERE id=?", (start_val, end_val, required_minutes, shift_id))
                else:
                    required_minutes = self.time_difference_minutes(start_val, end_val)
                    if required_minutes <= 0: required_minutes += 24 * 60
                    shift_id = con.execute("INSERT INTO shifts(name,start_time,end_time,required_minutes) VALUES (?,?,?,?)", (shift_name_val, start_val, end_val, required_minutes)).lastrowid

                code = generate_employee_code(con)
                user_id = con.execute("INSERT INTO users(username,password_hash,role,must_change_password) VALUES(?,?,?,1)", (code, password_hash(temp_password), "STAFF")).lastrowid
                new_employee_id = con.execute(
                    "INSERT INTO employees(employee_code,user_id,name,department_id,designation,joining_date,basic_salary,required_minutes,shift_id,leave_balance,monthly_paid_leaves,overtime_eligible,phone,date_of_birth) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (code, user_id, staff_name, dept_id, designation.get().strip() or None, join_val, salary_val, required_minutes, shift_id, opening_leave_val, monthly_leave_val, 0, phone.get().strip(), dob_val or None),
                ).lastrowid
                con.execute("INSERT INTO audit_logs(entity_type,entity_id,field_name,old_value,new_value,changed_by,reason) VALUES('employee',?,?,?,?,?,?)", (new_employee_id, "created", "", staff_name, self.session.user_id, "New staff added"))
            dialog.destroy()
            messagebox.showinfo("Staff added", f"{staff_name} was added successfully.\n\nLogin ID: {code}\nTemporary Password: {temp_password}\n\nShare these with the staff member securely. They'll be required to set their own password at first sign-in.")
            if messagebox.askyesno("Register face?", f"Set up face-scan clock in for {staff_name} now? Face scan is required before they can clock in/out, and they'll need to look at the camera for a few seconds at a few angles."):
                self.register_face_dialog(new_employee_id, staff_name)
            self.show_shell("Employees")

        buttons = tk.Frame(wrap, bg=PANEL); buttons.pack(anchor="e", pady=(22, 0))
        tk.Button(buttons, text="CANCEL", command=dialog.destroy, bg="#E2E8E7", fg=TEXT, relief="flat", cursor="hand2", font=("Segoe UI", 10, "bold"), padx=18, pady=10).pack(side="left", padx=(0, 10))
        tk.Button(buttons, text="SAVE STAFF", command=save, bg=PRIMARY_DARK, fg="white", relief="flat", cursor="hand2", font=("Segoe UI", 10, "bold"), padx=18, pady=10).pack(side="left")

    def edit_staff_dialog(self, employee_id):
        if self.session.role not in ("MANAGER", "ADMIN"):
            messagebox.showerror("Not allowed", "Only a Manager or Admin can edit staff."); return
        with db() as con:
            employee = con.execute("SELECT e.*, d.name department_name, s.name shift_name FROM employees e LEFT JOIN departments d ON d.id=e.department_id LEFT JOIN shifts s ON s.id=e.shift_id WHERE e.id=?", (employee_id,)).fetchone()
            departments = con.execute("SELECT id, name FROM departments WHERE active=1 ORDER BY name").fetchall()
            shifts = con.execute("SELECT id, name, start_time, end_time, required_minutes FROM shifts ORDER BY name").fetchall()
        if not employee:
            messagebox.showerror("Not found", "That staff record no longer exists."); return

        dialog = tk.Toplevel(self); dialog.title(f"Edit Staff — {employee['name']}"); dialog.configure(bg=PANEL); dialog.resizable(False, False); dialog.grab_set()
        wrap = tk.Frame(dialog, bg=PANEL, padx=30, pady=25); wrap.pack()
        tk.Label(wrap, text=f"Edit {employee['name']}", bg=PANEL, fg=TEXT, font=("Segoe UI", 16, "bold")).pack(anchor="w")
        tk.Label(wrap, text="The employee's database identity, face data, attendance and payroll history never change when the Login ID does.", bg=PANEL, fg=MUTED, wraplength=430, justify="left").pack(anchor="w", pady=(3, 16))

        columns = tk.Frame(wrap, bg=PANEL); columns.pack()
        left = tk.Frame(columns, bg=PANEL); left.pack(side="left", padx=(0, 24))
        right = tk.Frame(columns, bg=PANEL); right.pack(side="left")

        login_id = self.labeled_entry(left, "Login ID * (3-32 letters/numbers/-/_)"); login_id.insert(0, employee["employee_code"])
        name = self.labeled_entry(left, "Staff Name *"); name.insert(0, employee["name"])
        phone = self.labeled_entry(left, "Phone Number *"); phone.insert(0, employee["phone"] or "")
        dob = self.labeled_entry(left, "Birthday (YYYY-MM-DD)"); dob.insert(0, employee["date_of_birth"] or "")
        designation = self.labeled_entry(left, "Designation"); designation.insert(0, employee["designation"] or "")
        joining = self.labeled_entry(left, "Joining Date (YYYY-MM-DD)"); joining.insert(0, employee["joining_date"] or date.today().isoformat())

        tk.Label(right, text="Department * (pick or type a new one)", bg=PANEL, fg=TEXT, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(12, 5))
        dept_names = [d["name"] for d in departments]
        dept_box = ttk.Combobox(right, values=dept_names, font=("Segoe UI", 11)); dept_box.pack(fill="x", ipady=4)
        dept_box.set(employee["department_name"] or "")

        tk.Label(right, text="Shift Name * (pick or type a new one)", bg=PANEL, fg=TEXT, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(12, 5))
        shift_names = [s["name"] for s in shifts]
        shift_box = ttk.Combobox(right, values=shift_names, font=("Segoe UI", 11)); shift_box.pack(fill="x", ipady=4)
        shift_box.set(employee["shift_name"] or "")

        time_row = tk.Frame(right, bg=PANEL); time_row.pack(fill="x", pady=(10, 0))
        start_col = tk.Frame(time_row, bg=PANEL); start_col.pack(side="left", padx=(0, 10))
        end_col = tk.Frame(time_row, bg=PANEL); end_col.pack(side="left")
        tk.Label(start_col, text="Start (HH:MM)", bg=PANEL, fg=TEXT, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        start_time = tk.Entry(start_col, font=("Segoe UI", 11), relief="solid", bd=1, width=10); start_time.pack(ipady=6)
        tk.Label(end_col, text="End (HH:MM)", bg=PANEL, fg=TEXT, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        end_time = tk.Entry(end_col, font=("Segoe UI", 11), relief="solid", bd=1, width=10); end_time.pack(ipady=6)
        tk.Label(right, text="Only needed when typing a brand-new shift name — picking an\nexisting one fills these in for you.", bg=PANEL, fg=MUTED, font=("Segoe UI", 8), justify="left").pack(anchor="w", pady=(4, 0))

        def fill_shift_times(event=None):
            match = next((s for s in shifts if s["name"].lower() == shift_box.get().strip().lower()), None)
            start_time.delete(0, "end"); end_time.delete(0, "end")
            if match:
                start_time.insert(0, match["start_time"]); end_time.insert(0, match["end_time"])
        shift_box.bind("<<ComboboxSelected>>", fill_shift_times)
        shift_box.bind("<FocusOut>", fill_shift_times)
        fill_shift_times()

        salary = self.labeled_entry(right, "Monthly Salary (₹) *"); salary.insert(0, f"{employee['basic_salary']:g}")
        monthly_leave = self.labeled_entry(right, "Paid Leaves per Month"); monthly_leave.insert(0, f"{employee['monthly_paid_leaves']:g}")
        leave_balance = self.labeled_entry(right, "Leave Balance"); leave_balance.insert(0, f"{employee['leave_balance']:g}")

        def valid_clock(value):
            try:
                datetime.strptime(value, "%H:%M"); return True
            except ValueError:
                return False

        def save():
            dept_name_val = dept_box.get().strip()
            shift_name_val = shift_box.get().strip()
            new_login_id = login_id.get().strip()
            if not new_login_id or not name.get().strip() or not phone.get().strip() or not dept_name_val or not shift_name_val:
                messagebox.showerror("Missing details", "Login ID, name, phone, department and shift are required.", parent=dialog); return
            if not re.fullmatch(r"[A-Za-z0-9_-]{3,32}", new_login_id):
                messagebox.showerror("Invalid Login ID", "Use 3–32 letters, numbers, hyphens, or underscores.", parent=dialog); return
            try:
                salary_val = float(salary.get().strip())
            except ValueError:
                messagebox.showerror("Invalid salary", "Enter a numeric salary.", parent=dialog); return
            try:
                monthly_leave_val = float(monthly_leave.get().strip() or "1")
                leave_balance_val = float(leave_balance.get().strip() or "0")
            except ValueError:
                messagebox.showerror("Invalid leave value", "Leave values must be numeric.", parent=dialog); return
            dob_val = dob.get().strip()
            if dob_val:
                try: date.fromisoformat(dob_val)
                except ValueError: messagebox.showerror("Invalid date", "Birthday must be in YYYY-MM-DD format.", parent=dialog); return
            join_val = joining.get().strip() or date.today().isoformat()
            try: date.fromisoformat(join_val)
            except ValueError: messagebox.showerror("Invalid date", "Joining date must be in YYYY-MM-DD format.", parent=dialog); return

            existing_shift = next((s for s in shifts if s["name"].lower() == shift_name_val.lower()), None)
            start_val, end_val = start_time.get().strip(), end_time.get().strip()
            if not existing_shift:
                if not valid_clock(start_val) or not valid_clock(end_val):
                    messagebox.showerror("Shift timing needed", "Enter a valid Start and End time (HH:MM) to create a new shift.", parent=dialog); return

            staff_name = name.get().strip()
            with db() as con:
                duplicate = con.execute("SELECT id FROM users WHERE username=? AND id<>?", (new_login_id, employee["user_id"])).fetchone()
                if duplicate:
                    messagebox.showerror("Login ID already used", "Choose a different Login ID.", parent=dialog); return
                dept_row = con.execute("SELECT id FROM departments WHERE lower(name)=lower(?)", (dept_name_val,)).fetchone()
                dept_id = dept_row["id"] if dept_row else con.execute("INSERT INTO departments(name) VALUES (?)", (dept_name_val,)).lastrowid

                shift_row = con.execute("SELECT id, start_time, end_time, required_minutes FROM shifts WHERE lower(name)=lower(?)", (shift_name_val,)).fetchone()
                if shift_row:
                    shift_id, required_minutes = shift_row["id"], shift_row["required_minutes"]
                    if valid_clock(start_val) and valid_clock(end_val) and (start_val != shift_row["start_time"] or end_val != shift_row["end_time"]):
                        required_minutes = self.time_difference_minutes(start_val, end_val)
                        if required_minutes <= 0: required_minutes += 24 * 60
                        con.execute("UPDATE shifts SET start_time=?,end_time=?,required_minutes=? WHERE id=?", (start_val, end_val, required_minutes, shift_id))
                else:
                    required_minutes = self.time_difference_minutes(start_val, end_val)
                    if required_minutes <= 0: required_minutes += 24 * 60
                    shift_id = con.execute("INSERT INTO shifts(name,start_time,end_time,required_minutes) VALUES (?,?,?,?)", (shift_name_val, start_val, end_val, required_minutes)).lastrowid

                con.execute(
                    "UPDATE employees SET name=?,phone=?,date_of_birth=?,designation=?,joining_date=?,department_id=?,shift_id=?,basic_salary=?,required_minutes=?,leave_balance=?,monthly_paid_leaves=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (staff_name, phone.get().strip(), dob_val or None, designation.get().strip() or None, join_val, dept_id, shift_id, salary_val, required_minutes, leave_balance_val, monthly_leave_val, employee_id),
                )
                if new_login_id != employee["employee_code"]:
                    con.execute("UPDATE employees SET employee_code=? WHERE id=?", (new_login_id, employee_id))
                    con.execute("UPDATE users SET username=? WHERE id=?", (new_login_id, employee["user_id"]))
                    con.execute("INSERT INTO audit_logs(entity_type,entity_id,field_name,old_value,new_value,changed_by,reason) VALUES('employee',?,?,?,?,?,?)", (employee_id, "login_id", employee["employee_code"], new_login_id, self.session.user_id, "Manager/Admin changed Login ID"))
                con.execute("INSERT INTO audit_logs(entity_type,entity_id,field_name,old_value,new_value,changed_by,reason) VALUES('employee',?,?,?,?,?,?)", (employee_id, "updated", employee["name"], staff_name, self.session.user_id, "Staff details edited"))
            dialog.destroy()
            messagebox.showinfo("Staff updated", f"{staff_name}'s details were updated.")
            self.show_shell("Employees")

        def reset_password():
            if not messagebox.askyesno("Reset password", f"Generate a new temporary password for {employee['name']}?", parent=dialog): return
            new_password = temporary_password()
            with db() as con:
                con.execute("UPDATE users SET password_hash=?, must_change_password=1 WHERE id=?", (password_hash(new_password), employee["user_id"]))
                con.execute("INSERT INTO audit_logs(entity_type,entity_id,field_name,old_value,new_value,changed_by,reason) VALUES('user',?,?,?,?,?,?)", (employee["user_id"], "password_reset", "", "issued", self.session.user_id, "Manager/Admin issued temporary password"))
            messagebox.showinfo("Password reset", f"New temporary password for {employee['employee_code']}:\n\n{new_password}\n\nShare this with the staff member securely. They'll be required to set their own password at next sign-in.", parent=dialog)

        def deactivate():
            if not messagebox.askyesno("Deactivate staff", f"Deactivate {employee['name']}? They'll disappear from the active staff list but their attendance history is kept.", parent=dialog): return
            with db() as con:
                con.execute("UPDATE employees SET active=0, updated_at=CURRENT_TIMESTAMP WHERE id=?", (employee_id,))
                con.execute("UPDATE users SET active=0 WHERE id=?", (employee["user_id"],))
                con.execute("INSERT INTO audit_logs(entity_type,entity_id,field_name,old_value,new_value,changed_by,reason) VALUES('employee',?,?,?,?,?,?)", (employee_id, "active", "1", "0", self.session.user_id, "Staff deactivated"))
            dialog.destroy()
            messagebox.showinfo("Staff deactivated", f"{employee['name']} has been deactivated.")
            self.show_shell("Employees")

        action_row = tk.Frame(wrap, bg=PANEL); action_row.pack(fill="x", pady=(22, 0))
        tk.Button(action_row, text="REGISTER FACE", command=lambda: self.register_face_dialog(employee["id"], employee["name"]), bg="#E2E8E7", fg=TEXT, relief="flat", cursor="hand2", font=("Segoe UI", 9, "bold"), padx=14, pady=9).pack(side="left")
        tk.Button(action_row, text="RESET PASSWORD", command=reset_password, bg="#E2E8E7", fg=TEXT, relief="flat", cursor="hand2", font=("Segoe UI", 9, "bold"), padx=14, pady=9).pack(side="left", padx=(10, 0))
        tk.Button(action_row, text="DEACTIVATE", command=deactivate, bg="#F6D9D6", fg="#B03A2E", relief="flat", cursor="hand2", font=("Segoe UI", 9, "bold"), padx=14, pady=9).pack(side="left", padx=(10, 0))
        tk.Button(action_row, text="CANCEL", command=dialog.destroy, bg="#E2E8E7", fg=TEXT, relief="flat", cursor="hand2", font=("Segoe UI", 10, "bold"), padx=18, pady=10).pack(side="right")
        tk.Button(action_row, text="SAVE CHANGES", command=save, bg=PRIMARY_DARK, fg="white", relief="flat", cursor="hand2", font=("Segoe UI", 10, "bold"), padx=18, pady=10).pack(side="right", padx=(0, 10))

    def placeholder(self, parent, page):
        tk.Label(parent, text=f"{page} is reserved for the next phase.", bg=BACKGROUND, fg=TEXT, font=("Segoe UI", 15, "bold")).pack(anchor="w", pady=(35, 5))
        tk.Label(parent, text="The database tables are already in place; this screen is intentionally not presented as a finished feature.", bg=BACKGROUND, fg=MUTED, font=("Segoe UI", 10)).pack(anchor="w")
    

if __name__ == "__main__":
    ClinicApp().mainloop()