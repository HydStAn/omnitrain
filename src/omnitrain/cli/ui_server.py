"""FastAPI Web UI backend serving Section 10 Flutter Design Blueprint."""
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from omnitrain.core.models import CompletionStatus, SportType, Workout
from omnitrain.core.reconcile import StateMachineReconciler
from omnitrain.core.status_update import StatusUpdateEvent, StatusUpdateHandler, UpdateType
from omnitrain.load.pmc import DailyLoad, PMCEngine
from omnitrain.parser.checkin import CheckinParser
from omnitrain.parser.goal import GoalParser
from omnitrain.sports.daniels import get_daniels_zones
from omnitrain.sports.running import RunningStrategy
from omnitrain.storage.db import Database

app = FastAPI(title="OmniTrain Web UI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "omnitrain" / "omnitrain.db"


def get_db() -> Database:
    db = Database(DEFAULT_DB_PATH)
    db.migrate()
    return db


@app.get("/api/state")
def get_state():
    db = get_db()
    today_str = date.today().isoformat()
    with db.get_connection() as conn:
        plan_row = conn.execute(
            "SELECT * FROM plans WHERE status = 'active' ORDER BY created_at DESC LIMIT 1;"
        ).fetchone()

        if not plan_row:
            return {"active_plan": None}

        plan_id = plan_row["id"]

        # 1. Next / Current Workouts
        workouts = conn.execute(
            """
            SELECT * FROM workouts 
            WHERE week_id IN (SELECT id FROM weeks WHERE plan_id = ?)
            ORDER BY date ASC;
            """,
            (plan_id,)
        ).fetchall()

        # 2. Zones
        zone_row = conn.execute(
            "SELECT * FROM training_zones WHERE user_id = ? ORDER BY calculated_at DESC LIMIT 1;",
            (plan_row["user_id"],)
        ).fetchone()
        zones = json.loads(zone_row["zones_json"]) if zone_row else {}

        # 3. PMC
        checkins = conn.execute(
            "SELECT timestamp, actual_metrics_json FROM checkin_logs WHERE actual_metrics_json IS NOT NULL ORDER BY timestamp ASC;"
        ).fetchall()
        loads = []
        for c in checkins:
            m = json.loads(c["actual_metrics_json"])
            tss = m.get("tss", 0.0) or 40.0
            loads.append(DailyLoad(date=c["timestamp"][:10], tss=tss))
        pmc_points = PMCEngine.calculate_pmc_series(loads)
        latest_pmc = pmc_points[-1].model_dump() if pmc_points else {"ctl": 0.0, "atl": 0.0, "tsb": 0.0}

        # 4. Weeks
        weeks = conn.execute(
            "SELECT * FROM weeks WHERE plan_id = ? ORDER BY week_number ASC;",
            (plan_id,)
        ).fetchall()

        # Group workouts by date
        w_list = [dict(w) for w in workouts]
        today_workout = next((w for w in w_list if w["date"] == today_str), None)
        if not today_workout:
            today_workout = next((w for w in w_list if w["status"] == "planned" and w["date"] >= today_str), None)

        return {
            "active_plan": dict(plan_row),
            "today_workout": today_workout,
            "workouts": w_list,
            "weeks": [dict(w) for w in weeks],
            "zones": zones,
            "pmc": latest_pmc
        }


class CheckinRequest(BaseModel):
    freetext: str


@app.post("/api/checkin")
def post_checkin(req: CheckinRequest):
    db = get_db()
    today = date.today()
    now_dt = datetime.now(timezone.utc)

    parser = CheckinParser()
    event = parser.parse(req.freetext)

    with db.get_connection() as conn:
        plan_row = conn.execute("SELECT user_id FROM plans WHERE status = 'active' LIMIT 1;").fetchone()
        user_id = plan_row["user_id"] if plan_row else "default-user"

        open_wo = conn.execute(
            "SELECT * FROM workouts WHERE status = 'planned' AND date <= ? ORDER BY date DESC LIMIT 1;",
            (today.isoformat(),)
        ).fetchone()

        if not open_wo:
            open_wo = conn.execute("SELECT * FROM workouts WHERE date = ? LIMIT 1;", (today.isoformat(),)).fetchone()

        matched_wo_id = open_wo["id"] if open_wo else None

        upcoming_rows = conn.execute(
            "SELECT * FROM workouts WHERE status = 'planned' AND date >= ? ORDER BY date ASC LIMIT 10;",
            (today.isoformat(),)
        ).fetchall()

        upcoming_workouts = [
            Workout(
                id=r["id"],
                week_id=r["week_id"],
                sport_type=SportType(r["sport_type"]),
                date=date.fromisoformat(r["date"]),
                day_of_week=r["day_of_week"],
                workout_type=r["workout_type"],
                metric_primary=r["metric_primary"],
                metric_unit=r["metric_unit"],
                intensity_target=r["intensity_target"],
                intensity_detail=r["intensity_detail"],
                status=CompletionStatus(r["status"])
            )
            for r in upcoming_rows
        ]

        mutations = StateMachineReconciler.reconcile_checkin(
            checkin=event,
            checkin_date=today,
            upcoming_workouts=upcoming_workouts
        )

        for wo in upcoming_workouts:
            conn.execute(
                """
                UPDATE workouts 
                SET workout_type = ?, metric_primary = ?, intensity_target = ?, intensity_detail = ?, updated_at = ?
                WHERE id = ?;
                """,
                (wo.workout_type, wo.metric_primary, wo.intensity_target, wo.intensity_detail, now_dt.isoformat(), wo.id)
            )

        if matched_wo_id:
            conn.execute(
                "UPDATE workouts SET status = ?, updated_at = ? WHERE id = ?;",
                (event.completion_status.value, now_dt.isoformat(), matched_wo_id)
            )

        log_id = str(uuid.uuid4())
        metrics_json = json.dumps(event.actual_metrics.model_dump()) if event.actual_metrics else None
        symptoms_json = json.dumps([s.model_dump() for s in event.symptoms])
        applied_json = json.dumps([m.model_dump() for m in mutations])

        conn.execute(
            """
            INSERT INTO checkin_logs (
                id, workout_id, user_id, timestamp, raw_input, completion_status,
                actual_metrics_json, perceived_rpe, symptoms_json, applied_mutations_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                log_id, matched_wo_id, user_id, now_dt.isoformat(), req.freetext,
                event.completion_status.value, metrics_json, event.perceived_rpe,
                symptoms_json, applied_json, now_dt.isoformat()
            )
        )
        conn.commit()

    return {
        "event": event.model_dump(),
        "mutations": [m.model_dump() for m in mutations]
    }


class GoalRequest(BaseModel):
    prompt: str


@app.post("/api/onboard")
def post_onboard(req: GoalRequest):
    from omnitrain.cli.main import plan_new
    parser = GoalParser()
    res = parser.parse(req.prompt)
    plan_new(
        sport=res.sport_type,
        goal=res.target_event,
        vdot=res.user_profile.vdot or 45.0,
        weeks_count=res.target_weeks,
        baseline_volume=res.current_baseline.value
    )
    return {"status": "ok", "parsed": res.model_dump()}


@app.get("/", response_class=HTMLResponse)
def index():
    html_content = """<!DOCTYPE html>
<html lang="de" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>OmniTrain · Adaptive Multi-Sport Coach</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          fontFamily: {
            sans: ['Inter', 'sans-serif'],
            mono: ['JetBrains Mono', 'monospace'],
          },
          colors: {
            slate: { 850: '#151F32', 900: '#0F172A', 950: '#0A0F1D' },
            electric: '#0D9488',
            running: '#F97316',
            cycling: '#22C55E',
            strength: '#EF4444',
            swimming: '#06B6D4'
          }
        }
      }
    }
  </script>
  <style>
    body { background-color: #0A0F1D; color: #F8FAFC; }
    .glass-card { background: rgba(15, 23, 42, 0.75); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.08); }
    .glass-pill { background: rgba(30, 41, 59, 0.7); border: 1px solid rgba(255, 255, 255, 0.05); }
  </style>
