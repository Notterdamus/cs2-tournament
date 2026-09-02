# -*- coding: utf-8 -*-
"""
Обновление данных турнира по CS2 (FastCup).

Что делает:
  1. Читает tournament.json (команды, настройки, расписание).
  2. Если круговой этап (roundRobin) пуст — генерирует пары "каждый с каждым"
     и дописывает их обратно в tournament.json.
  3. Для каждого матча, где указан matchId / matchIds, забирает счёт и
     статистику игроков со страницы cs2.fastcup.net (через браузер Playwright).
     Результаты кешируются в папке cache/ — повторный запуск не тянет заново.
  4. Считает турнирную таблицу, посев в плей-офф и агрегированную статистику
     игроков, пишет всё в dashboard-data.js.
  5. Открой index.html в браузере, чтобы увидеть результат.

Запуск:
    python update.py                 # сгенерировать расписание + собрать данные (со скрапингом)
    python update.py --no-scrape      # только пересчитать таблицу без обращения к сайту
    python update.py --refresh 27053086   # игнорировать кеш для этого матча и скачать заново
    python update.py --headful        # показать окно браузера (если скрапинг падает)
    python update.py --watch          # онлайн-режим: сам ловит новые матчи по профилям капитанов
    python update.py --watch --push   # то же + git commit/push (публикация на GitHub Pages)
    python update.py --once           # один проход автоотслеживания и выход
    python update.py --live           # live-счёт идущего матча (сам ищет или укажи ссылку)
    python update.py --edit           # ручной ввод счёта (меню) — форс-мажор
    python update.py --set rr-3 13 7  # задать счёт клетке; --clear rr-3 — сбросить

Автоотслеживание: включи блок "autoTrack" в tournament.json и впиши slug'и
капитанов — скрипт сам найдёт сыгранные матчи турнира и проставит счёт.

Требуется один раз:
    pip install playwright
    python -m playwright install chromium
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

import os
DEBUG = bool(os.environ.get("FC_DEBUG"))

ROOT = Path(__file__).parent
CONF = ROOT / "tournament.json"
CACHE = ROOT / "cache"
OUT = ROOT / "dashboard-data.js"

STATS_URL = "https://cs2.fastcup.net/matches/{id}/stats?hl=ru"

NAV_PATHS = {
    "/matches", "/leagues", "/tournaments", "/users", "/teams", "/streams",
    "/highlights", "/faq", "/rules", "/premium", "/contacts", "/terms", "/",
    "/login", "/missions", "/drops", "/activity", "/stats", "/stream",
}

# Порядок колонок в таблице статистики матча 5x5 на FastCup:
# 0 K | 1 D | 2 A | 3 +/- | 4 K/D | 5 ADR | 6 HS% | 7 SICK-FRAGS
# 8 OS | 9 NS | 10 AS | 11 WB | 12 FK(entry) | 13 FD
# 14 1v5 | 15 1v4 | 16 1v3 | 17 1v2 | 18 1v1 | 19 5K | 20 4K | 21 3K | 22 2K | 23 Rating
ROW_LEN = 24


# --------------------------------------------------------------------------- #
#  Работа с конфигом
# --------------------------------------------------------------------------- #
def load_conf():
    with open(CONF, encoding="utf-8") as f:
        return json.load(f)


def save_conf(conf):
    with open(CONF, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)


def round_robin_pairs(team_ids, double=False):
    """Круговая система (метод «карусели»). Возвращает список туров."""
    ids = list(team_ids)
    if len(ids) < 2:
        return []
    if len(ids) % 2:
        ids.append(None)  # BYE
    n = len(ids)
    rounds = []
    arr = ids[:]
    for r in range(n - 1):
        pairs = []
        for i in range(n // 2):
            a, b = arr[i], arr[n - 1 - i]
            if a is not None and b is not None:
                # чередуем хозяев для равномерности
                pairs.append((a, b) if (r + i) % 2 == 0 else (b, a))
        rounds.append(pairs)
        arr = [arr[0]] + [arr[-1]] + arr[1:-1]  # поворот
    if double:
        base = [list(x) for x in rounds]
        for r in base:
            rounds.append([(b, a) for (a, b) in r])
    return rounds


def ensure_schedule(conf, force=False):
    rr = conf["schedule"].get("roundRobin") or []
    team_ids = [t["id"] for t in conf["teams"]]
    tset = set(team_ids)

    if rr and not force:
        used = set()
        for fx in rr:
            used.add(fx.get("home"))
            used.add(fx.get("away"))
        has_results = any(fx.get("matchId") or fx.get("matchIds") or fx.get("manualScore")
                          for fx in rr)
        # состав команд изменился и результатов ещё нет — можно пересобрать расписание
        if used == tset or has_results:
            if used != tset and has_results:
                print("[schedule] ВНИМАНИЕ: список команд не совпадает с расписанием, "
                      "но результаты уже введены — расписание не трогаю. "
                      "Очисти \"roundRobin\": [] вручную, если нужно пересоздать.")
            return False
        print("[schedule] Состав команд изменился — пересоздаю круговой этап.")

    double = conf["format"].get("doubleRoundRobin", False)
    rounds = round_robin_pairs(team_ids, double)
    generated = []
    k = 1
    for ri, pairs in enumerate(rounds, 1):
        for (home, away) in pairs:
            generated.append({
                "id": f"rr-{k}",
                "round": ri,
                "home": home,
                "away": away,
                "matchId": None,
                "matchIds": None,
                "manualScore": None,
            })
            k += 1
    conf["schedule"]["roundRobin"] = generated
    save_conf(conf)
    print(f"[schedule] Сгенерировано {len(generated)} матчей кругового этапа "
          f"в {len(rounds)} турах. Файл tournament.json обновлён.")
    return True


# --------------------------------------------------------------------------- #
#  Скрапинг FastCup
# --------------------------------------------------------------------------- #
PAGE_JS = r"""
() => {
  const NAV = new Set(%NAV%);
  const anchors = [...document.querySelectorAll('a[href]')]
    .filter(a => { const h = a.getAttribute('href') || '';
      return /^\/[A-Za-z0-9_.\-]+$/.test(h) && !NAV.has(h); });
  const seen = new Set(); const hdr = [];
  for (const a of anchors) {
    const slug = a.getAttribute('href').slice(1);
    if (seen.has(slug)) continue;
    seen.add(slug);
    const nick = (a.innerText || '').trim().split('\n')
      .map(s => s.trim()).filter(Boolean).pop() || slug;
    hdr.push({ slug, nick });
  }
  const players = hdr.slice(0, 10);

  const body = document.body.innerText;
  const m = body.match(/(\d{1,2})\s*:\s*(\d{1,2})/);
  const score = m ? [parseInt(m[1], 10), parseInt(m[2], 10)] : null;
  const MAPS = ['Mirage','Inferno','Nuke','Ancient','Anubis','Overpass',
                'Vertigo','Train','Cache','Dust II','Dust2'];
  const map = MAPS.find(x => body.includes(x)) || null;
  const finished = /Finished|Заверш|Окончен/i.test(body);

  // --- таблицы статистики (у каждой команды своя) ---
  const leavesOf = el => [...el.querySelectorAll('*')]
    .filter(x => x.children.length === 0 && x.textContent.trim() !== '')
    .map(x => x.textContent.trim());
  const ratingLeaves = [...document.querySelectorAll('*')]
    .filter(e => e.children.length === 0 && e.textContent.trim() === 'Rating');
  const tables = []; const usedContainers = new Set();
  for (const rl of ratingLeaves) {
    let hdrRow = rl;
    for (let i = 0; i < 12 && hdrRow; i++) {
      hdrRow = hdrRow.parentElement;
      if (!hdrRow) break;
      const lv = leavesOf(hdrRow);
      if (lv[0] === 'K' && lv.indexOf('Rating') > 10 && lv.length <= 40) break;
    }
    if (!hdrRow || !hdrRow.parentElement) continue;
    const container = hdrRow.parentElement;
    if (usedContainers.has(container)) continue;
    usedContainers.add(container);
    const rows = [];
    for (const r of container.children) {
      const lv = leavesOf(r);
      if (lv.length < 18) continue;
      if (lv[0] === 'K') continue;               // строка-заголовок
      rows.push({ nick: lv[0], vals: lv.slice(1) });
    }
    if (rows.length) tables.push(rows);
  }
  return { players, score, map, finished, tables, body };
}
""".replace("%NAV%", json.dumps(sorted(NAV_PATHS)))


OVERVIEW_JS = r"""
() => {
  const body = document.body.innerText;
  const MAPS = ['Mirage','Inferno','Nuke','Ancient','Anubis','Overpass',
                'Vertigo','Train','Cache','Dust II','Dust2'];
  const map = MAPS.find(x => new RegExp('(^|\\n)' + x + '(\\n|$)').test(body))
              || MAPS.find(x => body.includes(x)) || null;
  const dm = body.match(/(\d{1,2}\s+[A-Za-z]{3,}\s+at\s+\d{1,2}:\d{2})/);
  return { map, dateText: dm ? dm[1] : null };
}
"""


# лёгкое чтение идущего матча со страницы статистики
LIVE_JS = r"""
() => {
  const NAV = new Set(%NAV%);
  const seen = new Set(); const players = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const h = a.getAttribute('href') || '';
    if (!/^\/[A-Za-z0-9_.\-]+$/.test(h) || NAV.has(h)) continue;
    const slug = h.slice(1); if (seen.has(slug)) continue; seen.add(slug);
    const nick = (a.innerText || '').trim().split('\n').map(s=>s.trim()).filter(Boolean).pop() || slug;
    players.push({ slug, nick });
  }
  const body = document.body.innerText;
  const m = body.match(/(\d{1,2})\s*:\s*(\d{1,2})/);
  const MAPS = ['Mirage','Inferno','Nuke','Ancient','Anubis','Overpass',
                'Vertigo','Train','Cache','Dust II','Dust2'];
  const map = MAPS.find(x => body.includes(x)) || null;
  let status = 'live';
  if (/Finished|Заверш|Оконч/i.test(body)) status = 'finished';
  else if (/Waiting for players|Voting|Veto|Pick.?ban|Ожидание|Голосов|Ban \/ Ban/i.test(body)
           && !m) status = 'pending';
  return { score: m ? [parseInt(m[1],10), parseInt(m[2],10)] : null,
           players: players.slice(0,10), map, status,
           hasTable: /Sick-frags|SICK-FRAGS/i.test(body) };
}
""".replace("%NAV%", json.dumps(sorted(NAV_PATHS)))


# профиль игрока: /<slug>/matches  — плоский список
PROFILE_JS = r"""
() => ({ body: document.body.innerText,
         ids: [...document.querySelectorAll('a[href]')]
                .map(a => a.getAttribute('href') || '')
                .filter(h => /^\/matches\/\d+$/.test(h))
                .map(h => h.split('/')[2]) })
