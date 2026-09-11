import './style.css';
import { Camera } from './capture/camera';
import { defaultRegions, validateProfile, type Profile } from './core/profile';
import { SessionController } from './session/controller';
import { VisionAPI } from './transport/api';
import { drawDemo } from './ui/demo';

document.querySelector<HTMLDivElement>('#app')!.innerHTML = `
<aside class="rail"><a class="brand" href="/" aria-label="Table Vision">tv<span>●</span></a><div class="rail-active" title="Наблюдение">▣</div><div class="rail-caption">VISION<br>LAB</div><span class="rail-bottom">01</span></aside>
<main><header><div class="breadcrumb">РАБОЧЕЕ ПРОСТРАНСТВО <span>/</span> ПЕРВЫЙ ЭТАП</div><div class="connection"><i id="connection-dot"></i><span id="connection">Подключение…</span></div></header>
<section class="heading"><div><div class="eyebrow">TABLE VISION</div><h1>Наблюдение за столом</h1><p>Камера видит поток. На распознавание уходят значимые изменения.</p></div><span class="stage">ЭТАП 01 <b>Захват и распознавание</b></span></section>
<div class="workspace"><section class="video-panel panel"><div class="panel-heading"><h2>Видеовход <span id="source-badge">ДЕМО</span></h2><span class="mono" id="dimensions">1280 × 720</span></div>
<div id="viewport" class="viewport"><canvas id="demo" width="1280" height="720"></canvas><video id="video" muted playsinline hidden></video><canvas id="overlay" width="1280" height="720"></canvas><div class="video-label"><i></i><span id="video-label">Синтетическая сцена · проверка детектора</span></div></div>
<div class="video-toolbar"><button id="camera">◉ Включить камеру</button><label class="file-button">Открыть видео<input id="file" type="file" accept="video/*" hidden></label><button id="demo-button" class="quiet">Демо</button><button id="next" class="quiet">Изменить сцену →</button></div>
<div class="camera-select"><label>Камера <select id="devices"><option value="">По умолчанию</option></select></label><span id="camera-help">Видео обрабатывается локально до отбора кадра.</span></div>
<div class="metrics"><div><span>Проверка кадра</span><strong id="detector-ms">— <small>мс</small></strong></div><div><span>Изменение</span><strong id="delta">—</strong></div><div><span>Отправлено</span><strong id="requests">0</strong></div><div><span>Ревизия</span><strong id="revision">0</strong></div></div>
</section>
<section class="state-panel panel"><div class="panel-heading"><h2>Состояние стола</h2><span id="state-badge" class="badge">Ожидание</span></div><div class="state-content"><div class="state-age"><span>Последнее наблюдение</span><b id="age">—</b></div><div id="state-empty" class="empty"><div class="empty-icon">⌗</div><h3>Стол ещё не распознан</h3><p>Настройте области кадра<br>и запустите наблюдение.</p></div><pre id="json" hidden></pre></div><div class="state-footer"><i></i>Сервис решений пока не подключён</div></section></div>
<div class="bottom-grid"><section class="panel calibration"><div class="panel-heading"><h2>Области наблюдения</h2><label class="toggle"><input id="show-rois" type="checkbox" checked> Показать</label></div><div class="panel-body"><p class="hint">Выберите область и обведите её на видео. Таймеры и анимацию можно исключить.</p><div class="calibration-row"><select id="region" aria-label="Область"></select><button id="mask" class="quiet">+ Маска</button><button id="remove-mask" class="quiet">Удалить маску</button></div><div class="profile-fields"><label>Единицы<select id="unit"><option>chips</option><option>USD</option><option>EUR</option><option>BB</option></select></label><label>Знаков после запятой<input id="scale" type="number" min="0" max="6" value="0"></label><label>Место героя<input id="hero-seat" type="number" min="0" max="5" value="0"></label></div><div class="profile-actions"><button id="save-profile" class="quiet">Сохранить профиль</button><label class="file-button quiet">Импорт<input id="import-profile" type="file" accept="application/json" hidden></label><button id="export-profile" class="quiet">Экспорт</button></div></div></section>
<section class="panel controls"><div class="panel-heading"><h2>Распознавание</h2><span id="mode" class="badge">—</span></div><div class="panel-body"><label class="token-label">Токен доступа к вашему серверу<input id="token" type="password" autocomplete="off" placeholder="RECEIVER_TOKEN"></label><div class="run-options"><label><input id="diagnostic" type="checkbox"> Перепроверять каждые 2 с</label><span>Обычно — 15 с</span></div><div class="run-actions"><button id="start" class="primary">Начать наблюдение <span>↗</span></button><button id="stop" disabled>Стоп</button><button id="manual" disabled title="Отправить свежий кадр">↻ Кадр</button></div><div class="status-line" role="status" id="status">Готово к настройке. Камера ещё не включена.</div></div></section></div>
<section class="panel journal"><div class="panel-heading"><h2>Журнал <span>ПОСЛЕДНИЕ СОБЫТИЯ</span></h2><button id="recording" class="quiet" disabled>Скачать запись ↓</button></div><div id="events" class="events"><div><time>—</time><span>Здесь появятся причины отправки кадров и результаты.</span></div></div></section>
<footer>TABLE VISION <span>Локальный отбор · 5 проверок / с · подтверждение по двум кадрам</span><b>V 0.1</b></footer></main>`;

