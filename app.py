from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import uuid, math, time, threading, random

app = Flask(__name__)
app.secret_key = 'aeonv2'
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')

# ── Constantes ─────────────────────────────────────────────────────────────
MAP_S   = 200.0   # mapa de -100 a +100
TICK    = 1/20    # 20 ticks/s (suficiente para bots)
BOTS_N  = 7
BOT_SPD = 6.0
BOT_HP  = 80.0
BULLET_SPD = 60.0
BULLET_DMG = 25.0
BULLET_LIFE= 1.2  # segundos
PLAYER_HP  = 100.0
PLAYER_SPD = 10.0

ZONE = [
    {'r':90, 'wait':40, 'shrink':25, 'dmg':2},
    {'r':50, 'wait':30, 'shrink':20, 'dmg':5},
    {'r':20, 'wait':20, 'shrink':15, 'dmg':10},
    {'r':5,  'wait':0,  'shrink':10, 'dmg':20},
]

BOT_NAMES = ['Kira','Vex','Nox','Zara','Raze','Lyra','Dusk']

# ── Mapa global de jugadores por SID ───────────────────────────────────────
players   = {}   # sid -> Player
bullets   = []
bullets_lk= threading.Lock()
game_room = 'main'
game_started = False
game_lk   = threading.Lock()

zone_phase  = 0
zone_r      = float(ZONE[0]['r'])
zone_target = float(ZONE[0]['r'])
zone_shrink = False
zone_timer  = float(ZONE[0]['wait'])
zone_cx     = 0.0
zone_cz     = 0.0

kills_count = {}  # pid -> kills

class Player:
    def __init__(self, sid, name, is_bot=False):
        self.sid    = sid
        self.pid    = uuid.uuid4().hex[:6]
        self.name   = name[:12]
        self.is_bot = is_bot
        self.hp     = BOT_HP if is_bot else PLAYER_HP
        self.max_hp = self.hp
        self.spd    = BOT_SPD if is_bot else PLAYER_SPD
        # Spawn aleatorio dentro del mapa
        angle = random.uniform(0, math.pi*2)
        r     = random.uniform(10, 60)
        self.x = math.cos(angle)*r
        self.y = 0.0
        self.z = math.sin(angle)*r
        self.yaw  = random.uniform(0, math.pi*2)
        self.alive= True
        # Bot state
        self.bt_target = None
        self.bt_wx = random.uniform(-60,60)
        self.bt_wz = random.uniform(-60,60)
        self.bt_wt = 0.0
        self.bt_fire_t = random.uniform(0.3,1.0)
        self.bt_aggro  = random.uniform(20,45)

    def d(self):
        return {
            'pid':self.pid,'name':self.name,'bot':self.is_bot,
            'x':round(self.x,2),'y':round(self.y,2),'z':round(self.z,2),
            'yaw':round(self.yaw,3),'hp':round(self.hp,1),'max_hp':self.max_hp,
            'alive':self.alive
        }

class Bullet:
    def __init__(self, owner_pid, x,y,z, dx,dy,dz):
        self.id  = uuid.uuid4().hex[:4]
        self.own = owner_pid
        self.x=x; self.y=y; self.z=z
        self.dx=dx; self.dy=dy; self.dz=dz
        self.life= BULLET_LIFE

    def d(self):
        return {'id':self.id,'x':round(self.x,2),'y':round(self.y,2),'z':round(self.z,2)}


def get_state():
    global zone_phase, zone_r, zone_timer, zone_shrink
    return {
        'players': [p.d() for p in players.values()],
        'bullets': [b.d() for b in bullets],
        'zone': {
            'r': round(zone_r,2), 'cx':zone_cx,'cz':zone_cz,
            'phase':zone_phase,'timer':round(zone_timer,1),'shrink':zone_shrink
        },
        'alive': sum(1 for p in players.values() if p.alive)
    }


def hit_player(target, dmg, killer_pid):
    if not target.alive: return
    target.hp = max(0, target.hp - dmg)
    if target.hp <= 0:
        target.alive = False
        target.hp = 0
        kname = 'La zona'
        for p in players.values():
            if p.pid == killer_pid:
                kills_count[killer_pid] = kills_count.get(killer_pid,0)+1
                kname = p.name; break
        socketio.emit('killed', {'name':target.name,'killer':kname}, room=game_room)