"""


# главная страница профиля /<slug> — ищем текущий (идущий) матч
PROFILE_MAIN_JS = r"""
() => {
  const head = document.body.innerText.slice(0, 400);
  const online = /Online|Онлайн|В сети|In match|In game|В матче|Playing|Играет/i.test(head);
  const ids = [...document.querySelectorAll('a[href]')]
    .map(a => a.getAttribute('href') || '')
    .filter(h => /^\/matches\/\d+$/.test(h))
    .map(h => h.split('/')[2]);
  return { online, ids: [...new Set(ids)], head };
}
"""


def _isnum(s):
    if not s:
        return False
    s = s.replace("%", "").replace(",", ".")
    if s.startswith("-"):
        s = s[1:]
    return s.replace(".", "", 1).isdigit()


def parse_rows(body, nicks):
    lines = [l.strip() for l in body.split("\n")]
    nickset = set(nicks)
    out = {}
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln in nickset and ln not in out:
            j = i + 1
            toks = []
            while j < len(lines) and lines[j] not in nickset:
                if lines[j] == "":
                    j += 1
                    continue
                if not _isnum(lines[j]):
                    break
                toks.append(lines[j])
                j += 1
            if len(toks) >= 8:
                out[ln] = toks[:ROW_LEN]
                i = j
                continue
        i += 1
    return out


def num(s):
    s = s.replace("%", "").replace(",", ".")
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except ValueError:
        return 0


def row_stats(vals):
    """vals — список значений строки статистики игрока (без ника).
    Порядок колонок FastCup 5x5:
      0-7   K D A +/- K/D ADR HS% Sick-frags
      8-11  OS NS AS WB
      12-13 FK(вход) FD
      14-18 клатчи 1v5 1v4 1v3 1v2 1v1
      19..  мультикиллы (…4K 3K 2K 1K), последнее значение — Rating
    """
    g = lambda i: num(vals[i]) if 0 <= i < len(vals) else 0
    one_k_idx = len(vals) - 2                       # «1K» — предпоследнее
    multikills = sum(g(i) for i in range(19, max(19, one_k_idx)))
    return {
        "k": g(0), "d": g(1), "a": g(2), "plusminus": g(3),
        "kd": g(4), "adr": g(5), "hs": g(6), "sick": g(7),
        "entry": g(12),
        "clutches": g(14) + g(15) + g(16) + g(17) + g(18),
        "multikills": max(0, multikills),
        "onekills": g(one_k_idx),
        "rating": num(vals[-1]) if vals else 0,
    }


def scrape_match(page, match_id):
    url = STATS_URL.format(id=match_id)
    print(f"  → {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    # ждём прохождения проверки Cloudflare и отрисовки таблиц статистики
    data = None
    for attempt in range(25):
        page.wait_for_timeout(1200)
        try:
            data = page.evaluate(PAGE_JS)
        except Exception:
            continue
        if not (data.get("score") and len(data.get("players", [])) >= 2):
            continue
        stat_rows = sum(len(t) for t in data.get("tables", []))
        if stat_rows >= max(2, len(data["players"]) - 1):
            break
    if not data or not data.get("score"):
        raise RuntimeError(f"матч {match_id}: не удалось прочитать счёт "
                           f"(матч ещё не сыгран или страница не загрузилась)")

    nicks = [p["nick"] for p in data["players"]]

    # собираем все строки статистики в словарь nick -> vals
    row_by_nick = {}
    for tbl in data.get("tables", []):
        for r in tbl:
            row_by_nick.setdefault(r["nick"], r["vals"])
    # запасной вариант: парсинг из плоского текста
    if len(row_by_nick) < len(nicks):
        for n, toks in parse_rows(data["body"], nicks).items():
            row_by_nick.setdefault(n, toks)

    if DEBUG:
        for n in nicks:
            print(f"    DBG {n}: {row_by_nick.get(n)}")
    if not row_by_nick:
        print(f"  [!] матч {match_id}: счёт есть ({data['score']}), "
              f"но таблицы статистики не прочитались")

    players = []
    for idx, p in enumerate(data["players"]):
        vals = row_by_nick.get(p["nick"])
        side = 0 if idx < len(data["players"]) / 2 else 1
        rec = {"slug": p["slug"], "nick": p["nick"], "side": side}
        if vals:
            rec.update(row_stats(vals))
        players.append(rec)

    # карта и дата — со страницы обзора матча
    game_map = data.get("map")
    match_date = None
    try:
        page.goto(f"https://cs2.fastcup.net/matches/{match_id}?hl=en",
                  wait_until="domcontentloaded", timeout=45000)
        for _ in range(12):
            page.wait_for_timeout(1000)
            ov = page.evaluate(OVERVIEW_JS)
            if ov.get("map") or ov.get("dateText"):
                game_map = ov.get("map") or game_map
                match_date = ov.get("dateText") or match_date
                break
    except Exception:
        pass

    return {
        "matchId": int(match_id),
        "url": f"https://cs2.fastcup.net/matches/{match_id}/stats",
        "map": game_map,
        "dateText": match_date,
        "score": data["score"],
        "sides": [nicks[:len(nicks) // 2], nicks[len(nicks) // 2:]],
        "players": players,
        "scrapedAt": int(time.time()),
    }


def scrape_live(page, match_id):
    """Лёгкое чтение идущего матча: текущий счёт, статус, составы, карта."""
    url = f"https://cs2.fastcup.net/matches/{match_id}/stats?hl=en"
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    d = {}
    for _ in range(12):
        page.wait_for_timeout(1000)
        try:
            d = page.evaluate(LIVE_JS)
        except Exception:
            continue
        if d.get("score") or d.get("status") in ("finished", "pending"):
            break
    nicks = [p["nick"] for p in d.get("players", [])]
    half = len(nicks) // 2
    return {
        "matchId": int(match_id),
        "url": f"https://cs2.fastcup.net/matches/{match_id}/stats",
        "status": d.get("status", "live"),
        "score": d.get("score"),
        "map": d.get("map"),
        "sides": [nicks[:half], nicks[half:]],
        "players": d.get("players", []),
    }


def parse_match_id(val):
    """Принимает число, ID-строку или ссылку на матч FastCup. Возвращает str-ID."""
    if val is None:
        return None
    s = str(val).strip()
    m = re.search(r"/matches/(\d+)", s)
    if m:
        return m.group(1)
    if s.isdigit():
        return s
    nums = re.findall(r"\d+", s)
    return max(nums, key=len) if nums else None


def get_match(page, match_id, refresh):
    CACHE.mkdir(exist_ok=True)
    cf = CACHE / f"{match_id}.json"
    if cf.exists() and str(match_id) not in refresh:
        return json.loads(cf.read_text(encoding="utf-8"))
    if page is None:
        if cf.exists():
            return json.loads(cf.read_text(encoding="utf-8"))
        print(f"  [skip] матч {match_id}: нет кеша, скрапинг отключён (--no-scrape)")
        return None
    try:
        res = scrape_match(page, match_id)
    except Exception as e:
        print(f"  [ошибка] {e}")
        if cf.exists():
            return json.loads(cf.read_text(encoding="utf-8"))
        return None
    cf.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return res


# --------------------------------------------------------------------------- #
#  Сопоставление сторон матча с командами турнира
# --------------------------------------------------------------------------- #
def build_roster_index(conf):
    """alias (slug/nick в нижнем регистре) -> {key, teamId, nick}.
    key — канонический идентификатор игрока (slug из конфига, иначе nick)."""
    idx = {}
    for t in conf["teams"]:
        for pl in t["players"]:
            slug = (pl.get("slug") or "").strip()
            nick = (pl.get("nick") or "").strip()
            if not slug and not nick:
                continue
            rec = {"key": (slug or nick), "teamId": t["id"], "nick": nick or slug}
            if slug:
                idx[slug.lower()] = rec
            if nick:
                idx[nick.lower()] = rec
    return idx


def match_player(idx, slug, nick):
    """Найти игрока в ростере по slug или нику. Возвращает (key, teamId, nick)."""
    r = idx.get((slug or "").lower()) or idx.get((nick or "").lower())
    if r:
        return r["key"], r["teamId"], r["nick"]
    return (slug or nick), None, nick


def team_of(idx, nick_or_slug):
    r = idx.get((nick_or_slug or "").lower())
    return r["teamId"] if r else None


def side_team(nick_list, roster_idx):
    counts = {}
    for n in nick_list:
        r = roster_idx.get((n or "").lower())
        if r:
            counts[r["teamId"]] = counts.get(r["teamId"], 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)


def resolve_fixture(fx, raw_list, roster_idx, home_id, away_id):
    """raw_list — список результатов матчей (серия). Возвращает счёт по картам
    и per-player статистику, ориентированную как home/away."""
    if not raw_list:
        return None
    best_of = int(fx.get("bestOf") or 1)
    need = best_of // 2 + 1                 # карт для победы в серии
    map_home = map_away = 0
    per_player = {}          # slug/nick -> aggregated in match(es)
    per_map = []
    for raw in raw_list:
        if best_of > 1 and (map_home >= need or map_away >= need):
            break                          # серия уже решена
        sa = side_team(raw["sides"][0], roster_idx)
        # сторона 0 -> home?
        if sa == home_id:
            h_idx, a_idx = 0, 1
        elif sa == away_id:
            h_idx, a_idx = 1, 0
        else:
            sb = side_team(raw["sides"][1], roster_idx)
            if sb == home_id:
                h_idx, a_idx = 1, 0
            elif sb == away_id:
                h_idx, a_idx = 0, 1
            else:
                h_idx, a_idx = 0, 1  # не смогли определить — берём как есть
        hs, as_ = raw["score"][h_idx], raw["score"][a_idx]
        per_map.append({"map": raw.get("map"), "home": hs, "away": as_,
                        "url": raw.get("url")})
        if hs > as_:
            map_home += 1
        elif as_ > hs:
            map_away += 1
        for p in raw["players"]:
            side_team_id = home_id if p["side"] == h_idx else away_id
            key, matched_team, disp_nick = match_player(
                roster_idx, p.get("slug"), p.get("nick"))
            team_for = matched_team or side_team_id
            acc = per_player.setdefault(key, {
                "key": key,
                "nick": disp_nick or p["nick"], "slug": p.get("slug", ""),
                "teamId": team_for, "maps": 0,
                "k": 0, "d": 0, "a": 0, "adr": 0.0, "hs": 0.0,
                "rating": 0.0, "entry": 0, "clutches": 0, "multikills": 0,
                "bestRating": 0.0,
            })
            acc["maps"] += 1
            for f in ("k", "d", "a", "entry", "clutches", "multikills"):
                acc[f] += p.get(f, 0)
            acc["adr"] += p.get("adr", 0)
            acc["hs"] += p.get("hs", 0)
            acc["rating"] += p.get("rating", 0)
            acc["bestRating"] = max(acc["bestRating"], p.get("rating", 0))

    return {
        "mapScore": [map_home, map_away],
        "maps": per_map,
        "players": list(per_player.values()),
        "series": best_of > 1 or len(raw_list) > 1,
        "bestOf": best_of,
        "decided": (best_of == 1 and len(per_map) >= 1)
                   or map_home >= need or map_away >= need,
    }


def fixture_score(fx, resolved):
    """Единый счёт хозяева:гости для таблицы. Для серии — счёт по картам,
    для одиночного матча — счёт по раундам."""
    if fx.get("manualScore"):
        return list(fx["manualScore"]), None
    if not resolved:
        return None, None
    if resolved["series"]:
        return resolved["mapScore"], resolved
    m = resolved["maps"][0]
    return [m["home"], m["away"]], resolved


# --------------------------------------------------------------------------- #
#  Турнирная таблица
# --------------------------------------------------------------------------- #
def compute_standings(conf, rr_results):
    teams = {t["id"]: t for t in conf["teams"]}
    fmt = conf["format"]
    row = {tid: {"teamId": tid, "gp": 0, "w": 0, "d": 0, "l": 0,
                 "rw": 0, "rl": 0, "pts": 0} for tid in teams}
    h2h = {tid: {} for tid in teams}

    for fx, (score, _res) in rr_results:
        if score is None:
            continue
        h, a = fx["home"], fx["away"]
        sh, sa = score
        row[h]["gp"] += 1
        row[a]["gp"] += 1
        row[h]["rw"] += sh
        row[h]["rl"] += sa
        row[a]["rw"] += sa
        row[a]["rl"] += sh
        if sh > sa:
            row[h]["w"] += 1
            row[a]["l"] += 1
            row[h]["pts"] += fmt["pointsWin"]
            row[a]["pts"] += fmt["pointsLoss"]
            h2h[h][a] = h2h[h].get(a, 0) + 1
            h2h[a][h] = h2h[a].get(h, 0) - 1
        elif sa > sh:
            row[a]["w"] += 1
            row[h]["l"] += 1
            row[a]["pts"] += fmt["pointsWin"]
            row[h]["pts"] += fmt["pointsLoss"]
            h2h[a][h] = h2h[a].get(h, 0) + 1
            h2h[h][a] = h2h[h].get(a, 0) - 1
        else:
            row[h]["d"] += 1
            row[a]["d"] += 1
            row[h]["pts"] += fmt["pointsDraw"]
            row[a]["pts"] += fmt["pointsDraw"]

    for r in row.values():
        r["diff"] = r["rw"] - r["rl"]

    order = fmt.get("tiebreakers", ["points", "h2h", "roundDiff", "roundsWon", "wins"])

    def sort_key(tid):
        r = row[tid]
        key = []
        for crit in order:
            if crit == "points":
                key.append(r["pts"])
            elif crit == "roundDiff":
                key.append(r["diff"])
            elif crit == "roundsWon":
                key.append(r["rw"])
            elif crit == "wins":
                key.append(r["w"])
            elif crit == "h2h":
                key.append(0)  # применяется отдельно ниже для 2-way
        return tuple(key)

    ranked = sorted(row, key=sort_key, reverse=True)

    # разрешаем ничьи «2 команды с равными очками» по личным встречам
    i = 0
    while i < len(ranked) - 1:
        a, b = ranked[i], ranked[i + 1]
        if row[a]["pts"] == row[b]["pts"] and "h2h" in order:
            hv = h2h[a].get(b, 0)
            if hv < 0:
                ranked[i], ranked[i + 1] = b, a
        i += 1

    standings = []
    for pos, tid in enumerate(ranked, 1):
        r = dict(row[tid])
        r["pos"] = pos
        standings.append(r)
    return standings


def total_rr_matches(conf):
    return len(conf["schedule"]["roundRobin"])


# --------------------------------------------------------------------------- #
#  Статистика игроков (по всем матчам турнира)
# --------------------------------------------------------------------------- #
def aggregate_players(conf, all_resolved):
    teams = {t["id"]: t for t in conf["teams"]}
    # заранее заводим всех заявленных игроков
    agg = {}
    for t in conf["teams"]:
        for pl in t["players"]:
            slug = (pl.get("slug") or "").strip()
            nick = (pl.get("nick") or "").strip()
            if not slug and not nick:
                continue
            key = (slug or nick).lower()
            agg[key] = {
                "nick": nick or slug, "slug": slug,
                "teamId": t["id"], "teamName": t["name"], "teamTag": t["tag"],
                "maps": 0, "k": 0, "d": 0, "a": 0,
                "adrSum": 0.0, "hsSum": 0.0, "ratingSum": 0.0,
                "entry": 0, "clutches": 0, "multikills": 0, "bestRating": 0.0,
            }

    for res in all_resolved:
        if not res:
            continue
        for p in res["players"]:
            key = (p.get("key") or p.get("slug") or p["nick"]).lower()
            rec = agg.get(key)
            if rec is None:
                tid = p.get("teamId")
                t = teams.get(tid, {})
                rec = agg[key] = {
                    "nick": p["nick"], "slug": p.get("slug", ""),
                    "teamId": tid, "teamName": t.get("name", "—"),
                    "teamTag": t.get("tag", ""),
                    "maps": 0, "k": 0, "d": 0, "a": 0,
                    "adrSum": 0.0, "hsSum": 0.0, "ratingSum": 0.0,
                    "entry": 0, "clutches": 0, "multikills": 0, "bestRating": 0.0,
                }
            rec["maps"] += p.get("maps", 0)
            rec["k"] += p.get("k", 0)
            rec["d"] += p.get("d", 0)
            rec["a"] += p.get("a", 0)
            rec["adrSum"] += p.get("adr", 0)
            rec["hsSum"] += p.get("hs", 0)
            rec["ratingSum"] += p.get("rating", 0)
            rec["entry"] += p.get("entry", 0)
            rec["clutches"] += p.get("clutches", 0)
            rec["multikills"] += p.get("multikills", 0)
            rec["bestRating"] = max(rec["bestRating"], p.get("bestRating", 0))

    out = []
    for rec in agg.values():
        m = rec["maps"] or 0
        out.append({
            "nick": rec["nick"], "slug": rec["slug"],
            "teamId": rec["teamId"], "teamName": rec["teamName"],
            "teamTag": rec["teamTag"],
            "maps": m,
            "k": rec["k"], "d": rec["d"], "a": rec["a"],
            "kd": round(rec["k"] / rec["d"], 2) if rec["d"] else rec["k"],
            "adr": round(rec["adrSum"] / m, 1) if m else 0,
            "hs": round(rec["hsSum"] / m, 1) if m else 0,
            "rating": round(rec["ratingSum"] / m) if m else 0,
            "bestRating": round(rec["bestRating"]),
            "entry": rec["entry"], "clutches": rec["clutches"],
            "multikills": rec["multikills"],
        })
    out.sort(key=lambda x: (x["maps"] > 0, x["rating"]), reverse=True)
    return out


# --------------------------------------------------------------------------- #
#  Плей-офф
# --------------------------------------------------------------------------- #
def resolve_playoff(conf, standings, roster_idx, get):
    seeds = {i + 1: s["teamId"] for i, s in enumerate(standings)}
    rr_done = all(fx.get("manualScore") or fx.get("matchId") or fx.get("matchIds")
                  for fx in conf["schedule"]["roundRobin"]) and \
              len(conf["schedule"]["roundRobin"]) > 0
    out = []
    all_res = []
    for pf in conf["schedule"]["playoff"]:
        home_id = seeds.get(pf["homeSeed"]) if rr_done else None
        away_id = seeds.get(pf["awaySeed"]) if rr_done else None
        ids = pf.get("matchIds") or ([pf["matchId"]] if pf.get("matchId") else [])
        raw_list = [r for r in (get(mid) for mid in ids if mid) if r]
        resolved = None
        score = None
        if pf.get("manualScore"):
            score = list(pf["manualScore"])
        elif raw_list and home_id and away_id:
            resolved = resolve_fixture(pf, raw_list, roster_idx, home_id, away_id)
            score, _ = fixture_score(pf, resolved)
            all_res.append(resolved)
        best_of = int(pf.get("bestOf") or 1)
        need = best_of // 2 + 1
        winner = None
        if score and home_id and away_id:
            if best_of > 1:
                if score[0] >= need:
                    winner = home_id
                elif score[1] >= need:
                    winner = away_id
            else:
                winner = home_id if score[0] > score[1] else (
                    away_id if score[1] > score[0] else None)
        out.append({
            "id": pf["id"], "name": pf["name"],
            "homeSeed": pf["homeSeed"], "awaySeed": pf["awaySeed"],
            "homeId": home_id, "awayId": away_id,
            "bestOf": best_of,
            "score": score, "winnerId": winner,
            "maps": resolved["maps"] if resolved else None,
            "url": (raw_list[0]["url"] if raw_list else None),
        })
    return out, all_res, rr_done


# --------------------------------------------------------------------------- #
#  Пересчёт и запись dashboard-data.js
# --------------------------------------------------------------------------- #
def build_dashboard(conf, roster_idx, get, live=None):
    rr_results = []
    all_resolved = []
    for fx in conf["schedule"]["roundRobin"]:
        ids = fx.get("matchIds") or ([fx["matchId"]] if fx.get("matchId") else [])
        raw_list = [r for r in (get(mid) for mid in ids if mid) if r]
        resolved = None
        if raw_list:
            resolved = resolve_fixture(fx, raw_list, roster_idx, fx["home"], fx["away"])
            all_resolved.append(resolved)
        score, res = fixture_score(fx, resolved)
        rr_results.append((fx, (score, res)))

    standings = compute_standings(conf, rr_results)
    playoff, pf_resolved, rr_done = resolve_playoff(conf, standings, roster_idx, get)
    all_resolved += pf_resolved
    players = aggregate_players(conf, all_resolved)

    champ = bronze = None
    for pf in playoff:
        if pf["id"] == "final" and pf["winnerId"]:
            champ = pf["winnerId"]
        if pf["id"] == "third" and pf["winnerId"]:
            bronze = pf["winnerId"]

    def rr_row(fx, score, res):
        raw0 = None
        ids = fx.get("matchIds") or ([fx.get("matchId")] if fx.get("matchId") else [])
        for mid in ids:
            raw0 = get(mid)
            if raw0:
                break
        return {
            "id": fx["id"], "round": fx["round"],
            "home": fx["home"], "away": fx["away"],
            "score": score,
            "maps": (res["maps"] if res else None),
            "series": bool(res and res.get("series")),
            "date": (raw0.get("dateText") if raw0 else None),
            "url": (res["maps"][0]["url"] if (res and res.get("maps")) else
                    (f"https://cs2.fastcup.net/matches/{parse_match_id(fx.get('matchId'))}/stats"
                     if parse_match_id(fx.get("matchId")) else None)),
            "auto": bool(fx.get("_auto")),
            "played": score is not None,
        }

    seeds = {i + 1: s["teamId"] for i, s in enumerate(standings)}
    dashboard = {
        "title": conf["title"],
        "subtitle": conf.get("subtitle", ""),
        "format": conf["format"],
        "generatedAt": int(time.time()),
        "autoTrack": bool(conf.get("autoTrack", {}).get("enabled")),
        "teams": conf["teams"],
        "standings": standings,
        "roundRobin": [rr_row(fx, score, res) for fx, (score, res) in rr_results],
        "playoff": playoff,
        "players": players,
        "rrDone": rr_done,
        "championId": champ,
        "bronzeId": bronze,
        "rrPlayed": sum(1 for _, (s, _r) in rr_results if s is not None),
        "rrTotal": total_rr_matches(conf),
        "live": live or [],
    }
    OUT.write_text(
        "// Автогенерация update.py — не редактировать вручную\n"
        "window.TOURNAMENT_DATA = " +
        json.dumps(dashboard, ensure_ascii=False, indent=2) + ";\n",
        encoding="utf-8",
    )
    return {"rrPlayed": dashboard["rrPlayed"], "rrTotal": dashboard["rrTotal"],
            "rrDone": rr_done, "seeds": seeds}


# --------------------------------------------------------------------------- #
#  Публикация на GitHub Pages (git push)
# --------------------------------------------------------------------------- #
def _git(*a):
    import subprocess
    return subprocess.run(("git",) + a, cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def is_git_repo():
    try:
        return _git("rev-parse", "--is-inside-work-tree").returncode == 0
    except Exception:
        return False


def git_sync():
    """Подтянуть правки с GitHub (например, счёт, изменённый с телефона).
    При конфликте побеждает серверная версия — ручная правка важнее авто."""
    if not is_git_repo():
        return
    _git("add", "-A")
    _git("commit", "-m", "autosync")            # ок, если коммитить нечего
    if _git("fetch", "origin").returncode != 0:
        return                                  # нет сети — просто продолжаем
    r = _git("pull", "--no-rebase", "-X", "theirs", "--no-edit")
    if r.returncode != 0:
        _git("merge", "--abort")
        _git("rebase", "--abort")
        print("[git] не удалось подтянуть правки с GitHub, работаю с локальной версией")
    elif "Already up to date" not in (r.stdout or ""):
        print("[git] подтянул правки с GitHub")


def git_push(message):
    """Коммит + пуш dashboard-data.js и tournament.json, если что-то изменилось."""
    import subprocess

    def run(*a):
        return subprocess.run(("git",) + a, cwd=ROOT, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")

    if run("rev-parse", "--is-inside-work-tree").returncode != 0:
        print("[git] это не git-репозиторий — пропускаю публикацию "
              "(см. раздел про GitHub Pages в README)")
        return
    files = [f for f in ("dashboard-data.js", "tournament.json", "index.html",
                         "README.md", "ica_logo.png") if (ROOT / f).exists()]
    run("add", *files)
    if run("diff", "--cached", "--quiet").returncode == 0:
        return  # нечего коммитить
    c = run("commit", "-m", message)
    if c.returncode != 0:
        print(f"[git] commit не удался: {c.stderr.strip() or c.stdout.strip()}")
        return
    p = run("push")
    if p.returncode != 0:
        print(f"[git] push не удался: {p.stderr.strip() or p.stdout.strip()}")
    else:
        print("[git] опубликовано на GitHub")


# --------------------------------------------------------------------------- #
#  Автоотслеживание матчей по профилям капитанов
# --------------------------------------------------------------------------- #
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
     "nov", "dec"], 1)}


def parse_profile_date(s):
    """'26 Jul at 16:09' или относительное 'N minutes ago' -> datetime."""
    import datetime as _dt
    s = (s or "").strip()
    now = _dt.datetime.now()

    # относительное время у свежих матчей: 'just now', '4 minutes ago',
    # '2 hours ago', 'только что', '4 минуты назад', '2 часа назад'
    if re.search(r"just now|только что|момент назад", s, re.I):
        return now
    m = re.search(r"(\d+)\s*(min|minute|минут)", s, re.I)
    if m and re.search(r"ago|назад", s, re.I):
        return now - _dt.timedelta(minutes=int(m[1]))
    m = re.search(r"(\d+)\s*(hour|hr|час)", s, re.I)
    if m and re.search(r"ago|назад", s, re.I):
        return now - _dt.timedelta(hours=int(m[1]))
    if re.search(r"(a|an|один)\s+(minute|hour|минут|час)", s, re.I) \
            and re.search(r"ago|назад", s, re.I):
        return now - _dt.timedelta(minutes=30)
    m = re.search(r"(\d+)\s*(day|дн|день|сут)", s, re.I)
    if m and re.search(r"ago|назад", s, re.I):
        return now - _dt.timedelta(days=int(m[1]))
    if re.search(r"yesterday|вчера", s, re.I):
        return now - _dt.timedelta(days=1)

    m = re.match(r"(\d{1,2})\s+([A-Za-z]{3})[A-Za-z]*\s+at\s+(\d{1,2}):(\d{2})", s)
    if not m:
        return None
    day, mon, hh, mm = int(m[1]), MONTHS.get(m[2].lower()), int(m[3]), int(m[4])
    if not mon:
        return None
    year = now.year if mon <= now.month + 1 else now.year - 1
    try:
        return _dt.datetime(year, mon, day, hh, mm)
    except ValueError:
        return None


def parse_profile_matches(text):
    """Плоский текст /<slug>/matches -> [{id, mode, score, date}]."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    out = []
    for i, ln in enumerate(lines):
        m = re.match(r"#(\d+)$", ln)
        if not m:
            continue
        chunk = lines[i + 1:i + 7]
        mode = next((c for c in chunk if re.match(r"^[125]v[125]$", c)), None)
        score = next((c for c in chunk if re.match(r"^\d{1,2}:\d{1,2}$", c)), None)
        date = next((c for c in chunk if re.match(
            r"^\d{1,2}\s+[A-Za-z]{3,}\s+at\s+\d{1,2}:\d{2}$", c)
            or re.search(r"ago|назад|just now|только что|yesterday|вчера", c, re.I)),
            None)
        out.append({"id": m[1], "mode": mode, "score": score,
                    "date": parse_profile_date(date), "dateRaw": date})
    return out


