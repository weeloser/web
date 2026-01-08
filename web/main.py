import socketio
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
import uvicorn
import time
from collections import defaultdict

# Создаем сервер
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
app = FastAPI()
socket_app = socketio.ASGIApp(sio, app)

# Шаблоны (HTML)
templates = Jinja2Templates(directory="templates")

# Хранилище данных (в памяти)
# rooms = { room_id: { sid: { name, avatar, is_admin, ip } } }
rooms = defaultdict(dict)
# banned_ips = { ip: timestamp_until_unban }
banned_ips = {}

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/{room_id}")
async def room(request: Request, room_id: str):
    return templates.TemplateResponse("index.html", {"request": request})

# --- Логика Socket.IO ---

@sio.event
async def connect(sid, environ):
    # Получаем IP клиента (на Render он может быть в заголовке x-forwarded-for)
    headers = dict(environ.get('asgi.scope', {}).get('headers', []))
    # Простая логика извлечения IP для примера
    client_ip = environ.get('REMOTE_ADDR') 
    
    # Проверка бана
    current_time = time.time()
    if client_ip in banned_ips:
        if current_time < banned_ips[client_ip]:
            return False # Отклонить соединение
        else:
            del banned_ips[client_ip] # Бан истек

@sio.event
async def join_room(sid, data):
    room_id = data['room']
    user_name = data['name']
    avatar = data['avatar']
    
    # Получаем IP (упрощенно для примера)
    client_ip = "user_ip" 

    # Если комната пуста, первый вошедший - админ
    is_admin = len(rooms[room_id]) == 0
    
    rooms[room_id][sid] = {
        'name': user_name,
        'avatar': avatar,
        'is_admin': is_admin,
        'ip': client_ip
    }
    
    await sio.enter_room(sid, room_id)
    
    # Сообщаем всем, кто в комнате, что пришел новый участник
    await sio.emit('user_joined', {
        'sid': sid,
        'name': user_name,
        'avatar': avatar,
        'is_admin': is_admin
    }, room=room_id, skip_sid=sid)
    
    # Отправляем новому участнику список тех, кто уже там
    existing_users = []
    for existing_sid, info in rooms[room_id].items():
        if existing_sid != sid:
            existing_users.append({
                'sid': existing_sid,
                'name': info['name'],
                'avatar': info['avatar'],
                'is_admin': info['is_admin']
            })
    
    await sio.emit('existing_users', existing_users, to=sid)
    
    # Если ты админ, скажем об этом
    if is_admin:
        await sio.emit('set_admin', {'is_admin': True}, to=sid)

@sio.event
async def signal(sid, data):
    # Пересылка WebRTC сигналов (offer, answer, candidate) конкретному пользователю
    target_sid = data['target']
    await sio.emit('signal', {
        'sender': sid,
        'type': data['type'],
        'data': data['data']
    }, to=target_sid)

@sio.event
async def reaction(sid, data):
    room_id = data['room']
    emoji = data['emoji']
    await sio.emit('show_reaction', {'sid': sid, 'emoji': emoji}, room=room_id)

@sio.event
async def kick_user(sid, data):
    room_id = data['room']
    target_sid = data['target_sid']
    
    # Проверка прав
    if rooms[room_id].get(sid, {}).get('is_admin'):
        # Бан на 3 минуты (180 сек)
        target_ip = rooms[room_id].get(target_sid, {}).get('ip')
        if target_ip:
            banned_ips[target_ip] = time.time() + 180
            
        await sio.emit('kicked', {}, to=target_sid)
        await sio.disconnect(target_sid)
        
        # Удаляем из списка
        if target_sid in rooms[room_id]:
            del rooms[room_id][target_sid]
            
        await sio.emit('user_left', {'sid': target_sid}, room=room_id)

@sio.event
async def disconnect(sid):
    for room_id in list(rooms.keys()):
        if sid in rooms[room_id]:
            del rooms[room_id][sid]
            await sio.emit('user_left', {'sid': sid}, room=room_id)
            # Если комната пуста, удаляем её запись
            if not rooms[room_id]:
                del rooms[room_id]
            break

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)