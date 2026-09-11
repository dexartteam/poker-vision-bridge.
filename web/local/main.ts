import './style.css';
import { readTemplateSet, type CardTemplate } from './cards';
import { Camera } from '../capture/camera';
import { LocalController } from './controller';
import {
  openPokerProfile,
  validateLocalProfile,
  REGION_NAMES,
  type LocalObservation,
  type LocalProfile,
  type RegionName,
} from './contracts';

document.querySelector('#app')!.innerHTML = `
<main><header><a class="brand" href="/">TABLE VISION <span>LOCAL</span></a><a href="/server.html">Серверное распознавание ↗</a></header>
<section class="heading"><div><p class="eyebrow">НАБЛЮДЕНИЕ ЗА СТОЛОМ</p><h1>Распознавание в браузере</h1><p>Камера или запись. Кадры остаются на этом устройстве.</p></div><span class="badge">БЕЗ КЛЮЧА API</span></section>
<div class="workspace"><section class="panel input"><div class="panel-title"><h2>Видеовход</h2><span id="dimensions">Источник не выбран</span></div>
<div class="stage"><div id="placeholder"><b>Откройте запись или включите камеру</b><p>Для записи Open Poker с телефона уже подготовлены области чтения.</p></div><div id="preview" hidden><video id="video" muted playsinline></video><canvas id="overlay"></canvas></div></div>
<div class="toolbar"><label class="button primary">Открыть видео<input id="file" type="file" accept="video/*" hidden></label><button id="camera">Включить камеру</button><button id="play" disabled>Воспроизвести</button></div>
<div id="timeline" hidden><label for="seek">Позиция записи <span id="position">0:00</span></label><input id="seek" type="range" min="0" max="1" step="0.1" value="0"></div>
<p id="source-note" class="hint">Поддержка формата видео зависит от браузера. При ошибке используйте MP4 (H.264).</p>
<div class="metrics"><div><span>Детектор</span><b id="detector-ms">—</b></div><div><span>Изменение</span><b id="delta">—</b></div><div><span>Распознаваний</span><b id="count">0</b></div><div><span>Время OCR и карт</span><b id="elapsed">—</b></div></div></section>
<section class="panel output"><div class="panel-title"><h2>Прочитанные поля</h2><span id="state-badge" class="badge">Ожидание</span></div>
<div class="facts"><div><span>Раздача</span><strong id="hand">—</strong></div><div><span>Этап</span><strong id="street">—</strong></div><div><span>Банк, фишки</span><strong id="pot">—</strong></div><div><span>Режим в кадре</span><strong id="view-mode">—</strong></div></div>
<div class="board-block"><span>Общие карты</span><div id="cards" class="cards"></div></div><p id="evidence" class="hint">Результаты появятся после запуска наблюдения.</p>
<details><summary>JSON состояния</summary><pre id="json">{}</pre></details>
<div class="output-footer">Стеки, места и карты героя пока не распознаются. Сервис решений не подключён.</div></section></div>
<div class="settings"><section class="panel"><div class="panel-title"><h2>Области чтения</h2><label><input id="show" type="checkbox" checked> Показать</label></div>
<p class="hint">Выберите поле и обведите его на приостановленном видео. Банк — вся панель с POT, число банка — только цифры. Общие карты должны целиком помещаться в области.</p>
<div class="toolbar"><select id="region" aria-label="Область чтения"></select><button id="preset">Профиль этой записи</button></div>
<div class="toolbar"><button id="save-profile">Сохранить профиль</button><button id="export-profile">Экспорт</button><label class="button">Импорт<input id="import-profile" type="file" accept="application/json" hidden></label></div>
<label class="check"><input id="calibrated" type="checkbox"> Области совпадают с полями на моём видео</label>
<div class="toolbar"><label class="button">Загрузить шаблоны карт<input id="import-cards" type="file" accept="application/json" hidden></label></div><p id="card-model" class="hint">Шаблоны не загружены. Читаем текстовые поля; карты пока неизвестны.</p></section>
<section class="panel"><div class="panel-title"><h2>Наблюдение</h2><span class="badge">ЛОКАЛЬНО</span></div>
<p class="hint">Проверяем изменения 5 раз в секунду. Распознаём выбранные кадры с интервалом не меньше секунды; неподвижный стол перепроверяем каждые 2 секунды.</p>
<div class="toolbar"><button id="start" class="primary" disabled>Начать наблюдение</button><button id="stop" disabled>Стоп</button><button id="manual" disabled>Новый кадр</button></div>
<button id="export-state" disabled>Скачать наблюдения JSON</button><p id="status" role="status">Выберите видеовход.</p>
<p class="hint">Запись остаётся записью даже с надписью LIVE. Пауза, перемотка и скрытие вкладки останавливают наблюдение.</p></section></div>
<footer>TABLE VISION · Частичное наблюдение · Никаких игровых команд</footer></main>`;