def scrape_profile(page, slug):
    url = f"https://cs2.fastcup.net/{slug}/matches?hl=en"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        print(f"[auto] профиль {slug}: не загрузился ({e})")
        return []
    data = {}
    for _ in range(12):
        page.wait_for_timeout(1000)
        try:
            data = page.evaluate(PROFILE_JS)
        except Exception:
            continue
        body = data.get("body", "")
        if data.get("ids"):
            break
        # профиль загрузился, но матчей нет / приватный / 404
        if re.search(r"has no|нет матчей|not found|404|Reset\s*$", body) and \
                "Matches" in body:
            break
    return parse_profile_matches(data.get("body", "")) or \
        [{"id": i, "mode": None, "score": None, "date": None}
         for i in data.get("ids", [])]


def scrape_profile_current(page, slug):
    """Главная страница профиля /<slug>: ищем ссылку на идущий сейчас матч."""
    try:
        page.goto(f"https://cs2.fastcup.net/{slug}?hl=en",
                  wait_until="domcontentloaded", timeout=45000)
    except Exception:
        return []
    d = {}
    for _ in range(10):
        page.wait_for_timeout(1000)
        try:
            d = page.evaluate(PROFILE_MAIN_JS)
        except Exception:
            continue
        if d.get("head"):
            break
    return d.get("ids", [])


