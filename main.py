"""Enjaz: a server-authorized multi-user pilot. No third-party frontend services."""
import csv
import hashlib
import hmac
import io
import mimetypes
import os
import re
import secrets
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
from fastapi import FastAPI, Depends, HTTPException, Request, Response, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import (Boolean, Date, DateTime, ForeignKey, Integer, String, Text, LargeBinary,
                        create_engine, func, select, and_, or_)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column, relationship,
                            sessionmaker, Session)
from sqlalchemy.exc import IntegrityError
from openpyxl import Workbook, load_workbook
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.worksheet.datavalidation import DataValidation

UTC = timezone.utc
NOW = lambda: datetime.now(UTC)
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///./enjaz_local.db')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = 'postgresql+psycopg://' + DATABASE_URL[len('postgres://'):]
elif DATABASE_URL.startswith('postgresql://'):
    DATABASE_URL = 'postgresql+psycopg://' + DATABASE_URL[len('postgresql://'):]
IS_PROD = os.getenv('APP_ENV', 'development').lower() == 'production'
if IS_PROD and not DATABASE_URL.startswith('postgresql+psycopg://'):
    raise RuntimeError('Production requires PostgreSQL DATABASE_URL')
if IS_PROD and os.getenv('APP_BASE_URL') and not os.getenv('APP_BASE_URL').startswith('https://'):
    raise RuntimeError('APP_BASE_URL must use https:// in production')

engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args={'check_same_thread': False} if DATABASE_URL.startswith('sqlite') else {}, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
COOKIE_NAME = '__Host-enjaz_session' if IS_PROD else 'enjaz_session'
SESSION_SECONDS = 12 * 60 * 60
ONLINE_WINDOW_SECONDS = 75

class Base(DeclarativeBase):
    pass

class Department(Base):
    __tablename__ = 'departments'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)

class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(25), default='employee')
    department_id: Mapped[Optional[int]] = mapped_column(ForeignKey('departments.id'), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=NOW)
    department: Mapped[Optional[Department]] = relationship()

class AuthSession(Base):
    __tablename__ = 'auth_sessions'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    secret_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf: Mapped[str] = mapped_column(String(80))
    login_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=NOW)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=NOW)
    last_activity: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=NOW)
    logout_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    active_seconds: Mapped[int] = mapped_column(Integer, default=0)
    ended_reason: Mapped[Optional[str]] = mapped_column(String(30))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    user: Mapped[User] = relationship()

class Task(Base):
    __tablename__ = 'tasks'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(250))
    description: Mapped[str] = mapped_column(Text, default='')
    output: Mapped[str] = mapped_column(String(300), default='')
    result_text: Mapped[str] = mapped_column(Text, default='')
    department_id: Mapped[int] = mapped_column(ForeignKey('departments.id'))
    assigned_to: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    priority: Mapped[str] = mapped_column(String(15), default='medium')
    status: Mapped[str] = mapped_column(String(20), default='new')
    due_date: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=NOW)
    steps: Mapped[list['TaskStep']] = relationship(back_populates='task', cascade='all, delete-orphan', order_by='TaskStep.id')
    attachments: Mapped[list['TaskAttachment']] = relationship(back_populates='task', cascade='all, delete-orphan')

class TaskStep(Base):
    __tablename__ = 'task_steps'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey('tasks.id', ondelete='CASCADE'), index=True)
    title: Mapped[str] = mapped_column(String(220))
    weight: Mapped[int] = mapped_column(Integer)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    task: Mapped[Task] = relationship(back_populates='steps')

class TaskAttachment(Base):
    """Durable pilot storage in PostgreSQL, never Railway's ephemeral container filesystem."""
    __tablename__ = 'task_attachments'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey('tasks.id', ondelete='CASCADE'), index=True)
    uploaded_by: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    filename: Mapped[str] = mapped_column(String(240))
    media_type: Mapped[str] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(20))  # instruction or result
    content: Mapped[bytes] = mapped_column(LargeBinary)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=NOW)
    task: Mapped[Task] = relationship(back_populates='attachments')
    uploader: Mapped[User] = relationship()

class Message(Base):
    __tablename__ = 'messages'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(100), index=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    text: Mapped[str] = mapped_column(String(4000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=NOW)
    sender: Mapped[User] = relationship()

class InternalMailThread(Base):
    """Private two-person in-app mail thread. Director has no bypass to third-party mail."""
    __tablename__ = 'internal_mail_threads'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject: Mapped[str] = mapped_column(String(180))
    initiator_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    recipient_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=NOW)

class InternalMailEntry(Base):
    __tablename__ = 'internal_mail_entries'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey('internal_mail_threads.id', ondelete='CASCADE'), index=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    recipient_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=NOW)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Audit(Base):
    __tablename__ = 'audit_logs'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[Optional[int]] = mapped_column(ForeignKey('users.id'))
    action: Mapped[str] = mapped_column(String(80))
    details: Mapped[str] = mapped_column(String(1000), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=NOW)

class LoginAttempt(Base):
    __tablename__ = 'login_attempts'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    identity_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=NOW)

Base.metadata.create_all(engine)  # for pilot; production schema changes use versioned migrations

def bootstrap():
    with SessionLocal() as db:
        if not db.scalar(select(Department.id).limit(1)):
            db.add_all([Department(name='متابعة القروض المتعثرة'), Department(name='هيكلة القروض المتعثرة')]); db.commit()
        if not db.scalar(select(User.id).limit(1)):
            email = os.getenv('BOOTSTRAP_ADMIN_EMAIL', '').strip().lower()
            pw = os.getenv('BOOTSTRAP_ADMIN_PASSWORD', '')
            if email and len(pw) >= 14 and '@' in email:
                db.add(User(full_name='مدير عام الإدارة', email=email, password_hash=password_hasher.hash(pw), role='director', must_change_password=True))
                db.commit()
                print('ENJAZ: administrator account provisioned; remove bootstrap environment secrets', flush=True)
            else:
                print('ENJAZ: no users; configure BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_PASSWORD (14+ chars)', flush=True)
bootstrap()

app = FastAPI(title='Enjaz API', docs_url=None if IS_PROD else '/api-docs', redoc_url=None, openapi_url=None if IS_PROD else '/openapi.json')
STATIC = Path(__file__).parent / 'static'
app.mount('/assets', StaticFiles(directory=STATIC), name='assets')

@app.middleware('http')
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    if IS_PROD:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['Cache-Control'] = 'no-store' if request.url.path.startswith('/api/') else 'private, max-age=0'
    return response

def get_db():
    with SessionLocal() as db:
        yield db

def iso(dt):
    if not dt: return None
    return dt.replace(tzinfo=dt.tzinfo or UTC).astimezone(UTC).isoformat()

def require_login(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(COOKIE_NAME, '')
    if not token or len(token) != 64:
        raise HTTPException(401, 'يلزم تسجيل الدخول')
    ses = db.scalar(select(AuthSession).where(AuthSession.secret_hash == hashlib.sha256(token.encode()).hexdigest()))
    if not ses or ses.logout_at or iso(ses.expires_at) <= iso(NOW()) or not ses.user.active:
        raise HTTPException(401, 'انتهت جلسة الدخول')
    if request.method in ('POST', 'PATCH', 'PUT', 'DELETE') and not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), ses.csrf):
        raise HTTPException(403, 'فشل التحقق من الطلب')
    if ses.user.must_change_password and request.url.path not in ('/api/auth/change-password', '/api/auth/logout') and request.method != 'GET':
        raise HTTPException(403, 'يجب تغيير كلمة المرور المؤقتة')
    return ses

def actor(db, ses):
    return ses.user

def audit(db, user_id, action, details=''):
    db.add(Audit(actor_id=user_id, action=action, details=str(details)[:1000]))

def user_data(u):
    return {'id':u.id,'name':u.full_name,'email':u.email,'role':u.role,'department_id':u.department_id,'department':u.department.name if u.department else None,'active':u.active,'must_change_password':u.must_change_password}

def can_view_user(me, other):
    return me.role == 'director' or (me.role == 'head' and me.department_id == other.department_id) or me.id == other.id