const el = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const video = el<HTMLVideoElement>('video'),
  overlay = el<HTMLCanvasElement>('overlay');
const camera = new Camera();
let controller: LocalController | null = null;
let cardTemplates: CardTemplate[] = [];
let profile: LocalProfile = openPokerProfile();
let kind: 'camera' | 'recording' = 'recording',
  url = '',
  loaded = false,
  busy = false;
let launchGeneration = 0,
  initializing: number | null = null;
let latest: LocalObservation | null = null,
  history: unknown = null;
let drag: { x: number; y: number } | null = null;
const labels: Record<RegionName, string> = {
  header: 'Номер и этап раздачи',
  mode: 'Метка LIVE / REPLAY',
  pot: 'Банк — панель POT',
  amount: 'Число банка',
  board: 'Общие карты',
};
const status = (text: string) => {
  el('status').textContent = text;
};
const download = (name: string, data: unknown) => {
  const link = document.createElement('a'),
    href = URL.createObjectURL(
      new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }),
    );
  link.href = href;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
};
function buttons() {
  el<HTMLButtonElement>('start').disabled =
    !loaded || busy || !!controller || !el<HTMLInputElement>('calibrated').checked;
  for (const id of ['stop', 'manual']) el<HTMLButtonElement>(id).disabled = !controller;
  for (const id of [
    'file',
    'camera',
    'preset',
    'region',
    'import-profile',
    'import-cards',
    'calibrated',
  ])
    (el(id) as HTMLInputElement).disabled = busy || !!controller;
  el<HTMLButtonElement>('play').disabled = !loaded || busy;
  el<HTMLInputElement>('seek').disabled = !loaded || busy;
  el<HTMLButtonElement>('export-state').disabled = !history && !controller;
}
function invalidate() {
  latest = null;
  for (const id of ['hand', 'street', 'pot', 'view-mode']) el(id).textContent = '—';
  el('json').textContent = '{}';
  el('state-badge').textContent = 'Ждём кадр';
  el('evidence').textContent = 'Предыдущий результат больше не считается актуальным.';
  renderCards([]);
}
function renderCards(cards: LocalObservation['board']) {
  el('cards').replaceChildren(
    ...Array.from({ length: 5 }, (_, i) => {
      const node = document.createElement('span'),
        card = cards[i];
      node.className = 'card';
      if (card?.status === 'visible' && card.value) {
        const suits: Record<string, string> = { c: '♣', d: '♦', h: '♥', s: '♠' };
        node.textContent = card.value[0] + suits[card.value[1]];
        if ('dh'.includes(card.value[1])) node.classList.add('red');
      } else {
        node.textContent = card?.status === 'empty' ? '—' : '?';
        node.classList.add('unknown');
      }
      return node;
    }),
  );
}
function display(o: LocalObservation) {
  latest = structuredClone(o);
  el('hand').textContent = o.hand_number === null ? '—' : `#${o.hand_number}`;
  el('street').textContent = {
    preflop: 'Префлоп',
    flop: 'Флоп',
    turn: 'Тёрн',
    river: 'Ривер',
    showdown: 'Вскрытие',
    unknown: '—',
  }[o.ui_street];
  el('pot').textContent = o.pot_display.value?.toLocaleString('ru') ?? '—';
  el('view-mode').textContent = o.ui_mode.toUpperCase();
  el('state-badge').textContent = o.ui_mode === 'replay' ? 'Повтор раздачи' : 'Частично';
  el('elapsed').textContent = `${o.diagnostics.elapsed_ms} мс`;
  el('evidence').textContent =
    `${o.source.kind === 'recording' ? 'Из записи' : 'С камеры'} · ${o.evidence === 'repeated' ? 'чтение полей повторилось' : o.evidence === 'moving' ? 'кадр ещё движется' : 'первое чтение полей'} · полное состояние не подтверждено`;
  el('json').textContent = JSON.stringify(o, null, 2);
  renderCards(o.board);
}
async function stop() {
  if (initializing !== null) {
    launchGeneration++;
    initializing = null;
    busy = false;
  }
  const old = controller;
  if (old) {
    history = old.recording();
    controller = null;
    await old.stop();
  }
  buttons();
}
function drawOverlay() {
  const ctx = overlay.getContext('2d')!;
  ctx.clearRect(0, 0, overlay.width, overlay.height);
  if (!el<HTMLInputElement>('show').checked) return;
  for (const [name, r] of Object.entries(profile.regions)) {
    ctx.strokeStyle = name === el<HTMLSelectElement>('region').value ? '#d5f7b4' : '#70aaa0';
    ctx.lineWidth = Math.max(2, overlay.width / 350);
    ctx.strokeRect(
      r.x * overlay.width,
      r.y * overlay.height,
      r.w * overlay.width,
      r.h * overlay.height,
    );
    ctx.fillStyle = ctx.strokeStyle;
    ctx.font = `${Math.max(16, overlay.width / 55)}px sans-serif`;
    ctx.fillText(labels[name as RegionName], r.x * overlay.width + 4, r.y * overlay.height - 6);
  }
}
function sourceReady() {
  if (!video.videoWidth || !video.videoHeight) throw new Error('Видео ещё не декодировано');
  loaded = true;
  overlay.width = video.videoWidth;
  overlay.height = video.videoHeight;
  el('preview').hidden = false;
  el('placeholder').hidden = true;
  el('preview').style.aspectRatio = `${video.videoWidth} / ${video.videoHeight}`;
  el('preview').style.width = `min(100%, ${(video.videoWidth / video.videoHeight) * 68}vh)`;
  if (profile.width !== video.videoWidth || profile.height !== video.videoHeight) {
    profile = openPokerProfile(video.videoWidth, video.videoHeight);
  }
  profile.id = crypto.randomUUID();
  el<HTMLInputElement>('calibrated').checked = false;
  el('dimensions').textContent = `${video.videoWidth} × ${video.videoHeight}`;
  el('timeline').hidden = kind !== 'recording';
  el('source-note').textContent =
    kind === 'recording'
      ? 'Локальная запись. Проверьте области и начните наблюдение.'
      : 'Камера включена. Обведите поля на своём изображении и подтвердите калибровку.';
  el<HTMLInputElement>('seek').max = String(Number.isFinite(video.duration) ? video.duration : 1);
  drawOverlay();
  buttons();
}
async function resetSource() {
  await stop();
  camera.stop();
  video.pause();
  video.srcObject = null;
  video.removeAttribute('src');
  if (url) URL.revokeObjectURL(url);
  url = '';
  loaded = false;
  invalidate();
  el('camera').textContent = 'Включить камеру';
  el('preview').hidden = true;
  el('placeholder').hidden = false;
}
el<HTMLInputElement>('file').onchange = async (e) => {
  const file = (e.target as HTMLInputElement).files?.[0];
  if (!file || busy) return;
  busy = true;
  buttons();
  try {
    await resetSource();
    kind = 'recording';
    url = URL.createObjectURL(file);
    video.src = url;
    await video.play();
    video.pause();
    sourceReady();
    status('Видео открыто. Проверьте области чтения.');
  } catch {
    status('Браузер не смог открыть это видео. Попробуйте MP4 (H.264).');
  } finally {
    busy = false;
    buttons();
  }
};
el('camera').onclick = async () => {
  if (busy) return;
  busy = true;
  buttons();
  try {
    const wasCamera = kind === 'camera' && !!video.srcObject;
    await resetSource();
    if (wasCamera) {
      status('Камера выключена.');
      return;
    }
    kind = 'camera';
    await camera.start(video);
    sourceReady();
    el('camera').textContent = 'Выключить камеру';
    status('Камера включена. Настройте области чтения.');
  } catch (e) {
    status(e instanceof Error ? e.message : 'Не удалось включить камеру');
  } finally {
    busy = false;
    buttons();
  }
};
el('play').onclick = async () => {
  if (video.paused) {
    try {
      await video.play();
    } catch {
      status('Не удалось начать воспроизведение');
    }
  } else {
    await stop();
    video.pause();
  }
};
video.onplay = () => {
  el('play').textContent = 'Пауза';
};
video.onpause = () => {
  el('play').textContent = 'Воспроизвести';
};
video.onended = () => {
  status('Запись завершена. Наблюдения доступны для скачивания.');
};
video.onerror = () => {
  status('Ошибка видеовхода');
};
video.ontimeupdate = () => {
  el<HTMLInputElement>('seek').value = String(video.currentTime);
  el('position').textContent =
    `${Math.floor(video.currentTime / 60)}:${String(Math.floor(video.currentTime % 60)).padStart(2, '0')}`;
};
el<HTMLInputElement>('seek').oninput = async (e) => {
  const at = Number((e.target as HTMLInputElement).value);
  await stop();
  video.pause();
  video.currentTime = at;
  invalidate();
};
el('start').onclick = async () => {
  if (busy || controller || !loaded || !el<HTMLInputElement>('calibrated').checked) return;
  const generation = ++launchGeneration;
  initializing = generation;
  busy = true;
  buttons();
  status('Загружаются локальный OCR и языковая модель…');
  try {
    const instance = new LocalController(
      video,
      profile,
      kind,
      {
        state: display,
        invalidated: invalidate,
        error: status,
        count: (n) => {
          el('count').textContent = String(n);
        },
        detection: (d) => {
          el('detector-ms').textContent = `${d.elapsedMs.toFixed(1)} мс`;
          el('delta').textContent = `${(d.score * 100).toFixed(1)}%`;
        },
        stopped: () => {
          if (controller !== instance) return;
          history = instance.recording();
          controller = null;
          if (initializing === generation) {
            launchGeneration++;
            initializing = null;
            busy = false;
          }
          buttons();
        },
      },
      cardTemplates,
    );
    controller = instance;
    buttons();
    // Initialize while a recording is paused so no opening cards are skipped.
    await instance.start();
    if (generation === launchGeneration && controller === instance) {
      await video.play();
      if (generation === launchGeneration && controller === instance)
        status('Локальное наблюдение запущено.');
    }
  } catch (e) {
    if (generation === launchGeneration) {
      status(e instanceof Error ? e.message : 'Ошибка запуска OCR');
      await stop();
    }
  } finally {
    if (generation === launchGeneration) {
      initializing = null;
      busy = false;
      buttons();
    }
  }
};
el('stop').onclick = async () => {
  await stop();
  status('Наблюдение остановлено.');
};
el('manual').onclick = () => controller?.force();
el('export-state').onclick = () =>
  download('table-vision-local-observations.json', controller?.recording() ?? history);
