#!/usr/bin/env python3

import logging
import subprocess
import secrets
import threading
import time

from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from backup_engine import create_backup, get_history
import backup_auth
import backup_sessions

BASE = Path(__file__).resolve().parent
PERSIST = BASE / "persist"
PERSIST.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger("eisy-backup-web")

app = FastAPI(title="eisy Backup")

EISY_UI_TOKEN = None


def set_eisy_ui_token(token):
    global EISY_UI_TOKEN
    EISY_UI_TOKEN = token



BACKUP_DIRECTORY = str(PERSIST / "backups")
COOKIE_NAME = "eisy_backup_session"
COOKIE_MAX_AGE = 3600

backup_state = {
    "running": False,
    "status": "Ready",
    "last_backup": None,
    "error": None
}

state_lock = threading.Lock()
login_attempts = {}
login_lock = threading.Lock()
recovery_attempts = {}
recovery_lock = threading.Lock()

DEFAULT_CONFIG = {
    "enabled": False,
    "schedule": "daily",
    "backup_time": "02:00",
    "retention": 7,
    "backup_directory": BACKUP_DIRECTORY,
    "google_drive_enabled": False,
    "google_drive_folder": "eisy Backups"
}


def load_config():
    path = PERSIST / "config.json"
    if path.exists():
        try:
            import json
            return {**DEFAULT_CONFIG, **json.loads(path.read_text())}
        except Exception:
            LOGGER.exception("Configuration read failed")
    return DEFAULT_CONFIG.copy()


def save_config(data):
    import json
    path = PERSIST / "config.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2))
    temp.chmod(0o600)
    subprocess.run(["setfacl", "-m", "u:polyglot:r:allow", str(temp)], check=True)
    temp.replace(path)


def get_credentials():
    return backup_auth.read_credentials(BASE)