def can_view_task(me, task):
    return me.role == 'director' or (me.role == 'head' and me.department_id == task.department_id) or task.assigned_to == me.id

def can_approve(me, task):
    return me.role == 'director' or (me.role == 'head' and me.department_id == task.department_id)

def get_task(db, task_id, me):
    task = db.get(Task, task_id)
    if not task: raise HTTPException(404, 'المهمة غير موجودة')
    if not can_view_task(me, task): raise HTTPException(403, 'لا تملك صلاحية هذه المهمة')
    return task

def progress(task):
    return 100 if task.status == 'done' else min(95, sum(s.weight for s in task.steps if s.done))

def task_data(task, db):
    au = db.get(User, task.assigned_to)
    dept = db.get(Department, task.department_id)
    return {'id':task.id,'title':task.title,'description':task.description,'output':task.output,'result_text':task.result_text,'department_id':task.department_id,'department':dept.name if dept else '', 'assigned_to':task.assigned_to,'assignee':au.full_name if au else '', 'created_by':task.created_by,'priority':task.priority,'status':task.status,'due_date':task.due_date.isoformat(),'created_at':iso(task.created_at),'updated_at':iso(task.updated_at),'progress':progress(task),'steps':[{'id':s.id,'title':s.title,'weight':s.weight,'done':s.done} for s in task.steps]}

class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=250)
class ChangePasswordBody(BaseModel):
    old_password: str
    new_password: str = Field(min_length=12, max_length=128)
class ChangeEmailBody(BaseModel):
    new_email: str = Field(min_length=5, max_length=200)
    current_password: str = Field(min_length=1, max_length=250)

class InternalMailCompose(BaseModel):
    recipient_id: int = Field(ge=1)
    subject: str = Field(min_length=2, max_length=180)
    body: str = Field(min_length=1, max_length=6000)

class InternalMailReply(BaseModel):
    body: str = Field(min_length=1, max_length=6000)


class UserCreate(BaseModel):
    full_name: str = Field(min_length=2,max_length=160)
    email: str = Field(min_length=5,max_length=200)
    role: str = 'employee'
    department_id: Optional[int] = None
    temporary_password: str = Field(min_length=12,max_length=128)
class UserRoleChange(BaseModel):
    role: str = Field(min_length=4, max_length=12)

class UserEdit(BaseModel):
    full_name: Optional[str] = Field(default=None,min_length=2,max_length=160)
    department_id: Optional[int] = None
    active: Optional[bool] = None
    temporary_password: Optional[str] = Field(default=None,min_length=12,max_length=128)
class StepIn(BaseModel):
    title: str = Field(min_length=2,max_length=220)
    weight: int = Field(ge=1,le=100)
class TaskCreate(BaseModel):
    title: str = Field(min_length=3,max_length=250)
    description: str = Field(default='',max_length=4000)
    output: str = Field(default='',max_length=300)
    department_id: int
    assigned_to: int
    due_date: date
    priority: str = 'medium'
    steps: list[StepIn] = Field(min_length=1,max_length=12)
class StepToggle(BaseModel):
    done: bool
class SubmitBody(BaseModel):
    result_text: str = Field(min_length=3,max_length=8000)
class AssignBody(BaseModel):
    assigned_to: int
class Decision(BaseModel):
    decision: str
    reason: str = Field(default='',max_length=1000)
class MessageCreate(BaseModel):
    channel: str
    text: str = Field(min_length=1,max_length=4000)
class Pulse(BaseModel):
    active: bool = False

@app.get('/')
def index():
    return FileResponse(STATIC/'index.html')

@app.get('/healthz')
def health(db: Session=Depends(get_db)):
    db.execute(select(1)); return {'status':'ok'}

@app.post('/api/auth/login')
def login(payload: LoginBody, response: Response, db: Session=Depends(get_db)):
    email = payload.email.lower().strip()
    key = hashlib.sha256(email.encode()).hexdigest()
    recent = db.scalar(select(func.count()).select_from(LoginAttempt).where(LoginAttempt.identity_hash == key, LoginAttempt.created_at >= NOW()-timedelta(minutes=15)))
    if recent >= 8: raise HTTPException(429, 'محاولات كثيرة، حاول بعد 15 دقيقة')
    user = db.scalar(select(User).where(User.email==email, User.active.is_(True)))
    try:
        if not user or not password_hasher.verify(user.password_hash, payload.password): raise VerifyMismatchError('invalid')
    except (VerifyMismatchError, VerificationError):
        db.add(LoginAttempt(identity_hash=key)); db.commit()
        raise HTTPException(401, 'بيانات الدخول غير صحيحة')
    # Prevent unbounded growth of expired sessions / attempts on successful login
    db.query(LoginAttempt).filter(LoginAttempt.identity_hash==key).delete()
    token = secrets.token_hex(32)
    s = AuthSession(user_id=user.id,secret_hash=hashlib.sha256(token.encode()).hexdigest(), csrf=secrets.token_urlsafe(32),expires_at=NOW()+timedelta(seconds=SESSION_SECONDS))
    db.add(s); audit(db,user.id,'login','دخول ناجح'); db.commit()
    response.set_cookie(COOKIE_NAME,token,max_age=SESSION_SECONDS,httponly=True,secure=IS_PROD,samesite='lax',path='/')
    return {'user':user_data(user),'csrf':s.csrf}

@app.get('/api/me')
def me_endpoint(s=Depends(require_login)):
    return {'user':user_data(s.user),'csrf':s.csrf}

@app.post('/api/auth/logout')
def logout(response: Response, db: Session=Depends(get_db),s=Depends(require_login)):
    s.logout_at=NOW();s.ended_reason='logout';audit(db,s.user_id,'logout');db.commit();response.delete_cookie(COOKIE_NAME,path='/')
    return {'ok':True}

@app.post('/api/auth/change-password')
def change_password(data: ChangePasswordBody, db: Session=Depends(get_db),s=Depends(require_login)):
    try:
        if not password_hasher.verify(s.user.password_hash, data.old_password): raise VerifyMismatchError('invalid')
    except (VerifyMismatchError, VerificationError):
        raise HTTPException(400,'كلمة المرور الحالية غير صحيحة')
    if data.new_password == data.old_password: raise HTTPException(400,'اختر كلمة مرور مختلفة')
    s.user.password_hash=password_hasher.hash(data.new_password);s.user.must_change_password=False
    for other in db.scalars(select(AuthSession).where(AuthSession.user_id == s.user_id,AuthSession.id != s.id,AuthSession.logout_at.is_(None))):
        other.logout_at = NOW();other.ended_reason = 'password_changed'
    audit(db,s.user_id,'change_password');db.commit()
    return {'ok':True}

@app.patch('/api/me/email')
def update_own_email(data:ChangeEmailBody,db:Session=Depends(get_db),s=Depends(require_login)):
    """A user may update only their login email, after re-authentication."""
    new_email=data.new_email.strip().lower()
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', new_email):
        raise HTTPException(400,'صيغة البريد الإلكتروني غير صحيحة')
    if new_email == s.user.email:
        raise HTTPException(400,'البريد الجديد مطابق للبريد الحالي')
    throttle_key=hashlib.sha256(f'change-email:{s.user_id}'.encode()).hexdigest()
    attempts=db.scalar(select(func.count()).select_from(LoginAttempt).where(
        LoginAttempt.identity_hash==throttle_key,LoginAttempt.created_at>=NOW()-timedelta(minutes=15)))
    if attempts>=8: raise HTTPException(429,'محاولات كثيرة، حاول بعد 15 دقيقة')
    try:
        if not password_hasher.verify(s.user.password_hash,data.current_password):
            raise VerifyMismatchError('invalid')
    except (VerifyMismatchError,VerificationError):
        db.add(LoginAttempt(identity_hash=throttle_key));db.commit()
        raise HTTPException(400,'كلمة المرور الحالية غير صحيحة')
    if db.scalar(select(User.id).where(func.lower(User.email)==new_email,User.id!=s.user_id)):
        raise HTTPException(409,'البريد الإلكتروني مستخدم في حساب آخر')
    old_email=s.user.email
    s.user.email=new_email
    for other in db.scalars(select(AuthSession).where(AuthSession.user_id==s.user_id,
                              AuthSession.id!=s.id,AuthSession.logout_at.is_(None))):
        other.logout_at=NOW();other.ended_reason='email_changed'
    db.query(LoginAttempt).filter(LoginAttempt.identity_hash==throttle_key).delete()
    audit(db,s.user_id,'change_own_email',f'old={old_email},new={new_email}')
    try:db.commit()
    except IntegrityError:
        db.rollback();raise HTTPException(409,'البريد الإلكتروني مستخدم في حساب آخر')
    return {'ok':True,'user':user_data(s.user)}


