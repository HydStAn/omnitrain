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
    allow_origins=["*"],  # nosemgrep: python.fastapi.security.wildcard-cors.wildcard-cors
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
def get_state(plan_id: Optional[str] = None, show_archived: bool = False):
    db = get_db()
    today_str = date.today().isoformat()
    with db.get_connection() as conn:
        all_plans = conn.execute(
            "SELECT * FROM plans ORDER BY created_at DESC;"
        ).fetchall()

        if not all_plans:
            return {"active_plan": None, "all_plans": []}

        active_plans = [p for p in all_plans if p["status"] == "active"]
        candidate_plans = all_plans if show_archived else (active_plans if active_plans else all_plans)

        if plan_id:
            plan_row = next((p for p in all_plans if p["id"] == plan_id), candidate_plans[0])
        else:
            plan_row = candidate_plans[0]

        selected_plan_id = plan_row["id"]
        user_id = plan_row["user_id"]

        # 1. Workouts
        workouts = conn.execute(
            """
            SELECT * FROM workouts 
            WHERE week_id IN (SELECT id FROM weeks WHERE plan_id = ?)
            ORDER BY date ASC;
            """,
            (selected_plan_id,)
        ).fetchall()

        # 2. Zones for current plan sport
        plan_sport = plan_row["sport_type"] if plan_row else "running"
        zone_row = conn.execute(
            "SELECT * FROM training_zones WHERE user_id = ? AND sport_type = ? ORDER BY calculated_at DESC LIMIT 1;",
            (user_id, plan_sport)
        ).fetchone()
        if not zone_row:
            zone_row = conn.execute(
                "SELECT * FROM training_zones WHERE user_id = ? ORDER BY calculated_at DESC LIMIT 1;",
                (user_id,)
            ).fetchone()
        zones = json.loads(zone_row["zones_json"]) if zone_row else {}
        ref_vdot = zone_row["reference_value"] if zone_row else 45.0
        zone_model = zone_row["zone_model"] if zone_row else "daniels_vdot"

        # 3. User & Profile
        user_row = conn.execute("SELECT * FROM users WHERE id = ? LIMIT 1;", (user_id,)).fetchone()
        user_dict = dict(user_row) if user_row else {"display_name": "Stephan", "id": user_id}
        profile_json = json.loads(user_dict.get("profile_json") or "{}") if user_dict else {}

        # 4. PMC
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
        pmc_series_data = [p.model_dump() for p in pmc_points[-30:]] if pmc_points else []

        # 5. Weeks
        weeks = conn.execute(
            "SELECT * FROM weeks WHERE plan_id = ? ORDER BY week_number ASC;",
            (selected_plan_id,)
        ).fetchall()

        # Group workouts by date
        w_list = [dict(w) for w in workouts]
        today_workout = next((w for w in w_list if w["date"] == today_str), None)
        if not today_workout:
            today_workout = next((w for w in w_list if w["status"] == "planned" and w["date"] >= today_str), None)

        has_sick_workouts = any(
            w["date"] >= today_str and "Krankheit" in (w["intensity_detail"] or "")
            for w in w_list
        )

        return {
            "active_plan": dict(plan_row),
            "all_plans": [dict(p) for p in all_plans],
            "today_workout": today_workout,
            "workouts": w_list,
            "weeks": [dict(w) for w in weeks],
            "zones": zones,
            "ref_vdot": ref_vdot,
            "zone_model": zone_model,
            "user": user_dict,
            "profile": profile_json,
            "pmc": latest_pmc,
            "pmc_series": pmc_series_data,
            "is_sick_mode": has_sick_workouts
        }


class ProfileUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    weight_kg: Optional[float] = None
    resting_hr: Optional[int] = None
    max_hr: Optional[int] = None
    vdot: Optional[float] = None
    ftp_watts: Optional[int] = None
    swim_css: Optional[str] = None
    menstrual_cycle_active: Optional[bool] = None


@app.post("/api/profile/update")
def update_profile(req: ProfileUpdateRequest):
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    with db.get_connection() as conn:
        user_row = conn.execute("SELECT id FROM users LIMIT 1;").fetchone()
        if not user_row:
            raise HTTPException(status_code=404, detail="User not found")
        user_id = user_row["id"]

        extra_profile = {
            "ftp_watts": req.ftp_watts,
            "swim_css": req.swim_css,
            "menstrual_cycle_active": req.menstrual_cycle_active
        }

        conn.execute(
            """
            UPDATE users
            SET display_name = COALESCE(?, display_name),
                weight_kg = COALESCE(?, weight_kg),
                resting_hr = COALESCE(?, resting_hr),
                max_hr = COALESCE(?, max_hr),
                profile_json = ?,
                updated_at = ?
            WHERE id = ?;
            """,
            (req.display_name, req.weight_kg, req.resting_hr, req.max_hr, json.dumps(extra_profile), now, user_id)
        )

        if req.vdot:
            new_zones = get_daniels_zones(req.vdot)
            zone_row = conn.execute(
                "SELECT id FROM training_zones WHERE user_id = ? AND sport_type = 'running' LIMIT 1;",
                (user_id,)
            ).fetchone()
            if zone_row:
                conn.execute(
                    """
                    UPDATE training_zones
                    SET reference_value = ?, zones_json = ?, calculated_at = ?, updated_at = ?
                    WHERE id = ?;
                    """,
                    (req.vdot, json.dumps(new_zones), now, now, zone_row["id"])
                )
            else:
                conn.execute(
                    """
                    INSERT INTO training_zones (id, user_id, sport_type, zone_model, reference_value, zones_json, calculated_at, created_at, updated_at)
                    VALUES (?, ?, 'running', 'daniels_vdot', ?, ?, ?, ?, ?);
                    """,
                    (str(uuid.uuid4()), user_id, req.vdot, json.dumps(new_zones), now, now, now)
                )

        conn.commit()
    return {"status": "ok"}


class RecalculateZonesRequest(BaseModel):
    reference_value: Optional[float] = None
    vdot: Optional[float] = None
    sport_type: Optional[str] = None