def captain_slug_set(conf):
    return set(s.lower() for s in (conf.get("autoTrack", {}).get("captains") or []))


def identify_side(nicks, roster_idx, captain_slugs):
    """teamId стороны: сначала по капитану, потом по большинству ростера."""
    by_team = {}
    cap_team = None
    for n in nicks:
        r = roster_idx.get((n or "").lower())
        if not r:
            continue
        by_team[r["teamId"]] = by_team.get(r["teamId"], 0) + 1
        if r["key"].lower() in captain_slugs:
            cap_team = r["teamId"]
    if cap_team:
        return cap_team
    if by_team:
        best = max(by_team, key=by_team.get)
        if by_team[best] >= 3:
            return best
    return None


def match_pair(raw, roster_idx, captain_slugs):
    """frozenset из двух teamId по составам матча, или None."""
    s0 = identify_side(raw["sides"][0], roster_idx, captain_slugs)
    s1 = identify_side(raw["sides"][1], roster_idx, captain_slugs)
    if s0 and s1 and s0 != s1:
        return frozenset((s0, s1))
    return None


def place_match(conf, mid, raw, roster_idx, captain_slugs, seeds, rr_done, get, state):
    """Вписывает сыгранный матч в свободную клетку расписания.
    Возвращает (fixture, human_desc) или None. Мутирует conf и state."""
    mid = str(mid)
    ignored = set(state.setdefault("ignored", []))
    resolved = state.setdefault("resolved", {})

    if len(raw.get("players", [])) != 10 or not raw.get("score"):
        ignored.add(mid); state["ignored"] = sorted(ignored); return None
    pair = match_pair(raw, roster_idx, captain_slugs)
    if not pair:
        ignored.add(mid); state["ignored"] = sorted(ignored); return None

    def series_open(fx):
        bo = int(fx.get("bestOf") or 1)
        rl = [r for r in (get(x) for x in (fx.get("matchIds") or []) if x) if r]
        if not rl:
            return True
        rr = resolve_fixture(fx, rl, roster_idx,
                             fx.get("home") or seeds.get(fx.get("homeSeed")),
                             fx.get("away") or seeds.get(fx.get("awaySeed")))
        need2 = bo // 2 + 1
        return not (rr and (rr["mapScore"][0] >= need2 or rr["mapScore"][1] >= need2))

    target = None
    for fx in conf["schedule"]["roundRobin"]:
        if frozenset((fx["home"], fx["away"])) == pair and not (
                fx.get("matchId") or fx.get("matchIds") or fx.get("manualScore")):
            target = fx; break
    if target is None and rr_done:
        for pf in conf["schedule"]["playoff"]:
            hid, aid = seeds.get(pf["homeSeed"]), seeds.get(pf["awaySeed"])
            if frozenset((hid, aid)) != pair or pf.get("manualScore"):
                continue
            if int(pf.get("bestOf") or 1) <= 1:
                if not (pf.get("matchId") or pf.get("matchIds")):
                    target = pf; break
            else:
                if mid in [str(x) for x in (pf.get("matchIds") or [])]:
                    break
                if series_open(pf):
                    target = pf; break

    if target is None:
        a, b = tuple(pair)
        an = next((t["name"] for t in conf["teams"] if t["id"] == a), a)
        bn = next((t["name"] for t in conf["teams"] if t["id"] == b), b)
        print(f"[auto] матч #{mid} ({an} vs {bn}) — нет свободного места в расписании. Пропускаю.")
        ignored.add(mid); state["ignored"] = sorted(ignored); return None

    where = target.get("name") or f"тур {target.get('round')}"
    if int(target.get("bestOf") or 1) > 1:
        lst = target.get("matchIds") or []
        lst.append(int(mid))
        target["matchIds"] = lst
        target["matchId"] = None
    else:
        target["matchId"] = int(mid)
    target["_auto"] = True
    resolved[mid] = target["id"]
    a, b = tuple(pair)
    an = next((t["name"] for t in conf["teams"] if t["id"] == a), a)
    bn = next((t["name"] for t in conf["teams"] if t["id"] == b), b)
    print(f"[auto] + матч #{mid} -> {target['id']} ({where}): "
          f"{raw['score'][0]}:{raw['score'][1]}")
    return target, f"{where}: {an} {raw['score'][0]}:{raw['score'][1]} {bn} (#{mid})"