@app.get('/api/departments')
def departments(db: Session=Depends(get_db),s=Depends(require_login)):
    return [{'id':d.id,'name':d.name} for d in db.scalars(select(Department).order_by(Department.id))]

@app.get('/api/users')
def users(db: Session=Depends(get_db),s=Depends(require_login)):
    me=s.user
    all_users=db.scalars(select(User).order_by(User.full_name)).all()
    return [user_data(u) for u in all_users if can_view_user(me,u)]

def require_free_department_head(db: Session, department_id: int):
    """Lock department for the entire transaction before checking assigned head.

    All API head assignments use this lock to serialize concurrent requests on PostgreSQL.
    The head is considered assigned even if the account is currently disabled.
    """
    dept = db.scalar(select(Department).where(Department.id == department_id).with_for_update())
    if not dept:
        raise HTTPException(400, 'القسم المحدد غير موجود')
    current = db.scalar(select(User).where(User.department_id == department_id,
                                           User.role == 'head').order_by(User.id).limit(1))
    if current:
        raise HTTPException(409, f'القسم «{dept.name}» له رئيس حالي: {current.full_name}. يجب تحويل رئيس القسم الحالي إلى موظف أولًا، ثم تعيين الرئيس الجديد.')
    return dept


@app.patch('/api/users/{user_id}/role')
def change_user_role(user_id: int, data: UserRoleChange, db: Session=Depends(get_db), s=Depends(require_login)):
    """Director-only, explicit one-at-a-time promotion/demotion; never swaps heads automatically."""
    if s.user.role != 'director':
        raise HTTPException(403, 'تغيير الأدوار متاح لمدير عام الإدارة فقط')
    if data.role not in ('employee', 'head'):
        raise HTTPException(400, 'الدور المستهدف يجب أن يكون موظفًا أو رئيس قسم')
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, 'الموظف غير موجود')
    if target.role == 'director':
        raise HTTPException(403, 'لا يمكن تغيير صلاحية المدير العام من هذه الشاشة')
    if target.role == data.role:
        raise HTTPException(400, 'الموظف لديه هذه الصلاحية بالفعل')
    if not target.department_id:
        raise HTTPException(400, 'يجب أن يكون الموظف مرتبطًا بقسم قبل تعيينه رئيسًا')
    # Serialize promotions and demotions in the same department.
    dept = db.scalar(select(Department).where(Department.id == target.department_id).with_for_update())
    if not dept:
        raise HTTPException(400, 'القسم غير موجود')
    if data.role == 'head':
        if not target.active:
            raise HTTPException(400, 'لا يمكن تعيين حساب موقوف رئيسًا للقسم؛ فعّل الحساب أولًا')
        require_free_department_head(db, target.department_id)
    previous = target.role
    target.role = data.role
    # Immediately invalidate all old sessions, so demoted heads lose prior permissions.
    for user_session in db.scalars(select(AuthSession).where(AuthSession.user_id == target.id,
                                   AuthSession.logout_at.is_(None))):
        user_session.logout_at = NOW()
        user_session.ended_reason = 'role_changed'
    audit(db, s.user_id, 'change_user_role',
          f'employee_id={target.id},department_id={target.department_id},from={previous},to={target.role}')
    db.commit()
    return {'ok': True, 'user': user_data(target), 'previous_role': previous,
            'sessions_revoked': True}


@app.post('/api/users',status_code=201)
def create_user(data: UserCreate, db: Session=Depends(get_db),s=Depends(require_login)):
    me=s.user
    if me.role not in ('head','director'):raise HTTPException(403,'الصلاحية غير متاحة')
    if data.role not in ('head','employee') or (me.role=='head' and data.role!='employee'):
        raise HTTPException(400,'الدور غير مسموح')
    if not data.department_id or not db.get(Department,data.department_id):raise HTTPException(400,'القسم غير صحيح')
    if me.role=='head' and me.department_id!=data.department_id:raise HTTPException(403,'القسم غير مسموح')
    email=data.email.strip().lower()
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email):raise HTTPException(400,'البريد الإلكتروني غير صحيح')
    if db.scalar(select(User.id).where(User.email==email)):raise HTTPException(409,'البريد الإلكتروني مسجل مسبقاً')
    if data.role == 'head':
        require_free_department_head(db, data.department_id)
    u=User(full_name=data.full_name.strip(),email=email,role=data.role,department_id=data.department_id,password_hash=password_hasher.hash(data.temporary_password),must_change_password=True)
    db.add(u); db.flush();audit(db,me.id,'create_user',str(u.id));db.commit()
    return user_data(u)

@app.patch('/api/users/{user_id}')
def edit_user(user_id:int,data:UserEdit,db:Session=Depends(get_db),s=Depends(require_login)):
    me=s.user;u=db.get(User,user_id)
    if not u:raise HTTPException(404,'الموظف غير موجود')
    if me.role not in ('head','director') or u.role=='director' or (me.role=='head' and (u.role!='employee' or u.department_id!=me.department_id)):
        raise HTTPException(403,'الصلاحية غير متاحة')
    if data.full_name is not None:u.full_name=data.full_name.strip()
    if data.department_id is not None:
        if me.role!='director' or not db.get(Department,data.department_id):raise HTTPException(403,'لا يمكن تغيير القسم')
        if u.role == 'head' and data.department_id != u.department_id:
            raise HTTPException(409,'يجب تحويل رئيس القسم إلى موظف أولًا قبل نقله إلى قسم آخر')
        if db.scalar(select(Task.id).where(Task.assigned_to==u.id,Task.status!='done').limit(1)):
            raise HTTPException(400,'يجب إعادة إسناد المهام المفتوحة قبل نقل الموظف')
        u.department_id=data.department_id
    if data.active is not None:
        if not data.active and db.scalar(select(Task.id).where(Task.assigned_to==u.id,Task.status!='done').limit(1)):
            raise HTTPException(400,'يجب إعادة إسناد المهام المفتوحة قبل الإيقاف')
        u.active=data.active
        if not u.active:
            for active_s in db.scalars(select(AuthSession).where(AuthSession.user_id==u.id,AuthSession.logout_at.is_(None))):
                active_s.logout_at=NOW();active_s.ended_reason='disabled'
    if data.temporary_password is not None:
        u.password_hash=password_hasher.hash(data.temporary_password);u.must_change_password=True
        for active_s in db.scalars(select(AuthSession).where(AuthSession.user_id==u.id,AuthSession.logout_at.is_(None))):
            active_s.logout_at=NOW();active_s.ended_reason='reset'
    audit(db,me.id,'edit_user',str(u.id));db.commit()
    return user_data(u)

# Excel employee import: the file is processed in memory, never written to disk.
# Passwords are neither returned in API results nor written to application logs.
MAX_BULK_USERS = 100
MAX_BULK_BYTES = 2 * 1024 * 1024
IMPORT_HEADERS = ('اسم الموظف', 'البريد الإلكتروني', 'كلمة المرور', 'الدور', 'القسم')
HEADER_ALIASES = {
    'اسم الموظف':'name', 'اسم':'name', 'الاسم':'name', 'الاسم الكامل':'name', 'full_name':'name', 'name':'name',
    'البريد الإلكتروني':'email', 'البريد الالكتروني':'email', 'البريد':'email', 'email':'email',
    'كلمة المرور':'password', 'كلمة مرور':'password', 'كلمة المرور المؤقتة':'password', 'password':'password', 'temporary_password':'password',
    'الدور':'role', 'role':'role', 'الصفة':'role', 'الصلاحية':'role',
    'القسم':'department', 'اسم القسم':'department', 'department':'department',
}
IMPORT_ROLE_ALIASES = {'موظف':'employee', 'employee':'employee', 'رئيس قسم':'head', 'رئيس القسم':'head', 'head':'head'}


