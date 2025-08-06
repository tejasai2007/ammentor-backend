from typing import Optional, List
from datetime import date, timedelta
import os, json

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.schemas.submission import SubmissionOut
from app.db import models
from app.db.db import SessionLocal

SCOPE = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def _gspread_client():
    raw = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not raw:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS_JSON in environment")
    raw = raw.strip().strip("'").strip('"')
    creds_dict = json.loads(raw)
    credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scopes=SCOPE)
    return gspread.authorize(credentials)

def _to_date(d):
    if d is None:
        return None
    if isinstance(d, date):
        return d
    return getattr(d, "date", lambda: None)()

def get_user_by_email(db: Session, email: str):
    return db.query(models.User).filter(models.User.email == email).first()

def get_task(db: Session, track_id: int, task_no: int):
    return db.query(models.Task).filter_by(track_id=track_id, task_no=task_no).first()

def submit_task(db: Session, mentee_id: int, task_id: int, start_date: date, commit_hash: str):
    existing = db.query(models.Submission).filter_by(mentee_id=mentee_id, task_id=task_id).first()
    if existing:
        return None
    mentee = db.query(models.User).filter(models.User.id == mentee_id).first()
    task = db.query(models.Task).filter(models.Task.id == task_id).first()
    if not task:
        raise Exception("Task not found")

    submitted_at = date.today()
    deadline = start_date + timedelta(days=task.deadline_days or 0) if task.deadline_days else None
    if deadline and submitted_at > deadline:
        return "late submission not allowed"

    submission = models.Submission(
        mentee_id=mentee_id,
        task_id=task.id,
        task_name=task.title,
        task_no=task.task_no,
        submitted_at=submitted_at,
        status="submitted",
        start_date=start_date,
        commit_hash=commit_hash,
    )

    client = _gspread_client()
    if task.track_id in (1, 2):
        sheet = client.open("Copy of Praveshan 2025 Master DB").worksheet(
            "S1 Submissions" if task.track_id == 1 else "S2 Submissions"
        )
        name_column = sheet.col_values(1)
        try:
            row = name_column.index(mentee.name) + 1  # 1-based row
        except ValueError:
            row = len(name_column) + 1 if name_column else 1
            sheet.update_cell(row, 1, mentee.name)

        sheet.update_cell(row, task.task_no + 2, commit_hash)

    db.add(submission)
    db.commit()
    db.refresh(submission)
    return submission

def approve_submission(db: Session, submission_id: int, mentor_feedback: str, status: str):
    sub = db.query(models.Submission).filter_by(id=submission_id).first()
    if not sub:
        return None
    sub.status = status
    sub.mentor_feedback = mentor_feedback
    if status == "approved":
        sub.approved_at = date.today()
    db.commit()
    db.refresh(sub)
    return sub

def is_mentor_of(db: Session, mentor_id: int, mentee_id: int):
    return db.query(models.MentorMenteeMap).filter_by(
        mentor_id=mentor_id, mentee_id=mentee_id
    ).first() is not None

def get_leaderboard_data(db: Session, track_id: int):
    return (
        db.query(
            models.User.name,
            func.sum(models.Task.points).label("total_points"),
            func.count(models.Submission.id).label("tasks_completed"),
        )
        .join(models.Submission, models.Submission.mentee_id == models.User.id)
        .join(models.Task, models.Submission.task_id == models.Task.id)
        .filter(models.Submission.status == "submitted")
        .filter(models.Task.track_id == track_id)
        .group_by(models.User.id)
        .order_by(func.sum(models.Task.points).desc())
        .all()
    )

def get_otp_by_email(db, email):
    return db.query(models.OTP).filter(models.OTP.email == email).first()

def create_or_update_otp(db, email, otp, expires_at):
    entry = get_otp_by_email(db, email)
    if entry:
        entry.otp = otp
        entry.expires_at = expires_at
    else:
        entry = models.OTP(email=email, otp=otp, expires_at=expires_at)
        db.add(entry)
    db.commit()

def get_submissions_for_user(db: Session, email: str, track_id: Optional[int] = None) -> List[SubmissionOut]:
    user = db.query(models.User).filter(models.User.email == email).first()
    if not user:
        return []
    query = db.query(models.Submission).filter(models.Submission.mentee_id == user.id)
    if track_id is not None:
        query = query.join(models.Task).filter(models.Task.track_id == track_id)
    submissions = query.all()
    return [
        SubmissionOut(
            id=sub.id,
            mentee_id=sub.mentee_id,
            task_id=sub.task_id,
            task_name=sub.task_name,
            task_no=sub.task_no,
            status=sub.status,
            submitted_at=_to_date(sub.submitted_at),
            approved_at=_to_date(sub.approved_at),
            mentor_feedback=sub.mentor_feedback,
            start_date=_to_date(sub.start_date),
            commit_hash=sub.commit_hash, 
        )
        for sub in submissions
    ]
def get_sheet_data():
# Change sheet name
    client = _gspread_client()
    worksheet = client.open_by_key(os.getenv("GOOGLE_SHEET_ID")).worksheet("Praveshan Phase 3")
    expected_headers = ["Name", "Email Address","Faction name"]
    return worksheet.get_all_records(expected_headers=expected_headers)

def sync_users_from_sheet():
    db: Session = SessionLocal()
    try:
        rows = get_sheet_data()
        inserted_count = 0
        for row in rows:
            Faction_name = row.get("Faction name", "")
            email = row.get("Email Address", "").strip()
            name = row.get("Name", "").strip()
            if not email or not name:
                continue
            if get_user_by_email(db, email):

                continue 
            if Faction_name=="S2+":
                track = 2
            else:
                track=1
            user = models.User(name=name, email=email, role="mentee",group_name=Faction_name ,track=track)
            db.add(user)
            inserted_count += 1
        db.commit()
    except Exception as e:
        print(f"Error syncing users: {e}")
    finally:
        db.close()