const el = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const video = el<HTMLVideoElement>('video'),
  demo = el<HTMLCanvasElement>('demo'),
  overlay = el<HTMLCanvasElement>('overlay');
const camera = new Camera();
let profile: Profile = {
  calibration_id: crypto.randomUUID(),
  width: 1280,
  height: 720,
  unit: 'chips',
  scale: 0,
  hero_seat: 0,
  regions: defaultRegions(),
};
try {
  const saved = localStorage.getItem('table-vision-profile-v1');
  if (saved) profile = validateProfile(JSON.parse(saved));
} catch {
  /* Invalid saved profiles require calibration again. */
}
let controller: SessionController | null = null,
  source: 'demo' | 'camera' | 'file' = 'demo',
  scene = 0,
  objectUrl = '',
  observedAt: number | null = null,
  currentStatus = 'unknown';
let drawStart: { x: number; y: number } | null = null;
let starting = false,
  switching = false;
let lastRecording: unknown = null;
drawDemo(demo, scene);

function log(message: string) {
  const row = document.createElement('div'),
    time = document.createElement('time'),
    text = document.createElement('span');
  time.textContent = new Date().toLocaleTimeString('ru');
  text.textContent = message;
  row.append(time, text);
  const list = el('events');
  list.prepend(row);
  while (list.children.length > 30) list.lastElementChild!.remove();
}
function status(message: string) {
  el('status').textContent = message;
  log(message);
}
function download(name: string, value: unknown) {
  const a = document.createElement('a');
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }),
  );
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function invalidate() {
  currentStatus = 'uncertain';
  observedAt = null;
  el('state-badge').textContent = 'Изменение';
  el('state-badge').className = 'badge amber';
  el('json').hidden = true;
  el('state-empty').hidden = false;
  el('state-empty').querySelector('h3')!.textContent = 'Ждём свежий результат';
}
function regionOptions() {
  el<HTMLSelectElement>('region').replaceChildren(
    ...profile.regions.map((r) => new Option(r.name + (r.ignore ? ' · исключена' : ''), r.name)),
  );
  drawOverlay();
}
function drawOverlay() {
  const c = overlay.getContext('2d')!;
  c.clearRect(0, 0, overlay.width, overlay.height);
  if (!el<HTMLInputElement>('show-rois').checked) return;
  for (const r of profile.regions) {
    const selected = r.name === el<HTMLSelectElement>('region').value;
    c.strokeStyle = r.ignore ? '#f3ac76' : selected ? '#d5f7b4' : '#87afaa';
    c.lineWidth = selected ? 3 : 1.5;
    c.setLineDash(r.ignore ? [8, 5] : []);
    c.strokeRect(
      r.x * overlay.width,
      r.y * overlay.height,
      r.w * overlay.width,
      r.h * overlay.height,
    );
    c.fillStyle = c.strokeStyle;
    c.font = '15px monospace';
    c.fillText(r.name, r.x * overlay.width + 4, r.y * overlay.height + 18);
  }
}
function resize(width: number, height: number) {
  profile.width = width;
  profile.height = height;
  profile.calibration_id = crypto.randomUUID();
  overlay.width = width;
  overlay.height = height;
  el('dimensions').textContent = `${width} × ${height}`;
  drawOverlay();
}
function updateButtons(running: boolean) {
  for (const id of ['stop', 'manual', 'recording'])
    el<HTMLButtonElement>(id).disabled = !running && !(id === 'recording' && lastRecording);
  el<HTMLButtonElement>('start').disabled = running || starting || switching;
  for (const id of [
    'region',
    'mask',
    'remove-mask',
    'unit',
    'scale',
    'hero-seat',
    'import-profile',
    'diagnostic',
  ])
    (el(id) as HTMLInputElement).disabled = running || starting || switching;
  for (const id of ['camera', 'file', 'demo-button', 'devices'])
    (el(id) as HTMLInputElement).disabled = starting || switching;
}
async function stop() {
  const previous = controller;
  controller = null;
  updateButtons(false);
  if (previous) await previous.stop();
  if (!controller) {
    updateButtons(false);
    if (previous) status('Наблюдение остановлено.');
  }
}
async function setSource(next: typeof source) {
  if (switching || starting) throw new Error('Дождитесь завершения переключения источника.');
  switching = true;
  updateButtons(Boolean(controller));
  await stop();
  camera.stop();
  video.pause();
  video.srcObject = null;
  video.removeAttribute('src');
  if (objectUrl) {
    URL.revokeObjectURL(objectUrl);
    objectUrl = '';
  }
  source = next;
  demo.hidden = next !== 'demo';
  video.hidden = next === 'demo';
  el('source-badge').textContent =
    next === 'demo' ? 'ДЕМО' : next === 'camera' ? 'КАМЕРА' : 'ВИДЕО';
  el<HTMLButtonElement>('next').hidden = next !== 'demo';
  el('video-label').textContent =
    next === 'demo'
      ? 'Синтетическая сцена · проверка детектора'
      : next === 'camera'
        ? 'Видеопоток с камеры'
        : 'Локальный видеофайл';
}
el('camera').onclick = async () => {
  if (switching || starting) return;
  try {
    if (source === 'camera' && video.srcObject) {
      await setSource('demo');
      resize(1280, 720);
      drawDemo(demo, scene);
      el('camera').textContent = '◉ Включить камеру';
      status('Камера выключена.');
      return;
    }
    await setSource('camera');
    const settings = await camera.start(video, el<HTMLSelectElement>('devices').value || undefined);
    resize(video.videoWidth, video.videoHeight);
    el('camera-help').textContent =
      `Фактически: ${settings.width} × ${settings.height} · ${Math.round(settings.frameRate ?? 0)} fps`;
    const devices = await navigator.mediaDevices.enumerateDevices();
    el<HTMLSelectElement>('devices').replaceChildren(
      ...devices
        .filter((d) => d.kind === 'videoinput')
        .map((d) => new Option(d.label || 'Камера', d.deviceId)),
    );
    if (settings.deviceId) el<HTMLSelectElement>('devices').value = settings.deviceId;
    video.srcObject instanceof MediaStream &&
      video.srcObject.getTracks().forEach((t) =>
        t.addEventListener('ended', () => {
          void stop();
          status('Камера отключена.');
        }),
      );
    el('camera').textContent = '◉ Выключить камеру';
    status('Камера включена. Проверьте области перед запуском.');
  } catch (error) {
    status(`Камера: ${error instanceof Error ? error.message : 'нет доступа'}`);
  } finally {
    switching = false;
    updateButtons(Boolean(controller));
  }
};
el('demo-button').onclick = async () => {
  if (switching || starting) return;
  try {
    await setSource('demo');
    resize(1280, 720);
    drawDemo(demo, scene);
    el('camera').textContent = '◉ Включить камеру';
    status('Демонстрационный видеовход. Это синтетический стол.');
  } finally {
    switching = false;
    updateButtons(Boolean(controller));
  }
};
el('next').onclick = () => drawDemo(demo, ++scene);
el<HTMLInputElement>('file').onchange = async (e) => {
  const file = (e.target as HTMLInputElement).files?.[0];
  if (!file || switching || starting) return;
  await setSource('file');
  el('camera').textContent = '◉ Включить камеру';
  objectUrl = URL.createObjectURL(file);
  video.src = objectUrl;
  video.loop = false;
  try {
    await video.play();
    resize(video.videoWidth, video.videoHeight);
    status('Локальное видео открыто.');
  } catch {
    status('Не удалось открыть видео.');
  } finally {
    switching = false;
    updateButtons(Boolean(controller));
  }
};
video.onended = () => {
  void stop();
  status('Видеофайл завершён.');
};
video.onerror = () => {
  void stop();
  status('Ошибка видеовхода.');
};
function coordinates(e: PointerEvent) {
  const r = overlay.getBoundingClientRect();
  return {
    x: Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)),
    y: Math.max(0, Math.min(1, (e.clientY - r.top) / r.height)),
  };
}
overlay.onpointerdown = (e) => {
  if (controller || starting || switching) return;
  drawStart = coordinates(e);
  overlay.setPointerCapture(e.pointerId);
};
overlay.onpointerup = (e) => {
  if (!drawStart || controller || starting || switching) return;
  const end = coordinates(e),
    start = drawStart;
  drawStart = null;
  const region = profile.regions.find((r) => r.name === el<HTMLSelectElement>('region').value)!;
  const w = Math.abs(end.x - start.x),
    h = Math.abs(end.y - start.y);
  if (w < 0.005 || h < 0.005) return;
  Object.assign(region, { x: Math.min(start.x, end.x), y: Math.min(start.y, end.y), w, h });
  profile.calibration_id = crypto.randomUUID();
  drawOverlay();
  status(`Область ${region.name} обновлена.`);
};
overlay.onpointercancel = () => {
  drawStart = null;
};
el('region').onchange = drawOverlay;
el('show-rois').onchange = drawOverlay;
el('mask').onclick = () => {
  if (profile.regions.length >= 40) return;
  const name = `mask_${Date.now()}`;
  profile.regions.push({ name, x: 0.01, y: 0.01, w: 0.08, h: 0.06, ignore: true });
  profile.calibration_id = crypto.randomUUID();
  regionOptions();
  el<HTMLSelectElement>('region').value = name;
  drawOverlay();
};
el('remove-mask').onclick = () => {
  profile.regions = profile.regions.filter(
    (r) => r.name !== el<HTMLSelectElement>('region').value || !r.ignore,
  );
  profile.calibration_id = crypto.randomUUID();
  regionOptions();
};
function readFields() {
  profile.unit = el<HTMLSelectElement>('unit').value as Profile['unit'];
  profile.scale = Number(el<HTMLInputElement>('scale').value);
  profile.hero_seat = Number(el<HTMLInputElement>('hero-seat').value);
  return validateProfile(profile);
}
function fillFields() {
  el<HTMLSelectElement>('unit').value = profile.unit;
  el<HTMLInputElement>('scale').value = String(profile.scale);
  el<HTMLInputElement>('hero-seat').value = String(profile.hero_seat);
  regionOptions();
}
el('save-profile').onclick = () => {
  try {
    localStorage.setItem('table-vision-profile-v1', JSON.stringify(readFields()));
    status('Профиль сохранён в этом браузере.');
  } catch (e) {
    status(String(e));
  }
};
el('export-profile').onclick = () => {
  try {
    download('table-vision-profile.json', readFields());
  } catch (e) {
    status(String(e));
  }
};
el<HTMLInputElement>('import-profile').onchange = async (e) => {
  const file = (e.target as HTMLInputElement).files?.[0];
  if (!file || file.size > 100000) return;
  try {
    const imported = validateProfile(JSON.parse(await file.text()));
    if (controller || starting || switching) return;
    profile = {
      ...imported,
      width: profile.width,
      height: profile.height,
      calibration_id: crypto.randomUUID(),
    };
    fillFields();
    status('Профиль импортирован. Проверьте области на текущем кадре.');
  } catch {
    status('Некорректный файл профиля.');
  }
};
el('start').onclick = async () => {
  if (controller || starting || switching) return;
  starting = true;
  updateButtons(false);
  try {
    profile = readFields();
    const token = el<HTMLInputElement>('token').value;
    if (!token) throw new Error('Введите RECEIVER_TOKEN вашего сервера.');
    const fixedSource = source === 'demo' ? demo : video;
    const instance = new SessionController(
      () => fixedSource,
      structuredClone(profile),
      {
        detection: (d) => {
          el('detector-ms').textContent = `${d.elapsedMs.toFixed(1)} мс`;
          el('delta').textContent = `${(d.score * 100).toFixed(1)}%`;
          el('revision').textContent = String(d.revision);
          if (d.candidate)
            log(`Кадр: ${d.candidate} · ${d.stable ? 'стабильный' : 'движение не завершилось'}`);
        },
        invalidated: invalidate,
        error: status,
        stopped: () => {
          if (controller === instance) {
            controller = null;
            updateButtons(false);
          }
        },
        recorded: (r) => {
          lastRecording = r;
          updateButtons(Boolean(controller));
        },
        state: (r) => {
          currentStatus = r.state.status;
          observedAt = r.state.observed_at;
          el('requests').textContent = String(r.metrics.requests);
          el('state-badge').textContent =
            (
              {
                confirmed: 'Подтверждено',
                uncertain: 'Не подтверждено',
                stale: 'Устарело',
                unknown: 'Неизвестно',
              } as Record<string, string>
            )[currentStatus] ?? currentStatus;
          el('state-badge').className =
            'badge ' + (currentStatus === 'confirmed' ? 'green' : 'amber');
          el('json').textContent = JSON.stringify(r.state, null, 2);
          el('json').hidden = false;
          el('state-empty').hidden = true;
        },
      },
      el<HTMLInputElement>('diagnostic').checked,
    );
    controller = instance;
    await instance.start(token);
    if (controller === instance) {
      updateButtons(true);
      status('Наблюдение запущено. Сравнение кадров — в браузере.');
    }
  } catch (e) {
    status(e instanceof Error ? e.message : 'Ошибка запуска');
    await stop();
  } finally {
    starting = false;
    updateButtons(Boolean(controller));
  }
};
el('stop').onclick = () => void stop();
el('manual').onclick = () => controller?.force();
el('recording').onclick = async () => {
  try {
    if (controller) download('table-vision-recording.json', await controller.recording());
    else if (lastRecording) download('table-vision-recording.json', lastRecording);
  } catch {
    status('Не удалось скачать запись.');
  }
};
document.addEventListener('visibilitychange', () => {
  if (document.hidden && controller) {
    void stop();
    status('Наблюдение остановлено: вкладка скрыта.');
  }
});
window.addEventListener('pagehide', () => {
  void stop();
  camera.stop();
});
setInterval(() => {
  el('age').textContent =
    observedAt === null ? '—' : `${Math.max(0, (Date.now() - observedAt) / 1000).toFixed(1)} с`;
  if (observedAt !== null && Date.now() - observedAt > 5000 && currentStatus === 'confirmed') {
    currentStatus = 'stale';
    el('state-badge').textContent = 'Устарело';
    el('state-badge').className = 'badge amber';
  }
}, 250);
fillFields();
resize(1280, 720);
new VisionAPI()
  .status()
  .then((s) => {
    el('connection').textContent = 'Сервер подключён';
    el('connection-dot').className = 'online';
    el('mode').textContent = s.mode === 'live' ? 'LIVE · ' + s.model : 'MOCK · без распознавания';
    if (s.mode === 'mock') {
      el<HTMLInputElement>('token').placeholder = 'Токен из .env (локально: change-me-local-only)';
      status(
        'Mock-режим: проверяем поток и детектор. Для распознавания настройте сервер в live-режиме.',
      );
    }
  })
  .catch(() => {
    el('connection').textContent = 'Нет связи с сервером';
    status('Запустите сервис vision на порту 8001.');
  });