def require_bulk_director(s):
    if s.user.role != 'director':
        raise HTTPException(403, 'الاستيراد الجماعي متاح لمدير عام الإدارة فقط')


def normal_cell(value):
    if value is None:
        return ''
    return str(value).strip()


def parse_bulk_sheet(content: bytes, db: Session, default_department_id: Optional[int]):
    from zipfile import BadZipFile, ZipFile
    if not content or len(content) > MAX_BULK_BYTES:
        raise HTTPException(413, 'حجم الملف يجب ألا يتجاوز 2 ميجابايت')
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            if sum(x.file_size for x in archive.infolist()) > 12 * 1024 * 1024:
                raise HTTPException(413, 'حجم البيانات الداخلية للملف كبير للغاية')
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
        ws = wb.worksheets[0]
        if (ws.max_row or 0) > 1002 or (ws.max_column or 0) > 20:
            raise HTTPException(400, 'الملف يتضمن عددًا كبيرًا من الصفوف أو الأعمدة')
        iterator = ws.iter_rows(min_row=1, max_col=min(ws.max_column or 5, 20))
        first = next(iterator, None)
        if not first:
            raise HTTPException(400, 'ملف Excel فارغ')
        columns = {}
        for c, cell in enumerate(first):
            key = HEADER_ALIASES.get(normal_cell(cell.value).lower())
            if key:
                if key in columns:
                    raise HTTPException(400, f'العمود {normal_cell(cell.value)} مكرر')
                columns[key] = c
        missing = [h for h, k in zip(IMPORT_HEADERS[:4], ('name','email','password','role')) if k not in columns]
        if missing:
            raise HTTPException(400, 'الأعمدة الإلزامية غير موجودة: ' + '، '.join(missing))
        departments = db.scalars(select(Department)).all()
        departments_by_id = {d.id:d for d in departments}
        departments_by_name = {d.name.strip():d for d in departments}
        default_dept = departments_by_id.get(default_department_id) if default_department_id else None
        if default_department_id is not None and default_dept is None:
            raise HTTPException(400, 'القسم الافتراضي المحدد غير موجود')
        rows = []
        errors = []
        seen = set()
        for rownum, cells in enumerate(iterator, start=2):
            def raw(key):
                j = columns.get(key)
                return cells[j] if j is not None and j < len(cells) else None
            def value(key):
                cell = raw(key)
                return normal_cell(cell.value) if cell is not None else ''
            if not any(value(k) for k in ('name','email','password','role','department')):
                continue
            if len(rows) + sum(1 for err in errors if err.get('row',0)>0) >= MAX_BULK_USERS:
                raise HTTPException(400, f'الحد الأقصى هو {MAX_BULK_USERS} موظفًا في الملف الواحد؛ قسّم الملف إلى دفعات')
            name, email, password, role_text, department_text = (value(k) for k in ('name','email','password','role','department'))
            email = email.lower()
            row_errors = []
            for key in ('name', 'email', 'password', 'role', 'department'):
                cell = raw(key)
                if cell is not None and cell.data_type == 'f':
                    row_errors.append('لا يُسمح باستخدام صيغ Excel في بيانات الموظفين')
                    break
            if not 2 <= len(name) <= 160:
                row_errors.append('اسم الموظف مطلوب ويجب ألا يتجاوز 160 حرفًا')
            if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email) or len(email)>200:
                row_errors.append('البريد الإلكتروني غير صحيح')
            elif email in seen:
                row_errors.append('البريد الإلكتروني مكرر داخل الملف')
            else:
                seen.add(email)
            if not isinstance(raw('password').value if raw('password') else None, str) or not 12 <= len(password) <= 128:
                row_errors.append('كلمة المرور يجب أن تكون نصًا من 12 إلى 128 حرفًا')
            role = IMPORT_ROLE_ALIASES.get(role_text.lower())
            if not role:
                row_errors.append('الدور يجب أن يكون موظفًا أو رئيس قسم فقط')
            dept = departments_by_name.get(department_text) if department_text else default_dept
            if not dept:
                row_errors.append('القسم غير موجود أو لم يتم اختيار قسم افتراضي')
            if row_errors:
                errors.append({'row':rownum, 'message':'؛ '.join(row_errors)})
            else:
                rows.append({'row':rownum,'full_name':name,'email':email,'password':password,
                             'role':role,'department_id':dept.id,'department':dept.name})
        wb.close()
    except HTTPException:
        raise
    except (BadZipFile, ValueError, OSError, KeyError, TypeError, EOFError) as err:
        raise HTTPException(400,'تعذر قراءة ملف Excel. استخدم نموذج XLSX غير محمي بكلمة مرور') from err
    if not rows and not errors:
        raise HTTPException(400,'لا يحتوي الملف على موظفين')
    if rows:
        existing = set(db.scalars(select(User.email).where(User.email.in_([r['email'] for r in rows]))).all())
        kept = []
        for r in rows:
            if r['email'] in existing:
                errors.append({'row':r['row'], 'message':'البريد الإلكتروني مسجل مسبقًا في النظام'})
            else:
                kept.append(r)
        rows = kept
    # The same single-head rule also applies to Excel preview and import.
    # Existing head accounts, even disabled ones, must be demoted first.
    head_by_department = {
        u.department_id: u.full_name for u in db.scalars(select(User).where(User.role == 'head'))
    }
    accepted, incoming_head_by_dept = [], {}
    for r in rows:
        if r['role'] == 'head':
            dept_id = r['department_id']
            if dept_id in head_by_department:
                errors.append({'row':r['row'],
                               'message':f'القسم لديه رئيس حالي ({head_by_department[dept_id]})؛ يجب تحويله إلى موظف أولًا'})
                continue
            if dept_id in incoming_head_by_dept:
                errors.append({'row':r['row'],
                               'message':f'تم إدراج رئيس آخر لنفس القسم في الصف {incoming_head_by_dept[dept_id]}؛ لا يمكن تعيين رئيسين'})
                continue
            incoming_head_by_dept[dept_id] = r['row']
        accepted.append(r)
    rows = accepted
    errors.sort(key=lambda r:r['row'])
    return rows, errors


async def get_bulk_file(file: UploadFile):
    if not file.filename or not file.filename.lower().endswith('.xlsx'):
        raise HTTPException(400, 'اختر ملف Excel بصيغة .xlsx فقط')
    content = await file.read(MAX_BULK_BYTES + 1)
    await file.close()
    if len(content)>MAX_BULK_BYTES:
        raise HTTPException(413,'حجم الملف يجب ألا يتجاوز 2 ميجابايت')
    return content