@app.post("/api/profile/recalculate-zones")
def recalculate_zones(req: RecalculateZonesRequest):
    from omnitrain.sports.cycling import CyclingStrategy
    from omnitrain.sports.swimming import SwimStrategy
    from omnitrain.sports.strength import StrengthStrategy

    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    ref_val = req.reference_value or req.vdot or 45.0

    with db.get_connection() as conn:
        user_row = conn.execute("SELECT id FROM users LIMIT 1;").fetchone()
        if not user_row:
            raise HTTPException(status_code=404, detail="User not found")
        user_id = user_row["id"]

        sport = req.sport_type
        if not sport:
            active_plan = conn.execute("SELECT sport_type FROM plans WHERE status = 'active' ORDER BY created_at DESC LIMIT 1;").fetchone()
            sport = active_plan["sport_type"] if active_plan else "running"

        if sport == "cycling":
            strat = CyclingStrategy()
            new_zones = strat.calculate_zones(ref_val)
            zone_model = "coggan_ftp"
        elif sport == "swimming":
            strat = SwimStrategy()
            new_zones = strat.calculate_zones(ref_val)
            zone_model = "swim_css"
        elif sport == "strength":
            strat = StrengthStrategy()
            new_zones = strat.calculate_zones(ref_val)
            zone_model = "strength_dup"
        elif sport in ["triathlon", "multisport"]:
            new_zones = get_daniels_zones(ref_val)
            zone_model = "triathlon_hybrid"
        else:
            new_zones = get_daniels_zones(ref_val)
            zone_model = "daniels_vdot"

        zone_row = conn.execute(
            "SELECT id FROM training_zones WHERE user_id = ? AND sport_type = ? LIMIT 1;",
            (user_id, sport)
        ).fetchone()
        if zone_row:
            conn.execute(
                """
                UPDATE training_zones
                SET reference_value = ?, zones_json = ?, zone_model = ?, calculated_at = ?, updated_at = ?
                WHERE id = ?;
                """,
                (ref_val, json.dumps(new_zones), zone_model, now, now, zone_row["id"])
            )
        else:
            conn.execute(
                """
                INSERT INTO training_zones (id, user_id, sport_type, zone_model, reference_value, zones_json, calculated_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (str(uuid.uuid4()), user_id, sport, zone_model, ref_val, json.dumps(new_zones), now, now, now)
            )
        conn.commit()
    return {"status": "ok", "zones": new_zones, "zone_model": zone_model}


class CheckinRequest(BaseModel):
    freetext: str
    plan_id: Optional[str] = None


@app.post("/api/checkin")
def post_checkin(req: CheckinRequest):
    db = get_db()
    today = date.today()
    now_dt = datetime.now(timezone.utc)

    parser = CheckinParser()
    event = parser.parse(req.freetext)

    with db.get_connection() as conn:
        if req.plan_id:
            plan_row = conn.execute("SELECT * FROM plans WHERE id = ? LIMIT 1;", (req.plan_id,)).fetchone()
        else:
            plan_row = conn.execute("SELECT * FROM plans ORDER BY created_at DESC LIMIT 1;").fetchone()

        if not plan_row:
            raise HTTPException(status_code=404, detail="No active plan found")

        selected_plan_id = plan_row["id"]
        user_id = plan_row["user_id"]

        open_wo = conn.execute(
            """
            SELECT w.* FROM workouts w
            JOIN weeks wk ON w.week_id = wk.id
            WHERE wk.plan_id = ? AND w.status = 'planned' AND w.date <= ?
            ORDER BY w.date DESC LIMIT 1;
            """,
            (selected_plan_id, today.isoformat())
        ).fetchone()

        if not open_wo:
            open_wo = conn.execute(
                """
                SELECT w.* FROM workouts w
                JOIN weeks wk ON w.week_id = wk.id
                WHERE wk.plan_id = ? AND w.date = ? LIMIT 1;
                """,
                (selected_plan_id, today.isoformat())
            ).fetchone()

        matched_wo_id = open_wo["id"] if open_wo else None

        upcoming_rows = conn.execute(
            """
            SELECT w.* FROM workouts w
            JOIN weeks wk ON w.week_id = wk.id
            WHERE wk.plan_id = ? AND w.status = 'planned' AND w.date >= ?
            ORDER BY w.date ASC LIMIT 10;
            """,
            (selected_plan_id, today.isoformat())
        ).fetchall()

        before_state = {
            r["id"]: {
                "id": r["id"],
                "date": r["date"],
                "workout_type": r["workout_type"],
                "metric_primary": r["metric_primary"],
                "metric_unit": r["metric_unit"],
                "intensity_detail": r["intensity_detail"]
            }
            for r in upcoming_rows
        }

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

        diffs = []
        for wo in upcoming_workouts:
            before = before_state.get(wo.id)
            if before and (
                before["workout_type"] != wo.workout_type or
                before["metric_primary"] != wo.metric_primary or
                before["intensity_detail"] != wo.intensity_detail
            ):
                diffs.append({
                    "id": wo.id,
                    "date": str(wo.date),
                    "before": {
                        "workout_type": before["workout_type"],
                        "metric_primary": before["metric_primary"],
                        "metric_unit": before["metric_unit"],
                        "intensity_detail": before["intensity_detail"]
                    },
                    "after": {
                        "workout_type": wo.workout_type,
                        "metric_primary": wo.metric_primary,
                        "metric_unit": wo.metric_unit,
                        "intensity_detail": wo.intensity_detail
                    }
                })

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
        "mutations": [m.model_dump() for m in mutations],
        "diffs": diffs
    }


class StatusUpdateRequest(BaseModel):
    update_type: str  # recovery, pain_resolved, readiness
    note: Optional[str] = None
    severity: Optional[int] = None
    plan_id: Optional[str] = None


@app.post("/api/status-update")
def post_status_update(req: StatusUpdateRequest):
    db = get_db()
    today = date.today()
    now_dt = datetime.now(timezone.utc)

    u_type = UpdateType.RECOVERY
    details = {}
    if req.update_type == "recovery":
        u_type = UpdateType.RECOVERY
        details = {"condition": "sickness", "status": "resolved"}
    elif req.update_type in ["pain_resolved", "pain"]:
        u_type = UpdateType.PAIN_UPDATE
        details = {"status": "resolved"}
    elif req.update_type in ["readiness", "fatigue"]:
        u_type = UpdateType.READINESS_UPDATE
        details = {"severity": req.severity or 8}

    event = StatusUpdateEvent(update_type=u_type, details=details, notes=req.note)

    with db.get_connection() as conn:
        if req.plan_id:
            plan_row = conn.execute("SELECT * FROM plans WHERE id = ? LIMIT 1;", (req.plan_id,)).fetchone()
        else:
            plan_row = conn.execute("SELECT * FROM plans ORDER BY created_at DESC LIMIT 1;").fetchone()

        if not plan_row:
            raise HTTPException(status_code=404, detail="No active plan found")

        selected_plan_id = plan_row["id"]

        upcoming_rows = conn.execute(
            """
            SELECT w.* FROM workouts w
            JOIN weeks wk ON w.week_id = wk.id
            WHERE wk.plan_id = ? AND w.date >= ?
            ORDER BY w.date ASC LIMIT 14;
            """,
            (selected_plan_id, today.isoformat())
        ).fetchall()

        before_state = {
            r["id"]: {
                "id": r["id"],
                "date": r["date"],
                "workout_type": r["workout_type"],
                "metric_primary": r["metric_primary"],
                "metric_unit": r["metric_unit"],
                "intensity_detail": r["intensity_detail"]
            }
            for r in upcoming_rows
        }

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

        mutations = StatusUpdateHandler.handle_status_update(
            update=event,
            current_date=today,
            upcoming_workouts=upcoming_workouts
        )

        diffs = []
        for wo in upcoming_workouts:
            before = before_state.get(wo.id)
            if before and (
                before["workout_type"] != wo.workout_type or
                before["metric_primary"] != wo.metric_primary or
                before["intensity_detail"] != wo.intensity_detail
            ):
                diffs.append({
                    "id": wo.id,
                    "date": str(wo.date),
                    "before": {
                        "workout_type": before["workout_type"],
                        "metric_primary": before["metric_primary"],
                        "metric_unit": before["metric_unit"],
                        "intensity_detail": before["intensity_detail"]
                    },
                    "after": {
                        "workout_type": wo.workout_type,
                        "metric_primary": wo.metric_primary,
                        "metric_unit": wo.metric_unit,
                        "intensity_detail": wo.intensity_detail
                    }
                })

            conn.execute(
                """
                UPDATE workouts
                SET workout_type = ?, metric_primary = ?, intensity_target = ?, intensity_detail = ?, updated_at = ?
                WHERE id = ?;
                """,
                (wo.workout_type, wo.metric_primary, wo.intensity_target, wo.intensity_detail, now_dt.isoformat(), wo.id)
            )
        conn.commit()

    return {
        "status": "ok",
        "mutations": [m.model_dump() for m in mutations],
        "diffs": diffs
    }


class CheckinPreviewRequest(BaseModel):
    freetext: str


@app.post("/api/checkin/preview")
def preview_checkin(req: CheckinPreviewRequest):
    parser = CheckinParser()
    event = parser.parse(req.freetext)
    return {"event": event.model_dump()}


class GoalPreviewRequest(BaseModel):
    prompt: str


@app.post("/api/onboard/preview")
def preview_onboard(req: GoalPreviewRequest):
    parser = GoalParser()
    res = parser.parse(req.prompt)
    return {"parsed": res.model_dump()}


class PlanCreateRequest(BaseModel):
    sport: str
    goal: str
    vdot: float
    weeks_count: int
    baseline_volume: float


@app.post("/api/plan/create")
def create_plan(req: PlanCreateRequest):
    from omnitrain.cli.main import plan_new
    plan_new(
        sport=req.sport,
        goal=req.goal,
        vdot=req.vdot,
        weeks_count=req.weeks_count,
        baseline_volume=req.baseline_volume
    )
    return {"status": "ok"}


class PlanActionRequest(BaseModel):
    plan_id: str


@app.post("/api/plan/archive")
def archive_plan(req: PlanActionRequest):
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE plans SET status = 'archived', updated_at = ? WHERE id = ?;",
            (now, req.plan_id)
        )
        conn.commit()
    return {"status": "ok", "message": "Plan archiviert"}


@app.post("/api/plan/unarchive")
def unarchive_plan(req: PlanActionRequest):
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE plans SET status = 'active', updated_at = ? WHERE id = ?;",
            (now, req.plan_id)
        )
        conn.commit()
    return {"status": "ok", "message": "Plan reaktiviert"}


@app.post("/api/plan/delete")
def delete_plan(req: PlanActionRequest):
    db = get_db()
    with db.get_connection() as conn:
        # Cascade delete workouts, weeks, and plan
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("DELETE FROM workouts WHERE week_id IN (SELECT id FROM weeks WHERE plan_id = ?);", (req.plan_id,))
        conn.execute("DELETE FROM weeks WHERE plan_id = ?;", (req.plan_id,))
        conn.execute("DELETE FROM plans WHERE id = ?;", (req.plan_id,))
        conn.commit()
    return {"status": "ok", "message": "Plan gelöscht"}


@app.get("/", response_class=HTMLResponse)
def index():
    html_content = """<!DOCTYPE html>
<html lang="de" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>OmniTrain · Adaptive Multi-Sport Coach</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
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
    .hide-scrollbar::-webkit-scrollbar { display: none; }
    .hide-scrollbar { -ms-overflow-style: none; scrollbar-width: none; }
  </style>
</head>
<body class="min-h-screen font-sans flex justify-center py-4 sm:py-6 px-2.5 sm:px-4 pb-28 overflow-x-hidden w-full">
  <div class="w-full max-w-xl flex flex-col gap-5 sm:gap-6 min-w-0">

    <!-- Top Navigation / Status Header -->
    <header class="flex flex-col sm:flex-row sm:items-center justify-between border-b border-slate-800/80 pb-3 sm:pb-4 gap-3">
      <div class="min-w-0">
        <div class="flex items-center gap-2">
          <span class="w-2.5 h-2.5 rounded-full bg-teal-400 animate-pulse shrink-0"></span>
          <h1 class="font-bold text-lg tracking-tight truncate">OmniTrain</h1>
          <span class="text-xs px-2 py-0.5 rounded bg-slate-800 text-slate-400 font-mono shrink-0">v0.1 Blueprint</span>
        </div>
        <p id="plan-title" class="text-xs text-slate-400 mt-0.5 truncate">Lade Trainingsplan...</p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <select id="plan-selector" onchange="onPlanSelect(this.value)" class="text-xs bg-slate-900 border border-slate-700 text-slate-200 rounded-lg px-2.5 py-1.5 focus:outline-none focus:border-teal-500 font-medium max-w-[140px] sm:max-w-[200px] truncate shrink-0">
          <!-- Dynamically populated -->
        </select>
        <button onclick="openStatusModal()" class="text-xs font-semibold px-2.5 sm:px-3 py-1.5 rounded-lg bg-amber-500/20 text-amber-300 border border-amber-500/30 hover:bg-amber-500/30 transition flex items-center gap-1.5 shrink-0">
          <span>🩺</span> <span class="hidden xs:inline">Wie geht's dir?</span><span class="xs:hidden">Status</span>
        </button>
        <button onclick="openOnboardModal()" class="text-xs font-semibold px-2.5 sm:px-3 py-1.5 rounded-lg bg-teal-600 hover:bg-teal-500 text-white shadow transition shrink-0">
          + Ziel
        </button>
      </div>
    </header>

    <!-- SICK Mode Banner -->
    <div id="sick-banner" class="hidden glass-card border-rose-500/40 bg-rose-950/40 rounded-2xl p-3.5 sm:p-4 flex flex-col sm:flex-row sm:items-center justify-between gap-3 text-rose-200 text-xs">
      <div class="flex items-center gap-3 min-w-0">
        <span class="text-2xl shrink-0">🤒</span>
        <div class="min-w-0">
          <div class="font-bold text-rose-300 text-sm">Krankheitsmodus aktiv</div>
          <div class="text-rose-300/80 text-[11px] mt-0.5 break-words">Alle Workouts pausiert. Ruhe dich aus und schone deinen Körper.</div>
        </div>
      </div>
      <button onclick="submitStatusUpdate('recovery', 'Wieder fit und symptomfrei!')" class="shrink-0 self-start sm:self-auto px-3 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white font-semibold text-xs shadow-lg transition flex items-center gap-1">
        <span>🌱</span> Wieder gesund melden
      </button>
    </div>

    <!-- MAIN APP TABS: Heute · Wochenplan · Makrozyklus · Profil & Zonen -->

    <!-- ================= TAB 1: HEUTE ================= -->
    <div id="view-heute" class="flex flex-col gap-6">

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

      <!-- Screen 8: PMC Chart Canvas Section -->
      <section class="glass-card rounded-2xl p-3.5 sm:p-4 border border-slate-800/80">
        <div class="flex flex-col xs:flex-row justify-between items-start xs:items-center gap-1.5 mb-3">
          <div class="flex items-center gap-2">
            <span class="text-xs font-semibold uppercase tracking-wider text-slate-400">PMC Trend</span>
            <span class="text-[10px] text-teal-400 font-mono">CTL · ATL · TSB</span>
          </div>
          <div class="flex items-center gap-2.5 text-[10px] font-mono">
            <span class="flex items-center gap-1 text-teal-400"><span class="w-2 h-2 rounded-full bg-teal-400 inline-block"></span> CTL</span>
            <span class="flex items-center gap-1 text-purple-400"><span class="w-2 h-2 rounded-full bg-purple-400 inline-block"></span> ATL</span>
            <span class="flex items-center gap-1 text-amber-400"><span class="w-2 h-2 rounded-full bg-amber-400 inline-block"></span> TSB</span>
          </div>
        </div>
        <div class="h-36 sm:h-40 w-full relative">
          <canvas id="pmcChart"></canvas>
        </div>
      </section>

      <!-- Screen 1: Heutiges Workout (Hero Card) -->
      <section class="glass-card rounded-2xl p-4 sm:p-5 shadow-2xl relative overflow-hidden border border-teal-500/20">
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
          <h2 id="today-workout-title" class="text-xl sm:text-2xl font-bold tracking-tight text-white mb-1 truncate">--</h2>
          <div class="flex flex-wrap items-baseline gap-2">
            <span id="today-metric" class="font-mono text-2xl sm:text-3xl font-extrabold text-teal-400">--</span>
            <span id="today-intensity" class="text-xs sm:text-sm font-mono text-slate-300"></span>
          </div>
        </div>

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
        <div id="week-dots" class="grid grid-cols-7 gap-1 sm:gap-2"></div>
      </section>

      <!-- Screen 2: Conversational Check-in (Things 3 / Messenger UX) -->
      <section class="glass-card rounded-2xl p-4 sm:p-5">
        <div class="flex items-center gap-2 mb-3">
          <svg class="w-4 h-4 text-teal-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 100-6 3 3 0 000 6z"></path></svg>
          <h3 class="text-sm font-semibold tracking-tight text-slate-200">Conversational Coach</h3>
        </div>

        <div id="chat-messages" class="flex flex-col gap-3 max-h-80 overflow-y-auto mb-3 text-sm pr-1">
          <div class="bg-slate-800/80 rounded-2xl rounded-tl-sm p-3 max-w-[90%] sm:max-w-[88%] text-slate-300 break-words">
            Wie war dein Training heute? Erzähl mir frei von Distanz, Gefühl (RPE 1-10) oder etwaigen Schmerzen / Symptomen.
          </div>
        </div>

        <form onsubmit="handleCheckinSubmit(event)" class="relative flex items-center">
          <input id="checkin-input" type="text" placeholder="z.B. '6 km gelaufen, Knie zwickt leicht (3/10)'" 
                 class="w-full bg-slate-950/80 border border-slate-800 rounded-xl py-2.5 pl-3.5 pr-11 text-xs sm:text-sm text-slate-200 focus:outline-none focus:border-teal-500 transition placeholder-slate-500">
          <button type="submit" class="absolute right-1.5 p-1.5 rounded-lg bg-teal-600 hover:bg-teal-500 text-white transition">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>
          </button>
        </form>
      </section>

    </div>

    <!-- ================= TAB 2: WOCHENPLAN (Screen 3) ================= -->
    <div id="view-wochenplan" class="hidden flex flex-col gap-4 sm:gap-5">
      <div class="flex flex-col xs:flex-row xs:items-center justify-between gap-2.5">
        <div>
          <h2 class="text-base sm:text-lg font-bold tracking-tight text-white">Wochenplan</h2>
          <p id="week-carousel-subtitle" class="text-xs text-slate-400">Tages-Workout-Karten im Überblick</p>
        </div>
        <div class="flex items-center justify-between xs:justify-end gap-1.5">
          <button onclick="prevCarouselWeek()" class="p-1.5 px-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs transition">◀ Vorherige</button>
          <span id="current-carousel-week-label" class="text-xs font-mono font-bold text-teal-400 px-1 truncate">Woche 1</span>
          <button onclick="nextCarouselWeek()" class="p-1.5 px-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs transition">Nächste ▶</button>
        </div>
      </div>

      <!-- Weekly Carousel Header Meta -->
      <div id="carousel-week-meta" class="glass-card rounded-xl p-3 flex items-center justify-between text-xs border border-teal-500/20">
        <div class="flex items-center gap-2 min-w-0">
          <span id="carousel-phase-badge" class="px-2 py-0.5 rounded bg-teal-500/20 text-teal-300 font-bold uppercase tracking-wider text-[10px] shrink-0">BUILD</span>
          <span id="carousel-meso-text" class="text-slate-300 truncate">Woche vor Deload</span>
        </div>
        <div class="font-mono font-bold text-slate-200 shrink-0 ml-2" id="carousel-volume-text">--</div>
      </div>

      <!-- 7 Day Workout Cards Carousel -->
      <div id="weekly-days-cards" class="flex flex-col gap-2.5 sm:gap-3">
        <!-- Dynamically rendered day cards -->
      </div>
    </div>

    <!-- ================= TAB 3: MAKROZYKLUS (Screen 4) ================= -->
    <div id="view-makro" class="hidden flex flex-col gap-4 sm:gap-5">
      <div>
        <h2 class="text-base sm:text-lg font-bold tracking-tight text-white">Makrozyklus (Bird's-Eye-View)</h2>
        <p class="text-xs text-slate-400">Vollständige Periodisierung & Phasenverlauf</p>
      </div>

      <!-- Macro Volume Chart Canvas -->
      <section class="glass-card rounded-2xl p-3.5 sm:p-4 border border-slate-800/80">
        <div class="flex justify-between items-center mb-2">
          <span class="text-xs font-semibold uppercase tracking-wider text-slate-400">Wöchentliche Volumen-Kurve</span>
          <span id="macro-volume-unit-label" class="text-[10px] text-teal-400 font-mono">Volumen</span>
        </div>
        <div class="h-36 w-full relative">
          <canvas id="macroVolumeChart"></canvas>
        </div>
      </section>

      <!-- Vertical Macro Timeline of Weeks -->
      <div id="macro-timeline-list" class="flex flex-col gap-2.5">
        <!-- Dynamically populated weeks -->
      </div>
    </div>

    <!-- ================= TAB 4: PROFIL & ZONEN (Screen 5) ================= -->
    <div id="view-profil" class="hidden flex flex-col gap-4 sm:gap-5">
      <div>
        <h2 class="text-base sm:text-lg font-bold tracking-tight text-white">Profil & Trainingszonen</h2>
        <p class="text-xs text-slate-400">Sportwissenschaftliche Parameter, VDOT-Paces und Biomarker</p>
      </div>

      <!-- User Profile & Performance Numbers -->
      <section class="glass-card rounded-2xl p-4 sm:p-5 border border-slate-800 flex flex-col gap-4">
        <div class="flex items-center justify-between border-b border-slate-800 pb-3">
          <div class="min-w-0">
            <h3 id="profile-display-name" class="font-bold text-base text-white truncate">Stephan Bolten</h3>
            <span class="text-xs text-teal-400 font-mono">Athleten-Profil · Zürich</span>
          </div>
          <button onclick="saveUserProfile()" class="px-3 py-1.5 rounded-lg bg-teal-600 hover:bg-teal-500 text-white font-semibold text-xs shadow transition shrink-0">
            Speichern
          </button>
        </div>

        <div class="grid grid-cols-2 sm:grid-cols-4 gap-2.5 sm:gap-3 text-xs">
          <div>
            <label class="block text-[10px] uppercase tracking-wider text-slate-400 mb-1 font-semibold">Ruhepuls (BPM)</label>
            <input id="input-resting-hr" type="number" placeholder="z.B. 48" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-slate-200 font-mono text-xs focus:border-teal-500 outline-none">
          </div>
          <div>
            <label class="block text-[10px] uppercase tracking-wider text-slate-400 mb-1 font-semibold">Max-Puls (BPM)</label>
            <input id="input-max-hr" type="number" placeholder="z.B. 185" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-slate-200 font-mono text-xs focus:border-teal-500 outline-none">
          </div>
          <div>
            <label class="block text-[10px] uppercase tracking-wider text-slate-400 mb-1 font-semibold">Gewicht (kg)</label>
            <input id="input-weight" type="number" step="0.5" placeholder="z.B. 74.5" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-slate-200 font-mono text-xs focus:border-teal-500 outline-none">
          </div>
          <div>
            <label class="block text-[10px] uppercase tracking-wider text-slate-400 mb-1 font-semibold">Rad FTP (Watt)</label>
            <input id="input-ftp" type="number" placeholder="z.B. 250" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-slate-200 font-mono text-xs focus:border-teal-500 outline-none">
          </div>
        </div>

        <div class="pt-2 border-t border-slate-800 flex items-center justify-between gap-3">
          <div class="min-w-0">
            <div class="text-xs font-semibold text-slate-200">Female Cycle Tracking</div>
            <div class="text-[10px] text-slate-400 break-words">Passt Engine-Sensitivität in Lutealphase für RPE & Puls automatisch an</div>
          </div>
          <input id="input-cycle-toggle" type="checkbox" class="w-4 h-4 rounded text-teal-600 bg-slate-900 border-slate-700 focus:ring-teal-500 shrink-0">
        </div>
      </section>

      <!-- Multi-Sport Calculated Training Zones -->
      <section class="glass-card rounded-2xl p-4 sm:p-5 border border-slate-800">
        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2.5 mb-3">
          <div>
            <h3 id="zones-title" class="font-bold text-sm text-white">Daniels VDOT Trainingszonen</h3>
            <p id="zones-subtitle" class="text-[11px] text-slate-400">Paces nach Jack Daniels Running Formula</p>
          </div>
          <div class="flex items-center gap-2 self-start sm:self-auto">
            <span id="zones-ref-label" class="text-xs text-slate-400 font-mono">VDOT:</span>
            <input id="input-vdot-val" type="number" step="0.5" class="w-16 bg-slate-950 border border-slate-700 rounded p-1 text-center font-mono text-xs text-teal-400 font-bold focus:border-teal-500 outline-none" value="48.0">
            <button onclick="recalculateZonesBtn()" class="px-2.5 py-1 rounded bg-teal-500/20 hover:bg-teal-500/30 text-teal-300 border border-teal-500/30 text-[11px] font-semibold transition shrink-0">
              Neu berechnen
            </button>
          </div>
        </div>

        <div class="overflow-x-auto">
          <table class="w-full text-left text-xs">
            <thead>
              <tr class="text-slate-400 border-b border-slate-800/80">
                <th class="pb-2">Zone</th>
                <th id="zones-col-target" class="pb-2">Ziel-Bereich</th>
                <th class="pb-2">Fokus & Trainingswirkung</th>
              </tr>
            </thead>
            <tbody id="zones-full-table-body" class="divide-y divide-slate-800/50"></tbody>
          </table>
        </div>
      </section>

      <!-- Active Plans List with Management (Archive & Delete) -->
      <section class="glass-card rounded-2xl p-4 sm:p-5 border border-slate-800">
        <div class="flex flex-col xs:flex-row xs:items-center justify-between gap-2 mb-3">
          <div>
            <h3 class="font-bold text-sm text-white">Trainingsziele & Pläne verwalten</h3>
            <p class="text-[11px] text-slate-400">Aktive & archivierte Pläne, Countdowns und Aktionen</p>
          </div>
          <label class="flex items-center gap-1.5 text-xs text-slate-400 cursor-pointer select-none">
            <input id="toggle-show-archived" type="checkbox" onchange="toggleShowArchived(this.checked)" class="w-3.5 h-3.5 rounded bg-slate-900 border-slate-700 text-teal-600 focus:ring-teal-500">
            <span>Archivierte anzeigen</span>
          </label>
        </div>
        <div id="active-plans-list" class="flex flex-col gap-2.5">
          <!-- Dynamically populated with actions: Switch, Archive, Delete -->
        </div>
      </section>
    </div>

  </div>

  <!-- Screen 7: "Wie geht's dir?" Modal -->
  <div id="status-modal" class="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4 hidden">
    <div class="glass-card bg-slate-900 border border-slate-700 w-full max-w-md rounded-2xl p-6 shadow-2xl">
      <div class="flex justify-between items-center mb-3">
        <h3 class="text-lg font-bold flex items-center gap-2">
          <span>🩺</span> Wie geht's dir?
        </h3>
        <button onclick="closeStatusModal()" class="text-slate-400 hover:text-white text-sm">✕</button>
      </div>
      <p class="text-xs text-slate-400 mb-4">Wähle deinen aktuellen Gesundheitsstatus für automatische Anpassungen deines Trainingsplans:</p>
      
      <div class="flex flex-col gap-2.5">
        <button onclick="submitStatusUpdate('recovery', 'Vollständig genesen')" class="p-3 rounded-xl bg-slate-950/70 border border-emerald-500/30 hover:border-emerald-500 hover:bg-emerald-950/20 text-left transition flex items-start gap-3 group">
          <span class="text-xl">🌱</span>
          <div>
            <div class="text-sm font-semibold text-emerald-400 group-hover:text-emerald-300">Wieder gesund (Recovery)</div>
            <div class="text-xs text-slate-400 mt-0.5">Aktiviert Wiederaufbau: 1. Training 50% Volumen, 4 Tage kein Tempotraining.</div>
          </div>
        </button>

        <button onclick="submitStatusUpdate('pain_resolved', 'Schmerzen vollständig abgeklungen')" class="p-3 rounded-xl bg-slate-950/70 border border-teal-500/30 hover:border-teal-500 hover:bg-teal-950/20 text-left transition flex items-start gap-3 group">
          <span class="text-xl">🩹</span>
          <div>
            <div class="text-sm font-semibold text-teal-400 group-hover:text-teal-300">Schmerzfrei (Pain Resolved)</div>
            <div class="text-xs text-slate-400 mt-0.5">Beendet Schmerzpause, stellt geplante Einheiten als Easy-Runs wieder her.</div>
          </div>
        </button>

        <button onclick="submitStatusUpdate('readiness', 'Akute Erschöpfung / Schlechter Schlaf', 8)" class="p-3 rounded-xl bg-slate-950/70 border border-amber-500/30 hover:border-amber-500 hover:bg-amber-950/20 text-left transition flex items-start gap-3 group">
          <span class="text-xl">💤</span>
          <div>
            <div class="text-sm font-semibold text-amber-400 group-hover:text-amber-300">Erschöpft / Müde (Fatigue)</div>
            <div class="text-xs text-slate-400 mt-0.5">Drosselt Tempo- & Intervall-Einheiten der nächsten 48h auf Zone Easy.</div>
          </div>
        </button>
      </div>

      <div class="flex justify-end mt-4">
        <button onclick="closeStatusModal()" class="px-4 py-2 text-xs font-medium text-slate-400 hover:text-white">Schließen</button>
      </div>
    </div>
  </div>

  <!-- Screen 6: Guided Onboarding Modal (Conversational & Form Fallback) -->
  <div id="onboard-modal" class="fixed inset-0 bg-black/85 backdrop-blur-sm z-50 flex items-center justify-center p-4 hidden">
    <div class="glass-card bg-slate-900 border border-slate-700 w-full max-w-lg rounded-2xl p-6 shadow-2xl relative max-h-[90vh] overflow-y-auto">
      <div class="flex justify-between items-center mb-2">
        <h3 class="text-lg font-bold flex items-center gap-2">
          <span>🚀</span> Neues Trainingsziel erstellen
        </h3>
        <button onclick="closeOnboardModal()" class="text-slate-400 hover:text-white text-sm">✕</button>
      </div>
      <p class="text-xs text-slate-400 mb-4">Erstelle deinen personalisierten periodisierten Trainingsplan mit KI oder geführter Eingabe:</p>

      <!-- Step 1: Input (Conversational or Structured) -->
      <div id="onboard-step-input">
        <div class="flex border-b border-slate-800 mb-3 pb-2 gap-4 text-xs font-medium">
          <button onclick="switchOnboardMode('chat')" id="onboard-mode-chat-btn" class="text-teal-400 font-bold border-b-2 border-teal-400 pb-1">Freitext (Conversational)</button>
          <button onclick="switchOnboardMode('guided')" id="onboard-mode-guided-btn" class="text-slate-400 hover:text-slate-200 pb-1">Geführte Parameter</button>
        </div>

        <div id="onboard-chat-box">
          <textarea id="onboard-prompt" rows="3" class="w-full bg-slate-950 border border-slate-800 rounded-xl p-3 text-sm text-slate-200 focus:outline-none focus:border-teal-500 mb-3" placeholder="z.B. 'Halbmarathon in 16 Wochen. Laufe aktuell 25 km/Woche, VDOT 46, Ziel ist sub 1:45'"></textarea>
          <button onclick="analyzeOnboardPrompt()" class="w-full py-2.5 rounded-xl bg-teal-600 hover:bg-teal-500 text-white font-semibold text-xs shadow-lg transition flex items-center justify-center gap-2">
            <span>✨</span> Ziel analysieren & zusammenfassen
          </button>
        </div>

        <div id="onboard-guided-box" class="hidden flex flex-col gap-3">
          <div class="grid grid-cols-2 gap-3 text-xs">
            <div>
              <label class="block text-[10px] uppercase text-slate-400 font-semibold mb-1">Sportart</label>
              <select id="guided-sport" onchange="onSportChange(this.value)" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-slate-200 text-xs focus:border-teal-500 outline-none">
                <option value="running">Laufen (Running)</option>
                <option value="cycling">Radfahren (Cycling)</option>
                <option value="swimming">Schwimmen (Swimming)</option>
                <option value="strength">Krafttraining (Strength / DUP)</option>
                <option value="triathlon">Triathlon (Multi-Sport)</option>
              </select>
            </div>
            <div>
              <label class="block text-[10px] uppercase text-slate-400 font-semibold mb-1">Ziel-Event</label>
              <select id="guided-goal" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-slate-200 text-xs focus:border-teal-500 outline-none">
                <!-- Dynamically populated based on selected sport -->
              </select>
            </div>
          </div>
          <div class="grid grid-cols-3 gap-3 text-xs">
            <div>
              <label class="block text-[10px] uppercase text-slate-400 font-semibold mb-1">Dauer (Wochen)</label>
              <input id="guided-weeks" type="number" value="16" min="4" max="32" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-slate-200 font-mono text-xs focus:border-teal-500 outline-none">
            </div>
            <div>
              <label id="guided-volume-label" class="block text-[10px] uppercase text-slate-400 font-semibold mb-1 truncate">Basis (km/W)</label>
              <input id="guided-volume" type="number" value="30" min="1" max="15000" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-slate-200 font-mono text-xs focus:border-teal-500 outline-none">
            </div>
            <div>
              <label id="guided-vdot-label" class="block text-[10px] uppercase text-slate-400 font-semibold mb-1 truncate">VDOT / Fitness</label>
              <input id="guided-vdot" type="number" step="0.5" value="46.0" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-slate-200 font-mono text-xs focus:border-teal-500 outline-none">
            </div>
          </div>
          <button onclick="previewGuidedGoal()" class="w-full py-2.5 rounded-xl bg-teal-600 hover:bg-teal-500 text-white font-semibold text-xs shadow-lg transition flex items-center justify-center gap-2 mt-1">
            <span>✨</span> Vorschau generieren
          </button>
        </div>
      </div>

      <!-- Step 2: Summary / Confirmation Card -->
      <div id="onboard-step-summary" class="hidden flex flex-col gap-3">
        <div class="bg-slate-950/80 border border-teal-500/40 rounded-xl p-4 text-xs">
          <div class="font-bold text-teal-300 text-sm mb-2 flex items-center gap-2">
            <span>📋</span> Zusammenfassung deines Plans:
          </div>
          <div class="grid grid-cols-2 gap-2 text-slate-300 font-mono mb-3">
            <div>Sportart: <strong id="sum-sport" class="text-white">RUNNING</strong></div>
            <div>Event: <strong id="sum-goal" class="text-white">MARATHON</strong></div>
            <div>Dauer: <strong id="sum-weeks" class="text-white">16 Wochen</strong></div>
            <div>Basis: <strong id="sum-volume" class="text-white">30 km/Woche</strong></div>
            <div>VDOT: <strong id="sum-vdot" class="text-teal-400">46.0</strong></div>
            <div>Tage: <strong id="sum-days" class="text-white">4 Einheiten</strong></div>
          </div>
          <p class="text-[11px] text-slate-400 italic">Die OmniTrain Engine berechnet automatisierte Mesozyklen (Base, Build, Peak, Taper) mit 3:1 Belastungs- und Entlastungswellen.</p>
        </div>

        <div class="flex justify-end gap-2 mt-1">
          <button onclick="backToOnboardInput()" class="px-3 py-2 text-xs font-medium text-slate-400 hover:text-white">Zurück / Ändern</button>
          <button onclick="finalizePlanGeneration()" class="px-4 py-2 text-xs font-semibold rounded-lg bg-teal-600 hover:bg-teal-500 text-white shadow-lg transition flex items-center gap-2">
            <span>🚀</span> Plan jetzt aufbauen
          </button>
        </div>
      </div>

      <!-- Step 3: Build Animation -->
      <div id="onboard-step-animation" class="hidden flex flex-col items-center justify-center py-8 gap-4 text-center">
        <div class="w-12 h-12 border-4 border-teal-500 border-t-transparent rounded-full animate-spin"></div>
        <div>
          <div class="font-bold text-base text-white">Plan wird berechnet...</div>
          <div id="anim-stage" class="text-xs text-slate-400 mt-1">Generiere Daniels VDOT Zonen & Mesozyklus-Wochen...</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Bottom Tab Bar Navigation (§10 UX) -->
  <nav class="fixed bottom-0 left-0 right-0 z-40 bg-slate-900/90 backdrop-blur-md border-t border-slate-800/80 flex justify-center">
    <div class="w-full max-w-xl grid grid-cols-4 py-2 text-center text-xs">
      <button onclick="switchMainTab('heute')" id="nav-btn-heute" class="flex flex-col items-center py-1 text-teal-400 font-semibold transition">
        <svg class="w-5 h-5 mb-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6"></path></svg>
        <span>Heute</span>
      </button>

      <button onclick="switchMainTab('wochenplan')" id="nav-btn-wochenplan" class="flex flex-col items-center py-1 text-slate-400 hover:text-slate-200 transition">
        <svg class="w-5 h-5 mb-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"></path></svg>
        <span>Wochenplan</span>
      </button>

      <button onclick="switchMainTab('makro')" id="nav-btn-makro" class="flex flex-col items-center py-1 text-slate-400 hover:text-slate-200 transition">
        <svg class="w-5 h-5 mb-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"></path></svg>
        <span>Makrozyklus</span>
      </button>

      <button onclick="switchMainTab('profil')" id="nav-btn-profil" class="flex flex-col items-center py-1 text-slate-400 hover:text-slate-200 transition">
        <svg class="w-5 h-5 mb-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"></path></svg>
        <span>Profil & Zonen</span>
      </button>
    </div>
  </nav>

  <script>
    let appState = null;
    let currentPlanId = null;
    let showArchivedPlans = false;
    let pendingCheckinText = null;
    let pmcChartInstance = null;
    let macroChartInstance = null;
    let activeCarouselWeekIndex = 0;
    let pendingPlanParams = null;

    function getSportVolumeUnit(sport, isWeeklyRate = false) {
      if (sport === 'swimming') return isWeeklyRate ? 'm/W' : 'm';
      if (sport === 'strength') return isWeeklyRate ? 'Sätze/W' : 'Sätze';
      if (sport === 'cycling') return isWeeklyRate ? 'min/W' : 'min';
      if (sport === 'triathlon' || sport === 'multisport') return isWeeklyRate ? 'Einh./W' : 'Einh.';
      return isWeeklyRate ? 'km/W' : 'km';
    }

    function formatSportVolume(volume, sport, isWeeklyRate = false) {
      if (volume === undefined || volume === null) return '-';
      const num = (volume % 1 === 0) ? volume : Number(volume.toFixed(1));
      const unit = getSportVolumeUnit(sport, isWeeklyRate);
      return `${num} ${unit}`;
    }

    function formatWorkoutMetric(value, unit) {
      if (value === undefined || value === null) return '';
      const num = (value % 1 === 0) ? value : Number(value.toFixed(1));
      let u = unit || '';
      if (u === 'sets') u = 'Sätze';
      return `${num} ${u}`.trim();
    }

    async function loadState(planId = null) {
      try {
        let url = `/api/state?show_archived=${showArchivedPlans ? 'true' : 'false'}`;
        if (planId) {
          url += `&plan_id=${planId}`;
        } else if (currentPlanId) {
          url += `&plan_id=${currentPlanId}`;
        }
        const res = await fetch(url);
        appState = await res.json();
        if (appState && appState.active_plan) {
          currentPlanId = appState.active_plan.id;
        } else if (appState && appState.all_plans && appState.all_plans.length > 0) {
          currentPlanId = appState.all_plans[0].id;
        } else {
          currentPlanId = null;
        }
        render();
      } catch (err) {
        console.error("State loading error:", err);
      }
    }

    function toggleShowArchived(checked) {
      showArchivedPlans = checked;
      loadState();
    }

    function onPlanSelect(selectedId) {
      currentPlanId = selectedId;
      loadState(selectedId);
    }

    async function archivePlanAction(planId) {
      if (!confirm("Diesen Trainingsplan wirklich archivieren? Er wird pausiert und aus der aktiven Auswahl ausgeblendet.")) return;
      try {
        const res = await fetch('/api/plan/archive', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({plan_id: planId})
        });
        const data = await res.json();
        await loadState();
      } catch (err) {
        console.error(err);
        alert("Fehler beim Archivieren des Plans.");
      }
    }

    async function unarchivePlanAction(planId) {
      try {
        const res = await fetch('/api/plan/unarchive', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({plan_id: planId})
        });
        const data = await res.json();
        currentPlanId = planId;
        await loadState(planId);
      } catch (err) {
        console.error(err);
        alert("Fehler beim Reaktivieren des Plans.");
      }
    }

    async function deletePlanAction(planId, goalName) {
      if (!confirm(`Soll der Trainingsplan '${goalName}' wirklich UNWIDERRUFLICH GELÖSCHT werden? Alle dazugehörigen Einheiten werden entfernt.`)) return;
      try {
        const res = await fetch('/api/plan/delete', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({plan_id: planId})
        });
        const data = await res.json();
        if (currentPlanId === planId) {
          currentPlanId = null;
        }
        await loadState();
      } catch (err) {
        console.error(err);
        alert("Fehler beim Löschen des Plans.");
      }
    }

    function render() {
      if (!appState || !appState.active_plan) {
        document.getElementById('plan-title').innerText = "Kein aktiver Plan vorhanden";
        const selector = document.getElementById('plan-selector');
        if (selector) selector.innerHTML = '<option value="">Kein Plan</option>';
        renderProfileAndZones();
        return;
      }
      const plan = appState.active_plan;
      const isArchived = plan.status === 'archived';
      document.getElementById('plan-title').innerText = `${plan.sport_type.toUpperCase()} · ${plan.goal_type.replace('_', ' ').toUpperCase()} (Ziel: ${plan.target_date})${isArchived ? ' [ARCHIVIERT]' : ''}`;

      // Check-in input placeholder
      const checkinInput = document.getElementById('checkin-input');
      if (checkinInput) {
        if (plan.sport_type === 'swimming') {
          checkinInput.placeholder = "z.B. '2500m geschwommen, Schulter zwickt leicht (3/10)'";
        } else if (plan.sport_type === 'strength') {
          checkinInput.placeholder = "z.B. '16 Sätze absolviert, RPE 8, alles stabil'";
        } else if (plan.sport_type === 'cycling') {
          checkinInput.placeholder = "z.B. '75 min gefahren, 210W Schnitt, RPE 7'";
        } else if (plan.sport_type === 'triathlon' || plan.sport_type === 'multisport') {
          checkinInput.placeholder = "z.B. 'Koppeleinheit absolviert (90min Rad + 5km Lauf), RPE 7'";
        } else {
          checkinInput.placeholder = "z.B. '6 km gelaufen, Knie zwickt leicht (3/10)'";
        }
      }

      // Plan Selector
      const selector = document.getElementById('plan-selector');
      if (appState.all_plans) {
        selector.innerHTML = '';
        const visiblePlans = showArchivedPlans ? appState.all_plans : appState.all_plans.filter(p => p.status === 'active');
        visiblePlans.forEach(p => {
          const opt = document.createElement('option');
          opt.value = p.id;
          const statusTag = p.status === 'archived' ? ' 📦 [Archiv]' : '';
          opt.innerText = `${p.sport_type.toUpperCase()}: ${p.goal_type.replace('_', ' ').toUpperCase()}${statusTag}`;
          if (p.id === plan.id) opt.selected = true;
          selector.appendChild(opt);
        });
      }

      // Sick Banner
      const sickBanner = document.getElementById('sick-banner');
      if (appState.is_sick_mode) {
        sickBanner.classList.remove('hidden');
      } else {
        sickBanner.classList.add('hidden');
      }

      // PMC Metrics
      document.getElementById('pmc-ctl').innerText = appState.pmc ? appState.pmc.ctl : '--';
      document.getElementById('pmc-atl').innerText = appState.pmc ? appState.pmc.atl : '--';
      document.getElementById('pmc-tsb').innerText = appState.pmc ? appState.pmc.tsb : '--';

      // PMC Chart
      renderPMCChart(appState.pmc_series || []);

      // Today
      const tw = appState.today_workout;
      if (tw) {
        document.getElementById('today-workout-title').innerText = tw.workout_type.replace('_', ' ').toUpperCase();
        document.getElementById('today-metric').innerText = tw.metric_primary > 0 ? formatWorkoutMetric(tw.metric_primary, tw.metric_unit) : 'Regeneration';
        document.getElementById('today-intensity').innerText = tw.intensity_detail || tw.intensity_target || '';
        document.getElementById('today-date').innerText = tw.date;
        document.getElementById('today-status').innerText = tw.status.toUpperCase();
      } else {
        document.getElementById('today-workout-title').innerText = "Kein Workout";
        document.getElementById('today-metric').innerText = "--";
        document.getElementById('today-intensity').innerText = "";
        document.getElementById('today-date').innerText = "";
        document.getElementById('today-status').innerText = "";
      }

      // Mini-Wochenansicht
      renderWeekDots();

      // Screen 3: Wochenplan Carousel
      renderWochenplanCarousel();

      // Screen 4: Makrozyklus & Timeline
      renderMakrozyklus();

      // Screen 5: Profil & Zonen
      renderProfileAndZones();
    }

    function renderPMCChart(series) {
      const ctx = document.getElementById('pmcChart');
      if (!ctx) return;

      let labels = [];
      let ctlData = [];
      let atlData = [];
      let tsbData = [];

      if (series.length === 0) {
        labels = ['Tag -6', 'Tag -5', 'Tag -4', 'Tag -3', 'Tag -2', 'Gestern', 'Heute'];
        ctlData = [12, 13, 14, 14, 15, 16, 16.5];
        atlData = [18, 22, 19, 25, 20, 24, 22.0];
        tsbData = [-6, -9, -5, -11, -5, -8, -5.5];
      } else {
        labels = series.map(p => p.date.slice(5));
        ctlData = series.map(p => p.ctl);
        atlData = series.map(p => p.atl);
        tsbData = series.map(p => p.tsb);
      }

      if (pmcChartInstance) {
        pmcChartInstance.destroy();
      }

      pmcChartInstance = new Chart(ctx, {
        type: 'line',
        data: {
          labels: labels,
          datasets: [
            {
              label: 'CTL (Fitness)',
              data: ctlData,
              borderColor: '#14B8A6',
              backgroundColor: 'rgba(20, 184, 166, 0.1)',
              borderWidth: 2,
              tension: 0.3,
              pointRadius: 3,
              fill: false
            },
            {
              label: 'ATL (Fatigue)',
              data: atlData,
              borderColor: '#C084FC',
              backgroundColor: 'rgba(192, 132, 252, 0.1)',
              borderWidth: 2,
              tension: 0.3,
              pointRadius: 3,
              fill: false
            },
            {
              label: 'TSB (Form)',
              data: tsbData,
              borderColor: '#FBBF24',
              backgroundColor: 'rgba(251, 191, 36, 0.1)',
              borderWidth: 1.5,
              borderDash: [3, 3],
              tension: 0.3,
              pointRadius: 2,
              fill: true
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          plugins: { legend: { display: false } },
          scales: {
            x: { grid: { color: 'rgba(255, 255, 255, 0.05)' }, ticks: { color: '#64748B', font: { size: 10 } } },
            y: { grid: { color: 'rgba(255, 255, 255, 0.05)' }, ticks: { color: '#64748B', font: { size: 10 } } }
          }
        }
      });
    }

    function renderWeekDots() {
      const container = document.getElementById('week-dots');
      container.innerHTML = '';
      const days = ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So'];
      const currentWeekWorkouts = appState.workouts.slice(0, 7);
      days.forEach((dayName, idx) => {
        const wo = currentWeekWorkouts.find(w => w.day_of_week === (idx + 1));
        const col = document.createElement('div');
        col.className = 'flex flex-col items-center gap-1 p-1.5 sm:p-2 rounded-xl bg-slate-900/60 border border-slate-800/60 min-w-0';

        let dotColor = 'bg-slate-700';
        let statusText = 'Rest';
        if (wo) {
          statusText = formatWorkoutMetric(wo.metric_primary, wo.metric_unit);
          if (wo.status === 'completed') dotColor = 'bg-emerald-400';
          else if (wo.workout_type === 'rest') dotColor = 'bg-rose-500';
          else dotColor = 'bg-teal-400 ring-2 ring-teal-500/20';
        }

        col.innerHTML = `
          <span class="text-[10px] sm:text-[11px] font-semibold text-slate-400">${dayName}</span>
          <span class="w-3 h-3 sm:w-3.5 sm:h-3.5 rounded-full ${dotColor}"></span>
          <span class="text-[8px] sm:text-[9px] font-mono text-slate-500 truncate w-full text-center">${statusText}</span>
        `;
        container.appendChild(col);
      });
    }

    // ================= SCREEN 3: WOCHENPLAN CAROUSEL =================
    function renderWochenplanCarousel() {
      if (!appState.weeks || appState.weeks.length === 0) return;
      if (activeCarouselWeekIndex >= appState.weeks.length) {
        activeCarouselWeekIndex = 0;
      }
      const curWeek = appState.weeks[activeCarouselWeekIndex];
      document.getElementById('current-carousel-week-label').innerText = `Woche ${curWeek.week_number} von ${appState.weeks.length}`;

      // Meta
      const phaseBadge = document.getElementById('carousel-phase-badge');
      phaseBadge.innerText = curWeek.phase.toUpperCase();
      phaseBadge.className = `px-2 py-0.5 rounded font-bold uppercase tracking-wider text-[10px] shrink-0 ${
        curWeek.phase === 'peak' ? 'bg-red-500/20 text-red-300' :
        curWeek.phase === 'taper' ? 'bg-emerald-500/20 text-emerald-300' :
        curWeek.phase === 'build' ? 'bg-orange-500/20 text-orange-300' : 'bg-blue-500/20 text-blue-300'
      }`;
      document.getElementById('carousel-meso-text').innerText = curWeek.is_recovery_week ? '🌿 Entlastungswoche (-25%)' : 'Belastungswoche';
      const curSport = appState.active_plan ? appState.active_plan.sport_type : 'running';
      document.getElementById('carousel-volume-text').innerText = formatSportVolume(curWeek.target_weekly_volume, curSport);

      // Filter workouts of this week
      const weekWorkouts = appState.workouts.filter(w => w.week_id === curWeek.id);
      const container = document.getElementById('weekly-days-cards');
      container.innerHTML = '';

      const dayNames = ['Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag', 'Sonntag'];
      const todayStr = new Date().toISOString().slice(0, 10);

      for (let dayNum = 1; dayNum <= 7; dayNum++) {
        const wo = weekWorkouts.find(w => w.day_of_week === dayNum);
        const card = document.createElement('div');
        const isToday = wo && wo.date === todayStr;
        const isPast = wo && wo.date < todayStr;

        let borderClass = isToday ? 'border-teal-500/60 ring-1 ring-teal-500/40 bg-teal-950/20' : 'border-slate-800/80 bg-slate-900/60';
        let opacityClass = isPast ? 'opacity-65' : '';

        let statusBadge = '';
        if (wo) {
          if (wo.status === 'completed') statusBadge = '<span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 text-[10px] font-semibold shrink-0">Erledigt</span>';
          else if (wo.workout_type === 'rest') statusBadge = '<span class="px-2 py-0.5 rounded bg-slate-800 text-slate-400 text-[10px] shrink-0">Ruhetag</span>';
          else statusBadge = '<span class="px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 text-[10px] font-semibold shrink-0">Geplant</span>';
        }

        let sportIcon = '🏃';
        if (wo && wo.sport_type === 'cycling') sportIcon = '🚴';
        else if (wo && wo.sport_type === 'swimming') sportIcon = '🏊';
        else if (wo && wo.sport_type === 'strength') sportIcon = '🏋️';

        card.className = `glass-card rounded-xl p-3 sm:p-3.5 border ${borderClass} ${opacityClass} flex items-center justify-between gap-2 text-xs transition`;
        card.innerHTML = `
          <div class="flex items-center gap-2.5 sm:gap-3 min-w-0">
            <div class="w-8 h-8 rounded-lg bg-slate-800 flex items-center justify-center text-sm shrink-0">${wo ? sportIcon : '😴'}</div>
            <div class="min-w-0">
              <div class="flex items-center gap-2">
                <span class="font-bold text-white text-sm truncate">${dayNames[dayNum - 1]}</span>
                <span class="text-[10px] font-mono text-slate-500 shrink-0">${wo ? wo.date : ''}</span>
              </div>
              <div class="text-slate-400 mt-0.5 truncate">
                ${wo ? (wo.workout_type === 'rest' ? 'Regeneration' : `${wo.workout_type.toUpperCase()} · ${formatWorkoutMetric(wo.metric_primary, wo.metric_unit)}`) : 'Ruhetag'}
                ${wo && wo.intensity_detail ? `<span class="text-slate-500 block text-[11px] italic truncate">${wo.intensity_detail}</span>` : ''}
              </div>
            </div>
          </div>
          <div class="shrink-0">${statusBadge}</div>
        `;
        container.appendChild(card);
      }
    }

    function prevCarouselWeek() {
      if (activeCarouselWeekIndex > 0) {
        activeCarouselWeekIndex--;
        renderWochenplanCarousel();
      }
    }

    function nextCarouselWeek() {
      if (activeCarouselWeekIndex < (appState.weeks.length - 1)) {
        activeCarouselWeekIndex++;
        renderWochenplanCarousel();
      }
    }

    // ================= SCREEN 4: MAKROZYKLUS =================
    function renderMakrozyklus() {
      if (!appState.weeks) return;

      const curSport = appState.active_plan ? appState.active_plan.sport_type : 'running';
      const volUnit = getSportVolumeUnit(curSport);
      const unitBadge = document.getElementById('macro-volume-unit-label');
      if (unitBadge) {
        unitBadge.innerText = `Volumen (${volUnit})`;
      }

      // 1. Chart.js Macro Volume Wave Curve
      const ctx = document.getElementById('macroVolumeChart');
      if (ctx) {
        const labels = appState.weeks.map(w => `W${w.week_number}`);
        const dataV = appState.weeks.map(w => w.target_weekly_volume);

        if (macroChartInstance) {
          macroChartInstance.destroy();
        }

        macroChartInstance = new Chart(ctx, {
          type: 'bar',
          data: {
            labels: labels,
            datasets: [{
              label: `Wochenvolumen (${volUnit})`,
              data: dataV,
              backgroundColor: appState.weeks.map(w => {
                if (w.is_recovery_week) return 'rgba(251, 191, 36, 0.4)'; // Amber
                if (w.phase === 'peak') return 'rgba(239, 68, 68, 0.5)'; // Red
                if (w.phase === 'taper') return 'rgba(34, 197, 94, 0.5)'; // Green
                return 'rgba(20, 184, 166, 0.5)'; // Teal
              }),
              borderColor: appState.weeks.map(w => {
                if (w.is_recovery_week) return '#FBBF24';
                if (w.phase === 'peak') return '#EF4444';
                if (w.phase === 'taper') return '#22C55E';
                return '#14B8A6';
              }),
              borderWidth: 1.5,
              borderRadius: 4
            }]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
              x: { grid: { display: false }, ticks: { color: '#64748B', font: { size: 9 } } },
              y: { grid: { color: 'rgba(255, 255, 255, 0.05)' }, ticks: { color: '#64748B', font: { size: 9 } } }
            }
          }
        });
      }

      // 2. Timeline List
      const container = document.getElementById('macro-timeline-list');
      container.innerHTML = '';
      const todayStr = new Date().toISOString().slice(0, 10);

      appState.weeks.forEach(w => {
        const isCurrentWeek = w.week_number === (activeCarouselWeekIndex + 1);
        const item = document.createElement('div');
        const phaseColor = w.phase === 'peak' ? 'text-red-400 border-red-500/30' :
                           w.phase === 'taper' ? 'text-emerald-400 border-emerald-500/30' :
                           w.phase === 'build' ? 'text-orange-400 border-orange-500/30' : 'text-blue-400 border-blue-500/30';

        item.className = `p-3 rounded-xl bg-slate-950/70 border ${isCurrentWeek ? 'border-teal-500 ring-1 ring-teal-500/40 bg-teal-950/20' : 'border-slate-800/80'} flex items-center justify-between gap-2 text-xs transition`;
        item.innerHTML = `
          <div class="flex items-center gap-2.5 sm:gap-3 min-w-0">
            <div class="font-mono font-bold text-slate-400 w-7 sm:w-8 shrink-0">W${w.week_number}</div>
            <div class="min-w-0">
              <div class="flex flex-wrap items-center gap-1 sm:gap-1.5">
                <span class="font-bold uppercase tracking-wider text-[11px] ${phaseColor}">${w.phase}</span>
                ${isCurrentWeek ? '<span class="px-1.5 py-0.5 rounded bg-teal-500/20 text-teal-300 font-bold text-[9px] shrink-0">📍 HIER</span>' : ''}
                ${w.is_recovery_week ? '<span class="px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-300 font-semibold text-[9px] shrink-0">Entlastung</span>' : ''}
              </div>
              <span class="text-[10px] font-mono text-slate-500 block truncate">${w.week_start_date}</span>
            </div>
          </div>
          <div class="font-mono font-bold text-slate-200 shrink-0 ml-2">
            ${formatSportVolume(w.target_weekly_volume, curSport)}
          </div>
        `;
        container.appendChild(item);
      });
    }

    // ================= SCREEN 5: PROFIL & ZONEN =================
    function renderProfileAndZones() {
      // User info
      if (appState.user) {
        document.getElementById('profile-display-name').innerText = appState.user.display_name || 'Stephan Bolten';
        if (appState.user.resting_hr) document.getElementById('input-resting-hr').value = appState.user.resting_hr;
        if (appState.user.max_hr) document.getElementById('input-max-hr').value = appState.user.max_hr;
        if (appState.user.weight_kg) document.getElementById('input-weight').value = appState.user.weight_kg;
      }
      if (appState.profile) {
        if (appState.profile.ftp_watts) document.getElementById('input-ftp').value = appState.profile.ftp_watts;
        if (appState.profile.menstrual_cycle_active) document.getElementById('input-cycle-toggle').checked = true;
      }
      if (appState.ref_vdot) {
        document.getElementById('input-vdot-val').value = appState.ref_vdot;
      }

      // Multi-Sport Zones Header & Table Rendering
      const curSport = appState.active_plan ? appState.active_plan.sport_type : 'running';
      const zonesTitle = document.getElementById('zones-title');
      const zonesSubtitle = document.getElementById('zones-subtitle');
      const zonesRefLabel = document.getElementById('zones-ref-label');
      const colTarget = document.getElementById('zones-col-target');

      if (curSport === 'cycling') {
        zonesTitle.innerText = "Coggan 7-Zonen Leistungsmodell (FTP)";
        zonesSubtitle.innerText = "Watt-Bereiche & Intensitätsstufen nach Dr. Andrew Coggan";
        zonesRefLabel.innerText = "FTP (W):";
        colTarget.innerText = "Leistung (Watt)";
      } else if (curSport === 'swimming') {
        zonesTitle.innerText = "Critical Swim Speed (CSS) Trainingszonen";
        zonesSubtitle.innerText = "Paces pro 100m berechnet aus 400m / 200m Time Trials";
        zonesRefLabel.innerText = "CSS (s):";
        colTarget.innerText = "Pace / 100m";
      } else if (curSport === 'strength') {
        zonesTitle.innerText = "Strength & DUP Periodisierungs-Zonen";
        zonesSubtitle.innerText = "Wiederholungen, % 1RM, RPE und RIR Zielkorridore";
        zonesRefLabel.innerText = "1RM (kg):";
        colTarget.innerText = "Intensität & Wdh";
      } else {
        zonesTitle.innerText = "Daniels VDOT Trainingszonen";
        zonesSubtitle.innerText = "Paces nach Jack Daniels Running Formula";
        zonesRefLabel.innerText = "VDOT:";
        colTarget.innerText = "Pace-Bereich";
      }

      // Full Zones Table
      const tbody = document.getElementById('zones-full-table-body');
      tbody.innerHTML = '';
      for (const [k, z] of Object.entries(appState.zones || {})) {
        const tr = document.createElement('tr');
        tr.className = 'hover:bg-slate-800/20';

        let target = '-';
        if (z.min_watts !== undefined && z.max_watts !== undefined) {
          target = `${z.min_watts}–${z.max_watts} Watt`;
        } else if (z.min_pace && z.max_pace) {
          target = `${z.min_pace}–${z.max_pace} min/km`;
        } else if (z.pace) {
          target = `${z.pace} ${curSport === 'swimming' ? 'min/100m' : 'min/km'}`;
        } else if (z.intensity) {
          target = `${z.intensity} (${z.rpe || ''} RPE)`;
        }

        tr.innerHTML = `
          <td class="py-2.5 font-bold text-teal-400">${z.name || k}</td>
          <td class="py-2.5 font-mono text-slate-200">${target}</td>
          <td class="py-2.5 text-slate-400">${z.description || (z.rir ? `${z.rir}, Pause: ${z.rest || ''}` : '-')}</td>
        `;
        tbody.appendChild(tr);
      }

      // Active / Archived Plans List with Management Actions
      const plansList = document.getElementById('active-plans-list');
      plansList.innerHTML = '';
      if (appState.all_plans) {
        const today = new Date();
        const displayPlans = showArchivedPlans ? appState.all_plans : appState.all_plans.filter(p => p.status === 'active');
        
        if (displayPlans.length === 0) {
          plansList.innerHTML = `<div class="p-4 rounded-xl bg-slate-950/40 border border-slate-800 text-center text-xs text-slate-500 italic">Keine ${showArchivedPlans ? '' : 'aktiven '}Pläne vorhanden.</div>`;
        }

        displayPlans.forEach(p => {
          const target = new Date(p.target_date);
          const diffDays = Math.ceil((target - today) / (1000 * 60 * 60 * 24));
          const weeksLeft = Math.max(0, Math.ceil(diffDays / 7));
          const isSelected = appState.active_plan && appState.active_plan.id === p.id;
          const isArchived = p.status === 'archived';

          let sportIcon = '🏃';
          if (p.sport_type === 'cycling') sportIcon = '🚴';
          else if (p.sport_type === 'swimming') sportIcon = '🏊';
          else if (p.sport_type === 'strength') sportIcon = '🏋️';
          else if (p.sport_type === 'triathlon' || p.sport_type === 'multisport') sportIcon = '🏊';

          const cardBorder = isSelected ? 'border-teal-500/60 ring-1 ring-teal-500/40 bg-teal-950/20' : 'border-slate-800 bg-slate-950/60';
          const goalTitle = p.goal_type.replace('_', ' ').toUpperCase();

          const item = document.createElement('div');
          item.className = `p-3.5 rounded-xl border ${cardBorder} flex flex-col sm:flex-row sm:items-center justify-between gap-3 text-xs transition`;
          item.innerHTML = `
            <div class="flex items-start sm:items-center gap-3 min-w-0">
              <span class="text-2xl shrink-0 p-1.5 rounded-lg bg-slate-900 border border-slate-800">${sportIcon}</span>
              <div class="min-w-0">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="font-bold text-white text-sm truncate">${goalTitle} (${p.sport_type})</span>
                  ${isSelected ? '<span class="px-2 py-0.5 rounded bg-teal-500/20 text-teal-300 font-bold text-[10px] shrink-0">AKTIV AUSGEWÄHLT</span>' : ''}
                  ${isArchived ? '<span class="px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 font-semibold text-[10px] shrink-0">📦 ARCHIVIERT</span>' : ''}
                </div>
                <div class="text-[11px] text-slate-400 mt-0.5">
                  Zieltag: <strong class="text-slate-300 font-mono">${p.target_date}</strong> · Basis ${formatSportVolume(p.base_weekly_volume, p.sport_type, true)}
                </div>
              </div>
            </div>

            <div class="flex items-center justify-between sm:justify-end gap-3 shrink-0 pt-2 sm:pt-0 border-t sm:border-t-0 border-slate-800/80">
              <div class="text-left sm:text-right font-mono">
                <div class="font-bold text-teal-400 text-sm">noch ${weeksLeft} W</div>
                <div class="text-[10px] text-slate-500">(${diffDays} Tage)</div>
              </div>

              <div class="flex items-center gap-1.5">
                ${!isSelected ? `
                  <button onclick="onPlanSelect('${p.id}')" title="Als aktuellen Plan anzeigen" class="p-1.5 px-2.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 font-medium transition text-[11px]">
                    Wählen
                  </button>
                ` : ''}

                ${!isArchived ? `
                  <button onclick="archivePlanAction('${p.id}')" title="Plan archivieren (pausieren)" class="p-1.5 px-2 rounded-lg bg-amber-500/10 hover:bg-amber-500/20 text-amber-300 border border-amber-500/30 transition text-[11px] flex items-center gap-1">
                    <span>📦</span> <span class="hidden xs:inline">Archivieren</span>
                  </button>
                ` : `
                  <button onclick="unarchivePlanAction('${p.id}')" title="Plan reaktivieren" class="p-1.5 px-2 rounded-lg bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 transition text-[11px] flex items-center gap-1">
                    <span>🌱</span> <span class="hidden xs:inline">Reaktivieren</span>
                  </button>
                `}

                <button onclick="deletePlanAction('${p.id}', '${goalTitle}')" title="Plan endgültig löschen" class="p-1.5 px-2 rounded-lg bg-rose-500/10 hover:bg-rose-500/20 text-rose-300 border border-rose-500/30 transition text-[11px] flex items-center gap-1">
                  <span>🗑️</span> <span class="hidden xs:inline">Löschen</span>
                </button>
              </div>
            </div>
          `;
          plansList.appendChild(item);
        });
      }
    }

    async function saveUserProfile() {
      const data = {
        display_name: document.getElementById('profile-display-name').innerText,
        resting_hr: parseInt(document.getElementById('input-resting-hr').value) || null,
        max_hr: parseInt(document.getElementById('input-max-hr').value) || null,
        weight_kg: parseFloat(document.getElementById('input-weight').value) || null,
        ftp_watts: parseInt(document.getElementById('input-ftp').value) || null,
        menstrual_cycle_active: document.getElementById('input-cycle-toggle').checked
      };
      await fetch('/api/profile/update', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data)
      });
      alert('Profil erfolgreich gespeichert!');
      await loadState();
    }

    async function recalculateZonesBtn() {
      const v = parseFloat(document.getElementById('input-vdot-val').value);
      if (!v) return;
      const curSport = appState.active_plan ? appState.active_plan.sport_type : 'running';
      await fetch('/api/profile/recalculate-zones', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({reference_value: v, sport_type: curSport})
      });
      await loadState();
    }

    // ================= MAIN BOTTOM TAB NAVIGATION =================
    function switchMainTab(tab) {
      const views = ['heute', 'wochenplan', 'makro', 'profil'];
      views.forEach(v => {
        document.getElementById(`view-${v}`).classList.add('hidden');
        document.getElementById(`nav-btn-${v}`).className = 'flex flex-col items-center py-1 text-slate-400 hover:text-slate-200 transition';
      });

      document.getElementById(`view-${tab}`).classList.remove('hidden');
      document.getElementById(`nav-btn-${tab}`).className = 'flex flex-col items-center py-1 text-teal-400 font-semibold transition';

      if (tab === 'heute' && pmcChartInstance) {
        pmcChartInstance.resize();
      }
      if (tab === 'makro' && macroChartInstance) {
        macroChartInstance.resize();
      }
    }

    function focusCheckin() {
      document.getElementById('checkin-input').focus();
    }

    // Conversational Check-in Flow
    async function handleCheckinSubmit(e) {
      e.preventDefault();
      const input = document.getElementById('checkin-input');
      const text = input.value.trim();
      if (!text) return;

      const chat = document.getElementById('chat-messages');
      chat.innerHTML += `<div class="bg-teal-600 text-white rounded-2xl rounded-tr-sm p-3 max-w-[85%] self-end text-sm">${text}</div>`;
      input.value = '';
      chat.scrollTop = chat.scrollHeight;

      pendingCheckinText = text;

      try {
        const res = await fetch('/api/checkin/preview', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({freetext: text})
        });
        const data = await res.json();
        const ev = data.event;

        let pillsHtml = `<div class="flex flex-wrap gap-1.5 mt-2">`;
        pillsHtml += `<span class="px-2 py-0.5 rounded-md bg-teal-500/20 text-teal-300 border border-teal-500/30 text-[11px] font-mono shrink-0">Status: ${ev.completion_status.toUpperCase()}</span>`;
        if (ev.actual_metrics && ev.actual_metrics.metric_primary) {
          pillsHtml += `<span class="px-2 py-0.5 rounded-md bg-slate-700 text-slate-200 text-[11px] font-mono shrink-0">${ev.actual_metrics.metric_primary} ${ev.actual_metrics.unit}</span>`;
        }
        if (ev.perceived_rpe) {
          pillsHtml += `<span class="px-2 py-0.5 rounded-md bg-purple-500/20 text-purple-300 border border-purple-500/30 text-[11px] font-mono shrink-0">RPE: ${ev.perceived_rpe}/10</span>`;
        }
        if (ev.symptoms && ev.symptoms.length > 0) {
          ev.symptoms.forEach(s => {
            pillsHtml += `<span class="px-2 py-0.5 rounded-md bg-rose-500/20 text-rose-300 border border-rose-500/30 text-[11px] font-mono shrink-0">⚠️ ${s.type} (${s.severity}/10)</span>`;
          });
        }
        pillsHtml += `</div>`;

        const confirmId = 'confirm-' + Date.now();
        const confirmationBubble = `
          <div id="${confirmId}" class="bg-slate-800 rounded-2xl rounded-tl-sm p-3 max-w-[95%] sm:max-w-[90%] text-slate-200 text-sm border border-teal-500/30">
            <div class="font-semibold text-teal-300">Ich habe Folgendes verstanden:</div>
            ${pillsHtml}
            <div class="mt-3 pt-2.5 border-t border-slate-700 flex flex-col xs:flex-row items-start xs:items-center justify-between gap-2">
              <span class="text-xs text-slate-300 font-medium">Stimmt das so?</span>
              <div class="flex gap-2">
                <button onclick="discardConfirmation('${confirmId}')" class="px-2.5 py-1 text-xs rounded-lg bg-slate-700 hover:bg-slate-600 text-slate-300 transition">Verwerfen</button>
                <button onclick="executeCheckin('${confirmId}')" class="px-3 py-1 text-xs font-semibold rounded-lg bg-teal-600 hover:bg-teal-500 text-white shadow transition">Bestätigen</button>
              </div>
            </div>
          </div>
        `;
        chat.innerHTML += confirmationBubble;
        chat.scrollTop = chat.scrollHeight;
      } catch (err) {
        console.error(err);
      }
    }

    function discardConfirmation(bubbleId) {
      const bubble = document.getElementById(bubbleId);
      if (bubble) {
        bubble.innerHTML = `<span class="text-xs text-slate-400 italic">Eingabe verworfen. Bitte beschreibe es noch einmal genauer.</span>`;
      }
      pendingCheckinText = null;
    }

    async function executeCheckin(bubbleId) {
      if (!pendingCheckinText) return;
      const textToPost = pendingCheckinText;
      pendingCheckinText = null;

      const bubble = document.getElementById(bubbleId);
      if (bubble) {
        bubble.querySelector('.flex.items-center.justify-between')?.remove();
      }

      const chat = document.getElementById('chat-messages');

      try {
        const res = await fetch('/api/checkin', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            freetext: textToPost,
            plan_id: currentPlanId
          })
        });
        const data = await res.json();

        let reply = `✅ Workout gespeichert! Status: <strong>${data.event.completion_status.toUpperCase()}</strong>`;
        if (data.mutations && data.mutations.length > 0) {
          reply += `<div class="mt-2 pt-2 border-t border-slate-700/60 text-xs text-amber-300 font-medium">${data.mutations[0].description}</div>`;
        }

        if (data.diffs && data.diffs.length > 0) {
          reply += `<div class="mt-3 pt-2.5 border-t border-slate-700 flex flex-col gap-2">
            <span class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Plan-Anpassungen (Vorher vs. Nachher):</span>`;
          data.diffs.forEach(d => {
            reply += `
              <div class="p-2.5 rounded-xl bg-slate-950/80 border border-slate-700/80 text-xs flex flex-col gap-1.5">
                <span class="font-mono text-slate-400 font-semibold text-[11px]">${d.date}</span>
                <div class="flex flex-wrap items-center gap-1.5">
                  <span class="px-2 py-0.5 rounded bg-rose-500/10 text-rose-300 line-through text-[11px]">${d.before.workout_type.toUpperCase()} · ${formatWorkoutMetric(d.before.metric_primary, d.before.metric_unit)}</span>
                  <span class="text-slate-500">→</span>
                  <span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 font-semibold text-[11px]">${d.after.workout_type.toUpperCase()} · ${formatWorkoutMetric(d.after.metric_primary, d.after.metric_unit)}</span>
                </div>
                ${d.after.intensity_detail ? `<span class="text-[10px] text-slate-400 italic break-words">${d.after.intensity_detail}</span>` : ''}
              </div>
            `;
          });
          reply += `</div>`;
        }

        chat.innerHTML += `<div class="bg-slate-800 rounded-2xl rounded-tl-sm p-3 max-w-[95%] sm:max-w-[92%] text-slate-200 text-sm border border-slate-700">${reply}</div>`;
        chat.scrollTop = chat.scrollHeight;

        await loadState();
      } catch (err) {
        console.error(err);
      }
    }

    // Modal controls: "Wie geht's dir?"
    function openStatusModal() { document.getElementById('status-modal').classList.remove('hidden'); }
    function closeStatusModal() { document.getElementById('status-modal').classList.add('hidden'); }

    async function submitStatusUpdate(type, note, severity = null) {
      closeStatusModal();
      const chat = document.getElementById('chat-messages');
      chat.innerHTML += `<div class="bg-amber-600 text-white rounded-2xl rounded-tr-sm p-2.5 max-w-[85%] self-end text-xs font-semibold">🩺 Status-Update: ${note}</div>`;

      try {
        const res = await fetch('/api/status-update', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            update_type: type,
            note: note,
            severity: severity,
            plan_id: currentPlanId
          })
        });
        const data = await res.json();

        let reply = `✅ Gesundheits-Update verarbeitet.`;
        if (data.mutations && data.mutations.length > 0) {
          reply += `<div class="mt-2 pt-2 border-t border-slate-700/60 text-xs text-amber-300 font-medium">${data.mutations[0].description}</div>`;
        }

        if (data.diffs && data.diffs.length > 0) {
          reply += `<div class="mt-3 pt-2.5 border-t border-slate-700 flex flex-col gap-2">
            <span class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Plan-Anpassungen (Vorher vs. Nachher):</span>`;
          data.diffs.forEach(d => {
            reply += `
              <div class="p-2.5 rounded-xl bg-slate-950/80 border border-slate-700/80 text-xs flex flex-col gap-1.5">
                <span class="font-mono text-slate-400 font-semibold text-[11px]">${d.date}</span>
                <div class="flex flex-wrap items-center gap-1.5">
                  <span class="px-2 py-0.5 rounded bg-rose-500/10 text-rose-300 line-through text-[11px]">${d.before.workout_type.toUpperCase()} · ${formatWorkoutMetric(d.before.metric_primary, d.before.metric_unit)}</span>
                  <span class="text-slate-500">→</span>
                  <span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 font-semibold text-[11px]">${d.after.workout_type.toUpperCase()} · ${formatWorkoutMetric(d.after.metric_primary, d.after.metric_unit)}</span>
                </div>
                ${d.after.intensity_detail ? `<span class="text-[10px] text-slate-400 italic break-words">${d.after.intensity_detail}</span>` : ''}
              </div>
            `;
          });
          reply += `</div>`;
        }

        chat.innerHTML += `<div class="bg-slate-800 rounded-2xl rounded-tl-sm p-3 max-w-[92%] text-slate-200 text-sm border border-slate-700">${reply}</div>`;
        chat.scrollTop = chat.scrollHeight;

        await loadState();
      } catch (err) {
        console.error(err);
      }
    }

    const SPORT_GOALS = {
      running: [
        { value: 'marathon', label: 'Marathon (42.2 km)' },
        { value: 'half_marathon', label: 'Halbmarathon (21.1 km)' },
        { value: '10k', label: '10 km Wettkampf' },
        { value: '5k', label: '5 km Speed' }
      ],
      cycling: [
        { value: 'ftp_builder', label: 'FTP Builder / Schwellenpower' },
        { value: 'gran_fondo', label: 'Gran Fondo / Radmarathon (120+ km)' },
        { value: 'climbing', label: 'Berg- & Kletter-Spezialist' },
        { value: 'time_trial', label: 'Einzelzeitfahren / TT' },
        { value: 'criterium', label: 'Kriterium / Sprint & VO2max' }
      ],
      swimming: [
        { value: 'css_improvement', label: 'CSS Schwellen-Verbesserung' },
        { value: 'open_water', label: 'Open Water / Freiwasser-Langdistanz' },
        { value: 'endurance_1500m', label: '1500 m Ausdauerschwimmen' },
        { value: 'speed_sprint', label: 'Sprint & Technik (50-200m)' }
      ],
      strength: [
        { value: 'hypertrophy', label: 'Hypertrophie (Muskelaufbau / DUP)' },
        { value: 'max_strength', label: 'Maximalkraft (Powerlifting / 1RM)' },
        { value: 'strength_endurance', label: 'Kraftausdauer / Definition' }
      ],
      triathlon: [
        { value: 'triathlon_olympic', label: 'Olympische Distanz (1.5 / 40 / 10)' },
        { value: 'triathlon_70_3', label: 'Mitteldistanz / 70.3 (1.9 / 90 / 21.1)' },
        { value: 'triathlon_ironman', label: 'Langdistanz / Ironman (3.8 / 180 / 42.2)' },
        { value: 'triathlon_sprint', label: 'Sprint-Distanz (0.75 / 20 / 5)' }
      ]
    };

    function onSportChange(sport) {
      const goalSelect = document.getElementById('guided-goal');
      if (!goalSelect) return;
      goalSelect.innerHTML = '';
      const goals = SPORT_GOALS[sport] || SPORT_GOALS['running'];
      goals.forEach(g => {
        const opt = document.createElement('option');
        opt.value = g.value;
        opt.innerText = g.label;
        goalSelect.appendChild(opt);
      });

      // Adjust labels and placeholders
      const volLabel = document.getElementById('guided-volume-label');
      const volInput = document.getElementById('guided-volume');
      const vdotLabel = document.getElementById('guided-vdot-label');
      const vdotInput = document.getElementById('guided-vdot');

      if (sport === 'cycling') {
        volLabel.innerText = "Basis (TSS / W)";
        volInput.value = 250;
        vdotLabel.innerText = "FTP (Watt)";
        vdotInput.value = 220;
      } else if (sport === 'swimming') {
        volLabel.innerText = "Basis (Meter / W)";
        volInput.value = 4000;
        vdotLabel.innerText = "CSS (s/100m)";
        vdotInput.value = 105;
      } else if (sport === 'strength') {
        volLabel.innerText = "Basis (Sätze / W)";
        volInput.value = 14;
        vdotLabel.innerText = "1RM Benchmark (kg)";
        vdotInput.value = 100;
      } else if (sport === 'triathlon') {
        volLabel.innerText = "Basis (TSS / W)";
        volInput.value = 350;
        vdotLabel.innerText = "Lauf VDOT";
        vdotInput.value = 46;
      } else {
        volLabel.innerText = "Basis (km/W)";
        volInput.value = 30;
        vdotLabel.innerText = "VDOT / Fitness";
        vdotInput.value = 46;
      }
    }

    // ================= SCREEN 6: ONBOARDING FLOW =================
    function openOnboardModal() {
      document.getElementById('onboard-modal').classList.remove('hidden');
      onSportChange(document.getElementById('guided-sport').value || 'running');
      backToOnboardInput();
    }
    function closeOnboardModal() {
      document.getElementById('onboard-modal').classList.add('hidden');
    }

    function switchOnboardMode(mode) {
      if (mode === 'chat') {
        document.getElementById('onboard-chat-box').classList.remove('hidden');
        document.getElementById('onboard-guided-box').classList.add('hidden');
        document.getElementById('onboard-mode-chat-btn').className = "text-teal-400 font-bold border-b-2 border-teal-400 pb-1";
        document.getElementById('onboard-mode-guided-btn').className = "text-slate-400 hover:text-slate-200 pb-1";
      } else {
        document.getElementById('onboard-chat-box').classList.add('hidden');
        document.getElementById('onboard-guided-box').classList.remove('hidden');
        document.getElementById('onboard-mode-guided-btn').className = "text-teal-400 font-bold border-b-2 border-teal-400 pb-1";
        document.getElementById('onboard-mode-chat-btn').className = "text-slate-400 hover:text-slate-200 pb-1";
        onSportChange(document.getElementById('guided-sport').value || 'running');
      }
    }

    async function analyzeOnboardPrompt() {
      const prompt = document.getElementById('onboard-prompt').value.trim();
      if (!prompt) return;

      const res = await fetch('/api/onboard/preview', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({prompt})
      });
      const data = await res.json();
      const p = data.parsed;

      let baseVol = p.current_baseline.value || 30.0;
      let refFit = p.user_profile.vdot || 45.0;
      if (p.sport_type === 'cycling' && baseVol < 50) baseVol = 250.0;
      if (p.sport_type === 'swimming' && baseVol < 500) baseVol = 4000.0;
      if (p.sport_type === 'strength' && baseVol > 40) baseVol = 14.0;
      if (p.sport_type === 'triathlon' && baseVol < 100) baseVol = 350.0;

      pendingPlanParams = {
        sport: p.sport_type,
        goal: p.target_event,
        vdot: refFit,
        weeks_count: p.target_weeks || 16,
        baseline_volume: baseVol
      };

      showSummaryView();
    }

    function previewGuidedGoal() {
      pendingPlanParams = {
        sport: document.getElementById('guided-sport').value,
        goal: document.getElementById('guided-goal').value,
        vdot: parseFloat(document.getElementById('guided-vdot').value) || 45.0,
        weeks_count: parseInt(document.getElementById('guided-weeks').value) || 16,
        baseline_volume: parseFloat(document.getElementById('guided-volume').value) || 30.0
      };
      showSummaryView();
    }

    function showSummaryView() {
      document.getElementById('onboard-step-input').classList.add('hidden');
      document.getElementById('onboard-step-summary').classList.remove('hidden');

      const sport = pendingPlanParams.sport;
      let volUnit = "km/W";
      let fitLabel = "VDOT";
      if (sport === 'cycling') { volUnit = "TSS/W"; fitLabel = "FTP (W)"; }
      else if (sport === 'swimming') { volUnit = "m/W"; fitLabel = "CSS (s/100m)"; }
      else if (sport === 'strength') { volUnit = "Sätze/W"; fitLabel = "1RM (kg)"; }
      else if (sport === 'triathlon') { volUnit = "TSS/W"; fitLabel = "VDOT"; }

      document.getElementById('sum-sport').innerText = pendingPlanParams.sport.toUpperCase();
      document.getElementById('sum-goal').innerText = pendingPlanParams.goal.replace('_', ' ').toUpperCase();
      document.getElementById('sum-weeks').innerText = `${pendingPlanParams.weeks_count} Wochen`;
      document.getElementById('sum-volume').innerText = `${pendingPlanParams.baseline_volume} ${volUnit}`;
      document.getElementById('sum-vdot').innerText = `${pendingPlanParams.vdot.toFixed(1)} (${fitLabel})`;
    }

    function backToOnboardInput() {
      document.getElementById('onboard-step-input').classList.remove('hidden');
      document.getElementById('onboard-step-summary').classList.add('hidden');
      document.getElementById('onboard-step-animation').classList.add('hidden');
    }

    async function finalizePlanGeneration() {
      if (!pendingPlanParams) return;
      document.getElementById('onboard-step-summary').classList.add('hidden');
      document.getElementById('onboard-step-animation').classList.remove('hidden');

      const stages = [
        "Berechne Periodisierung (Base, Build, Peak, Taper)...",
        "Generiere Belastungswellen und Entlastungswochen...",
        "Erstelle tägliche Workouts nach Daniels Pacing...",
        "Fertig! Trainingsplan aufgebaut."
      ];

      for (let i = 0; i < stages.length; i++) {
        document.getElementById('anim-stage').innerText = stages[i];
        await new Promise(r => setTimeout(r, 400));
      }

      await fetch('/api/plan/create', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(pendingPlanParams)
      });

      closeOnboardModal();
      await loadState();
      switchMainTab('wochenplan');
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
