import socketio
import random
import string
import time
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from collections import defaultdict
import uvicorn

sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*', ping_timeout=60)
app = FastAPI()
socket_app = socketio.ASGIApp(sio, app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")

rooms = defaultdict(dict)
room_meta = defaultdict(lambda: {'locked': False, 'banned': {}, 'muted': {}})

def generate_room_code():
    while True:
        code = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
        if code not in rooms:
            return code

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "room_id": ""})

@app.get("/{room_id}")
async def room(request: Request, room_id: str):
    return templates.TemplateResponse("index.html", {"request": request, "room_id": room_id})

@app.post("/create_code")
async def create_code():
    code = generate_room_code()
    return {"code": code}

@sio.event
async def connect(sid, environ):
    headers = dict(environ.get('asgi.scope', {}).get('headers', []))
    x_forwarded_for = None
    for k, v in headers.items():
        if k == b'x-forwarded-for':
            x_forwarded_for = v.decode()
            break
    client_ip = x_forwarded_for if x_forwarded_for else environ.get('REMOTE_ADDR')
    await sio.save_session(sid, {'ip': client_ip})

@sio.event
async def join_room(sid, data):
    room_id = data['room']
    
    session = await sio.get_session(sid)
    client_ip = session.get('ip', 'unknown')
    
    current_time = time.time()
    
    if room_id in room_meta:
        banned = room_meta[room_id]['banned']
        if client_ip in banned:
            if current_time < banned[client_ip]:
                await sio.emit('error', {'message': 'Вы забанены в этой комнате'}, to=sid)
                return
            else:
                del banned[client_ip]

        if room_meta[room_id]['locked']:
            await sio.emit('error', {'message': 'Комната закрыта (Private)'}, to=sid)
            return

    is_admin = len(rooms[room_id]) == 0
    
    user_info = {
        'name': data['name'],
        'avatar': data['avatar'],
        'is_admin': is_admin,
        'ip': client_ip,
        'video_enabled': data.get('video_enabled', False),
        'audio_enabled': data.get('audio_enabled', False),
        'id': sid
    }
    
    rooms[room_id][sid] = user_info
    await sio.enter_room(sid, room_id)
    
    await sio.emit('user_joined', {'sid': sid, **user_info}, room=room_id, skip_sid=sid)
    
    existing_users = []
    for existing_sid, info in rooms[room_id].items():
        if existing_sid != sid:
            existing_users.append({'sid': existing_sid, **info})
    
    await sio.emit('existing_users', existing_users, to=sid)
    
    if is_admin:
        await sio.emit('set_admin', {'is_admin': True}, to=sid)

    # Проверка глобального мута при входе
    if client_ip in room_meta[room_id]['muted']:
        mute_until = room_meta[room_id]['muted'][client_ip]
        if current_time < mute_until:
             await sio.emit('admin_command', {'command': 'mute_force', 'duration': mute_until - current_time}, to=sid)

@sio.event
async def signal(sid, data):
    target_sid = data['target']
    if target_sid in rooms.get(data.get('room', ''), {}) or \
       any(target_sid in r for r in rooms.values()):
        await sio.emit('signal', {
            'sender': sid,
            'type': data['type'],
            'data': data['data']
        }, to=target_sid)

@sio.event
async def state_change(sid, data):
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
    await sio.emit('show_reaction', {'sid': sid, 'emoji': data['emoji']}, room=data['room'])

@sio.event
async def chat_message(sid, data):
    room_id = data['room']
    user = rooms[room_id].get(sid, {'name': 'Unknown'})
    await sio.emit('chat_message', {
        'sid': sid,
        'name': user['name'],
        'text': data['text'],
        'time': time.strftime("%H:%M")
    }, room=room_id)

@sio.event
async def raise_hand(sid, data):
    room_id = data['room']
    await sio.emit('user_hand_raised', {'sid': sid}, room=room_id)

@sio.event
async def admin_action(sid, data):
    room_id = data['room']
    command = data['command']
    target_sid = data.get('target_sid')
    
    if not rooms[room_id].get(sid, {}).get('is_admin'):
        return

    meta = room_meta[room_id]
    current_time = time.time()

    if command == 'kick':
        if target_sid:
            await sio.emit('kicked', {}, to=target_sid)
            await sio.disconnect(target_sid)

    elif command == 'ban':
        duration = data.get('duration', 5) * 60
        target_info = rooms[room_id].get(target_sid)
        if target_info:
            meta['banned'][target_info['ip']] = current_time + duration
            await sio.emit('kicked', {'reason': 'ban'}, to=target_sid)
            await sio.disconnect(target_sid)

    elif command == 'mute':
        duration = data.get('duration', 5) * 60
        target_info = rooms[room_id].get(target_sid)
        if target_info:
            meta['muted'][target_info['ip']] = current_time + duration
            await sio.emit('admin_command', {'command': 'mute_force', 'duration': duration}, to=target_sid)

    elif command == 'unmute':
        target_info = rooms[room_id].get(target_sid)
        if target_info and target_info['ip'] in meta['muted']:
            del meta['muted'][target_info['ip']]
            await sio.emit('admin_command', {'command': 'unmute_force'}, to=target_sid)

    elif command == 'toggle_lock':
        meta['locked'] = not meta['locked']
        await sio.emit('room_locked', {'locked': meta['locked']}, room=room_id)

@sio.event
async def disconnect(sid):
    for room_id in list(rooms.keys()):
        if sid in rooms[room_id]:
            del rooms[room_id][sid]
            await sio.emit('user_left', {'sid': sid}, room=room_id)
            if not rooms[room_id]:
                del rooms[room_id]
                if room_id in room_meta:
                    del room_meta[room_id]
            break

if __name__ == "__main__":
    uvicorn.run("main:socket_app", host="0.0.0.0", port=8000, reload=True)