@app.get('/api/users/import/template')
def download_bulk_template(db: Session=Depends(get_db), s=Depends(require_login)):
    require_bulk_director(s)
    wb = Workbook()
    ws = wb.active
    ws.title = 'الموظفون'
    ws.sheet_view.rightToLeft = True
    ws.append(list(IMPORT_HEADERS))
    widths = [31, 39, 31, 20, 38]
    for idx, (title, width) in enumerate(zip(IMPORT_HEADERS,widths),start=1):
        col=ws.cell(1,idx)
        col.fill=PatternFill('solid',fgColor='123E34')
        col.font=Font(name='Arial',size=12,bold=True,color='FFFFFF')
        col.alignment=Alignment(horizontal='center',vertical='center')
        ws.column_dimensions[col.column_letter].width=width
    ws.row_dimensions[1].height=32
    for r in range(2,102):
        for c in range(1,6):
            cell=ws.cell(r,c)
            cell.fill=PatternFill('solid',fgColor='F5F8F6' if r%2==0 else 'FFFFFF')
            cell.alignment=Alignment(horizontal='right',vertical='center')
            cell.number_format='@'  # preserve leading zeros and prevent date conversion of passwords
    ws.freeze_panes='A2'
    ws.auto_filter.ref='A1:E101'
    dv=DataValidation(type='list',formula1='"موظف,رئيس قسم"',allow_blank=False)
    dv.error='اختر موظف أو رئيس قسم';dv.errorTitle='دور غير صحيح';dv.showErrorMessage=True
    ws.add_data_validation(dv);dv.add('D2:D101')
    instructions=wb.create_sheet('تعليمات')
    instructions.sheet_view.rightToLeft=True
    notes=[
        ['إرشادات استيراد الموظفين إلى إنجاز'],
        ['املأ صفًا لكل موظف في ورقة «الموظفون». لا تغيّر أسماء الأعمدة ولا تدرج صيغ Excel.'],
        ['الحقول الأربعة الأولى إلزامية: اسم الموظف، البريد الإلكتروني، كلمة المرور، الدور.'],
        ['الدور المسموح: «موظف» أو «رئيس قسم». لا يمكن إضافة رئيس قسم إذا كان للقسم رئيس حالي؛ يجب تخفيض دور الرئيس الحالي أولًا.'],
        ['القسم اختياري داخل الجدول إذا اخترت قسمًا افتراضيًا في التطبيق؛ وإلا فيجب كتابته كما يظهر في التطبيق.'],
        ['كلمات المرور مؤقتة (12 حرفًا على الأقل) ويُلزم الموظف بتغييرها عند أول دخول.'],
        ['الاستيراد شامل: إذا وُجد خطأ في صف واحد فلن يُضاف أي موظف حتى تصحح الملف.'],
        ['الحد الأقصى 100 موظف لكل ملف، وحجم الملف 2 ميجابايت.'],
        ['مهم: الملف يتضمن كلمات مرور نصية. خزّنه في مكان آمن، ولا ترسله بالبريد أو المراسلات، واحذفه آمنًا بعد إنشاء الحسابات.'],
        ['الأقسام المتاحة حاليًا:']
    ]
    notes.extend([[d.name] for d in db.scalars(select(Department).order_by(Department.id))])
    for note in notes:instructions.append(note)
    instructions.column_dimensions['A'].width=110
    for i in range(1,len(notes)+1):
        instructions.cell(i,1).alignment=Alignment(wrap_text=True,vertical='center',horizontal='right')
        instructions.row_dimensions[i].height=28 if i==1 else 30
    instructions['A1'].font=Font(size=16,bold=True,color='17664E')
    output=io.BytesIO();wb.save(output);wb.close();output.seek(0)
    return StreamingResponse(output,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename="enjaz_employees_template.xlsx"','Cache-Control':'no-store'})


@app.post('/api/users/import/preview')
async def preview_bulk_users(file: UploadFile=File(...), default_department_id: Optional[int]=Form(None),
                             db: Session=Depends(get_db), s=Depends(require_login)):
    require_bulk_director(s)
    rows, errors = parse_bulk_sheet(await get_bulk_file(file),db,default_department_id)
    return {'total':len(rows)+len(errors),'valid_count':len(rows),'error_count':len(errors),
            'errors':errors,'preview':[{'row':r['row'],'name':r['full_name'],'email':r['email'],
                                       'role':r['role'],'department':r['department']} for r in rows[:25]]}


@app.post('/api/users/import')
async def import_bulk_users(file: UploadFile=File(...), default_department_id: Optional[int]=Form(None),
                            db: Session=Depends(get_db), s=Depends(require_login)):
    require_bulk_director(s)
    rows, errors = parse_bulk_sheet(await get_bulk_file(file),db,default_department_id)
    if errors:
        return JSONResponse(status_code=422,content={'detail':'الملف يحتوي على أخطاء. لم تتم إضافة أي موظف','errors':errors})
    try:
        # Lock departments and recheck for heads to prevent conflicts between preview and save.
        for dept_id in sorted({r['department_id'] for r in rows if r['role'] == 'head'}):
            require_free_department_head(db, dept_id)
        for r in rows:
            db.add(User(full_name=r['full_name'],email=r['email'],role=r['role'],department_id=r['department_id'],
                        password_hash=password_hasher.hash(r['password']),must_change_password=True))
        db.flush()
        audit(db,s.user_id,'bulk_import_users',f'count={len(rows)}, heads={sum(r["role"] == "head" for r in rows)}')
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409,'حدث تعارض في البيانات أثناء الحفظ؛ راجع عناوين البريد ثم أعد المحاولة')
    except Exception:
        db.rollback()
        raise
    return {'created':len(rows),'message':f'تمت إضافة {len(rows)} حسابًا بنجاح','force_password_change':True}


@app.get('/api/tasks')
def tasks(db: Session=Depends(get_db),s=Depends(require_login)):
    me=s.user
    q=select(Task)
    if me.role=='head':q=q.where(Task.department_id==me.department_id)
    if me.role=='employee':q=q.where(Task.assigned_to==me.id)
    return [task_data(t,db) for t in db.scalars(q.order_by(Task.created_at.desc(),Task.id.desc()))]

@app.post('/api/tasks',status_code=201)
def create_task(data:TaskCreate,db:Session=Depends(get_db),s=Depends(require_login)):
    me=s.user
    if me.role=='employee':raise HTTPException(403,'لا يمكنك إنشاء التكليفات')
    if me.role=='head' and me.department_id!=data.department_id:raise HTTPException(403,'القسم غير مسموح')
    if not db.get(Department,data.department_id):raise HTTPException(400,'القسم غير موجود')
    owner=db.get(User,data.assigned_to)
    if not owner or not owner.active or owner.department_id!=data.department_id:raise HTTPException(400,'الموظف غير تابع للقسم أو غير نشط')
    if data.priority not in ('low','medium','high'):raise HTTPException(400,'الأولوية غير صحيحة')
    if sum(step.weight for step in data.steps)!=100:raise HTTPException(400,'يجب أن يكون مجموع أوزان المراحل 100%')
    task=Task(title=data.title.strip(),description=data.description,output=data.output,department_id=data.department_id,assigned_to=data.assigned_to,created_by=me.id,due_date=data.due_date,priority=data.priority)
    task.steps=[TaskStep(title=step.title.strip(),weight=step.weight) for step in data.steps]
    db.add(task);db.flush();audit(db,me.id,'create_task',str(task.id));db.commit()
    return task_data(task,db)

# Attachment policy: small documents only, stored in DB for durability across Railway redeploys.
# For large/long-term workloads use approved private object storage + malware scanning.
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_TASK_ATTACHMENTS = 20
MAX_TASK_ATTACHMENT_TOTAL = 40 * 1024 * 1024
MAX_ATTACHMENTS_GLOBAL = 250 * 1024 * 1024  # pilot safety cap for 500 MB Postgres volume
ATTACHMENT_MIME = {
    '.pdf': 'application/pdf',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.txt': 'text/plain', '.csv': 'text/csv',
}
OOXML_ENTRY = {'.docx': 'word/document.xml', '.xlsx': 'xl/workbook.xml', '.pptx': 'ppt/presentation.xml'}


def clean_filename(value: str) -> str:
    # Reject path-separators and controls, rather than trusting client-supplied names.
    name = (value or '').strip()
    if (not name or len(name)>240 or name in ('.', '..') or '/' in name or '\\' in name
            or any(ord(ch)<32 or ord(ch)==127 for ch in name)):
        raise HTTPException(400, 'اسم الملف غير صالح')
    return name


def validate_attachment(name: str, data: bytes):
    ext = Path(name).suffix.lower()
    if ext not in ATTACHMENT_MIME:
        raise HTTPException(400, 'نوع الملف غير مسموح. الصيغ المقبولة: PDF وWord وExcel وPowerPoint والصور وTXT وCSV')
    if not data or len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(413, 'يجب أن يكون الملف غير فارغ وألا يتجاوز 8 ميجابايت')
    if ext=='.pdf' and not data.startswith(b'%PDF-'):
        raise HTTPException(400, 'ملف PDF غير صالح')
    if ext=='.png' and not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise HTTPException(400, 'ملف PNG غير صالح')
    if ext in ('.jpg', '.jpeg') and not data.startswith(b'\xff\xd8\xff'):
        raise HTTPException(400, 'ملف JPEG غير صالح')
    if ext in OOXML_ENTRY:
        if not zipfile.is_zipfile(io.BytesIO(data)):
            raise HTTPException(400, 'ملف Office غير صالح')
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = set(z.namelist())
                # Inspection without decompressing document contents.
                if (len(names)>2500 or '[Content_Types].xml' not in names
                    or OOXML_ENTRY[ext] not in names or any('vbaproject.bin' in n.lower() for n in names)):
                    raise HTTPException(400, 'الملف ليس مستند Office صالحًا أو يحتوي على ماكرو')
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as err:
            raise HTTPException(400, 'ملف Office غير صالح') from err
    return ATTACHMENT_MIME[ext]