</head>
<body class="min-h-screen font-sans flex justify-center py-6 px-4">
  <div class="w-full max-w-xl flex flex-col gap-6">

    <!-- Top Navigation / Status Header -->
    <header class="flex items-center justify-between border-b border-slate-800/80 pb-4">
      <div>
        <div class="flex items-center gap-2">
          <span class="w-2.5 h-2.5 rounded-full bg-teal-400 animate-pulse"></span>
          <h1 class="font-bold text-lg tracking-tight">OmniTrain</h1>
          <span class="text-xs px-2 py-0.5 rounded bg-slate-800 text-slate-400 font-mono">v0.1 Core</span>
        </div>
        <p id="plan-title" class="text-xs text-slate-400 mt-0.5">Lade Trainingsplan...</p>
      </div>
      <div class="flex items-center gap-2">
        <button onclick="openOnboardModal()" class="text-xs font-medium px-3 py-1.5 rounded-lg bg-teal-600/20 text-teal-400 border border-teal-500/30 hover:bg-teal-600/30 transition">
          + Neues Ziel
        </button>
      </div>
    </header>

    <!-- PMC Metrics Pill Bar -->
    <div id="pmc-bar" class="grid grid-cols-3 gap-2 text-center text-xs">
      <div class="glass-pill p-2 rounded-xl">
        <span class="text-slate-400 block text-[10px] uppercase tracking-wider font-semibold">Fitness (CTL)</span>
        <span id="pmc-ctl" class="font-mono text-base font-bold text-teal-400">--</span>
      </div>
      <div class="glass-pill p-2 rounded-xl">
        <span class="text-slate-400 block text-[10px] uppercase tracking-wider font-semibold">Fatigue (ATL)</span>
        <span id="pmc-atl" class="font-mono text-base font-bold text-purple-400">--</span>
      </div>
      <div class="glass-pill p-2 rounded-xl">
        <span class="text-slate-400 block text-[10px] uppercase tracking-wider font-semibold">Form (TSB)</span>
        <span id="pmc-tsb" class="font-mono text-base font-bold text-amber-400">--</span>
      </div>
    </div>

    <!-- Screen 1: Heutiges Workout (Hero Card) -->
    <section class="glass-card rounded-2xl p-5 shadow-2xl relative overflow-hidden border border-teal-500/20">
      <div class="flex justify-between items-start mb-3">
        <div class="flex items-center gap-2">
          <span id="sport-badge" class="px-2 py-0.5 text-xs font-semibold rounded-md bg-orange-500/20 text-orange-400 border border-orange-500/30 uppercase tracking-wide">
            Laufen
          </span>
          <span id="today-date" class="text-xs text-slate-400 font-mono">Heute</span>
        </div>
        <span id="today-status" class="text-xs font-medium text-amber-400 bg-amber-400/10 px-2 py-0.5 rounded">Geplant</span>
      </div>

      <div class="my-3">
        <h2 id="today-workout-title" class="text-2xl font-bold tracking-tight text-white mb-1">Dauerlauf</h2>
        <div class="flex items-baseline gap-2">
          <span id="today-metric" class="font-mono text-3xl font-extrabold text-teal-400">-- km</span>
          <span id="today-intensity" class="text-sm font-mono text-slate-300">Pace: --:-- min/km</span>
        </div>
      </div>

      <!-- Quick Action: Conversational Check-in trigger -->
      <div class="mt-4 pt-4 border-t border-slate-800 flex gap-2">
        <button onclick="focusCheckin()" class="flex-1 bg-gradient-to-r from-teal-600 to-teal-500 hover:from-teal-500 hover:to-teal-400 text-white font-medium text-sm py-2.5 px-4 rounded-xl shadow-lg transition flex items-center justify-center gap-2">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z"></path></svg>
          Workout Check-in
        </button>
      </div>
    </section>

    <!-- Mini-Wochenansicht (7 Kreise Mo-So) -->
    <section>
      <div class="flex justify-between items-center mb-2 px-1">
        <h3 class="text-xs font-semibold uppercase tracking-wider text-slate-400">Diese Woche</h3>
        <span id="week-phase" class="text-xs font-medium text-teal-400">Phase: BUILD</span>
      </div>
      <div id="week-dots" class="grid grid-cols-7 gap-2"></div>
    </section>

    <!-- Screen 2: Conversational Check-in (Things 3 / Messenger UX) -->
    <section class="glass-card rounded-2xl p-5">
      <div class="flex items-center gap-2 mb-3">
        <svg class="w-4 h-4 text-teal-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 100-6 3 3 0 000 6z"></path></svg>
        <h3 class="text-sm font-semibold tracking-tight text-slate-200">Conversational Coach</h3>
      </div>

      <div id="chat-messages" class="flex flex-col gap-2.5 max-h-64 overflow-y-auto mb-3 text-sm pr-1">
        <div class="bg-slate-800/80 rounded-2xl rounded-tl-sm p-3 max-w-[88%] text-slate-300">
          Wie war dein Training heute? Erzähl mir frei von Distanz, Gefühl (RPE 1-10) oder etwaigen Schmerzen / Symptomen.
        </div>
      </div>

      <form onsubmit="submitCheckin(event)" class="relative flex items-center">
        <input id="checkin-input" type="text" placeholder="z.B. '6 km gelaufen, Knie zwickt leicht (3/10), sonst RPE 6'" 
               class="w-full bg-slate-950/80 border border-slate-800 rounded-xl py-2.5 pl-3.5 pr-12 text-sm text-slate-200 focus:outline-none focus:border-teal-500 transition placeholder-slate-500">
        <button type="submit" class="absolute right-1.5 p-1.5 rounded-lg bg-teal-600 hover:bg-teal-500 text-white transition">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>
        </button>
      </form>
    </section>

    <!-- Screen 3: Zonen & Makrozyklus Tabs -->
    <section class="glass-card rounded-2xl p-5">
      <div class="flex border-b border-slate-800 mb-4 pb-2 gap-4">
        <button onclick="switchTab('zones')" id="tab-btn-zones" class="text-sm font-semibold text-teal-400 border-b-2 border-teal-400 pb-1">Trainingszonen</button>
        <button onclick="switchTab('macro')" id="tab-btn-macro" class="text-sm font-medium text-slate-400 hover:text-slate-200 pb-1">Makrozyklus (Wochen)</button>
      </div>

      <div id="tab-content-zones" class="overflow-x-auto">
        <table class="w-full text-left text-xs">
          <thead>
            <tr class="text-slate-400 border-b border-slate-800/80">
              <th class="pb-2">Zone</th>
              <th class="pb-2">Bereich</th>
              <th class="pb-2">Fokus</th>
            </tr>
          </thead>
          <tbody id="zones-table-body" class="divide-y divide-slate-800/50"></tbody>
        </table>
      </div>

      <div id="tab-content-macro" class="hidden max-h-60 overflow-y-auto pr-1">
        <div id="macro-weeks-list" class="flex flex-col gap-2"></div>
      </div>
    </section>

  </div>

  <!-- Onboard Modal -->
  <div id="onboard-modal" class="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4 hidden">
    <div class="glass-card bg-slate-900 border border-slate-700 w-full max-w-md rounded-2xl p-6 shadow-2xl">
      <h3 class="text-lg font-bold mb-2">Neues Ziel definieren</h3>
      <p class="text-xs text-slate-400 mb-4">Beschreibe dein Ziel in natürlicher Sprache (Sportart, Distanz, Wochen, Tage, Fitness):</p>
      <textarea id="onboard-prompt" rows="3" class="w-full bg-slate-950 border border-slate-800 rounded-xl p-3 text-sm text-slate-200 focus:outline-none focus:border-teal-500 mb-4" placeholder="z.B. 'Marathon in 20 Wochen. Laufe aktuell 30 km/Woche an Di, Do, Sa, So. VDOT 48'"></textarea>
      <div class="flex justify-end gap-2">
        <button onclick="closeOnboardModal()" class="px-4 py-2 text-xs font-medium text-slate-400 hover:text-white">Abbrechen</button>
        <button onclick="submitOnboard()" class="px-4 py-2 text-xs font-semibold rounded-lg bg-teal-600 hover:bg-teal-500 text-white transition">Plan generieren</button>
      </div>
    </div>
  </div>

  <script>
    let appState = null;

    async function loadState() {
      try {
        const res = await fetch('/api/state');
        appState = await res.json();
        render();
      } catch (err) {
        console.error(err);
      }
    }

    function render() {
      if (!appState || !appState.active_plan) {
        document.getElementById('plan-title').innerText = "Kein aktiver Plan vorhanden";
        return;
      }
      const plan = appState.active_plan;
      document.getElementById('plan-title').innerText = `${plan.sport_type.toUpperCase()} · ${plan.goal_type.replace('_', ' ').toUpperCase()} (Ziel: ${plan.target_date})`;

      // PMC
      document.getElementById('pmc-ctl').innerText = appState.pmc.ctl;
      document.getElementById('pmc-atl').innerText = appState.pmc.atl;
      document.getElementById('pmc-tsb').innerText = appState.pmc.tsb;

      // Today
      const tw = appState.today_workout;
      if (tw) {
        document.getElementById('today-workout-title').innerText = tw.workout_type.replace('_', ' ').toUpperCase();
        document.getElementById('today-metric').innerText = tw.metric_primary > 0 ? `${tw.metric_primary} ${tw.metric_unit}` : 'Regeneration';
        document.getElementById('today-intensity').innerText = tw.intensity_detail || tw.intensity_target || '';
        document.getElementById('today-date').innerText = tw.date;
        document.getElementById('today-status').innerText = tw.status.toUpperCase();
      }

      // Mini-Wochenansicht
      renderWeekDots();

      // Zones Table
      renderZones();

      // Macro
      renderMacro();
    }

    function renderWeekDots() {
      const container = document.getElementById('week-dots');
      container.innerHTML = '';
      const days = ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So'];
      const todayStr = new Date().toISOString().slice(0, 10);

      // Take next 7 workouts or current week
      const currentWeekWorkouts = appState.workouts.slice(0, 7);
      days.forEach((dayName, idx) => {
        const wo = currentWeekWorkouts.find(w => w.day_of_week === (idx + 1));
        const col = document.createElement('div');
        col.className = 'flex flex-col items-center gap-1.5 p-2 rounded-xl bg-slate-900/60 border border-slate-800/60';

        let dotColor = 'bg-slate-700';
        let statusText = 'Rest';
        if (wo) {
          statusText = `${wo.metric_primary}${wo.metric_unit}`;
          if (wo.status === 'completed') dotColor = 'bg-emerald-400';
          else if (wo.workout_type === 'rest') dotColor = 'bg-rose-500';
          else dotColor = 'bg-teal-400 ring-2 ring-teal-500/20';
        }

        col.innerHTML = `
          <span class="text-[11px] font-semibold text-slate-400">${dayName}</span>
          <span class="w-3.5 h-3.5 rounded-full ${dotColor}"></span>
          <span class="text-[9px] font-mono text-slate-500 truncate w-full text-center">${statusText}</span>
        `;
        container.appendChild(col);
      });
    }

    function renderZones() {
      const tbody = document.getElementById('zones-table-body');
      tbody.innerHTML = '';
      for (const [k, z] of Object.entries(appState.zones)) {
        const tr = document.createElement('tr');
        tr.className = 'hover:bg-slate-800/20';
        const target = z.min_pace ? `${z.min_pace}–${z.max_pace} min/km` : (z.pace ? `${z.pace} min/km` : '-');
        tr.innerHTML = `
          <td class="py-2.5 font-bold text-teal-400">${z.name || k}</td>
          <td class="py-2.5 font-mono text-slate-200">${target}</td>
          <td class="py-2.5 text-slate-400">${z.description || '-'}</td>
        `;
        tbody.appendChild(tr);
      }
    }

    function renderMacro() {
      const container = document.getElementById('macro-weeks-list');
      container.innerHTML = '';
      appState.weeks.forEach(w => {
        const item = document.createElement('div');
        item.className = 'flex items-center justify-between p-2 rounded-lg bg-slate-950/60 border border-slate-800 text-xs';
        const phaseColor = w.phase === 'peak' ? 'text-red-400' : (w.phase === 'taper' ? 'text-emerald-400' : 'text-teal-400');
        item.innerHTML = `
          <div class="flex items-center gap-2">
            <span class="font-mono text-slate-400">W${w.week_number}</span>
            <span class="font-bold ${phaseColor}">${w.phase.toUpperCase()}</span>
            ${w.is_recovery_week ? '<span class="text-[10px] text-amber-400 bg-amber-400/10 px-1.5 py-0.5 rounded">Entlastung</span>' : ''}
          </div>
          <div class="font-mono font-bold text-slate-200">
            ${w.target_weekly_volume} km
          </div>
        `;
        container.appendChild(item);
      });
    }

    function focusCheckin() {
      document.getElementById('checkin-input').focus();
    }

    async function submitCheckin(e) {
      e.preventDefault();
      const input = document.getElementById('checkin-input');
      const text = input.value.trim();
      if (!text) return;

      const chat = document.getElementById('chat-messages');
      chat.innerHTML += `<div class="bg-teal-600 text-white rounded-2xl rounded-tr-sm p-3 max-w-[85%] self-end text-sm">${text}</div>`;
      input.value = '';
      chat.scrollTop = chat.scrollHeight;

      try {
        const res = await fetch('/api/checkin', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({freetext: text})
        });
        const data = await res.json();
        
        let reply = `✅ Status: <strong>${data.event.completion_status.toUpperCase()}</strong> (RPE: ${data.event.perceived_rpe || '-'})`;
        if (data.mutations && data.mutations.length > 0) {
          reply += `<div class="mt-2 pt-2 border-t border-slate-700/60 text-xs text-amber-300">${data.mutations[0].description}</div>`;
        }
        chat.innerHTML += `<div class="bg-slate-800 rounded-2xl rounded-tl-sm p-3 max-w-[88%] text-slate-200 text-sm">${reply}</div>`;
        chat.scrollTop = chat.scrollHeight;

        await loadState();
      } catch (err) {
        console.error(err);
      }
    }

    function openOnboardModal() { document.getElementById('onboard-modal').classList.remove('hidden'); }
    function closeOnboardModal() { document.getElementById('onboard-modal').classList.add('hidden'); }

    async function submitOnboard() {
      const prompt = document.getElementById('onboard-prompt').value;
      if (!prompt) return;
      await fetch('/api/onboard', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({prompt})
      });
      closeOnboardModal();
      await loadState();
    }

    function switchTab(t) {
      if (t === 'zones') {
        document.getElementById('tab-content-zones').classList.remove('hidden');
        document.getElementById('tab-content-macro').classList.add('hidden');
        document.getElementById('tab-btn-zones').className = "text-sm font-semibold text-teal-400 border-b-2 border-teal-400 pb-1";
        document.getElementById('tab-btn-macro').className = "text-sm font-medium text-slate-400 hover:text-slate-200 pb-1";
      } else {
        document.getElementById('tab-content-zones').classList.add('hidden');
        document.getElementById('tab-content-macro').classList.remove('hidden');
        document.getElementById('tab-btn-macro').className = "text-sm font-semibold text-teal-400 border-b-2 border-teal-400 pb-1";
        document.getElementById('tab-btn-zones').className = "text-sm font-medium text-slate-400 hover:text-slate-200 pb-1";
      }
    }

    loadState();
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)


def start_server(host: str = "127.0.0.1", port: int = 8089):
    import uvicorn
    uvicorn.run(app, host=host, port=port)