# ── Game loop ──────────────────────────────────────────────────────────────
def game_loop():
    global bullets, zone_phase, zone_r, zone_target, zone_shrink, zone_timer, game_started

    last = time.time()
    while True:
        now = time.time()
        dt  = min(now-last, 0.1)
        last = now

        with game_lk:
            alive = [p for p in players.values() if p.alive]

            # ── Bots ──────────────────────────────────────────────────────
            for b in alive:
                if not b.is_bot: continue
                enemies = [p for p in alive if p.pid != b.pid]
                if not enemies: continue

                # Elegir target
                if b.bt_target is None or random.random() < 0.01:
                    b.bt_target = min(enemies, key=lambda e: math.hypot(e.x-b.x,e.z-b.z)).pid
                t = next((e for e in enemies if e.pid==b.bt_target), None)
                if not t: t=enemies[0]

                dx=t.x-b.x; dz=t.z-b.z
                dist=math.hypot(dx,dz)

                if dist < b.bt_aggro:
                    # Perseguir y disparar
                    b.yaw = math.atan2(dx,dz)
                    if dist > 8:
                        s=b.spd*dt
                        b.x+=dx/dist*s; b.z+=dz/dist*s
                    # Disparo
                    b.bt_fire_t -= dt
                    if b.bt_fire_t <= 0:
                        b.bt_fire_t = random.uniform(0.35,0.8)
                        sp=0.08
                        fdx=dx/dist+random.uniform(-sp,sp)
                        fdy=0.0
                        fdz=dz/dist+random.uniform(-sp,sp)
                        l=math.sqrt(fdx*fdx+fdy*fdy+fdz*fdz)
                        bullets.append(Bullet(b.pid,b.x,b.y+1.0,b.z,fdx/l,fdy,fdz/l))
                else:
                    # Merodear
                    b.bt_wt -= dt
                    if b.bt_wt <= 0:
                        b.bt_wx=random.uniform(-70,70)
                        b.bt_wz=random.uniform(-70,70)
                        b.bt_wt=random.uniform(3,6)
                    wx=b.bt_wx-b.x; wz=b.bt_wz-b.z
                    wd=math.hypot(wx,wz)
                    if wd > 1:
                        s=b.spd*dt*0.5
                        b.x+=wx/wd*s; b.z+=wz/wd*s
                        b.yaw=math.atan2(wx,wz)

                # Ir a zona
                if math.hypot(b.x-zone_cx,b.z-zone_cz) > zone_r*0.88:
                    a=math.atan2(zone_cz-b.z,zone_cx-b.x)
                    s=b.spd*dt
                    b.x+=math.cos(a)*s; b.z+=math.sin(a)*s

                # Límites
                b.x=max(-MAP_S/2,min(MAP_S/2,b.x))
                b.z=max(-MAP_S/2,min(MAP_S/2,b.z))

            # ── Balas ──────────────────────────────────────────────────────
            new_bullets=[]
            for bl in bullets:
                bl.x+=bl.dx*BULLET_SPD*dt
                bl.y+=bl.dy*BULLET_SPD*dt
                bl.z+=bl.dz*BULLET_SPD*dt
                bl.life-=dt
                if bl.life<=0: continue
                hit=False
                for p in alive:
                    if p.pid==bl.own: continue
                    if math.sqrt((p.x-bl.x)**2+(p.y+1.0-bl.y)**2+(p.z-bl.z)**2)<1.0:
                        hit_player(p,BULLET_DMG,bl.own); hit=True; break
                if not hit: new_bullets.append(bl)
            bullets=new_bullets

            # ── Zona ───────────────────────────────────────────────────────
            if zone_shrink:
                fr=float(ZONE[zone_phase]['r'])
                st=float(ZONE[zone_phase]['shrink'])
                zone_r=max(zone_target, zone_r-(fr-zone_target)/st*dt)
                if zone_r<=zone_target+0.05:
                    zone_r=zone_target; zone_shrink=False
                    zone_phase+=1
                    if zone_phase<len(ZONE): zone_timer=float(ZONE[zone_phase]['wait'])
            else:
                if zone_timer>0: zone_timer-=dt
                elif zone_phase+1<len(ZONE):
                    zone_target=float(ZONE[zone_phase+1]['r']); zone_shrink=True

            # Daño zona
            zone_dmg=ZONE[min(zone_phase,len(ZONE)-1)]['dmg']
            for p in alive:
                if math.hypot(p.x-zone_cx,p.z-zone_cz)>zone_r:
                    hit_player(p,zone_dmg*dt,None)

            # Game over
            alive2=[p for p in players.values() if p.alive]
            humans=[p for p in alive2 if not p.is_bot]
            if game_started and len(alive2)<=1:
                w=alive2[0].name if alive2 else 'Nadie'
                socketio.emit('game_over',{'winner':w},room=game_room)

        socketio.emit('state', get_state(), room=game_room)
        time.sleep(max(0, TICK-(time.time()-now)))


# Arrancar loop en background
_loop_thread = threading.Thread(target=game_loop, daemon=True)
_loop_thread.start()


# ── Bots iniciales ─────────────────────────────────────────────────────────
def spawn_initial_bots():
    for i in range(BOTS_N):
        sid = 'bot_'+uuid.uuid4().hex[:6]
        b = Player(sid, BOT_NAMES[i%len(BOT_NAMES)], is_bot=True)
        players[sid] = b

spawn_initial_bots()


# ── Flask routes ───────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('game.html')


@socketio.on('connect')
def on_connect():
    join_room(game_room)
    emit('connected', {'msg':'ok'})

@socketio.on('join')
def on_join(data):
    global game_started
    name = (data.get('name') or 'Player')[:12]
    p = Player(request.sid, name, is_bot=False)
    with game_lk:
        players[request.sid] = p
        game_started = True
    emit('joined', {'pid': p.pid, 'state': get_state()})

@socketio.on('move')
def on_move(d):
    with game_lk:
        p = players.get(request.sid)
        if not p or not p.alive: return
        p.x = max(-MAP_S/2, min(MAP_S/2, float(d.get('x', p.x))))
        p.z = max(-MAP_S/2, min(MAP_S/2, float(d.get('z', p.z))))
        p.yaw = float(d.get('yaw', p.yaw))

@socketio.on('shoot')
def on_shoot(d):
    with game_lk:
        p = players.get(request.sid)
        if not p or not p.alive: return
        dx=float(d.get('dx',0)); dy=float(d.get('dy',0)); dz=float(d.get('dz',0))
        l=math.sqrt(dx*dx+dy*dy+dz*dz)
        if l<0.001: return
        bullets.append(Bullet(p.pid, p.x, p.y+1.4, p.z, dx/l, dy/l, dz/l))

@socketio.on('disconnect')
def on_disconnect():
    with game_lk:
        players.pop(request.sid, None)
    leave_room(game_room)

if __name__ == '__main__':
    socketio.run(app, debug=False, host='0.0.0.0', port=5001)