def load_state():
    sf = CACHE / "_autotrack.json"
    if sf.exists():
        try:
            return json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"ignored": [], "resolved": {}}


def save_state(st):
    CACHE.mkdir(exist_ok=True)
    (CACHE / "_autotrack.json").write_text(
        json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def autotrack_scan(page, conf, roster_idx, get, seeds, rr_done):
    at = conf.get("autoTrack", {})
    slugs = list(at.get("captains") or [])
    if not slugs:
        for t in conf["teams"]:
            for pl in t["players"]:
                if pl.get("slug"):
                    slugs.append(pl["slug"])
    slugs = list(dict.fromkeys(slugs))
    if not slugs:
        print("[auto] Ни у одного игрока не заполнен slug — автоотслеживание не работает. "
              "Впиши slug капитанов в tournament.json (autoTrack.captains).")
        return []

    since = None
    if at.get("since"):
        try:
            import datetime as _dt
            since = _dt.datetime.strptime(str(at["since"])[:10], "%Y-%m-%d")
        except Exception:
            print(f"[auto] autoTrack.since='{at['since']}' — ожидается формат ГГГГ-ММ-ДД, игнорирую фильтр по дате")

    captain_slugs = captain_slug_set(conf)

    st = load_state()
    ignored = set(st.get("ignored", []))

    # уже занятые пары (RR + playoff) — по id матча
    assigned_ids = set()
    for fx in conf["schedule"]["roundRobin"] + conf["schedule"]["playoff"]:
        for mid in (fx.get("matchIds") or [fx.get("matchId")]):
            pid = parse_match_id(mid)
            if pid:
                assigned_ids.add(pid)

    # собираем кандидатов с профилей
    print(f"[auto] опрашиваю профили: {', '.join(slugs)}")
    cand = {}
    for slug in slugs:
        try:
            rows = scrape_profile(page, slug)
        except Exception as e:
            print(f"[auto] профиль {slug}: {e}")
            continue
        added = 0
        for r in rows:
            if r["mode"] and r["mode"] != "5v5":
                continue
            if r["id"] in ignored or r["id"] in assigned_ids:
                continue
            if since and r["date"] and r["date"] < since:
                ignored.add(r["id"])       # дата распозналась и она старая — больше не трогаем
                continue
            # дату не распознали ('4 minutes ago' и т.п.) — пропускаем дальше,
            # реальную дату проверим уже по странице матча
            cand.setdefault(r["id"], r)
            added += 1
        print(f"[auto]   {slug}: новых кандидатов {added}")

    newly = []
    for mid, meta in sorted(cand.items(), key=lambda kv: int(kv[0])):
        raw = get(mid)
        if not raw or not raw.get("score"):
            ignored.add(mid)
            continue
        # проверка даты по странице матча (авторитетнее профиля)
        if since:
            md = parse_profile_date(raw.get("dateText") or "")
            if md and md < since:
                ignored.add(mid)
                continue
        st["ignored"] = sorted(ignored)
        res = place_match(conf, mid, raw, roster_idx, captain_slugs,
                          seeds, rr_done, get, st)
        ignored = set(st.get("ignored", []))
        if res:
            newly.append(res[1])

    st["ignored"] = sorted(ignored)
    save_state(st)
    if newly:
        save_conf(conf)
    return newly


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #
def open_browser(headful):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Не установлен Playwright.  pip install playwright")
    pw = sync_playwright().start()

    # 1) уже установленный в системе браузер (не требует загрузки Playwright),
    # 2) собственный chromium Playwright — как запасной вариант.
    attempts = []
    forced = os.environ.get("FC_BROWSER_CHANNEL")  # chrome | msedge | chrome-beta ...
    if forced:
        attempts.append({"channel": forced})
    attempts += [{"channel": "chrome"}, {"channel": "msedge"}, {}]

    browser = None
    errors = []
    for kw in attempts:
        try:
            browser = pw.chromium.launch(headless=not headful, **kw)
            if kw.get("channel"):
                print(f"[browser] использую системный {kw['channel']}")
            break
        except Exception as e:
            errors.append(f"{kw.get('channel') or 'chromium'}: {str(e).splitlines()[0]}")

    if browser is None:
        pw.stop()
        sys.exit(
            "Не удалось запустить браузер. Варианты:\n"
            "  • установить Google Chrome (обычно уже есть) — скрипт подхватит его сам;\n"
            "  • либо разово скачать браузер Playwright в обход корпоративного фильтра:\n"
            "      set NODE_TLS_REJECT_UNAUTHORIZED=0\n"
            "      python -m playwright install chromium\n\n"
            "Подробности: " + " | ".join(errors))

    ctx = browser.new_context(
        locale="ru-RU",
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"),
    )
    return pw, browser, ctx.new_page()


# --------------------------------------------------------------------------- #
#  Ручной ввод счёта (форс-мажор)
# --------------------------------------------------------------------------- #
def find_fixture(conf, fid):
    fid = str(fid).strip().lower()
    for fx in conf["schedule"]["roundRobin"] + conf["schedule"]["playoff"]:
        if str(fx["id"]).lower() == fid:
            return fx
    return None


def _forget_ids(fx, state):
    """убрать id матчей этой клетки из авто-состояния, чтобы --watch не вернул их"""
    ids = list(fx.get("matchIds") or [])
    if fx.get("matchId"):
        ids.append(fx["matchId"])
    ign = set(state.setdefault("ignored", []))
    res = state.setdefault("resolved", {})
    for m in ids:
        pid = parse_match_id(m)
        if pid:
            ign.add(pid)
            res.pop(pid, None)
    state["ignored"] = sorted(ign)


def set_manual(conf, fid, a, b, state):
    fx = find_fixture(conf, fid)
    if not fx:
        print(f"[ручной] нет клетки '{fid}'. Список: --edit")
        return False
    try:
        a, b = int(a), int(b)
    except ValueError:
        print("[ручной] счёт должен быть числами, напр.: --set rr-3 13 7")
        return False
    _forget_ids(fx, state)
    fx["manualScore"] = [a, b]
    fx["matchId"] = None
    fx["matchIds"] = None
    fx["_auto"] = False
    fx["_manual"] = True
    name = fx.get("name") or f"тур {fx.get('round')}"
    print(f"[ручной] {fx['id']} ({name}): счёт вручную {a}:{b}")
    return True


def clear_fixture(conf, fid, state):
    fx = find_fixture(conf, fid)
    if not fx:
        print(f"[ручной] нет клетки '{fid}'.")
        return False
    _forget_ids(fx, state)
    fx["manualScore"] = None
    fx["matchId"] = None
    fx["matchIds"] = None
    fx.pop("_auto", None)
    fx.pop("_manual", None)
    print(f"[ручной] {fx['id']}: сброшено")
    return True


def _fixture_line(conf, fx, seeds):
    tn = {t["id"]: t for t in conf["teams"]}
    if "home" in fx:
        h, aw = tn.get(fx["home"], {}).get("name", fx["home"]), \
                tn.get(fx["away"], {}).get("name", fx["away"])
        head = f"тур {fx['round']:<2} {h} — {aw}"
    else:
        hid, aid = seeds.get(fx["homeSeed"]), seeds.get(fx["awaySeed"])
        h = tn.get(hid, {}).get("name") or f"{fx['homeSeed']}-е место"
        aw = tn.get(aid, {}).get("name") or f"{fx['awaySeed']}-е место"
        head = f"{fx['name']} ({h} — {aw})"
    if fx.get("manualScore"):
        sc = f"{fx['manualScore'][0]}:{fx['manualScore'][1]} (вручную)"
    elif fx.get("matchId") or fx.get("matchIds"):
        sc = "по матчу FastCup" + (" (авто)" if fx.get("_auto") else "")
    else:
        sc = "— не сыграно"
    return f"{head:<48} {sc}"


def manual_menu(conf, state, seeds):
    while True:
        print("\n=== Круговой этап ===")
        rr = conf["schedule"]["roundRobin"]
        for i, fx in enumerate(rr, 1):
            print(f" {i:>2}. [{fx['id']}] {_fixture_line(conf, fx, seeds)}")
        print("=== Плей-офф ===")
        po = conf["schedule"]["playoff"]
        for j, fx in enumerate(po, 1):
            print(f" P{j}. [{fx['id']}] {_fixture_line(conf, fx, seeds)}")
        print("\nВыбери: номер (1, P1) чтобы задать счёт | c <номер> сбросить | "
              "q сохранить и выйти")
        raw = input("> ").strip()
        if not raw:
            continue
        if raw.lower() in ("q", "exit", "quit", "й"):
            return
        clear = raw.lower().startswith("c ")
        token = raw[2:].strip() if clear else raw

        def resolve_token(tok):
            tok = tok.strip().lower()
            if tok.startswith("p") and tok[1:].isdigit():
                k = int(tok[1:]) - 1
                return po[k]["id"] if 0 <= k < len(po) else None
            if tok.isdigit():
                k = int(tok) - 1
                return rr[k]["id"] if 0 <= k < len(rr) else None
            return find_fixture(conf, tok) and tok
        fid = resolve_token(token)
        if not fid:
            print("не понял номер"); continue
        if clear:
            clear_fixture(conf, fid, state)
            continue
        fx = find_fixture(conf, fid)
        bo = int(fx.get("bestOf") or 1)
        hint = "счёт по картам, напр. 2 1" if bo > 1 else "счёт, напр. 13 7"
        s = input(f"  {fid} — {hint} (пусто — отмена): ").strip()
        if not s:
            continue
        parts = s.replace(":", " ").split()
        if len(parts) != 2:
            print("нужно два числа"); continue
        set_manual(conf, fid, parts[0], parts[1], state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-scrape", action="store_true",
                    help="не обращаться к сайту, только пересчитать по кешу")
    ap.add_argument("--refresh", nargs="*", default=[],
                    help="ID матчей, которые надо перекачать, игнорируя кеш")
    ap.add_argument("--headful", action="store_true",
                    help="показать окно браузера")
    ap.add_argument("--regen-schedule", action="store_true",
                    help="пересоздать расписание кругового этапа с нуля")
    ap.add_argument("--watch", action="store_true",
                    help="онлайн-режим: крутиться в цикле и ловить новые матчи")
    ap.add_argument("--interval", type=int, default=180,
                    help="период опроса в секундах для --watch (по умолчанию 180)")
    ap.add_argument("--once", action="store_true",
                    help="один проход автоотслеживания и выход")
    ap.add_argument("--push", action="store_true",
                    help="после обновления делать git commit + push (GitHub Pages)")
    ap.add_argument("--live", nargs="*", metavar="MATCH",
                    help="live-счёт идущих матчей. Без аргументов — сам ищет "
                         "идущие матчи капитанов; либо укажи ID/ссылки вручную")
    ap.add_argument("--live-interval", type=int, default=45,
                    help="период опроса для --live в секундах (по умолчанию 45)")
    ap.add_argument("--edit", action="store_true",
                    help="ручной ввод счёта (меню) — для форс-мажора")
    ap.add_argument("--set", nargs=3, metavar=("FIXTURE", "A", "B"),
                    help="задать счёт вручную: --set rr-3 13 7  (для плей-офф — по картам)")
    ap.add_argument("--clear", metavar="FIXTURE",
                    help="сбросить матч в клетке (убрать счёт/ссылку): --clear rr-3")
    args = ap.parse_args()
    refresh = set(str(x) for x in args.refresh)

    if not CONF.exists():
        sys.exit("Нет файла tournament.json рядом со скриптом.")

    conf = load_conf()
    ensure_schedule(conf, force=args.regen_schedule)
    conf = load_conf()

    def scan_ids(c):
        ids = []
        for fx in c["schedule"]["roundRobin"] + c["schedule"]["playoff"]:
            for mid in (fx.get("matchIds") or [fx.get("matchId")]):
                pid = parse_match_id(mid)
                if pid:
                    ids.append(pid)
        return ids

    manual_mode = bool(args.edit or args.set or args.clear)

    auto = bool(conf.get("autoTrack", {}).get("enabled")) and not manual_mode
    need_ids = scan_ids(conf)
    must_scrape = [m for m in need_ids
                   if (m in refresh) or not (CACHE / f"{m}.json").exists()]

    pw = browser = page = None
    want_browser = (not args.no_scrape) and (not manual_mode) and (
        must_scrape or args.watch or args.once or auto or args.live is not None)
    if want_browser:
        pw, browser, page = open_browser(args.headful)

    cache_mem = {}

    def get(mid):
        mid = parse_match_id(mid)
        if not mid:
            return None
        if mid in cache_mem:
            return cache_mem[mid]
        r = get_match(page, mid, refresh)
        cache_mem[mid] = r
        return r

    def one_pass(first):
        nonlocal conf
        if args.push:
            git_sync()                     # подтянуть правки счёта с GitHub/телефона
        conf = load_conf()
        roster_idx = build_roster_index(conf)
        summ = build_dashboard(conf, roster_idx, get)
        if (auto or args.once) and page is not None:
            newly = autotrack_scan(page, conf, roster_idx, get,
                                   summ["seeds"], summ["rrDone"])
            if newly:
                conf = load_conf()
                roster_idx = build_roster_index(conf)
                summ = build_dashboard(conf, roster_idx, get)
        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] dashboard-data.js обновлён · "
              f"круговой этап {summ['rrPlayed']}/{summ['rrTotal']}"
              + ("" if not first else " · открой index.html"))
        if args.push:
            git_push(f"tournament update {time.strftime('%Y-%m-%d %H:%M')}")
        return summ

    def live_loop():
        nonlocal conf
        manual = [x for x in (parse_match_id(v) for v in args.live) if x]
        auto_find = not manual          # --live без аргументов -> ищем сами
        itv = max(20, args.live_interval)
        tracking = list(manual)
        seen_done = set()

        conf0 = load_conf()
        cap_slugs = list(conf0.get("autoTrack", {}).get("captains") or [])
        if not cap_slugs:
            for t in conf0["teams"]:
                for pl in t["players"]:
                    if pl.get("slug"):
                        cap_slugs.append(pl["slug"])
        cap_slugs = list(dict.fromkeys(cap_slugs))

        if auto_find:
            if not cap_slugs:
                sys.exit("[live] нет slug капитанов в tournament.json — укажи матчи вручную: "
                         "python update.py --live <ссылка>")
            print(f"[live] ищу идущие матчи по профилям капитанов "
                  f"({len(cap_slugs)} шт.), опрос каждые {itv} с. Ctrl+C — остановить.")
        else:
            print(f"[live] слежу за: {', '.join('#'+t for t in tracking)} "
                  f"(каждые {itv} с). Ctrl+C — остановить.")

        idle = 0
        while True:
            if args.push:
                git_sync()
            conf = load_conf()
            roster_idx = build_roster_index(conf)
            caps = captain_slug_set(conf)
            base = build_dashboard(conf, roster_idx, get)
            seeds, rr_done = base["seeds"], base["rrDone"]

            # --- авто-поиск идущих матчей по профилям ---
            if auto_find:
                found = []
                for slug in cap_slugs:
                    try:
                        for mid in scrape_profile_current(page, slug):
                            if mid not in tracking and mid not in seen_done:
                                found.append(mid)
                    except Exception as e:
                        print(f"[live] профиль {slug}: {e}")
                for mid in dict.fromkeys(found):
                    try:
                        chk = scrape_live(page, mid)
                    except Exception:
                        continue
                    if chk.get("status") == "live" and len(chk.get("players", [])) == 10 \
                            and match_pair(chk, roster_idx, caps):
                        tracking.append(mid)
                        print(f"[live] нашёл идущий матч #{mid}")
                    else:
                        seen_done.add(mid)

            live_entries = []
            done_now = []
            for mid in list(tracking):
                try:
                    d = scrape_live(page, mid)
                except Exception as e:
                    print(f"[live] #{mid}: {e}")
                    continue
                pair = match_pair(d, roster_idx, caps) if len(d.get("players", [])) == 10 else None
                fx = None
                if pair:
                    for f in conf["schedule"]["roundRobin"] + conf["schedule"]["playoff"]:
                        hid = f.get("home") or seeds.get(f.get("homeSeed"))
                        aid = f.get("away") or seeds.get(f.get("awaySeed"))
                        if hid and aid and frozenset((hid, aid)) == pair:
                            fx = f
                            break
                if d["status"] == "finished" and d.get("score"):
                    full = get_match(page, mid, {str(mid)})   # полноценный разбор со статой
                    st = load_state()
                    res = place_match(conf, mid, full or d, roster_idx, caps,
                                      seeds, rr_done, get, st)
                    save_state(st)
                    if res:
                        save_conf(conf)
                        print(f"[live] #{mid} завершён — записан в {res[0]['id']}")
                    done_now.append(mid)
                    continue
                if d["status"] == "pending":
                    print(f"[live] #{mid}: матч ещё не начался")
                if d.get("score"):
                    hid = aid = None
                    a, b = tuple(pair) if pair else (None, None)
                    # ориентируем счёт: сторона 0 -> какая команда
                    s0 = identify_side(d["sides"][0], roster_idx, caps) if pair else None
                    if fx is not None and pair:
                        home = fx.get("home") or seeds.get(fx.get("homeSeed"))
                        sc = d["score"] if s0 == home else [d["score"][1], d["score"][0]]
                        hid, aid = home, (fx.get("away") or seeds.get(fx.get("awaySeed")))
                    else:
                        sc = d["score"]
                    live_entries.append({
                        "matchId": int(mid),
                        "fixtureId": fx["id"] if fx else None,
                        "name": (fx.get("name") if fx else None)
                                or (f"тур {fx.get('round')}" if fx else "матч"),
                        "homeId": hid, "awayId": aid,
                        "score": sc, "map": d.get("map"),
                        "url": d["url"], "status": "live",
                    })
                    print(f"[live] #{mid}: {sc[0]}:{sc[1]}"
                          + (f"  ({live_entries[-1]['name']})" if fx else ""))
            for m in done_now:
                tracking.remove(m)
                seen_done.add(m)
            conf = load_conf()
            roster_idx = build_roster_index(conf)
            build_dashboard(conf, roster_idx, get, live=live_entries)
            if args.push and (live_entries or done_now):
                git_push(f"live {time.strftime('%H:%M')}")

            if not auto_find and not tracking:
                print("[live] все матчи завершены.")
                break
            if auto_find and not tracking:
                idle += 1
                if idle % 10 == 1:
                    print(f"[live] идущих матчей нет, продолжаю следить "
                          f"({time.strftime('%H:%M:%S')})")
            else:
                idle = 0
            time.sleep(itv if tracking else max(itv, 90))

    try:
        if manual_mode:
            git_sync()
            conf = load_conf()
            roster_idx = build_roster_index(conf)
            seeds = build_dashboard(conf, roster_idx, get)["seeds"]
            st = load_state()
            changed = False
            if args.set:
                changed = set_manual(conf, args.set[0], args.set[1], args.set[2], st) or changed
            if args.clear:
                changed = clear_fixture(conf, args.clear, st) or changed
            if args.edit:
                try:
                    manual_menu(conf, st, seeds)
                    changed = True
                except (EOFError, KeyboardInterrupt):
                    print()
            if changed:
                save_conf(conf)
                save_state(st)
                conf = load_conf()
                roster_idx = build_roster_index(conf)
                summ = build_dashboard(conf, roster_idx, get)
                print(f"[ок] пересчитано · круговой этап "
                      f"{summ['rrPlayed']}/{summ['rrTotal']}")
                if args.push:
                    git_push(f"manual {time.strftime('%Y-%m-%d %H:%M')}")
                elif is_git_repo():
                    try:
                        ans = input("Опубликовать на GitHub? (y/n): ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        ans = ""
                    if ans in ("y", "yes", "д", "да"):
                        git_push(f"manual {time.strftime('%Y-%m-%d %H:%M')}")
                    else:
                        print("Не опубликовано. Позже: python update.py --no-scrape --push")
            else:
                print("[ручной] изменений нет")
        elif args.live is not None:
            live_loop()
        elif args.watch:
            print(f"[watch] онлайн-режим, опрос каждые {args.interval} с. "
                  f"Ctrl+C — остановить.")
            first = True
            while True:
                try:
                    one_pass(first)
                except Exception as e:
                    print(f"[watch] ошибка прохода: {e}")
                first = False
                time.sleep(max(30, args.interval))
        else:
            one_pass(True)
    except KeyboardInterrupt:
        print("\n[stop] остановлено.")
    finally:
        if browser:
            browser.close()
        if pw:
            pw.stop()


if __name__ == "__main__":
    main()