def attachment_meta(a):
    return {'id':a.id,'task_id':a.task_id,'filename':a.filename,'category':a.category,
            'size_bytes':a.size_bytes,'uploaded_by':a.uploaded_by,
            'uploader':a.uploader.full_name if a.uploader else '',
            'uploaded_at':iso(a.uploaded_at)}


@app.get('/api/tasks/{task_id}/attachments')
def list_attachments(task_id:int, db:Session=Depends(get_db), s=Depends(require_login)):
    get_task(db,task_id,s.user)
    rows=db.scalars(select(TaskAttachment).where(TaskAttachment.task_id==task_id)
                    .order_by(TaskAttachment.uploaded_at.desc(),TaskAttachment.id.desc())).all()
    return [attachment_meta(a) for a in rows]


@app.post('/api/tasks/{task_id}/attachments', status_code=201)
async def upload_attachment(task_id:int, file: UploadFile=File(...), category:str=Form('result'),
                            db:Session=Depends(get_db),s=Depends(require_login)):
    task=get_task(db,task_id,s.user)
    if s.user.id not in (task.assigned_to, task.created_by) and not can_approve(s.user,task):
        raise HTTPException(403, 'لا تملك صلاحية الإرفاق لهذه المهمة')
    if task.status=='done':
        raise HTTPException(409, 'المهمة معتمدة ومغلقة ولا تقبل مرفقات إضافية')
    if category not in ('instruction','result'):
        raise HTTPException(400, 'تصنيف المرفق غير صحيح')
    if category=='instruction' and not (task.created_by==s.user.id or can_approve(s.user,task)):
        raise HTTPException(403, 'إرفاق تعليمات التكليف متاح لمصدر المهمة أو صاحب صلاحية الاعتماد')
    existing=db.scalar(select(func.count(TaskAttachment.id)).where(TaskAttachment.task_id==task_id)) or 0
    if existing>=MAX_TASK_ATTACHMENTS:
        raise HTTPException(409, 'بلغت المهمة الحد الأقصى 20 مرفقًا')
    name=clean_filename(file.filename or '')
    try:
        data=await file.read(MAX_ATTACHMENT_BYTES+1)
    finally:
        await file.close()
    mime=validate_attachment(name,data)
    task_bytes=db.scalar(select(func.coalesce(func.sum(TaskAttachment.size_bytes),0))
                         .where(TaskAttachment.task_id==task_id)) or 0
    global_bytes=db.scalar(select(func.coalesce(func.sum(TaskAttachment.size_bytes),0))) or 0
    if task_bytes+len(data)>MAX_TASK_ATTACHMENT_TOTAL:
        raise HTTPException(413,'إجمالي مرفقات المهمة يجب ألا يتجاوز 40 ميجابايت')
    if global_bytes+len(data)>MAX_ATTACHMENTS_GLOBAL:
        raise HTTPException(507,'بلغ تخزين المرفقات التجريبي الحد المخصص له. يرجى التواصل مع مدير النظام')
    att=TaskAttachment(task_id=task_id,uploaded_by=s.user.id,filename=name,media_type=mime,
                       size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),
                       content=data,category=category)
    db.add(att);db.flush()
    audit(db,s.user.id,'upload_attachment',f'task={task_id},attachment={att.id},category={category},bytes={len(data)}')
    db.commit()
    return attachment_meta(att)


@app.get('/api/tasks/{task_id}/attachments/{attachment_id}/download')
def download_attachment(task_id:int, attachment_id:int,db:Session=Depends(get_db),s=Depends(require_login)):
    get_task(db,task_id,s.user)
    attachment=db.get(TaskAttachment,attachment_id)
    if not attachment or attachment.task_id!=task_id:
        raise HTTPException(404, 'المرفق غير موجود')
    audit(db,s.user.id,'download_attachment',f'task={task_id},attachment={attachment_id}')
    db.commit()
    header_name=quote(attachment.filename, safe='')
    ascii_suffix=Path(attachment.filename).suffix.lower()
    return Response(content=attachment.content,media_type='application/octet-stream',headers={
        'Content-Disposition':f"attachment; filename=\"enjaz-file-{attachment.id}{ascii_suffix}\"; filename*=UTF-8''{header_name}",
        'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
        'Content-Security-Policy':"default-src 'none'; sandbox",
    })


@app.post('/api/tasks/{task_id}/assign')
def assign_task(task_id:int,data:AssignBody,db:Session=Depends(get_db),s=Depends(require_login)):
    me=s.user;t=get_task(db,task_id,me)
    if not can_approve(me,t):raise HTTPException(403,'ليس لديك صلاحية إعادة التكليف')
    if t.status=='done':raise HTTPException(409,'لا يمكن إعادة إسناد مهمة مكتملة')
    u=db.get(User,data.assigned_to)
    if not u or not u.active or u.department_id!=t.department_id:raise HTTPException(400,'الموظف غير نشط أو خارج القسم')
    prev=t.assigned_to;t.assigned_to=u.id;t.updated_at=NOW()
    audit(db,me.id,'assign_task',f'task={t.id},from={prev},to={u.id}')
    db.commit();return task_data(t,db)

@app.post('/api/tasks/{task_id}/steps/{step_id}')
def toggle_step(task_id:int,step_id:int,data:StepToggle,db:Session=Depends(get_db),s=Depends(require_login)):
    me=s.user;t=get_task(db,task_id,me)
    if t.assigned_to!=me.id and not can_approve(me,t):raise HTTPException(403,'لا يمكنك تنفيذ هذه المهمة')
    if t.status in ('review','done'):raise HTTPException(409,'المهمة قيد الاعتماد أو مكتملة')
    step=next((x for x in t.steps if x.id==step_id),None)
    if not step:raise HTTPException(404,'المرحلة غير موجودة')
    step.done=data.done;step.updated_at=NOW()
    t.status='working' if any(st.done for st in t.steps) else 'new';t.updated_at=NOW()
    audit(db,me.id,'toggle_step',f'task={task_id},step={step_id},done={data.done}')
    db.commit();return task_data(t,db)

@app.post('/api/tasks/{task_id}/submit')
def submit_task(task_id:int,data:SubmitBody,db:Session=Depends(get_db),s=Depends(require_login)):
    me=s.user;t=get_task(db,task_id,me)
    if t.assigned_to != me.id and not can_approve(me,t):raise HTTPException(403,'الصلاحية غير متاحة')
    if t.status=='done' or t.status=='review':raise HTTPException(409,'المهمة قيد المراجعة أو مكتملة')
    if not all(st.done for st in t.steps):raise HTTPException(409,'أكمل المراحل قبل رفع المهمة للمراجعة')
    t.status='review';t.result_text=data.result_text.strip();t.updated_at=NOW();audit(db,me.id,'submit_task',str(t.id));db.commit();return task_data(t,db)

@app.post('/api/tasks/{task_id}/decision')
def task_decision(task_id:int,data:Decision,db:Session=Depends(get_db),s=Depends(require_login)):
    me=s.user;t=get_task(db,task_id,me)
    if not can_approve(me,t):raise HTTPException(403,'ليس لديك صلاحية الاعتماد')
    if t.status != 'review':raise HTTPException(409,'المهمة ليست قيد المراجعة')
    if data.decision not in ('approve','revision'):raise HTTPException(400,'قرار غير صالح')
    t.status='done' if data.decision=='approve' else 'revision';t.updated_at=NOW()
    audit(db,me.id,'task_'+data.decision,f'task={t.id},reason={data.reason}')
    if data.reason:
        db.add(Message(channel=f'task:{t.id}',sender_id=me.id,text=('اعتماد المهمة: ' if data.decision=='approve' else 'إعادة للمراجعة: ')+data.reason))
    db.commit();return task_data(t,db)