def authenticated(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    # Background polling must not extend the session. Only explicit user
    # activity, marked by the browser, refreshes the inactivity timeout.
    refresh = request.headers.get("X-User-Activity") == "1"
    if not backup_sessions.validate_session(token, refresh=refresh):
        raise HTTPException(401, "Login required")


def client_key(request):
    return request.client.host if request.client else "unknown"


def check_rate_limit(table, key, maximum, interval):
    now = time.monotonic()
    with login_lock:
        attempts = [t for t in table.get(key, []) if now - t < interval]
        if len(attempts) >= maximum:
            table[key] = attempts
            return False
        attempts.append(now)
        table[key] = attempts
        return True


def clear_rate_limit(table, key):
    with login_lock:
        table.pop(key, None)


def backup_worker():
    try:
        config = load_config()

        with state_lock:
            backup_state["status"] = "Creating native eisy backup"
            backup_state["error"] = None

        if not EISY_UI_TOKEN:
            raise RuntimeError("eisy-ui authentication token unavailable")

        result = create_backup(
            directory=config["backup_directory"],
            retention=config["retention"],
            token=EISY_UI_TOKEN
        )

        with state_lock:
            backup_state["status"] = "Backup completed successfully"
            backup_state["last_backup"] = result
            backup_state["error"] = None

        LOGGER.info("Backup completed: %s", result["filename"])

    except Exception as exc:
        LOGGER.exception("Backup failed")
        with state_lock:
            backup_state["status"] = "Backup failed"
            backup_state["error"] = str(exc)

    finally:
        with state_lock:
            backup_state["running"] = False


class PasswordSetup(BaseModel):
    password: str
    confirm: str


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


class PasswordRecovery(BaseModel):
    code: str
    new_password: str
    confirm_password: str


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTML


@app.get("/api/auth/status")
def auth_status(request: Request):
    credentials = get_credentials()
    token = request.cookies.get(COOKIE_NAME)

    return {
        "setup_required": credentials is None,
        "authenticated": backup_sessions.validate_session(token)
    }


@app.post("/api/auth/setup")
def setup_password(data: PasswordSetup, request: Request, response: Response):
    if get_credentials() is not None:
        raise HTTPException(409, "Administrator password is already configured")

    if data.password != data.confirm:
        raise HTTPException(400, "Passwords do not match")

    try:
        backup_auth.create_initial_password(BASE, data.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    LOGGER.info("Initial administrator password created")
    return {"ok": True}


@app.post("/api/auth/login")
def login(data: LoginRequest, request: Request, response: Response):
    if get_credentials() is None:
        raise HTTPException(409, "Initial password setup is required")

    key = client_key(request)

    if not check_rate_limit(login_attempts, key, 5, 300):
        raise HTTPException(429, "Too many login attempts. Try again later.")

    credentials = get_credentials()

    if (
        credentials is None
        or not secrets.compare_digest(data.username, "admin")
        or not backup_auth.check_password(data.password, *credentials)
    ):
        raise HTTPException(401, "Invalid username or password")

    clear_rate_limit(login_attempts, key)

    token = backup_sessions.create_session()
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return {"ok": True}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME)
    backup_sessions.revoke_session(token)
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return {"ok": True}


@app.post("/api/auth/heartbeat", dependencies=[Depends(authenticated)])
def heartbeat(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if not backup_sessions.validate_session(token, refresh=True):
        raise HTTPException(401, "Session expired")
    return {"ok": True}


@app.post("/api/auth/change", dependencies=[Depends(authenticated)])
def change_password(data: PasswordChange):
    if data.new_password != data.confirm_password:
        raise HTTPException(400, "New passwords do not match")

    try:
        backup_auth.change_password(BASE, data.current_password, data.new_password)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    backup_sessions.revoke_all_sessions()

    return {"ok": True}


@app.post("/api/auth/recover")
def recover_password(data: PasswordRecovery, request: Request, response: Response):
    key = client_key(request)

    if not check_rate_limit(recovery_attempts, key, 5, 900):
        raise HTTPException(429, "Too many recovery attempts")

    if data.new_password != data.confirm_password:
        raise HTTPException(400, "New passwords do not match")

    try:
        backup_auth.complete_recovery(BASE, data.code, data.new_password)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    clear_rate_limit(recovery_attempts, key)
    backup_sessions.revoke_all_sessions()

    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    LOGGER.warning("Administrator password recovered")
    return {"ok": True}


class ConfigUpdate(BaseModel):
    enabled: bool = False
    schedule: str = Field(pattern="^(daily|weekly|manual)$")
    backup_time: str = Field(pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$")
    retention: int = Field(ge=1, le=365)
    backup_directory: str
    google_drive_enabled: bool = False
    google_drive_folder: str


@app.get("/api/config")
def get_config(auth=Depends(authenticated)):
    return load_config()


@app.post("/api/config")
def update_config(data: ConfigUpdate, auth=Depends(authenticated)):
    config = data.model_dump()
    save_config(config)
    return {"success": True, "config": config}


@app.post("/api/backup")
def start_backup(auth=Depends(authenticated)):
    with state_lock:
        if backup_state["running"]:
            raise HTTPException(409, "Backup already running")

        backup_state["running"] = True
        backup_state["status"] = "Starting backup"
        backup_state["error"] = None

    threading.Thread(target=backup_worker, daemon=True).start()
    return {"success": True, "message": "Backup started"}


@app.get("/api/history")
def history(auth=Depends(authenticated)):
    return get_history(load_config()["backup_directory"])


@app.get("/api/status")
def status(auth=Depends(authenticated)):
    with state_lock:
        return dict(backup_state)


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>eisy Backup</title>
<style>
body{font-family:Arial,sans-serif;background:#f3f5f8;color:#263244;margin:0;padding:24px}
main{max-width:950px;margin:auto}
header{margin-bottom:24px}
h1{margin-bottom:5px}
h2{font-size:18px;margin-top:0}
.subtitle,.note{color:#687586}
.card{background:white;border-radius:10px;padding:22px;margin-bottom:18px;box-shadow:0 2px 8px #0000000b}
label{display:block;font-weight:600;margin:14px 0 6px}
input,select{box-sizing:border-box;width:100%;padding:10px;border:1px solid #ccd3dc;border-radius:6px}
button{background:#2463a6;color:white;border:0;border-radius:6px;padding:11px 18px;cursor:pointer;font-weight:600;margin-top:18px}
button:hover{background:#194d85}
button:disabled{opacity:.5;cursor:not-allowed}
.status{font-weight:600}
.success{color:#25804b}
.error{color:#b42318}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
table{width:100%;border-collapse:collapse;margin-top:12px}
th,td{text-align:left;padding:11px 8px;border-bottom:1px solid #e5e9ef;font-size:14px}
th{color:#687586}
.note{font-size:13px}
.auth{max-width:420px;margin:9vh auto}
.hidden{display:none!important}
.top-actions{float:right}
@media(max-width:600px){.grid{grid-template-columns:1fr}body{padding:12px}.card{padding:16px}table{font-size:12px}}
</style>
</head>
<body>

<div id="authScreen" class="auth card">
<h1>eisy Backup</h1>
<h2 id="authTitle">Loading...</h2>
<p id="authDescription" class="note"></p>

<div id="setupPanel" class="hidden">
<label>Administrator password</label>
<input id="setupPassword" type="password" autocomplete="new-password">
<label>Confirm password</label>
<input id="setupConfirm" type="password" autocomplete="new-password">
<button onclick="setupPassword()">Create password</button>
</div>

<div id="loginPanel" class="hidden">
<label>Username</label>
<input id="loginUsername" value="admin" autocomplete="username">
<label>Password</label>
<input id="loginPassword" type="password" autocomplete="current-password">
<button onclick="login()">Login</button>
<p><a href="#" onclick="showRecovery();return false">Recover password</a></p>
</div>

<div id="recoveryPanel" class="hidden">
<label>Recovery code from SSH</label>
<input id="recoveryCode" autocomplete="off">
<label>New password</label>
<input id="recoveryPassword" type="password" autocomplete="new-password">
<label>Confirm new password</label>
<input id="recoveryConfirm" type="password" autocomplete="new-password">
<button onclick="recoverPassword()">Set new password</button>
<button onclick="showLogin()">Back to login</button>
</div>
<p id="authError" class="error"></p>
</div>

<main id="dashboard" class="hidden">
<header>
<button class="top-actions" onclick="logout()">Logout</button>
<h1>eisy Backup</h1>
<div class="subtitle">Native eisy backups and Google Drive replication</div>
</header>

<div class="card">
<h2>Backup status</h2>
<div id="status" class="status">Checking...</div>
<p id="last" class="note"></p>
<button id="backupButton" onclick="startBackup()">Backup Now</button>
<div id="message" class="note"></div>
</div>

<div class="card">
<h2>Backup settings</h2>
<label>Automatic backups</label>
<select id="enabled">
<option value="false">Disabled</option>
<option value="true">Enabled</option>
</select>
<div class="grid">
<div><label>Schedule</label>
<select id="schedule">
<option value="daily">Daily</option>
<option value="weekly">Weekly</option>
<option value="manual">Manual only</option>
</select></div>
<div><label>Backup time</label><input id="backup_time" type="time"></div>
</div>
<div class="grid">
<div><label>Local backups to retain</label><input id="retention" type="number" min="1" max="365"></div>
<div><label>Local backup directory</label><input id="backup_directory" type="text"></div>
</div>
<button onclick="saveConfig()">Save settings</button>
<div id="configMessage" class="note"></div>
</div>

<div class="card">
<h2>Backup history</h2>
<div id="history">Loading...</div>
</div>

<div class="card">
<h2>Google Drive</h2>
<div class="status">Not connected</div>
<p class="note">Google Drive authorization and uploads will be added in a later version.</p>
</div>

<div class="card">
<h2>Administrator</h2>
<button onclick="showChangePassword()">Change password</button>
<div id="changePanel" class="hidden">
<label>Current password</label><input id="currentPassword" type="password">
<label>New password</label><input id="newPassword" type="password">
<label>Confirm new password</label><input id="newConfirm" type="password">
<button onclick="changePassword()">Update password</button>
<p id="changeMessage" class="note"></p>
</div>
</div>
</main>

<script>
let heartbeatTimer=null;
let authenticatedSession=false;
let lastActivity=0;
const IDLE_LIMIT=3600000;

["pointerdown","keydown"].forEach(eventName=>{
 document.addEventListener(eventName,()=>{
  if(authenticatedSession)lastActivity=Date.now();
 },{passive:true});
});

function showAuth(title,description){
 authenticatedSession=false;
 document.getElementById('authScreen').classList.remove('hidden');
 document.getElementById('dashboard').classList.add('hidden');
 document.getElementById('authTitle').textContent=title;
 document.getElementById('authDescription').textContent=description||'';
 document.getElementById('authError').textContent='';
}

function showPanel(name){
 for(const id of ['setupPanel','loginPanel','recoveryPanel'])
  document.getElementById(id).classList.add('hidden');
 document.getElementById(name).classList.remove('hidden');
}

async function authStatus(){
 try{
  const r=await fetch('/api/auth/status',{credentials:'same-origin',cache:'no-store'});
  const s=await r.json();
  if(s.authenticated){showDashboard();return;}
  if(s.setup_required){
   showAuth('Create administrator password','Choose a password of at least 12 characters.');
   showPanel('setupPanel');
  }else{
   showAuth('Administrator login','Sign in to manage your backups.');
   showPanel('loginPanel');
  }
 }catch(e){
  showAuth('Connection error','Unable to contact the backup server.');
 }
}

function authError(message){
 document.getElementById('authError').textContent=message;
}

async function setupPassword(){
 const password=document.getElementById('setupPassword').value;
 const confirm=document.getElementById('setupConfirm').value;
 if(password.length<12){authError('Password must contain at least 12 characters.');return;}
 if(password!==confirm){authError('Passwords do not match.');return;}
 try{
  const r=await fetch('/api/auth/setup',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({password,confirm})});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Setup failed');
  document.getElementById('setupPassword').value='';
  document.getElementById('setupConfirm').value='';
  showLogin();
  authError('Administrator password created. Please log in.');
 }catch(e){authError(e.message);}
}

async function login(){
 try{
  const r=await fetch('/api/auth/login',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:document.getElementById('loginUsername').value,password:document.getElementById('loginPassword').value})});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Login failed');
  document.getElementById('loginPassword').value='';
  showDashboard();
 }catch(e){authError(e.message);}
}

function showRecovery(){
 showPanel('recoveryPanel');
 document.getElementById('authTitle').textContent='Password recovery';
 document.getElementById('authDescription').textContent='Enter the temporary recovery code generated over SSH.';
}

function showLogin(){
 showPanel('loginPanel');
 document.getElementById('authTitle').textContent='Administrator login';
 document.getElementById('authDescription').textContent='Sign in to manage your backups.';
 authError('');
}

async function recoverPassword(){
 const code=document.getElementById('recoveryCode').value.trim();
 const password=document.getElementById('recoveryPassword').value;
 const confirm=document.getElementById('recoveryConfirm').value;
 if(password.length<12){authError('Password must contain at least 12 characters.');return;}
 if(password!==confirm){authError('Passwords do not match.');return;}
 try{
  const r=await fetch('/api/auth/recover',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({code,new_password:password,confirm_password:confirm})});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Recovery failed');
  showLogin();
  authError('Password changed. Please log in.');
 }catch(e){authError(e.message);}
}

async function logout(){
 if(heartbeatTimer)clearInterval(heartbeatTimer);
 await fetch('/api/auth/logout',{method:'POST',credentials:'same-origin',cache:'no-store'});
 authenticatedSession=false;
 authStatus();
}

function showDashboard(){
 authenticatedSession=true;
 lastActivity=Date.now();
 document.getElementById('authScreen').classList.add('hidden');
 document.getElementById('dashboard').classList.remove('hidden');
 loadConfig();
 refreshAll();
 if(heartbeatTimer)clearInterval(heartbeatTimer);
 heartbeatTimer=setInterval(async()=>{
  if(!authenticatedSession || Date.now()-lastActivity>=60000)return;
  const r=await fetch('/api/auth/heartbeat',{method:'POST',credentials:'same-origin',cache:'no-store'});
  if(r.status===401){authenticatedSession=false;clearInterval(heartbeatTimer);authStatus();}
 },30000);
}

setInterval(()=>{
 if(authenticatedSession && Date.now()-lastActivity>=IDLE_LIMIT)logout();
},30000);


function showChangePassword(){
 document.getElementById('changePanel').classList.toggle('hidden');
}

async function changePassword(){
 const current_password=document.getElementById('currentPassword').value;
 const new_password=document.getElementById('newPassword').value;
 const confirm=document.getElementById('newConfirm').value;
 if(new_password.length<12){document.getElementById('changeMessage').textContent='Password must contain at least 12 characters.';return;}
 try{
  const r=await fetch('/api/auth/change',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({current_password,new_password,confirm_password:confirm})});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Password change failed');
  if(heartbeatTimer)clearInterval(heartbeatTimer);
  authStatus();
  authError('Password changed. Please log in again.');
 }catch(e){document.getElementById('changeMessage').textContent=e.message;}
}

async function apiFetch(url,options={}){
 const headers={...(options.headers||{})};
 if(authenticatedSession && Date.now()-lastActivity<60000){
  headers['X-User-Activity']='1';
 }
 const r=await fetch(url,{...options,headers,credentials:'same-origin',cache:'no-store'});
 if(r.status===401){authStatus();throw new Error('Session expired. Please log in again.');}
 return r;
}

async function loadConfig(){
 try{
  const r=await apiFetch('/api/config');if(!r.ok)return;
  const c=await r.json();
  for(const key of ['schedule','backup_time','retention','backup_directory'])
   document.getElementById(key).value=c[key];
  document.getElementById('enabled').value=String(c.enabled);
 }catch(e){}
}

async function saveConfig(){
 const c={
  enabled:document.getElementById('enabled').value==='true',
  schedule:document.getElementById('schedule').value,
  backup_time:document.getElementById('backup_time').value,
  retention:Number(document.getElementById('retention').value),
  backup_directory:document.getElementById('backup_directory').value,
  google_drive_enabled:false,
  google_drive_folder:'eisy Backups'
 };
 try{
  const r=await apiFetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(c)});
  document.getElementById('configMessage').textContent=r.ok?'Settings saved.':'Unable to save settings.';
 }catch(e){document.getElementById('configMessage').textContent=e.message;}
}

async function startBackup(){
 const button=document.getElementById('backupButton');
 button.disabled=true;
 document.getElementById('message').textContent='Starting backup...';
 try{
  const r=await apiFetch('/api/backup',{method:'POST'});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Unable to start backup');
  document.getElementById('message').textContent=d.message;
 }catch(e){document.getElementById('message').textContent=e.message;}
 await refreshStatus();
}

async function refreshStatus(){
 try{
  const r=await apiFetch('/api/status');const s=await r.json();
  const el=document.getElementById('status');
  el.textContent=s.status;
  el.className='status '+(s.error?'error':(s.running?'':'success'));
  document.getElementById('last').textContent=s.error?s.error:(s.last_backup?'Last backup: '+s.last_backup.filename:'');
  document.getElementById('backupButton').disabled=s.running;
 }catch(e){}
}

async function refreshHistory(){
 try{
  const r=await apiFetch('/api/history');const items=await r.json();
  if(!items.length){document.getElementById('history').textContent='No backups yet.';return;}
  let html='<table><thead><tr><th>Date</th><th>Result</th><th>Filename</th><th>Size</th><th>Cloud</th></tr></thead><tbody>';
  for(const item of items){
   const date=new Date(item.date).toLocaleString();
   const size=item.size?(item.size/1048576).toFixed(1)+' MB':'—';
   const success=item.status==='successful'||(item.status===undefined&&item.successful);
   const result=success?'<span style="color:#25804b">Success</span>':item.status==='running'?'<span>Running</span>':'<span style="color:#b42318">Failed</span>';
   html+='<tr><td>'+date+'</td><td>'+result+'</td><td>'+item.filename+'</td><td>'+size+'</td><td>'+(item.google_drive||'—')+'</td></tr>';
   if(item.error)html+='<tr><td colspan="5" style="color:#b42318">Error: '+item.error+'</td></tr>';
  }
  html+='</tbody></table>';
  document.getElementById('history').innerHTML=html;
 }catch(e){document.getElementById('history').textContent='Unable to load history.';}
}

async function refreshAll(){await refreshStatus();await refreshHistory();}

authStatus();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn

    certificates = list(BASE.glob("plugin_*.cert"))

    if len(certificates) != 1:
        raise RuntimeError(
            f"Expected one PG3x certificate, found {len(certificates)}"
        )

    cert = certificates[0]
    key = cert.with_suffix(".key")

    if not key.is_file():
        raise RuntimeError(f"HTTPS key missing: {key}")

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8768,
        ssl_certfile=str(cert),
        ssl_keyfile=str(key),
        access_log=False
    )