el<HTMLSelectElement>('region').replaceChildren(
  ...REGION_NAMES.map((name) => new Option(labels[name], name)),
);
el('region').onchange = drawOverlay;
el('show').onchange = drawOverlay;
el('calibrated').onchange = buttons;
el('preset').onclick = () => {
  profile = openPokerProfile(profile.width, profile.height);
  el<HTMLInputElement>('calibrated').checked = false;
  drawOverlay();
  buttons();
  status('Профиль сброшен. Он рассчитан на запись 1206 × 2622; проверьте совмещение.');
};
el('save-profile').onclick = () => {
  try {
    localStorage.setItem(
      'table-vision-local-profile-v1',
      JSON.stringify(validateLocalProfile(profile)),
    );
    status('Профиль сохранён в браузере.');
  } catch {
    status('Не удалось сохранить профиль. Используйте экспорт.');
  }
};
el('export-profile').onclick = () => download('table-vision-local-profile.json', profile);
el<HTMLInputElement>('import-profile').onchange = async (e) => {
  const file = (e.target as HTMLInputElement).files?.[0];
  if (!file || file.size > 100000 || controller || busy) return;
  try {
    const imported = validateLocalProfile(JSON.parse(await file.text()));
    if (controller || busy) return;
    if (loaded && (imported.width !== video.videoWidth || imported.height !== video.videoHeight))
      throw new Error('Размер профиля не совпадает с видеовходом');
    profile = imported;
    profile.id = crypto.randomUUID();
    el<HTMLInputElement>('calibrated').checked = false;
    drawOverlay();
    buttons();
    status('Профиль импортирован. Проверьте совмещение областей.');
  } catch (e) {
    status(e instanceof Error ? e.message : 'Некорректный профиль');
  }
};
el<HTMLInputElement>('import-cards').onchange = async (e) => {
  const file = (e.target as HTMLInputElement).files?.[0];
  if (!file || busy || controller) return;
  if (file.size > 500000) {
    status('Слишком большой файл шаблонов');
    return;
  }
  try {
    const templates = readTemplateSet(JSON.parse(await file.text()));
    if (busy || controller) return;
    cardTemplates = templates;
    invalidate();
    el('card-model').textContent =
      `Загружено ${templates.length} шаблонов. Качество на других картах и ракурсах ещё не проверено.`;
    status('Шаблоны карт загружены на это устройство. Можно начать наблюдение.');
  } catch (error) {
    status(error instanceof Error ? error.message : 'Не удалось прочитать шаблоны');
  }
};
function point(e: PointerEvent) {
  const r = overlay.getBoundingClientRect();
  return {
    x: Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)),
    y: Math.max(0, Math.min(1, (e.clientY - r.top) / r.height)),
  };
}
overlay.onpointerdown = (e) => {
  if (!loaded || busy || controller) return;
  drag = point(e);
  overlay.setPointerCapture(e.pointerId);
};
overlay.onpointerup = (e) => {
  if (!drag || busy || controller) return;
  const a = drag,
    b = point(e);
  drag = null;
  if (Math.abs(a.x - b.x) < 0.005 || Math.abs(a.y - b.y) < 0.005) return;
  profile.regions[el<HTMLSelectElement>('region').value as RegionName] = {
    x: Math.min(a.x, b.x),
    y: Math.min(a.y, b.y),
    w: Math.abs(a.x - b.x),
    h: Math.abs(a.y - b.y),
  };
  profile.id = crypto.randomUUID();
  el<HTMLInputElement>('calibrated').checked = false;
  drawOverlay();
  buttons();
};
overlay.onpointercancel = () => {
  drag = null;
};
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    void stop();
    status('Вкладка скрыта. Наблюдение остановлено.');
  }
});
window.addEventListener('pagehide', () => {
  void stop();
  camera.stop();
  if (url) URL.revokeObjectURL(url);
});
setInterval(() => {
  if (latest && Date.now() - latest.source.captured_at > 5000) {
    latest.status = 'stale';
    el('state-badge').textContent = 'Устарело';
    el('json').textContent = JSON.stringify(latest, null, 2);
    for (const id of ['hand', 'street', 'pot', 'view-mode']) el(id).textContent = '—';
    renderCards([]);
  }
}, 250);
try {
  const saved = localStorage.getItem('table-vision-local-profile-v1');
  if (saved) profile = validateLocalProfile(JSON.parse(saved));
} catch {
  /* Explicit recalibration remains required. */
}
renderCards([]);
buttons();