def allowed_channel(me,channel,db):
    if channel=='general':return True
    if channel.startswith('dept:'):
        try:dept=int(channel.split(':')[1])
        except: return False
        return me.role=='director' or me.department_id==dept
    if channel.startswith('task:'):
        try:tid=int(channel.split(':')[1])
        except:return False
        task=db.get(Task,tid)
        return bool(task and can_view_task(me,task))
    return False

@app.get('/api/messages')
def get_messages(channel:str='general',after:int=0,db:Session=Depends(get_db),s=Depends(require_login)):
    if not allowed_channel(s.user,channel,db):raise HTTPException(403,'المحادثة غير متاحة')
    q=select(Message).where(Message.channel==channel,Message.id>max(after,0)).order_by(Message.id.desc()).limit(150)
    ms=db.scalars(q).all()
    return [{'id':m.id,'channel':m.channel,'sender':m.sender.full_name,'sender_id':m.sender_id,'text':m.text,'created_at':iso(m.created_at)} for m in reversed(ms)]

@app.post('/api/messages',status_code=201)
def post_message(data:MessageCreate,db:Session=Depends(get_db),s=Depends(require_login)):
    if not allowed_channel(s.user,data.channel,db):raise HTTPException(403,'المحادثة غير متاحة')
    m=Message(channel=data.channel,sender_id=s.user_id,text=data.text.strip());db.add(m);db.flush();audit(db,s.user_id,'message',data.channel);db.commit()
    return {'id':m.id,'ok':True}

@app.post('/api/activity/pulse')
def heartbeat(data:Pulse,db:Session=Depends(get_db),s=Depends(require_login)):
    now=NOW()
    last=s.last_seen.replace(tzinfo=s.last_seen.tzinfo or UTC)
    elapsed=max(0,min(int((now-last).total_seconds()),35))
    # This is client-reported activity, NOT verified employee attendance or productivity.
    if data.active:s.active_seconds+=elapsed;s.last_activity=now
    s.last_seen=now;db.commit();return {'ok':True,'server_time':iso(now)}

def activity_data(a):
    now=NOW()
    last=a.last_seen.replace(tzinfo=a.last_seen.tzinfo or UTC)
    online=not a.logout_at and last>=now-timedelta(seconds=ONLINE_WINDOW_SECONDS)
    end=a.logout_at or (min(now,last+timedelta(seconds=ONLINE_WINDOW_SECONDS)) if not online else now)
    login=a.login_at.replace(tzinfo=a.login_at.tzinfo or UTC)
    end=end.replace(tzinfo=end.tzinfo or UTC)
    return {'id':a.id,'user_id':a.user_id,'name':a.user.full_name,'department':a.user.department.name if a.user.department else 'الإدارة العامة','login_at':iso(a.login_at),'logout_at':iso(a.logout_at),'last_seen':iso(a.last_seen),'duration_minutes':round(max(0,(end-login).total_seconds())/60,1),'active_minutes':round(a.active_seconds/60,1),'status':'online' if online else 'offline','ended_reason':a.ended_reason or ('signal_lost' if not online else '')}

def activity_rows(me,db,user_id:Optional[int],start:Optional[date],end:Optional[date]):
    q=select(AuthSession).join(User,AuthSession.user_id==User.id)
    if me.role=='head':q=q.where(User.department_id==me.department_id)
    if me.role=='employee':q=q.where(User.id==me.id)
    if user_id:q=q.where(User.id==user_id)
    if start:q=q.where(AuthSession.login_at>=datetime.combine(start,datetime.min.time(),UTC))
    if end:q=q.where(AuthSession.login_at<datetime.combine(end+timedelta(days=1),datetime.min.time(),UTC))
    return [activity_data(x) for x in db.scalars(q.order_by(AuthSession.login_at.desc()))]

@app.get('/api/activity')
def activity(user_id:Optional[int]=None,start:Optional[date]=None,end:Optional[date]=None,db:Session=Depends(get_db),s=Depends(require_login)):
    return activity_rows(s.user,db,user_id,start,end)[:1000]

@app.get('/api/activity/export')
def export_activity(user_id:Optional[int]=None,start:Optional[date]=None,end:Optional[date]=None,db:Session=Depends(get_db),s=Depends(require_login)):
    rows=activity_rows(s.user,db,user_id,start,end)
    cols=[('id','معرف الجلسة'),('name','اسم الموظف'),('department','القسم'),('login_at','تاريخ ووقت الدخول UTC'),('logout_at','تاريخ ووقت الخروج UTC'),('last_seen','آخر اتصال UTC'),('duration_minutes','مدة الاتصال (دقيقة)'),('active_minutes','مدة النشاط المرصود (دقيقة)'),('status','الحالة'),('ended_reason','سبب الإنهاء')]
    stream=io.StringIO();writer=csv.writer(stream);writer.writerow([v for k,v in cols])
    def cell(x):
        x=str(x if x is not None else '')
        return "'"+x if x.lstrip().startswith(('=','+','-','@','\t','\r')) else x
    for row in rows:writer.writerow([cell(row.get(k)) for k,v in cols])
    data='\ufeff'+stream.getvalue()
    filename='enjaz_activity_'+(str(user_id) if user_id else 'all')+'.csv'
    return Response(data,media_type='text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="{filename}"'})

@app.get('/api/presence')
def presence(db: Session = Depends(get_db), s = Depends(require_login)):
    """Live browser-session signals grouped by department, subject to role visibility.

    An employee with several open sessions is counted once. This is not an
    attendance, hours-worked, or productivity measurement.
    """
    me = s.user
    now = NOW()
    cutoff = now - timedelta(seconds=ONLINE_WINDOW_SECONDS)

    users_query = select(User).where(User.active.is_(True)).order_by(User.full_name)
    if me.role == 'head':
        users_query = users_query.where(User.department_id == me.department_id) if me.department_id else users_query.where(User.id == me.id)
    elif me.role == 'employee':
        users_query = users_query.where(User.id == me.id)
    visible_users = db.scalars(users_query).all()
    ids = [u.id for u in visible_users]
    recent_by_user = {}
    if ids:
        recent = db.execute(select(AuthSession.user_id, AuthSession.last_seen, AuthSession.last_activity).where(
            AuthSession.user_id.in_(ids),
            AuthSession.logout_at.is_(None),
            AuthSession.expires_at > now,
            AuthSession.last_seen >= cutoff,
        )).all()
        for user_id, seen, activity_at in recent:
            seen = seen.replace(tzinfo=seen.tzinfo or UTC)
            activity_at = activity_at.replace(tzinfo=activity_at.tzinfo or UTC)
            if user_id not in recent_by_user or seen > recent_by_user[user_id][0]:
                recent_by_user[user_id] = (seen, activity_at)

    if me.role == 'director':
        departments = db.scalars(select(Department).order_by(Department.id)).all()
    elif me.department_id:
        department = db.get(Department, me.department_id)
        departments = [department] if department else []
    else:
        departments = []

    groups = [{'department_id': d.id, 'department': d.name, 'total_staff': 0,
               'online_count': 0, 'employees': []} for d in departments]
    groups_by_id = {group['department_id']: group for group in groups}
    if me.role == 'director' and any(u.department_id is None for u in visible_users):
        office = {'department_id': None, 'department': 'الإدارة العامة', 'total_staff': 0,
                  'online_count': 0, 'employees': []}
        groups.append(office)
        groups_by_id[None] = office
    elif me.role != 'director' and me.department_id is None:
        office = {'department_id': None, 'department': 'الإدارة العامة', 'total_staff': 0,
                  'online_count': 0, 'employees': []}
        groups.append(office)
        groups_by_id[None] = office

    for user in visible_users:
        group = groups_by_id.get(user.department_id)
        if group is None:
            continue
        group['total_staff'] += 1
        recent = recent_by_user.get(user.id)
        if recent is None:
            continue
        last_seen, last_activity = recent
        group['online_count'] += 1
        group['employees'].append({
            'id': user.id, 'name': user.full_name, 'role': user.role,
            'last_seen': iso(last_seen),
            'active_now': last_activity >= now - timedelta(seconds=120),
        })

    return {
        'as_of': iso(now), 'online_window_seconds': ONLINE_WINDOW_SECONDS,
        'scope': me.role,
        'total_staff': sum(group['total_staff'] for group in groups),
        'online_total': sum(group['online_count'] for group in groups),
        'departments': groups,
    }

