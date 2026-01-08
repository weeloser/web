import socketio
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import time
from collections import defaultdict

# Настройка сервера
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*', ping_timeout=60)
app = FastAPI()
socket_app = socketio.ASGIApp(sio, app)

# Разрешаем CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")

# Структура данных: room_id -> sid -> user_info
rooms = defaultdict(dict)
# Баны: ip -> timestamp
banned_ips = {}

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/{room_id}")
async def room(request: Request, room_id: str):
    return templates.TemplateResponse("index.html", {"request": request})

@sio.event
async def connect(sid, environ):
    # Получаем реальный IP через заголовки прокси (для Render)
    headers = dict(environ.get('asgi.scope', {}).get('headers', []))
    # x-forwarded-for обычно содержит реальный IP на Render
    x_forwarded_for = None
    for k, v in headers.items():
        if k == b'x-forwarded-for':
            x_forwarded_for = v.decode()
            break
            
    client_ip = x_forwarded_for if x_forwarded_for else environ.get('REMOTE_ADDR')
    
    current_time = time.time()
    if client_ip in banned_ips:
        if current_time < banned_ips[client_ip]:
            return False 
        else:
            del banned_ips[client_ip]

    # Сохраняем IP в сессии сокета для использования при бане
    await sio.save_session(sid, {'ip': client_ip})

@sio.event
async def join_room(sid, data):
    room_id = data['room']
    user_name = data['name']
    avatar = data['avatar']
    
    # Получаем сессию для IP
    session = await sio.get_session(sid)
    client_ip = session.get('ip', 'unknown')

    is_admin = len(rooms[room_id]) == 0
    
    user_info = {
        'name': user_name,
        'avatar': avatar,
        'is_admin': is_admin,
        'ip': client_ip,
        'video_enabled': data.get('video_enabled', False),
        'audio_enabled': data.get('audio_enabled', False)
    }
    
    rooms[room_id][sid] = user_info
    
    await sio.enter_room(sid, room_id)
    
    # Уведомляем остальных
    await sio.emit('user_joined', {
        'sid': sid,
        **user_info
    }, room=room_id, skip_sid=sid)
    
    # Отправляем список существующих пользователей новому
    existing_users = []
    for existing_sid, info in rooms[room_id].items():
        if existing_sid != sid:
            existing_users.append({'sid': existing_sid, **info})
    
    await sio.emit('existing_users', existing_users, to=sid)
    
    if is_admin:
        await sio.emit('set_admin', {'is_admin': True}, to=sid)

@sio.event
async def signal(sid, data):
    target_sid = data['target']
    if target_sid in rooms.get(data.get('room', ''), {}): # Простая проверка существования
         await sio.emit('signal', {
            'sender': sid,
            'type': data['type'],
            'data': data['data']
        }, to=target_sid)
    elif target_sid in [k for r in rooms.values() for k in r]: # Fallback поиск по всем
        await sio.emit('signal', {
            'sender': sid,
            'type': data['type'],
            'data': data['data']
        }, to=target_sid)

@sio.event
async def state_change(sid, data):
    # Обновление состояния (камера/микрофон вкл/выкл) для отображения заглушек у других
    room_id = data['room']
    if sid in rooms[room_id]:
        rooms[room_id][sid]['video_enabled'] = data.get('video', False)
        rooms[room_id][sid]['audio_enabled'] = data.get('audio', False)
        await sio.emit('user_state_changed', {
            'sid': sid,
            'video': data.get('video'),
            'audio': data.get('audio')
        }, room=room_id, skip_sid=sid)

@sio.event
async def reaction(sid, data):
    room_id = data['room']
    await sio.emit('show_reaction', {'sid': sid, 'emoji': data['emoji']}, room=room_id)

@sio.event
async def kick_user(sid, data):
    room_id = data['room']
    target_sid = data['target_sid']
    
    if rooms[room_id].get(sid, {}).get('is_admin'):
        target_info = rooms[room_id].get(target_sid)
        if target_info:
            target_ip = target_info['ip']
            banned_ips[target_ip] = time.time() + 180 # 3 минуты
            
            await sio.emit('kicked', {}, to=target_sid)
            await sio.disconnect(target_sid)

@sio.event
async def disconnect(sid):
    for room_id in list(rooms.keys()):
        if sid in rooms[room_id]:
            del rooms[room_id][sid]
            await sio.emit('user_left', {'sid': sid}, room=room_id)
            if not rooms[room_id]:
                del rooms[room_id]
            break

if __name__ == "__main__":
    uvicorn.run("main:socket_app", host="0.0.0.0", port=8000, reload=True)