@app.get('/api/dashboard')
def dashboard(db:Session=Depends(get_db),s=Depends(require_login)):
    me=s.user;q=select(Task)
    if me.role=='head':q=q.where(Task.department_id==me.department_id)
    if me.role=='employee':q=q.where(Task.assigned_to==me.id)
    ts=db.scalars(q).all()
    return {'total':len(ts),'done':sum(t.status=='done' for t in ts),'late':sum(t.status!='done' and t.due_date<date.today() for t in ts),'review':sum(t.status=='review' for t in ts),'progress':round(sum(progress(t) for t in ts)/len(ts)) if ts else 0,'by_department':[{'department':d.name,'count':len([t for t in ts if t.department_id==d.id]),'progress':round(sum(progress(t) for t in ts if t.department_id==d.id)/max(1,len([t for t in ts if t.department_id==d.id])))} for d in db.scalars(select(Department))]}

@app.get('/api/audit')
def audit_logs(db:Session=Depends(get_db),s=Depends(require_login)):
    if s.user.role!='director':raise HTTPException(403,'الصلاحية غير متاحة')
    return [{'action':a.action,'actor_id':a.actor_id,'details':a.details,'created_at':iso(a.created_at)} for a in db.scalars(select(Audit).order_by(Audit.id.desc()).limit(250))]

# Internal mail: private on-platform messaging (no SMTP, no external emails).
# Each thread is between two active accounts; reads are strictly participant-only.
def internal_mail_thread(db: Session,thread_id:int,user:User) -> InternalMailThread:
    th=db.get(InternalMailThread,thread_id)
    if not th or user.id not in (th.initiator_id,th.recipient_id):
        raise HTTPException(404,'الرسالة غير موجودة')
    return th


def internal_mail_person(u: User):
    return {'id':u.id,'name':u.full_name,'department':u.department.name if u.department else 'الإدارة العامة',
            'role':u.role}


def internal_mail_thread_summary(db: Session,th: InternalMailThread,me:User):
    other_id=th.recipient_id if th.initiator_id==me.id else th.initiator_id
    other=db.get(User,other_id)
    latest=db.scalar(select(InternalMailEntry).where(InternalMailEntry.thread_id==th.id)
                     .order_by(InternalMailEntry.id.desc()).limit(1))
    unread=db.scalar(select(func.count()).select_from(InternalMailEntry).where(
        InternalMailEntry.thread_id==th.id,InternalMailEntry.recipient_id==me.id,
        InternalMailEntry.read_at.is_(None))) or 0
    return {'id':th.id,'subject':th.subject,'other':internal_mail_person(other) if other else None,
            'created_at':iso(th.created_at),'updated_at':iso(th.updated_at),
            'last_body':latest.body[:180] if latest else '',
            'last_sender_id':latest.sender_id if latest else None,
            'unread_count':int(unread),'message_count':int(db.scalar(select(func.count()).select_from(InternalMailEntry).where(InternalMailEntry.thread_id==th.id)) or 0)}


@app.get('/api/inbox/contacts')
def internal_mail_contacts(db:Session=Depends(get_db),s=Depends(require_login)):
    q=select(User).where(User.active.is_(True),User.id!=s.user_id).order_by(User.full_name)
    return [internal_mail_person(user) for user in db.scalars(q)]


@app.get('/api/inbox/unread')
def internal_mail_unread(db:Session=Depends(get_db),s=Depends(require_login)):
    rows=db.execute(select(func.count(func.distinct(InternalMailEntry.thread_id)),
                           func.count(InternalMailEntry.id)).where(
        InternalMailEntry.recipient_id==s.user_id,InternalMailEntry.read_at.is_(None))).one()
    return {'unread_threads':rows[0],'unread_messages':rows[1]}


@app.get('/api/inbox')
def list_internal_mail(folder:str='inbox',db:Session=Depends(get_db),s=Depends(require_login)):
    if folder not in ('inbox','sent'):
        raise HTTPException(400,'المجلد غير صحيح')
    match=InternalMailEntry.recipient_id if folder=='inbox' else InternalMailEntry.sender_id
    ids=select(InternalMailEntry.thread_id).where(match==s.user_id).distinct()
    q=select(InternalMailThread).where(InternalMailThread.id.in_(ids)).order_by(
        InternalMailThread.updated_at.desc(), InternalMailThread.id.desc()).limit(200)
    return {'folder':folder,'threads':[internal_mail_thread_summary(db,th,s.user) for th in db.scalars(q)]}


@app.post('/api/inbox',status_code=201)
def compose_internal_mail(data:InternalMailCompose,db:Session=Depends(get_db),s=Depends(require_login)):
    receiver=db.get(User,data.recipient_id)
    if not receiver or not receiver.active or receiver.id==s.user_id:
        raise HTTPException(400,'اختر موظفًا نشطًا غير حسابك')
    subject=data.subject.strip();body=data.body.strip()
    if len(subject)<2 or not body:raise HTTPException(400,'اكتب الموضوع ونص الرسالة')
    now=NOW()
    thread=InternalMailThread(subject=subject,initiator_id=s.user_id,recipient_id=receiver.id,
                              created_at=now,updated_at=now)
    db.add(thread);db.flush()
    db.add(InternalMailEntry(thread_id=thread.id,sender_id=s.user_id,recipient_id=receiver.id,
                             body=body,created_at=now))
    audit(db,s.user_id,'internal_mail_send',f'thread={thread.id},recipient={receiver.id}')
    db.commit()
    return {'id':thread.id,'ok':True}


@app.get('/api/inbox/{thread_id}')
def read_internal_mail(thread_id:int,db:Session=Depends(get_db),s=Depends(require_login)):
    thread=internal_mail_thread(db,thread_id,s.user)
    items=db.scalars(select(InternalMailEntry).where(InternalMailEntry.thread_id==thread.id)
                     .order_by(InternalMailEntry.id.asc()).limit(500)).all()
    userids=set([thread.initiator_id,thread.recipient_id]);people={uid:db.get(User,uid) for uid in userids}
    return {'thread':internal_mail_thread_summary(db,thread,s.user),'messages':[
        {'id':x.id,'sender_id':x.sender_id,'sender':people[x.sender_id].full_name if people.get(x.sender_id) else '',
         'recipient_id':x.recipient_id,'body':x.body,'created_at':iso(x.created_at),'read_at':iso(x.read_at)}
        for x in items]}


@app.post('/api/inbox/{thread_id}/read')
def mark_internal_mail_read(thread_id:int,db:Session=Depends(get_db),s=Depends(require_login)):
    internal_mail_thread(db,thread_id,s.user)
    entries=db.scalars(select(InternalMailEntry).where(
        InternalMailEntry.thread_id==thread_id,InternalMailEntry.recipient_id==s.user_id,
        InternalMailEntry.read_at.is_(None))).all()
    now=NOW()
    for e in entries:e.read_at=now
    if entries:db.commit()
    return {'ok':True,'marked':len(entries)}


@app.post('/api/inbox/{thread_id}/reply',status_code=201)
def reply_internal_mail(thread_id:int,data:InternalMailReply,db:Session=Depends(get_db),s=Depends(require_login)):
    th=internal_mail_thread(db,thread_id,s.user)
    other_id=th.recipient_id if s.user_id==th.initiator_id else th.initiator_id
    other=db.get(User,other_id)
    if not other or not other.active:raise HTTPException(409,'لا يمكن الرد على حساب غير نشط')
    body=data.body.strip()
    if not body:raise HTTPException(400,'اكتب نص الرد')
    now=NOW();db.add(InternalMailEntry(thread_id=th.id,sender_id=s.user_id,recipient_id=other_id,
                                       body=body,created_at=now))
    th.updated_at=now
    audit(db,s.user_id,'internal_mail_reply',f'thread={th.id},recipient={other_id}')
    db.commit()
    return {'ok':True,'thread_id':th.id}
