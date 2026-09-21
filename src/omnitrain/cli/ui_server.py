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
from omnitrain.core.llm import UnifiedLLMClient
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
            m = json.loads(c["actual_metrics_json"] or "{}")
            tss = m.get("tss", 0.0)
            if not tss or tss <= 0:
                dist = float(m.get("metric_primary", 0.0) or 0.0)
                unit = m.get("unit", "km")
                if unit == "km" and dist > 0:
                    tss = round(dist * 6.5, 1)  # ~6.5 TSS per km of running
                elif unit == "min" and dist > 0:
                    tss = round(dist * 0.85, 1)  # ~50 TSS per hour of cycling
                elif unit == "m" and dist > 0:
                    tss = round((dist / 1000.0) * 20.0, 1)  # ~20 TSS per 1000m swim
                elif unit == "sets" and dist > 0:
                    tss = round(dist * 3.0, 1)  # ~3 TSS per work set
                else:
                    tss = 0.0
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
        has_pain_workouts = any(
            w["date"] >= today_str and any(k in (w["intensity_detail"] or "") for k in ["Schmerz", "Niggle", "Schonung"])
            for w in w_list
        )
        pain_detail = None
        for w in w_list:
            if w["date"] >= today_str and any(k in (w["intensity_detail"] or "") for k in ["Schmerz", "Niggle", "Schonung"]):
                pain_detail = w["intensity_detail"]
                break

        # 6. Recent Check-in Logs for Conversational History
        recent_checkin_rows = conn.execute(
            """
            SELECT c.id, c.timestamp, c.raw_input, c.completion_status, c.perceived_rpe,
                   c.actual_metrics_json, c.applied_mutations_json, w.workout_type, w.sport_type, w.date as workout_date
            FROM checkin_logs c
            LEFT JOIN workouts w ON c.workout_id = w.id
            ORDER BY c.timestamp DESC LIMIT 20;
            """
        ).fetchall()
        recent_checkins = [dict(r) for r in reversed(recent_checkin_rows)]

        llm_client = UnifiedLLMClient()
        llm_info = llm_client.get_info()

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
            "is_sick_mode": has_sick_workouts,
            "is_pain_mode": has_pain_workouts,
            "pain_detail": pain_detail,
            "recent_checkins": recent_checkins,
            "llm": llm_info,
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
    update_type: str  # recovery, sickness, pain_resolved, pain_report, readiness, fatigue, fit
    note: Optional[str] = None
    severity: Optional[int] = None
    location: Optional[str] = None
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
    elif req.update_type == "sickness":
        u_type = UpdateType.SICKNESS
        details = {"condition": "sickness"}
    elif req.update_type in ["pain_resolved", "pain_clear"]:
        u_type = UpdateType.PAIN_UPDATE
        details = {"status": "resolved"}
    elif req.update_type == "pain_report":
        u_type = UpdateType.PAIN_REPORT
        details = {"severity": req.severity or 5, "location": req.location or "Gelenk/Muskel"}
    elif req.update_type in ["readiness", "fatigue"]:
        u_type = UpdateType.READINESS_UPDATE
        details = {"severity": req.severity or 8}
    elif req.update_type == "fit":
        u_type = UpdateType.FIT
        details = {"readiness": 10}

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
            ORDER BY w.date ASC;
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
    name: Optional[str] = None


@app.post("/api/plan/create")
def create_plan(req: PlanCreateRequest):
    from omnitrain.cli.main import plan_new
    plan_new(
        sport=req.sport,
        goal=req.goal,
        vdot=req.vdot,
        weeks_count=req.weeks_count,
        baseline_volume=req.baseline_volume,
        name=req.name
    )
    return {"status": "ok"}


class PlanRenameRequest(BaseModel):
    plan_id: str
    name: str


@app.post("/api/plan/rename")
def rename_plan(req: PlanRenameRequest):
    new_name = req.name.strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="Plan name must not be empty")
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE plans SET name = ?, updated_at = ? WHERE id = ?;",
            (new_name, now, req.plan_id)
        )
        conn.commit()
    return {"status": "ok", "message": "Plan umbenannt", "name": new_name}


class PlanUpdateParametersRequest(BaseModel):
    plan_id: str
    name: Optional[str] = None
    target_date: Optional[str] = None
    weeks_count: Optional[int] = None
    base_weekly_volume: Optional[float] = None
    reference_value: Optional[float] = None


@app.post("/api/plan/update-parameters")
def update_plan_parameters(req: PlanUpdateParametersRequest):
    from omnitrain.sports.cycling import CyclingStrategy
    from omnitrain.sports.swimming import SwimStrategy
    from omnitrain.sports.strength import StrengthStrategy
    from omnitrain.sports.triathlon import TriathlonStrategy

    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    today = date.today()

    with db.get_connection() as conn:
        plan_row = conn.execute("SELECT * FROM plans WHERE id = ? LIMIT 1;", (req.plan_id,)).fetchone()
        if not plan_row:
            raise HTTPException(status_code=404, detail="Plan not found")

        sport = plan_row["sport_type"]
        goal = plan_row["goal_type"]
        user_id = plan_row["user_id"]
        start_date = date.fromisoformat(plan_row["start_date"])
        current_target_date = date.fromisoformat(plan_row["target_date"])

        # 1. Determine new target_date
        new_target_date = current_target_date
        if req.target_date:
            try:
                new_target_date = date.fromisoformat(req.target_date)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid target_date format. Use YYYY-MM-DD.")
        elif req.weeks_count and req.weeks_count >= 4:
            new_target_date = start_date + timedelta(weeks=req.weeks_count)

        if new_target_date <= start_date:
            raise HTTPException(status_code=400, detail="Zieldatum muss nach dem Startdatum liegen.")

        # 2. Determine base_weekly_volume
        new_base_vol = float(req.base_weekly_volume) if req.base_weekly_volume is not None and req.base_weekly_volume > 0 else float(plan_row["base_weekly_volume"])

        # 3. Determine new name
        new_name = req.name.strip() if req.name and req.name.strip() else plan_row["name"]

        # 4. Determine available days & sessions
        available_days = json.loads(plan_row["available_days"] or "[]")
        if not available_days:
            if sport in ["swimming", "strength"]:
                available_days = [1, 3, 5]
            elif sport in ["triathlon", "multisport"]:
                available_days = [1, 2, 3, 4, 5, 6, 7]
            else:
                available_days = [2, 4, 6, 7]
        sessions_per_week = plan_row["sessions_per_week"] or len(available_days)

        # 5. Fetch existing reference value
        zone_row = conn.execute(
            "SELECT * FROM training_zones WHERE user_id = ? AND sport_type = ? ORDER BY calculated_at DESC LIMIT 1;",
            (user_id, sport)
        ).fetchone()
        existing_ref = float(zone_row["reference_value"]) if zone_row and zone_row["reference_value"] else 45.0
        ref_val = float(req.reference_value) if req.reference_value is not None and req.reference_value > 0 else existing_ref

        # 6. Update plans table
        conn.execute(
            """
            UPDATE plans
            SET name = ?, target_date = ?, base_weekly_volume = ?, updated_at = ?
            WHERE id = ?;
            """,
            (new_name, new_target_date.isoformat(), new_base_vol, now, req.plan_id)
        )

        # 7. Update training_zones if reference_value provided or sport-specific zones
        if sport == "cycling":
            strat = CyclingStrategy()
            zones = strat.calculate_zones(ref_val)
            zone_model = "coggan_ftp"
        elif sport == "swimming":
            strat = SwimStrategy()
            zones = strat.calculate_zones(ref_val)
            zone_model = "swim_css"
        elif sport == "strength":
            strat = StrengthStrategy()
            zones = strat.calculate_zones(ref_val)
            zone_model = "strength_dup"
        elif sport in ["triathlon", "multisport"]:
            zones = get_daniels_zones(ref_val)
            zone_model = "triathlon_hybrid"
        else:
            zones = get_daniels_zones(ref_val)
            zone_model = "daniels_vdot"

        new_zone_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO training_zones (
                id, user_id, sport_type, zone_model, reference_value,
                zones_json, calculated_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (new_zone_id, user_id, sport, zone_model, ref_val, json.dumps(zones), now, now, now)
        )

        # 8. Check if there are past/completed workouts
        completed_count = conn.execute(
            """
            SELECT COUNT(*) as cnt FROM workouts w
            JOIN weeks wk ON w.week_id = wk.id
            WHERE wk.plan_id = ? AND (w.status = 'completed' OR w.date < ?);
            """,
            (req.plan_id, today.isoformat())
        ).fetchone()["cnt"]

        if completed_count == 0:
            # Full regeneration from original start_date
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("DELETE FROM workouts WHERE week_id IN (SELECT id FROM weeks WHERE plan_id = ?);", (req.plan_id,))
            conn.execute("DELETE FROM weeks WHERE plan_id = ?;", (req.plan_id,))

            if sport == "swimming":
                strat = SwimStrategy()
                weeks = strat.generate_plan(
                    plan_id=req.plan_id,
                    start_date=start_date,
                    target_date=new_target_date,
                    base_weekly_volume=new_base_vol,
                    available_days=available_days,
                    reference_value=ref_val,
                    sessions_per_week=sessions_per_week
                )
            elif sport == "strength":
                strat = StrengthStrategy()
                weeks = strat.generate_plan(
                    plan_id=req.plan_id,
                    start_date=start_date,
                    target_date=new_target_date,
                    base_weekly_volume=new_base_vol,
                    available_days=available_days,
                    reference_value=ref_val,
                    sessions_per_week=sessions_per_week
                )
            elif sport == "cycling":
                strat = CyclingStrategy()
                weeks = strat.generate_plan(
                    plan_id=req.plan_id,
                    start_date=start_date,
                    target_date=new_target_date,
                    base_weekly_volume=new_base_vol,
                    available_days=available_days,
                    reference_value=ref_val,
                    sessions_per_week=sessions_per_week
                )
            elif sport in ["triathlon", "multisport"]:
                weeks = TriathlonStrategy.generate_plan(
                    plan_id=req.plan_id,
                    start_date=start_date,
                    target_date=new_target_date,
                    base_weekly_volume=new_base_vol,
                    available_days=available_days,
                    limiter="swim",
                    target_event=goal if "triathlon" in goal else "triathlon_olympic"
                )
            else:
                weeks = RunningStrategy.generate_plan(
                    plan_id=req.plan_id,
                    start_date=start_date,
                    target_date=new_target_date,
                    base_weekly_volume=new_base_vol,
                    available_days=available_days,
                    vdot=ref_val,
                    sessions_per_week=sessions_per_week
                )

            for w in weeks:
                conn.execute(
                    """
                    INSERT INTO weeks (
                        id, plan_id, week_number, week_start_date, phase,
                        target_weekly_volume, target_weekly_tss, is_recovery_week,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (w.id, req.plan_id, w.week_number, w.week_start_date.isoformat(), w.phase.value,
                     w.target_weekly_volume, w.target_weekly_tss, 1 if w.is_recovery_week else 0, now, now)
                )
                for wo in w.workouts:
                    conn.execute(
                        """
                        INSERT INTO workouts (
                            id, week_id, sport_type, date, day_of_week, workout_type,
                            metric_primary, metric_unit, intensity_target, intensity_detail,
                            target_duration_min, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (wo.id, w.id, wo.sport_type.value, wo.date.isoformat(), wo.day_of_week,
                         wo.workout_type, wo.metric_primary, wo.metric_unit, wo.intensity_target,
                         wo.intensity_detail, wo.target_duration_min, wo.status.value, now, now)
                    )
        else:
            # Has past checkins or completed workouts: preserve past, regenerate future from next Monday
            # Delete upcoming planned workouts and weeks that have no completed workouts
            conn.execute(
                """
                DELETE FROM workouts
                WHERE date >= ? AND status = 'planned'
                AND week_id IN (SELECT id FROM weeks WHERE plan_id = ?);
                """,
                (today.isoformat(), req.plan_id)
            )

            # Delete any weeks that no longer contain any workouts
            conn.execute(
                """
                DELETE FROM weeks
                WHERE plan_id = ?
                AND id NOT IN (SELECT DISTINCT week_id FROM workouts WHERE week_id IS NOT NULL);
                """,
                (req.plan_id,)
            )

            # Find max existing week number
            max_wk_row = conn.execute(
                "SELECT MAX(week_number) as max_w FROM weeks WHERE plan_id = ?;",
                (req.plan_id,)
            ).fetchone()
            start_week_num = (max_wk_row["max_w"] or 0) + 1

            # Next Monday
            days_ahead = (7 - today.weekday()) % 7
            next_monday = today + timedelta(days=days_ahead if days_ahead > 0 else 7)

            if next_monday < new_target_date:
                # Generate future weeks
                if sport == "cycling":
                    strat = CyclingStrategy()
                    future_weeks = strat.generate_plan(
                        plan_id=req.plan_id,
                        start_date=next_monday,
                        target_date=new_target_date,
                        base_weekly_volume=new_base_vol,
                        available_days=available_days,
                        reference_value=ref_val,
                        sessions_per_week=sessions_per_week
                    )
                elif sport == "swimming":
                    strat = SwimStrategy()
                    future_weeks = strat.generate_plan(
                        plan_id=req.plan_id,
                        start_date=next_monday,
                        target_date=new_target_date,
                        base_weekly_volume=new_base_vol,
                        available_days=available_days,
                        reference_value=ref_val,
                        sessions_per_week=sessions_per_week
                    )
                elif sport == "strength":
                    strat = StrengthStrategy()
                    future_weeks = strat.generate_plan(
                        plan_id=req.plan_id,
                        start_date=next_monday,
                        target_date=new_target_date,
                        base_weekly_volume=new_base_vol,
                        available_days=available_days,
                        reference_value=ref_val,
                        sessions_per_week=sessions_per_week
                    )
                elif sport in ["triathlon", "multisport"]:
                    future_weeks = TriathlonStrategy.generate_plan(
                        plan_id=req.plan_id,
                        start_date=next_monday,
                        target_date=new_target_date,
                        base_weekly_volume=new_base_vol,
                        available_days=available_days,
                        limiter="swim",
                        target_event=goal if "triathlon" in goal else "triathlon_olympic"
                    )
                else:
                    future_weeks = RunningStrategy.generate_plan(
                        plan_id=req.plan_id,
                        start_date=next_monday,
                        target_date=new_target_date,
                        base_weekly_volume=new_base_vol,
                        available_days=available_days,
                        vdot=ref_val,
                        sessions_per_week=sessions_per_week
                    )

                for idx, w in enumerate(future_weeks):
                    w_num = start_week_num + idx
                    conn.execute(
                        """
                        INSERT INTO weeks (
                            id, plan_id, week_number, week_start_date, phase,
                            target_weekly_volume, target_weekly_tss, is_recovery_week,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (w.id, req.plan_id, w_num, w.week_start_date.isoformat(), w.phase.value,
                         w.target_weekly_volume, w.target_weekly_tss, 1 if w.is_recovery_week else 0, now, now)
                    )
                    for wo in w.workouts:
                        conn.execute(
                            """
                            INSERT INTO workouts (
                                id, week_id, sport_type, date, day_of_week, workout_type,
                                metric_primary, metric_unit, intensity_target, intensity_detail,
                                target_duration_min, status, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                            """,
                            (wo.id, w.id, wo.sport_type.value, wo.date.isoformat(), wo.day_of_week,
                             wo.workout_type, wo.metric_primary, wo.metric_unit, wo.intensity_target,
                             wo.intensity_detail, wo.target_duration_min, wo.status.value, now, now)
                        )

        conn.commit()

    return {
        "status": "ok",
        "message": "Plan-Eckdaten erfolgreich aktualisiert",
        "plan_id": req.plan_id,
        "name": new_name,
        "target_date": new_target_date.isoformat(),
        "base_weekly_volume": new_base_vol,
        "reference_value": ref_val
    }


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
    /* Modern Slim Dark Scrollbar */
    .custom-scrollbar::-webkit-scrollbar { width: 5px; height: 5px; }
    .custom-scrollbar::-webkit-scrollbar-track { background: transparent; }
    .custom-scrollbar::-webkit-scrollbar-thumb { background: rgba(100, 116, 139, 0.35); border-radius: 9999px; }
    .custom-scrollbar::-webkit-scrollbar-thumb:hover { background: rgba(13, 148, 136, 0.6); }
    .custom-scrollbar { scrollbar-width: thin; scrollbar-color: rgba(100, 116, 139, 0.35) transparent; }
  </style>
</head>
<body class="min-h-screen font-sans flex justify-center py-4 sm:py-6 px-2.5 sm:px-4 pb-36 overflow-x-hidden w-full">
  <div class="w-full max-w-xl flex flex-col gap-5 sm:gap-6 min-w-0">

    <!-- Top Navigation / Status Header -->
    <header class="flex flex-col sm:flex-row sm:items-center justify-between border-b border-slate-800/80 pb-3 sm:pb-4 gap-3">
      <div class="min-w-0">
        <div class="flex items-center gap-2">
          <span class="w-2.5 h-2.5 rounded-full bg-teal-400 animate-pulse shrink-0"></span>
          <h1 class="font-bold text-lg tracking-tight truncate">OmniTrain</h1>
          <span class="text-xs px-2 py-0.5 rounded bg-slate-800 text-slate-400 font-mono shrink-0">v0.1 Blueprint</span>
        </div>
        <div class="flex items-center gap-1.5 mt-0.5 min-w-0">
          <p id="plan-title" class="text-xs text-slate-400 truncate">Lade Trainingsplan...</p>
          <button id="header-plan-edit-btn" onclick="openCurrentPlanEditModal()" title="Eckdaten dieses Plans bearbeiten (Zieldatum, Basis, VDOT...)" class="text-slate-400 hover:text-teal-400 p-0.5 text-xs transition shrink-0 hidden">
            ⚙️
          </button>
        </div>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <select id="plan-selector" onchange="onPlanSelect(this.value)" class="text-xs bg-slate-900 border border-slate-700 text-slate-200 rounded-lg px-2.5 py-1.5 focus:outline-none focus:border-teal-500 font-medium max-w-[140px] sm:max-w-[200px] truncate shrink-0">
          <!-- Dynamically populated -->
        </select>
        <button onclick="openGlossaryModal()" class="text-xs font-semibold px-2.5 sm:px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 hover:border-slate-600 transition flex items-center gap-1.5 shrink-0" title="Glossar & Erklärungen aller Abkürzungen (CTL, VDOT, FTP, etc.)">
          <span>❓</span> <span class="hidden xs:inline">Hilfe & Glossar</span><span class="xs:hidden">Hilfe</span>
        </button>
        <button id="header-status-btn" onclick="openStatusModal()" class="text-xs font-semibold px-2.5 sm:px-3 py-1.5 rounded-lg bg-amber-500/20 text-amber-300 border border-amber-500/30 hover:bg-amber-500/30 transition flex items-center gap-1.5 shrink-0">
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

    <!-- PAIN Mode Banner -->
    <div id="pain-banner" class="hidden glass-card border-amber-500/40 bg-amber-950/40 rounded-2xl p-3.5 sm:p-4 flex flex-col sm:flex-row sm:items-center justify-between gap-3 text-amber-200 text-xs">
      <div class="flex items-center gap-3 min-w-0">
        <span class="text-2xl shrink-0">🩹</span>
        <div class="min-w-0">
          <div class="font-bold text-amber-300 text-sm">Schonmodus aktiv</div>
          <div id="pain-banner-detail" class="text-amber-300/80 text-[11px] mt-0.5 break-words">Workouts werden wegen Beschwerden geschont.</div>
        </div>
      </div>
      <div class="flex items-center gap-2 shrink-0 self-start sm:self-auto">
        <button onclick="submitStatusUpdate('pain_resolved', 'Schmerzen vollständig abgeklungen')" class="px-3 py-2 rounded-xl bg-teal-600 hover:bg-teal-500 text-white font-semibold text-xs shadow-lg transition flex items-center gap-1">
          <span>✨</span> Schmerzfrei
        </button>
        <button onclick="openStatusModal()" class="px-2.5 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs transition">
          Anpassen
        </button>
      </div>
    </div>

    <!-- MAIN APP TABS: Heute · Wochenplan · Makrozyklus · Profil & Zonen -->

    <!-- ================= TAB 1: HEUTE ================= -->
    <div id="view-heute" class="flex flex-col gap-6">

      <!-- PMC Metrics Pill Bar -->
      <div id="pmc-bar" class="grid grid-cols-3 gap-2 text-center text-xs">
        <div class="glass-pill p-2 rounded-xl">
          <div class="flex items-center justify-center gap-1">
            <span class="text-slate-400 text-[10px] uppercase tracking-wider font-semibold">Fitness (CTL)</span>
            <button type="button" onclick="showTermHelp('ctl', event)" class="w-3.5 h-3.5 rounded-full bg-slate-800 hover:bg-teal-500 text-slate-400 hover:text-white flex items-center justify-center text-[9px] font-bold transition shrink-0" title="Was bedeutet CTL? (Chronic Training Load / Fitness)">?</button>
          </div>
          <span id="pmc-ctl" class="font-mono text-base font-bold text-teal-400 block mt-0.5">--</span>
        </div>
        <div class="glass-pill p-2 rounded-xl">
          <div class="flex items-center justify-center gap-1">
            <span class="text-slate-400 text-[10px] uppercase tracking-wider font-semibold">Fatigue (ATL)</span>
            <button type="button" onclick="showTermHelp('atl', event)" class="w-3.5 h-3.5 rounded-full bg-slate-800 hover:bg-purple-500 text-slate-400 hover:text-white flex items-center justify-center text-[9px] font-bold transition shrink-0" title="Was bedeutet ATL? (Acute Training Load / Ermüdung)">?</button>
          </div>
          <span id="pmc-atl" class="font-mono text-base font-bold text-purple-400 block mt-0.5">--</span>
        </div>
        <div class="glass-pill p-2 rounded-xl">
          <div class="flex items-center justify-center gap-1">
            <span class="text-slate-400 text-[10px] uppercase tracking-wider font-semibold">Form (TSB)</span>
            <button type="button" onclick="showTermHelp('tsb', event)" class="w-3.5 h-3.5 rounded-full bg-slate-800 hover:bg-amber-500 text-slate-400 hover:text-white flex items-center justify-center text-[9px] font-bold transition shrink-0" title="Was bedeutet TSB? (Training Stress Balance / Frische)">?</button>
          </div>
          <span id="pmc-tsb" class="font-mono text-base font-bold text-amber-400 block mt-0.5">--</span>
        </div>
      </div>

      <!-- Screen 8: PMC Chart Canvas Section -->
      <section class="glass-card rounded-2xl p-3.5 sm:p-4 border border-slate-800/80">
        <div class="flex flex-col xs:flex-row justify-between items-start xs:items-center gap-1.5 mb-3">
          <div class="flex items-center gap-1.5">
            <span class="text-xs font-semibold uppercase tracking-wider text-slate-400">PMC Trend</span>
            <button type="button" onclick="showTermHelp('pmc', event)" class="w-3.5 h-3.5 rounded-full bg-slate-800 hover:bg-teal-500 text-slate-400 hover:text-white flex items-center justify-center text-[9px] font-bold transition shrink-0" title="Erklärung zum Performance Management Chart (PMC)">?</button>
            <span class="text-[10px] text-teal-400 font-mono cursor-pointer hover:underline" onclick="showTermHelp('pmc', event)" title="Erklärung zu PMC">CTL · ATL · TSB</span>
          </div>
          <div class="flex items-center gap-2.5 text-[10px] font-mono">
            <span class="flex items-center gap-1 text-teal-400 cursor-pointer hover:underline" onclick="showTermHelp('ctl', event)" title="Chronic Training Load (Fitness)"><span class="w-2 h-2 rounded-full bg-teal-400 inline-block"></span> CTL</span>
            <span class="flex items-center gap-1 text-purple-400 cursor-pointer hover:underline" onclick="showTermHelp('atl', event)" title="Acute Training Load (Fatigue)"><span class="w-2 h-2 rounded-full bg-purple-400 inline-block"></span> ATL</span>
            <span class="flex items-center gap-1 text-amber-400 cursor-pointer hover:underline" onclick="showTermHelp('tsb', event)" title="Training Stress Balance (Form)"><span class="w-2 h-2 rounded-full bg-amber-400 inline-block"></span> TSB</span>
          </div>
        </div>
        <div class="h-36 sm:h-40 w-full relative">
          <canvas id="pmcChart"></canvas>
          <div id="pmc-empty-notice" class="hidden absolute inset-0 flex flex-col items-center justify-center bg-slate-950/80 backdrop-blur-xs rounded-xl p-3 text-center z-10">
            <span class="text-xl mb-1">📈</span>
            <span class="text-xs font-semibold text-slate-300">Noch keine Belastungsdaten</span>
            <span class="text-[10px] text-slate-500 mt-0.5 max-w-xs">Sobald Einheiten erfasst werden, visualisiert das PMC hier deine Fitness (CTL), Ermüdung (ATL) und Form (TSB).</span>
          </div>
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
          <button type="button" onclick="showTermHelp('phases', event)" id="week-phase" class="text-xs font-medium text-teal-400 hover:underline flex items-center gap-1 cursor-pointer" title="Erklärung zur Trainingsphase (Base, Build, Peak, Taper)">
            <span>Phase: BUILD</span> <span class="w-3.5 h-3.5 rounded-full bg-slate-800 text-slate-400 inline-flex items-center justify-center text-[9px] font-bold">?</span>
          </button>
        </div>
        <div id="week-dots" class="grid grid-cols-7 gap-1 sm:gap-2"></div>
      </section>

      <!-- Screen 2: Conversational Check-in (Things 3 / Messenger UX) -->
      <section class="glass-card rounded-2xl p-4 sm:p-5 mb-4">
        <div class="flex flex-wrap items-center justify-between gap-2 mb-3">
          <div class="flex flex-wrap items-center gap-2 min-w-0">
            <div class="flex items-center gap-1.5 shrink-0">
              <svg class="w-4 h-4 text-teal-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 100-6 3 3 0 000 6z"></path></svg>
              <h3 class="text-sm font-semibold tracking-tight text-slate-200">Conversational Coach</h3>
            </div>
            <span id="llm-status-badge" class="text-[10px] font-mono px-2 py-0.5 rounded-full bg-slate-800 text-slate-400 border border-slate-700 truncate max-w-[200px]">Connecting LLM...</span>
          </div>
          <button type="button" onclick="showTermHelp('rpe', event)" class="text-[10px] text-teal-400 hover:underline flex items-center gap-1 shrink-0" title="Erklärung zur RPE-Belastungsskala (1-10)">
            <span>Was ist RPE?</span> <span class="w-3 h-3 rounded-full bg-slate-800 text-slate-400 inline-flex items-center justify-center text-[8px] font-bold">?</span>
          </button>
        </div>

        <div id="chat-messages" class="flex flex-col gap-2.5 max-h-64 sm:max-h-72 overflow-y-auto mb-3 text-xs sm:text-sm pr-1.5 custom-scrollbar">
          <div class="bg-slate-800/80 rounded-2xl rounded-tl-sm p-3 max-w-[90%] sm:max-w-[88%] text-slate-300 break-words">
            Wie war dein Training heute? Erzähl mir frei von Distanz, Gefühl (<span class="text-teal-300 cursor-pointer underline decoration-dotted" onclick="showTermHelp('rpe', event)" title="Rating of Perceived Exertion (1=sehr leicht, 10=maximal)">RPE 1-10 ℹ</span>) oder etwaigen Schmerzen / Symptomen.
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
          <button type="button" onclick="showTermHelp('phases', event)" id="carousel-phase-badge" class="px-2 py-0.5 rounded bg-teal-500/20 text-teal-300 font-bold uppercase tracking-wider text-[10px] shrink-0 hover:ring-1 hover:ring-teal-400 transition" title="Erklärung zur Trainingsphase">BUILD</button>
          <button type="button" onclick="showTermHelp('deload', event)" id="carousel-meso-text" class="text-slate-300 truncate hover:text-teal-300 transition text-left cursor-pointer" title="Erklärung zu Belastungs- & Entlastungswochen (Deload)">Woche vor Deload</button>
        </div>
        <div class="font-mono font-bold text-slate-200 shrink-0 ml-2 cursor-pointer hover:text-teal-300 flex items-center gap-1" id="carousel-volume-text" onclick="showTermHelp('units', event)" title="Erklärung zu Trainingsvolumen & Einheiten">--</div>
      </div>

      <!-- 7 Day Workout Cards Carousel -->
      <div id="weekly-days-cards" class="flex flex-col gap-2.5 sm:gap-3">
        <!-- Dynamically rendered day cards -->
      </div>
    </div>

    <!-- ================= TAB 3: MAKROZYKLUS (Screen 4) ================= -->
    <div id="view-makro" class="hidden flex flex-col gap-4 sm:gap-5">
      <div>
        <div class="flex items-center gap-2">
          <h2 class="text-base sm:text-lg font-bold tracking-tight text-white">Makrozyklus (Bird's-Eye-View)</h2>
          <button type="button" onclick="showTermHelp('phases', event)" class="w-3.5 h-3.5 rounded-full bg-slate-800 hover:bg-teal-500 text-slate-400 hover:text-white flex items-center justify-center text-[9px] font-bold transition shrink-0" title="Erklärung zu Makrozyklus & Periodisierungs-Phasen">?</button>
        </div>
        <p class="text-xs text-slate-400">Vollständige Periodisierung & Phasenverlauf</p>
      </div>

      <!-- Macro Volume Chart Canvas -->
      <section class="glass-card rounded-2xl p-3.5 sm:p-4 border border-slate-800/80">
        <div class="flex justify-between items-center mb-2">
          <div class="flex items-center gap-1.5">
            <span class="text-xs font-semibold uppercase tracking-wider text-slate-400">Wöchentliche Volumen-Kurve</span>
            <button type="button" onclick="showTermHelp('units', event)" class="w-3.5 h-3.5 rounded-full bg-slate-800 hover:bg-teal-500 text-slate-400 hover:text-white flex items-center justify-center text-[9px] font-bold transition shrink-0" title="Erklärung zum wöchentlichen Volumen">?</button>
          </div>
          <button type="button" id="macro-volume-unit-label" onclick="showTermHelp('units', event)" class="text-[10px] text-teal-400 font-mono hover:underline cursor-pointer" title="Erklärung zu Einheiten">Volumen</button>
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
            <label class="flex items-center gap-1 text-[10px] uppercase tracking-wider text-slate-400 mb-1 font-semibold">
              <span>Ruhepuls (BPM)</span>
              <button type="button" onclick="showTermHelp('resting_hr', event)" class="w-3 h-3 rounded-full bg-slate-800 hover:bg-teal-500 text-slate-400 hover:text-white flex items-center justify-center text-[8px] font-bold" title="Erklärung zu Ruhepuls & BPM">?</button>
            </label>
            <input id="input-resting-hr" type="number" placeholder="z.B. 48" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-slate-200 font-mono text-xs focus:border-teal-500 outline-none">
          </div>
          <div>
            <label class="flex items-center gap-1 text-[10px] uppercase tracking-wider text-slate-400 mb-1 font-semibold">
              <span>Max-Puls (BPM)</span>
              <button type="button" onclick="showTermHelp('max_hr', event)" class="w-3 h-3 rounded-full bg-slate-800 hover:bg-teal-500 text-slate-400 hover:text-white flex items-center justify-center text-[8px] font-bold" title="Erklärung zu Max-Puls & Herzfrequenzzonen">?</button>
            </label>
            <input id="input-max-hr" type="number" placeholder="z.B. 185" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-slate-200 font-mono text-xs focus:border-teal-500 outline-none">
          </div>
          <div>
            <label class="block text-[10px] uppercase tracking-wider text-slate-400 mb-1 font-semibold">Gewicht (kg)</label>
            <input id="input-weight" type="number" step="0.5" placeholder="z.B. 74.5" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-slate-200 font-mono text-xs focus:border-teal-500 outline-none">
          </div>
          <div>
            <label class="flex items-center gap-1 text-[10px] uppercase tracking-wider text-slate-400 mb-1 font-semibold">
              <span>Rad FTP (Watt)</span>
              <button type="button" onclick="showTermHelp('ftp', event)" class="w-3 h-3 rounded-full bg-slate-800 hover:bg-teal-500 text-slate-400 hover:text-white flex items-center justify-center text-[8px] font-bold" title="Erklärung zu FTP (Functional Threshold Power)">?</button>
            </label>
            <input id="input-ftp" type="number" placeholder="z.B. 250" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-slate-200 font-mono text-xs focus:border-teal-500 outline-none">
          </div>
        </div>

        <div class="pt-2 border-t border-slate-800 flex items-center justify-between gap-3">
          <div class="min-w-0">
            <div class="text-xs font-semibold text-slate-200 flex items-center gap-1.5">
              <span>Female Cycle Tracking</span>
              <button type="button" onclick="showTermHelp('cycle', event)" class="w-3.5 h-3.5 rounded-full bg-slate-800 hover:bg-teal-500 text-slate-400 hover:text-white flex items-center justify-center text-[9px] font-bold" title="Erklärung zum Menstruationszyklus-Tracking">?</button>
            </div>
            <div class="text-[10px] text-slate-400 break-words">Passt Engine-Sensitivität in Lutealphase für RPE & Puls automatisch an</div>
          </div>
          <input id="input-cycle-toggle" type="checkbox" class="w-4 h-4 rounded text-teal-600 bg-slate-900 border-slate-700 focus:ring-teal-500 shrink-0">
        </div>
      </section>

      <!-- Multi-Sport Calculated Training Zones -->
      <section class="glass-card rounded-2xl p-4 sm:p-5 border border-slate-800">
        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2.5 mb-3">
          <div>
            <div class="flex items-center gap-2">
              <h3 id="zones-title" class="font-bold text-sm text-white">Daniels VDOT Trainingszonen</h3>
              <button type="button" onclick="showCurrentSportZoneHelp(event)" class="w-3.5 h-3.5 rounded-full bg-slate-800 hover:bg-teal-500 text-slate-400 hover:text-white flex items-center justify-center text-[9px] font-bold transition shrink-0" title="Erklärung zum sportartspezifischen Zonenmodell">?</button>
            </div>
            <p id="zones-subtitle" class="text-[11px] text-slate-400">Paces nach Jack Daniels Running Formula</p>
          </div>
          <div class="flex items-center gap-2 self-start sm:self-auto">
            <div class="flex items-center gap-1">
              <span id="zones-ref-label" class="text-xs text-slate-400 font-mono">VDOT:</span>
              <button type="button" onclick="showCurrentSportZoneHelp(event)" class="w-3 h-3 rounded-full bg-slate-800 hover:bg-teal-500 text-slate-400 hover:text-white flex items-center justify-center text-[8px] font-bold shrink-0" title="Erklärung zum Referenzwert">?</button>
            </div>
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
    <div class="glass-card bg-slate-900 border border-slate-700 w-full max-w-md rounded-2xl p-6 shadow-2xl relative max-h-[90vh] overflow-y-auto">
      <div class="flex justify-between items-center mb-2">
        <h3 id="status-modal-title" class="text-lg font-bold flex items-center gap-2">
          <span>🩺</span> Wie geht's dir?
        </h3>
        <button onclick="closeStatusModal()" class="text-slate-400 hover:text-white text-sm">✕</button>
      </div>
      <p id="status-modal-subtitle" class="text-xs text-slate-400 mb-4">Wähle deinen aktuellen Gesundheitsstatus für automatische Anpassungen deines Trainingsplans:</p>
      
      <!-- Dynamically populated via renderStatusModalContent() -->
      <div id="status-modal-body"></div>

      <div class="flex justify-end mt-4 pt-2 border-t border-slate-800/80">
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
          <div>
            <label class="block text-[10px] uppercase text-slate-400 font-semibold mb-1">Individueller Planname (optional)</label>
            <input id="guided-plan-name" type="text" placeholder="z.B. Frühjahrs-Marathon Zürich 2027" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-slate-200 text-xs focus:border-teal-500 outline-none">
          </div>
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
              <label id="guided-volume-label" class="flex items-center gap-1 text-[10px] uppercase text-slate-400 font-semibold mb-1 truncate">
                <span>Basis (km/W)</span>
                <button type="button" onclick="showTermHelp('units', event)" class="w-3 h-3 rounded-full bg-slate-800 hover:bg-teal-500 text-slate-400 hover:text-white flex items-center justify-center text-[8px] font-bold" title="Erklärung zu Volumen & Einheiten">?</button>
              </label>
              <input id="guided-volume" type="number" value="30" min="1" max="15000" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2 text-slate-200 font-mono text-xs focus:border-teal-500 outline-none">
            </div>
            <div>
              <label id="guided-vdot-label" class="flex items-center gap-1 text-[10px] uppercase text-slate-400 font-semibold mb-1 truncate">
                <span>VDOT / Fitness</span>
                <button type="button" onclick="showCurrentSportOnboardHelp(event)" class="w-3 h-3 rounded-full bg-slate-800 hover:bg-teal-500 text-slate-400 hover:text-white flex items-center justify-center text-[8px] font-bold" title="Erklärung zu VDOT / Fitness-Index">?</button>
              </label>
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
          <div class="font-bold text-teal-300 text-sm mb-3 flex items-center gap-2">
            <span>📋</span> Zusammenfassung deines Plans:
          </div>
          <div class="mb-3">
            <label class="block text-[10px] uppercase text-slate-400 font-semibold mb-1">Planname (frei anpassbar):</label>
            <input id="sum-plan-name" type="text" class="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-white font-semibold text-xs focus:border-teal-400 outline-none">
          </div>
          <div class="grid grid-cols-2 gap-2 text-slate-300 font-mono mb-3 bg-slate-900/50 p-2.5 rounded-lg border border-slate-800">
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

  <!-- Screen: Plan-Eckdaten bearbeiten Modal -->
  <div id="plan-edit-modal" class="fixed inset-0 bg-black/85 backdrop-blur-sm z-50 flex items-center justify-center p-3 sm:p-4 hidden" onclick="onPlanEditBackdropClick(event)">
    <div class="glass-card bg-slate-900 border border-slate-700 w-full max-w-lg rounded-2xl p-4 sm:p-6 shadow-2xl relative max-h-[90vh] overflow-y-auto" onclick="event.stopPropagation()">
      <div class="flex justify-between items-center mb-2 pb-2 border-b border-slate-800">
        <h3 class="text-base sm:text-lg font-bold text-white flex items-center gap-2">
          <span>⚙️</span> Plan-Eckdaten anpassen
        </h3>
        <button onclick="closePlanEditModal()" class="text-slate-400 hover:text-white text-base p-1 rounded-lg hover:bg-slate-800 transition">✕</button>
      </div>
      <p class="text-xs text-slate-400 mb-4">
        Passe die Kernparameter dieses Plans nachträglich an. Zukünftige geplante Einheiten und Zonen werden automatisch neu berechnet. Bisherige absolvierte Einheiten bleiben erhalten.
      </p>

      <form onsubmit="submitPlanEdit(event)" class="flex flex-col gap-4 text-xs">
        <input type="hidden" id="edit-plan-id" value="">
        <input type="hidden" id="edit-plan-sport" value="running">

        <!-- Plan Name -->
        <div>
          <label class="block text-[10px] uppercase text-slate-400 font-semibold mb-1">Name des Plans</label>
          <input id="edit-plan-name" type="text" required class="w-full bg-slate-950 border border-slate-700 rounded-xl px-3 py-2.5 text-white font-semibold text-xs focus:border-teal-400 outline-none">
        </div>

        <!-- Sport & Goal (Read-only context badge) -->
        <div class="p-2.5 rounded-xl bg-slate-950/60 border border-slate-800 flex items-center justify-between text-slate-300">
          <div class="flex items-center gap-2">
            <span id="edit-sport-icon" class="text-lg">🏃</span>
            <div>
              <span id="edit-sport-goal-label" class="font-bold text-white uppercase text-[11px]">MARATHON (RUNNING)</span>
              <div class="text-[10px] text-slate-500">Startdatum: <span id="edit-plan-start-date" class="font-mono">--</span></div>
            </div>
          </div>
          <span class="px-2 py-0.5 rounded bg-teal-500/20 text-teal-300 text-[10px] font-mono font-semibold">Struktur adaptiv</span>
        </div>

        <!-- Target Date & Weeks Count Grid -->
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label class="block text-[10px] uppercase text-slate-400 font-semibold mb-1">Zieldatum / Wettkampftag</label>
            <input id="edit-plan-target-date" type="date" required onchange="onEditTargetDateChange(this.value)" class="w-full bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-slate-200 font-mono text-xs focus:border-teal-400 outline-none">
          </div>
          <div>
            <label class="block text-[10px] uppercase text-slate-400 font-semibold mb-1">Dauer ab Start (Wochen)</label>
            <input id="edit-plan-weeks-count" type="number" min="4" max="52" onchange="onEditWeeksChange(this.value)" class="w-full bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-slate-200 font-mono text-xs focus:border-teal-400 outline-none">
          </div>
        </div>

        <!-- Baseline Volume & Reference Value Grid -->
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label id="edit-volume-label" class="flex items-center gap-1 text-[10px] uppercase text-slate-400 font-semibold mb-1">
              <span>Basisvolumen (km/W)</span>
              <button type="button" onclick="showTermHelp('units', event)" class="w-3.5 h-3.5 rounded-full bg-slate-800 hover:bg-teal-500 text-slate-400 hover:text-white flex items-center justify-center text-[8px] font-bold" title="Erklärung zu Volumen & Einheiten">?</button>
            </label>
            <input id="edit-plan-base-volume" type="number" step="0.1" min="1" max="20000" required class="w-full bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-slate-200 font-mono text-xs focus:border-teal-400 outline-none">
          </div>
          <div>
            <label id="edit-ref-label" class="flex items-center gap-1 text-[10px] uppercase text-slate-400 font-semibold mb-1">
              <span>Fitnesswert (VDOT)</span>
              <button type="button" id="edit-ref-help-btn" onclick="showTermHelp('vdot', event)" class="w-3.5 h-3.5 rounded-full bg-slate-800 hover:bg-teal-500 text-slate-400 hover:text-white flex items-center justify-center text-[8px] font-bold" title="Erklärung zum Fitnesswert">?</button>
            </label>
            <input id="edit-plan-reference-value" type="number" step="0.5" min="1" max="1000" required class="w-full bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-slate-200 font-mono text-xs focus:border-teal-400 outline-none">
          </div>
        </div>

        <!-- Info Notice -->
        <div class="p-2.5 rounded-xl bg-slate-950/70 border border-slate-800 text-[11px] text-slate-400 flex items-start gap-2">
          <span class="text-teal-400 text-sm shrink-0">💡</span>
          <span>Beim Speichern werden anstehende geplante Einheiten und Zonen nach den neuen Eckdaten regeneriert.</span>
        </div>

        <!-- Actions -->
        <div class="flex items-center justify-end gap-2 pt-2 border-t border-slate-800">
          <button type="button" onclick="closePlanEditModal()" class="px-3.5 py-2 rounded-xl text-slate-400 hover:text-white text-xs transition">
            Abbrechen
          </button>
          <button type="submit" id="btn-save-plan-edit" class="px-4 py-2 rounded-xl bg-teal-600 hover:bg-teal-500 text-white font-semibold text-xs shadow-lg transition flex items-center gap-1.5">
            <span>💾</span> Eckdaten speichern & Plan anpassen
          </button>
        </div>
      </form>
    </div>
  </div>

  <!-- Screen: Interactive Glossar & Abkürzungshilfe Modal -->
  <div id="glossary-modal" class="fixed inset-0 bg-black/85 backdrop-blur-sm z-50 flex items-center justify-center p-3 sm:p-4 hidden" onclick="onGlossaryBackdropClick(event)">
    <div class="glass-card bg-slate-900 border border-slate-700 w-full max-w-xl rounded-2xl p-4 sm:p-6 shadow-2xl flex flex-col max-h-[90vh] overflow-hidden" onclick="event.stopPropagation()">
      <!-- Header -->
      <div class="flex justify-between items-start mb-3 pb-3 border-b border-slate-800">
        <div>
          <h3 class="text-base sm:text-lg font-bold text-white flex items-center gap-2">
            <span>📖</span> Abkürzungen & Sportwissenschaft
          </h3>
          <p class="text-xs text-slate-400 mt-0.5">Kurzerklärungen aller Metriken, Zonen & Trainingsbegriffe</p>
        </div>
        <button onclick="closeGlossaryModal()" class="text-slate-400 hover:text-white p-1 rounded-lg hover:bg-slate-800 transition text-base">✕</button>
      </div>

      <!-- Focused Term Highlight Banner (When opened for a specific term) -->
      <div id="glossary-highlight-banner" class="hidden mb-3 p-2.5 rounded-xl bg-teal-950/50 border border-teal-500/40 text-xs text-teal-200 flex items-center justify-between gap-2">
        <div class="flex items-center gap-2 min-w-0">
          <span class="text-teal-400 font-bold text-base shrink-0">💡</span>
          <span class="truncate">Fokussierte Erklärung: <strong id="glossary-highlight-term" class="text-white font-mono">--</strong></span>
        </div>
        <button onclick="showAllGlossaryTerms()" class="text-[11px] text-teal-400 hover:text-teal-200 underline shrink-0 font-medium">Alle anzeigen</button>
      </div>

      <!-- Search Bar -->
      <div class="relative mb-3">
        <input id="glossary-search" type="text" oninput="filterGlossary(this.value)" placeholder="Suche Abkürzung oder Begriff (z.B. CTL, VDOT, FTP, RPE, TSB...)"
               class="w-full bg-slate-950 border border-slate-700 rounded-xl py-2 pl-9 pr-8 text-xs sm:text-sm text-slate-200 focus:outline-none focus:border-teal-500 placeholder-slate-500 transition">
        <svg class="w-4 h-4 text-slate-500 absolute left-3 top-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>
        <button id="glossary-clear-btn" onclick="clearGlossarySearch()" class="hidden absolute right-2.5 top-2 text-slate-400 hover:text-white text-xs">✕</button>
      </div>

      <!-- Category Filter Chips -->
      <div class="flex items-center gap-1.5 overflow-x-auto pb-2 mb-2 hide-scrollbar shrink-0 text-[11px]">
        <button onclick="setGlossaryCategory('all')" id="cat-btn-all" class="px-2.5 py-1 rounded-lg bg-teal-500/20 text-teal-300 font-semibold border border-teal-500/40 shrink-0">Alle</button>
        <button onclick="setGlossaryCategory('pmc')" id="cat-btn-pmc" class="px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 shrink-0">📈 Belastung (PMC)</button>
        <button onclick="setGlossaryCategory('running')" id="cat-btn-running" class="px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 shrink-0">🏃 Laufen (VDOT)</button>
        <button onclick="setGlossaryCategory('cycling')" id="cat-btn-cycling" class="px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 shrink-0">🚴 Rad & Schwimmen</button>
        <button onclick="setGlossaryCategory('strength')" id="cat-btn-strength" class="px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 shrink-0">🏋️ Kraft & RPE</button>
        <button onclick="setGlossaryCategory('periodization')" id="cat-btn-periodization" class="px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 shrink-0">📅 Phasen & Makro</button>
        <button onclick="setGlossaryCategory('vitals')" id="cat-btn-vitals" class="px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 shrink-0">💓 Puls & Vital</button>
      </div>

      <!-- Scrollable List of Terms -->
      <div id="glossary-list" class="flex flex-col gap-2.5 overflow-y-auto pr-1 flex-1">
        <!-- Dynamically rendered by JS -->
      </div>

      <!-- Footer Close Button & Counter -->
      <div class="mt-3 pt-3 border-t border-slate-800 flex justify-between items-center text-xs text-slate-400">
        <span id="glossary-count-label">-- Begriffe</span>
        <button onclick="closeGlossaryModal()" class="px-4 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 font-medium transition">
          Schließen
        </button>
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
    const GLOSSARY = {
      ctl: {
        term: "CTL",
        fullName: "Chronic Training Load (Fitness)",
        category: "pmc",
        categoryLabel: "Belastung (PMC)",
        short: "Deine langfristige Fitnessbasis über die letzten ca. 42 Tage.",
        detail: "CTL ist der exponentiell gewichtete Durchschnitt deiner täglichen Belastung (TSS) der vergangenen 6 Wochen (~42 Tage). Er repräsentiert deine aerobe Kapazität und physiologische Belastbarkeit. Je höher der Wert, desto mehr Trainingsvolumen verkraftet dein Körper.",
        guide: "Empfohlener Anstieg (Ramp Rate): ca. +3 bis +7 Punkte pro Woche. Ein zu schneller Anstieg (> +8/Woche) führt häufig zu Übertraining oder Sehnenproblemen."
      },
      atl: {
        term: "ATL",
        fullName: "Acute Training Load (Fatigue / Ermüdung)",
        category: "pmc",
        categoryLabel: "Belastung (PMC)",
        short: "Deine kurzfristige Ermüdung über die letzten ca. 7 Tage.",
        detail: "ATL ist der exponentiell gewichtete Durchschnitt deiner Trainingsbelastung der letzten 7 Tage. Harte Intervalle oder lange Läufe lassen die ATL sprunghaft ansteigen.",
        guide: "Hohe ATL = schwere Beine. Nach Ruhetagen oder Entlastungswochen sinkt die ATL rasch wieder ab, während die Fitness (CTL) langsamer abgebaut wird."
      },
      tsb: {
        term: "TSB",
        fullName: "Training Stress Balance (Form / Frische)",
        category: "pmc",
        categoryLabel: "Belastung (PMC)",
        short: "Formwert: TSB = CTL - ATL. Zeigt Frische vs. Erschöpfung.",
        detail: "Die mathematische Differenz zwischen deiner langfristigen Fitness und der kurzfristigen Ermüdung. Zeigt direkt an, wie 'frisch' du bist.",
        guide: "• Wettkampf-Form: +5 bis +20 (ausgeruht, spritzig & schnell)<br>• Aufbau-Training: -10 bis -25 (produktiver Trainingsreiz)<br>• Überlastung / Vorsicht: Unter -30 (hohes Verletzungsrisiko, Ruhetag einlegen!)"
      },
      pmc: {
        term: "PMC",
        fullName: "Performance Management Chart",
        category: "pmc",
        categoryLabel: "Belastung (PMC)",
        short: "Wissenschaftliches Steuerungsmodell für Training & Form.",
        detail: "Kombiniert CTL (Fitness), ATL (Ermüdung) und TSB (Form) in einer fortlaufenden Zeitreihe. Entwickelt von Dr. Andrew Coggan, um Trainingspläne exakt auf einen Wettkampftag hin zuzuspitzen (Tapering) und Überlastung frühzeitig zu erkennen.",
        guide: "Ziel: Bis 2 Wochen vor dem Event CTL maximieren, dann durch Reduktion des Volumens ATL senken, sodass TSB am Wettkampftag im positiven Bereich (+5 bis +15) landet."
      },
      tss: {
        term: "TSS",
        fullName: "Training Stress Score",
        category: "pmc",
        categoryLabel: "Belastung (PMC)",
        short: "Punktwert für die physiologische Gesamtschwere einer Einheit.",
        detail: "Kombiniert Trainingsdauer und Intensität in eine einzige Kennzahl. Als Referenz gilt: Genau 1 Stunde All-Out an der anaeroben Schwelle (bzw. FTP) entspricht 100 TSS.",
        guide: "• 30–50 TSS: Leichte Regeneration<br>• 50–100 TSS: Solides Training, am Folgetag meist erholt<br>• 100–180 TSS: Sehr fordernde Einheit, Ermüdung spürbar<br>• 180+ TSS: Extrem belastend, erfordert 2 Tage Regeneration"
      },
      vdot: {
        term: "VDOT",
        fullName: "Jack Daniels VDOT (Lauf-Formwert)",
        category: "running",
        categoryLabel: "Laufen (VDOT)",
        short: "Fitness-Index für Läufer zur Berechnung aller Trainings-Paces.",
        detail: "Vom renommierten Sportwissenschaftler Dr. Jack Daniels ('Running Formula') entwickelt. Berechnet aus einer aktuellen Wettkampfzeit (z.B. 5k, 10k oder Halbmarathon) deine relative Sauerstoffaufnahme und leitet daraus deine optimalen Trainingsgeschwindigkeiten ab.",
        guide: "Verhindert 'Junk Miles': Du trainierst jede Einheit (Easy, Marathon, Schwelle, Intervall) im physiologisch exakten Tempobereich."
      },
      epace: {
        term: "E-Pace",
        fullName: "Easy Pace (Grundlagenausdauer GA1)",
        category: "running",
        categoryLabel: "Laufen (VDOT)",
        short: "Lockeres Grundlagen- und Regenerationstempo (ca. 65–79% HFmax).",
        detail: "Macht 75–80% deines gesamten Laufvolumens aus. Trainiert die Kapillarisierung im Muskelgewebe, vermehrt Mitochondrien (die 'Kraftwerke' der Zellen) und trainiert die Fettverbrennung.",
        guide: "Goldene Regel: 'Talk Test' – du musst dich während des Laufens locker in vollständigen Sätzen unterhalten können. Wenn du nach Luft schnappst, bist du zu schnell!"
      },
      mpace: {
        term: "M-Pace",
        fullName: "Marathon Pace (Wettkampftempo)",
        category: "running",
        categoryLabel: "Laufen (VDOT)",
        short: "Geplantes Marathon-Renntempo (ca. 80–87% HFmax).",
        detail: "Etwas zügiger als Easy Pace. Dient der physiologischen und mentalen Gewöhnung an das spezifische Marathon-Renntempo und testet den Glykogenverbrauch unter Rennbedingungen.",
        guide: "Wird meistens in Blöcken in den langen Sonntags-Dauerlauf (Long Run) integriert."
      },
      tpace: {
        term: "T-Pace",
        fullName: "Threshold Pace (Laktatschwelle GA2)",
        category: "running",
        categoryLabel: "Laufen (VDOT)",
        short: "Schwellentempo (ca. 88–92% HFmax). 'Comfortably Hard'.",
        detail: "Die anaerobe Laktatschwelle – die maximale Intensität, bei der Laktataufbau und Laktatabbau gerade noch im Gleichgewicht stehen. Tempo, das man ca. 50–60 Minuten im Rennen halten könnte.",
        guide: "Schult den Körper darin, Laktat effizient abzutransportieren und zu recyceln. Typisch: 20-minütige Tempoläufe oder Cruise-Intervalle (z.B. 4x 2 km mit 1 min Trabpause)."
      },
      ipace: {
        term: "I-Pace",
        fullName: "Interval Pace (VO2max-Tempo)",
        category: "running",
        categoryLabel: "Laufen (VDOT)",
        short: "Harte Intervalle zur Steigerung der max. Sauerstoffaufnahme (95–100% HFmax).",
        detail: "Tempo für 3- bis 5-minütige Belastungen (z.B. 800m bis 1200m). Da das Herz-Kreislauf-System ca. 1,5 bis 2 Minuten braucht, um VO2max zu erreichen, sind Intervalle dieser Länge optimal.",
        guide: "Sehr anstrengend (RPE 8–9). Trabpause sollte ca. gleich lang wie die Belastungszeit sein, damit das nächste Intervall im Zielkorridor geschafft wird."
      },
      rpace: {
        term: "R-Pace",
        fullName: "Repetition Pace (Schnelligkeit & Ökonomie)",
        category: "running",
        categoryLabel: "Laufen (VDOT)",
        short: "Sehr schnelle Wiederholungen (200m bis 400m) mit voller Gehpause.",
        detail: "Dient nicht dem Ausdaueraufbau, sondern der Verbesserung von Schrittfrequenz, Laufökonomie und neuromuskulärer Ansteuerung.",
        guide: "Lange Erholungspause zwischen den Läufen! Du solltest vor jedem Repetition-Lauf wieder voll durchatmen können. Saubere Technik steht an oberster Stelle."
      },
      ftp: {
        term: "FTP",
        fullName: "Functional Threshold Power (Rad)",
        category: "cycling",
        categoryLabel: "Rad & Schwimmen",
        short: "Funktionelle Schwellenleistung beim Radfahren in Watt.",
        detail: "Die maximale Durchschnittsleistung in Watt, die du über eine Stunde gleichmäßig auf das Pedal bringen kannst. Ermittelt über einen 20-Minuten-Test (95% des Schnitts) oder Stufentest.",
        guide: "Alle Rad-Zonen (Z1 Erholung bis Z7 Sprint) sowie der Bike-TSS (bTSS) basieren prozentual auf diesem Watt-Wert."
      },
      css: {
        term: "CSS",
        fullName: "Critical Swim Speed (Schwimm-Schwelle)",
        category: "cycling",
        categoryLabel: "Rad & Schwimmen",
        short: "Kritische Schwellen-Pace beim Schwimmen (Pace pro 100m).",
        detail: "Deine anaerobe Schwimmschwelle in Sekunden pro 100 Meter. Sie entspricht der Geschwindigkeit, die du über eine 1500m-Distanz ohne fortschreitende Übersäuerung halten kannst.",
        guide: "Ermittelt aus zwei Time Trials (400m & 200m Vollgas). CSS = (t400 - t200) / 2. Zonen orientieren sich an dieser Baseline."
      },
      onerm: {
        term: "1RM",
        fullName: "One-Repetition Maximum (Maximalkraft)",
        category: "strength",
        categoryLabel: "Kraft & RPE",
        short: "Das Maximalgewicht für genau eine saubere Wiederholung.",
        detail: "Die maximale Last, die du in einer Grundübung (Kniebeuge, Bankdrücken, Kreuzheben, Schulterdrücken) für exakt eine Wiederholung technisch einwandfrei bewegen kannst.",
        guide: "Trainingsgewichte werden oft in % 1RM angegeben (z.B. 70–80% 1RM für Muskelaufbau, 85–90%+ für reine Maximalkraft)."
      },
      rpe: {
        term: "RPE",
        fullName: "Rating of Perceived Exertion (Belastungsskala)",
        category: "strength",
        categoryLabel: "Kraft & RPE",
        short: "Subjektive Belastungsskala von 1 bis 10.",
        detail: "• 1–2: Sehr leicht (Spaziergang, lockeres Dehnen)<br>• 3–4: Leicht bis moderat (Lockeres Redetempo, Easy Pace)<br>• 5–6: Zügig (Marathon-Pace, spürbare Atmung)<br>• 7–8: Schwellenbereich ('angenehm hart', man spricht nur kurze Wortgruppen)<br>• 9: Hartes Intervall (Brennen der Muskulatur, hohe Atemfrequenz)<br>• 10: All-Out Maximalbelastung / Muskelversagen",
        guide: "Nutze die RPE-Angabe im täglichen Check-in, damit OmniTrain deinen Plan automatisch drosselt, wenn Einheiten schwerer fallen als geplant."
      },
      rir: {
        term: "RIR",
        fullName: "Reps in Reserve (Krafttraining)",
        category: "strength",
        categoryLabel: "Kraft & RPE",
        short: "Verbleibende Wiederholungen im Tank vor dem Muskelversagen.",
        detail: "Gibt an, wie viele saubere Wiederholungen du am Ende eines Kraftsatzes noch hättest ausführen können, bevor das Gewicht nicht mehr hochgeht.",
        guide: "• RIR 3: Sehr sauber, viel Reserve (Technik- oder Warm-up-Satz)<br>• RIR 1–2: Der wissenschaftliche Sweet Spot für optimalen Muskelaufbau ohne ZNS-Erschöpfung<br>• RIR 0: Absolutes Muskelversagen"
      },
      dup: {
        term: "DUP",
        fullName: "Daily Undulating Periodization",
        category: "strength",
        categoryLabel: "Kraft & RPE",
        short: "Tägliche wellenförmige Periodisierung im Krafttraining.",
        detail: "Statt wochenlang dasselbe Schema zu trainieren, wechseln Intensität und Wiederholungsbereich von Training zu Training ab (z.B. Hypertrophie 8–12 Wdh, Maximalkraft 3–5 Wdh, Kraftausdauer 15+ Wdh).",
        guide: "Erzeugt unterschiedliche physiologische Anpassungen in derselben Woche und beugt Stagnation (Plateaus) nachweislich vor."
      },
      bpm: {
        term: "BPM",
        fullName: "Beats per Minute (Herzfrequenz / Puls)",
        category: "vitals",
        categoryLabel: "Puls & Vital",
        short: "Herzschläge pro Minute. Grundbaustein für Puls-Trainingszonen.",
        detail: "Zeigt die Arbeitsleistung deines Herzmuskels. Zwei Schlüsselwerte steuern deinen Trainingsplan: Ruhepuls (Erholung) und Maximalpuls (Ausbelastung).",
        guide: "Puls reagiert verzögert auf Tempoänderungen (Cardiac Lag) und wird durch Hitze, Dehydrierung oder Koffein beeinflusst."
      },
      resting_hr: {
        term: "Ruhepuls",
        fullName: "Ruhe-Herzfrequenz (Morgenpuls in BPM)",
        category: "vitals",
        categoryLabel: "Puls & Vital",
        short: "Niedrigste Herzfrequenz im wachen, entspannten Zustand.",
        detail: "Am besten morgens direkt nach dem Aufwachen oder über die Schlafanalyse deiner Sportuhr (Garmin Fenix) gemessen. Sinkt mit zunehmender aerober Fitness durch größeres Schlagvolumen des Herzens.",
        guide: "Liegt dein Ruhepuls an 2 aufeinanderfolgenden Tagen um >5 BPM über deinem normalen Durchschnitt, ist dein Körper gestresst (beginnender Infekt, schlechte Erholung oder Übertraining)."
      },
      max_hr: {
        term: "Max-Puls",
        fullName: "Maximale Herzfrequenz (HRmax in BPM)",
        category: "vitals",
        categoryLabel: "Puls & Vital",
        short: "Höchste Herzfrequenz, die unter maximaler Belastung erreicht wird.",
        detail: "Ein individueller anatomischer Wert (nicht durch Training steigerbar, sinkt mit dem Alter). Dient als 100%-Referenz für prozentuale Pulszonen (Zone 1 bis Zone 5).",
        guide: "Sollte durch einen echten Ausbelastungstest (z.B. 3x Bergan-Sprint All-Out) ermittelt werden, da Standardformeln ('220 minus Alter') oft um bis zu 15 BPM abweichen."
      },
      cycle: {
        term: "Cycle",
        fullName: "Female Cycle Tracking (Zyklus-Adaption)",
        category: "vitals",
        categoryLabel: "Puls & Vital",
        short: "Automatische Anpassung an Follikel- und Lutealphase.",
        detail: "In der Lutealphase (nach dem Eisprung) steigen Körperkerntemperatur und Ruhepuls leicht an, während die Erholungsfähigkeit sinkt. In der Follikelphase ist die Kraft- und Glykogenverwertung oft gesteigert.",
        guide: "OmniTrain berücksichtigt dies bei aktivierter Option und passt Sensitivitätsschwellen für Erschöpfung und RPE an."
      },
      phases: {
        term: "Phasen",
        fullName: "Periodisierungs-Phasen (Base · Build · Peak · Taper)",
        category: "periodization",
        categoryLabel: "Phasen & Makro",
        short: "Die 4 Phasen eines sportwissenschaftlichen Makrozyklus.",
        detail: "• BASE (Grundlage): Hoher Umfang bei geringer Intensität zum Aufbau der aeroben Kapazität und Gelenk-Stabilität.<br>• BUILD (Aufbau): Gezielte Steigerung von Intensität, Tempoläufen und Schwellen-Intervallen.<br>• PEAK (Spitzenform): Wettkampfspezifische Härte bei stabilisiertem Gesamtumfang.<br>• TAPER (Zuspitzung): Reduktion des Volumens um 40–50% bei gleichbleibender Intensität zum Abbau der Ermüdung vor dem Wettkampf.",
        guide: "OmniTrain strukturiert Wochenpläne automatisch in diese Phasen und baut im 3:1 Rhythmus geplante Entlastungswochen ein."
      },
      deload: {
        term: "Deload",
        fullName: "Entlastungswoche / Regenerationswoche",
        category: "periodization",
        categoryLabel: "Phasen & Makro",
        short: "Geplante Reduktion des Wochenvolumens um ca. 25%.",
        detail: "Nach typischerweise 3 fordernden Belastungswochen folgt eine Entlastungswoche mit reduzierter Kilometerzahl. In dieser Woche repariert der Körper Muskelmikroschäden und stärkt Sehnen und Bänder.",
        guide: "Echtes Muskel- und Ausdauerwachstum geschieht in der Erholung (Superkompensation). Ohne Deload-Wochen drohen Stagnation und Überlastungsverletzungen."
      },
      units: {
        term: "Einheiten",
        fullName: "Sportartspezifische Volumen- & Metrik-Einheiten",
        category: "periodization",
        categoryLabel: "Phasen & Makro",
        short: "km/W · min/W · m/W · Sätze/W · TSS/W",
        detail: "• km/W: Kilometer pro Woche (Laufen)<br>• min/W: Netto-Fahrzeit in Minuten pro Woche (Radsport)<br>• m/W: Gesamtdistanz in Metern pro Woche (Schwimmen)<br>• Sätze/W: Anzahl schwerer Arbeitssätze pro Muskelgruppe pro Woche (Krafttraining)<br>• TSS/W: Kumulierte Trainingsbelastungspunkte aller Sportarten (Multi-Sport / Triathlon)",
        guide: "OmniTrain stellt Kennzahlen, Kurven und Berechnungen immer in der passenden Maßeinheit deiner Sportart dar."
      }
    };

    let currentGlossaryCategory = 'all';
    let currentGlossarySearch = '';

    function openGlossaryModal(preselectedKey = null) {
      const modal = document.getElementById('glossary-modal');
      modal.classList.remove('hidden');
      document.body.style.overflow = 'hidden';

      if (preselectedKey && GLOSSARY[preselectedKey]) {
        const item = GLOSSARY[preselectedKey];
        document.getElementById('glossary-highlight-banner').classList.remove('hidden');
        document.getElementById('glossary-highlight-term').innerText = `${item.term} — ${item.fullName}`;
        currentGlossaryCategory = 'all';
        currentGlossarySearch = '';
        document.getElementById('glossary-search').value = '';
        updateCategoryChips();
        renderGlossary(preselectedKey);
        setTimeout(() => {
          const card = document.getElementById(`glossary-card-${preselectedKey}`);
          if (card) {
            card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            card.classList.add('ring-2', 'ring-teal-400', 'bg-teal-950/40');
            setTimeout(() => {
              card.classList.remove('ring-2', 'ring-teal-400', 'bg-teal-950/40');
            }, 2500);
          }
        }, 100);
      } else {
        document.getElementById('glossary-highlight-banner').classList.add('hidden');
        renderGlossary();
      }
    }

    function closeGlossaryModal() {
      document.getElementById('glossary-modal').classList.add('hidden');
      document.body.style.overflow = '';
    }

    function onGlossaryBackdropClick(e) {
      if (e.target.id === 'glossary-modal') {
        closeGlossaryModal();
      }
    }

    function showTermHelp(key, event) {
      if (event) {
        event.stopPropagation();
        event.preventDefault();
      }
      openGlossaryModal(key);
    }

    function showAllGlossaryTerms() {
      document.getElementById('glossary-highlight-banner').classList.add('hidden');
      document.getElementById('glossary-search').value = '';
      currentGlossarySearch = '';
      currentGlossaryCategory = 'all';
      updateCategoryChips();
      renderGlossary();
    }

    function setGlossaryCategory(cat) {
      currentGlossaryCategory = cat;
      updateCategoryChips();
      renderGlossary();
    }

    function updateCategoryChips() {
      const cats = ['all', 'pmc', 'running', 'cycling', 'strength', 'periodization', 'vitals'];
      cats.forEach(c => {
        const btn = document.getElementById(`cat-btn-${c}`);
        if (btn) {
          if (c === currentGlossaryCategory) {
            btn.className = "px-2.5 py-1 rounded-lg bg-teal-500/20 text-teal-300 font-semibold border border-teal-500/40 shrink-0";
          } else {
            btn.className = "px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 shrink-0";
          }
        }
      });
    }

    function filterGlossary(searchQuery) {
      currentGlossarySearch = searchQuery.trim().toLowerCase();
      const clearBtn = document.getElementById('glossary-clear-btn');
      if (clearBtn) {
        if (currentGlossarySearch) clearBtn.classList.remove('hidden');
        else clearBtn.classList.add('hidden');
      }
      renderGlossary();
    }

    function clearGlossarySearch() {
      document.getElementById('glossary-search').value = '';
      currentGlossarySearch = '';
      document.getElementById('glossary-clear-btn').classList.add('hidden');
      renderGlossary();
    }

    function showCurrentSportZoneHelp(event) {
      const curSport = appState && appState.active_plan ? appState.active_plan.sport_type : 'running';
      if (curSport === 'cycling') showTermHelp('ftp', event);
      else if (curSport === 'swimming') showTermHelp('css', event);
      else if (curSport === 'strength') showTermHelp('dup', event);
      else showTermHelp('vdot', event);
    }

    function showCurrentSportOnboardHelp(event) {
      const sport = document.getElementById('guided-sport') ? document.getElementById('guided-sport').value : 'running';
      if (sport === 'cycling') showTermHelp('ftp', event);
      else if (sport === 'swimming') showTermHelp('css', event);
      else if (sport === 'strength') showTermHelp('onerm', event);
      else showTermHelp('vdot', event);
    }

    function renderGlossary(focusedKey = null) {
      const container = document.getElementById('glossary-list');
      if (!container) return;
      container.innerHTML = '';

      let keys = Object.keys(GLOSSARY);

      // Category filter
      if (currentGlossaryCategory !== 'all') {
        keys = keys.filter(k => GLOSSARY[k].category === currentGlossaryCategory);
      }

      // Search query filter
      if (currentGlossarySearch) {
        const q = currentGlossarySearch;
        keys = keys.filter(k => {
          const item = GLOSSARY[k];
          return item.term.toLowerCase().includes(q) ||
                 item.fullName.toLowerCase().includes(q) ||
                 item.short.toLowerCase().includes(q) ||
                 item.detail.toLowerCase().includes(q) ||
                 (item.guide && item.guide.toLowerCase().includes(q));
        });
      }

      // If focusedKey is given and present, put it first
      if (focusedKey && keys.includes(focusedKey)) {
        keys = [focusedKey, ...keys.filter(k => k !== focusedKey)];
      }

      const countLabel = document.getElementById('glossary-count-label');
      if (countLabel) {
        countLabel.innerText = `${keys.length} ${keys.length === 1 ? 'Begriff' : 'Begriffe'} gefunden`;
      }

      if (keys.length === 0) {
        container.innerHTML = `
          <div class="py-8 text-center text-slate-500 text-xs italic">
            Keine passenden Begriffe für "${currentGlossarySearch}" gefunden.<br>
            <button onclick="showAllGlossaryTerms()" class="mt-2 text-teal-400 hover:underline">Alle Begriffe anzeigen</button>
          </div>
        `;
        return;
      }

      keys.forEach(k => {
        const item = GLOSSARY[k];
        const isFocused = (k === focusedKey);
        const card = document.createElement('div');
        card.id = `glossary-card-${k}`;
        card.className = `glass-pill p-3.5 sm:p-4 rounded-xl border ${isFocused ? 'border-teal-500/80 bg-teal-950/30' : 'border-slate-800/90'} hover:border-slate-700 transition flex flex-col gap-2`;

        const formattedDetail = item.detail;
        const formattedGuide = item.guide || '';

        card.innerHTML = `
          <div class="flex items-start justify-between gap-2">
            <div>
              <div class="flex items-center gap-2">
                <span class="font-mono font-bold text-teal-400 text-sm bg-teal-500/10 px-2 py-0.5 rounded border border-teal-500/30">${item.term}</span>
                <span class="font-bold text-white text-xs sm:text-sm">${item.fullName}</span>
              </div>
              <span class="text-[11px] text-teal-300/90 font-medium mt-1 block">${item.short}</span>
            </div>
            <span class="text-[9px] uppercase tracking-wider px-2 py-0.5 rounded bg-slate-800/80 text-slate-400 font-semibold shrink-0">${item.categoryLabel}</span>
          </div>

          <p class="text-xs text-slate-300 leading-relaxed mt-0.5">
            ${formattedDetail}
          </p>

          ${item.guide ? `
            <div class="mt-1 pt-2 border-t border-slate-800/80 bg-slate-950/50 rounded-lg p-2.5 text-[11px] text-slate-300 flex items-start gap-2">
              <span class="text-amber-400 font-bold shrink-0">💡 Tipp / Richtwert:</span>
              <span class="leading-relaxed">${formattedGuide}</span>
            </div>
          ` : ''}
        `;
        container.appendChild(card);
      });
    }

    window.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        closeGlossaryModal();
        closeStatusModal();
        closeOnboardModal();
        closePlanEditModal();
      }
    });

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

    async function renamePlanAction(planId, currentName) {
      const newName = prompt("Neuen Namen für diesen Trainingsplan eingeben:", currentName || "");
      if (!newName || !newName.trim() || newName.trim() === currentName) return;
      try {
        const res = await fetch('/api/plan/rename', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({plan_id: planId, name: newName.trim()})
        });
        const data = await res.json();
        if (data.status === 'ok') {
          await loadState(planId);
        } else {
          alert("Fehler beim Umbenennen: " + (data.detail || "Unbekannter Fehler"));
        }
      } catch (err) {
        console.error(err);
        alert("Fehler beim Umbenennen des Plans.");
      }
    }

    function openCurrentPlanEditModal() {
      if (appState && appState.active_plan) {
        openPlanEditModal(appState.active_plan.id);
      }
    }

    function openPlanEditModal(planId) {
      if (!appState || !appState.all_plans) return;
      const plan = appState.all_plans.find(p => p.id === planId) || appState.active_plan;
      if (!plan) return;

      document.getElementById('edit-plan-id').value = plan.id;
      document.getElementById('edit-plan-sport').value = plan.sport_type;
      document.getElementById('edit-plan-name').value = plan.name || '';
      document.getElementById('edit-plan-target-date').value = plan.target_date || '';
      document.getElementById('edit-plan-start-date').innerText = plan.start_date || '--';
      document.getElementById('edit-plan-base-volume').value = plan.base_weekly_volume || 30;

      // Calculate weeks count
      const startD = new Date(plan.start_date);
      const targetD = new Date(plan.target_date);
      const diffWeeks = Math.max(4, Math.round((targetD - startD) / (7 * 24 * 60 * 60 * 1000)));
      document.getElementById('edit-plan-weeks-count').value = diffWeeks;

      // Sport Icon & Goal label
      let sportIcon = '🏃';
      if (plan.sport_type === 'cycling') sportIcon = '🚴';
      else if (plan.sport_type === 'swimming') sportIcon = '🏊';
      else if (plan.sport_type === 'strength') sportIcon = '🏋️';
      else if (plan.sport_type === 'triathlon' || plan.sport_type === 'multisport') sportIcon = '🏊';
      document.getElementById('edit-sport-icon').innerText = sportIcon;
      document.getElementById('edit-sport-goal-label').innerText = `${(plan.goal_type || '').replace('_', ' ').toUpperCase()} (${plan.sport_type.toUpperCase()})`;

      // Dynamic units & fitness labels
      const volLabel = document.getElementById('edit-volume-label');
      const refLabel = document.getElementById('edit-ref-label');
      const refInput = document.getElementById('edit-plan-reference-value');
      const refHelpBtn = document.getElementById('edit-ref-help-btn');

      const curRef = (appState.active_plan && appState.active_plan.id === plan.id && appState.ref_vdot) ? appState.ref_vdot : 45.0;

      if (plan.sport_type === 'cycling') {
        volLabel.querySelector('span').innerText = "Basisvolumen (TSS/W)";
        refLabel.querySelector('span').innerText = "FTP (Watt)";
        refInput.value = curRef > 100 ? curRef : 220;
        refHelpBtn.setAttribute('onclick', "showTermHelp('ftp', event)");
      } else if (plan.sport_type === 'swimming') {
        volLabel.querySelector('span').innerText = "Basisvolumen (Meter/W)";
        refLabel.querySelector('span').innerText = "CSS (s/100m)";
        refInput.value = curRef > 50 ? curRef : 105;
        refHelpBtn.setAttribute('onclick', "showTermHelp('css', event)");
      } else if (plan.sport_type === 'strength') {
        volLabel.querySelector('span').innerText = "Basisvolumen (Sätze/W)";
        refLabel.querySelector('span').innerText = "1RM Benchmark (kg)";
        refInput.value = curRef || 100;
        refHelpBtn.setAttribute('onclick', "showTermHelp('dup', event)");
      } else if (plan.sport_type === 'triathlon' || plan.sport_type === 'multisport') {
        volLabel.querySelector('span').innerText = "Basisvolumen (TSS/W)";
        refLabel.querySelector('span').innerText = "Lauf VDOT";
        refInput.value = curRef || 46;
        refHelpBtn.setAttribute('onclick', "showTermHelp('vdot', event)");
      } else {
        volLabel.querySelector('span').innerText = "Basisvolumen (km/W)";
        refLabel.querySelector('span').innerText = "Fitnesswert (VDOT)";
        refInput.value = curRef || 45.0;
        refHelpBtn.setAttribute('onclick', "showTermHelp('vdot', event)");
      }

      document.getElementById('plan-edit-modal').classList.remove('hidden');
      document.body.style.overflow = 'hidden';
    }

    function closePlanEditModal() {
      document.getElementById('plan-edit-modal').classList.add('hidden');
      document.body.style.overflow = '';
    }

    function onPlanEditBackdropClick(e) {
      if (e.target.id === 'plan-edit-modal') {
        closePlanEditModal();
      }
    }

    function onEditTargetDateChange(newDateStr) {
      if (!newDateStr) return;
      const startStr = document.getElementById('edit-plan-start-date').innerText;
      if (!startStr || startStr === '--') return;
      const startD = new Date(startStr);
      const targetD = new Date(newDateStr);
      const weeks = Math.max(4, Math.round((targetD - startD) / (7 * 24 * 60 * 60 * 1000)));
      document.getElementById('edit-plan-weeks-count').value = weeks;
    }

    function onEditWeeksChange(newWeeks) {
      const weeks = parseInt(newWeeks, 10);
      if (!weeks || weeks < 4) return;
      const startStr = document.getElementById('edit-plan-start-date').innerText;
      if (!startStr || startStr === '--') return;
      const startD = new Date(startStr);
      startD.setDate(startD.getDate() + (weeks * 7));
      document.getElementById('edit-plan-target-date').value = startD.toISOString().slice(0, 10);
    }

    async function submitPlanEdit(e) {
      e.preventDefault();
      const planId = document.getElementById('edit-plan-id').value;
      const name = document.getElementById('edit-plan-name').value.trim();
      const targetDate = document.getElementById('edit-plan-target-date').value;
      const weeksCount = parseInt(document.getElementById('edit-plan-weeks-count').value, 10);
      const baseVol = parseFloat(document.getElementById('edit-plan-base-volume').value);
      const refVal = parseFloat(document.getElementById('edit-plan-reference-value').value);

      const btn = document.getElementById('btn-save-plan-edit');
      const originalText = btn.innerHTML;
      btn.innerHTML = '<span>⏳</span> Aktualisiere Plan...';
      btn.disabled = true;

      try {
        const res = await fetch('/api/plan/update-parameters', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            plan_id: planId,
            name: name,
            target_date: targetDate,
            weeks_count: weeksCount,
            base_weekly_volume: baseVol,
            reference_value: refVal
          })
        });
        const data = await res.json();
        if (res.ok && data.status === 'ok') {
          closePlanEditModal();
          await loadState(planId);
        } else {
          alert("Fehler beim Aktualisieren: " + (data.detail || "Unbekannter Fehler"));
        }
      } catch (err) {
        console.error(err);
        alert("Fehler beim Aktualisieren der Plan-Eckdaten.");
      } finally {
        btn.innerHTML = originalText;
        btn.disabled = false;
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
      const displayName = plan.name || `${plan.goal_type.replace('_', ' ').toUpperCase()} (${plan.sport_type})`;
      document.getElementById('plan-title').innerText = `${displayName} · Ziel: ${plan.target_date}${isArchived ? ' [ARCHIVIERT]' : ''}`;
      const headerEditBtn = document.getElementById('header-plan-edit-btn');
      if (headerEditBtn) {
        headerEditBtn.classList.remove('hidden');
      }

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
          const pName = p.name ? `${p.name} (${p.sport_type.toUpperCase()})` : `${p.sport_type.toUpperCase()}: ${p.goal_type.replace('_', ' ').toUpperCase()}`;
          opt.innerText = `${pName}${statusTag}`;
          if (p.id === plan.id) opt.selected = true;
          selector.appendChild(opt);
        });
      }

      // Sick Banner & Pain Banner
      const sickBanner = document.getElementById('sick-banner');
      if (sickBanner) {
        if (appState.is_sick_mode) {
          sickBanner.classList.remove('hidden');
        } else {
          sickBanner.classList.add('hidden');
        }
      }

      const painBanner = document.getElementById('pain-banner');
      if (painBanner) {
        if (!appState.is_sick_mode && appState.is_pain_mode) {
          painBanner.classList.remove('hidden');
          const detailEl = document.getElementById('pain-banner-detail');
          if (detailEl && appState.pain_detail) {
            detailEl.innerText = `Workouts werden geschont: ${appState.pain_detail}`;
          }
        } else {
          painBanner.classList.add('hidden');
        }
      }

      // Header Status Button Dynamic Appearance
      const statusBtn = document.getElementById('header-status-btn');
      if (statusBtn) {
        if (appState.is_sick_mode) {
          statusBtn.className = "text-xs font-semibold px-2.5 sm:px-3 py-1.5 rounded-lg bg-rose-500/20 text-rose-300 border border-rose-500/40 hover:bg-rose-500/30 transition flex items-center gap-1.5 shrink-0 animate-pulse";
          statusBtn.innerHTML = `<span>🤒</span> <span class="hidden xs:inline">Krankheitsmodus</span><span class="xs:hidden">Krank</span>`;
        } else if (appState.is_pain_mode) {
          statusBtn.className = "text-xs font-semibold px-2.5 sm:px-3 py-1.5 rounded-lg bg-amber-500/20 text-amber-300 border border-amber-500/40 hover:bg-amber-500/30 transition flex items-center gap-1.5 shrink-0";
          statusBtn.innerHTML = `<span>🩹</span> <span class="hidden xs:inline">Schonmodus</span><span class="xs:hidden">Schonung</span>`;
        } else {
          statusBtn.className = "text-xs font-semibold px-2.5 sm:px-3 py-1.5 rounded-lg bg-slate-800 text-slate-200 border border-slate-700 hover:bg-slate-700 transition flex items-center gap-1.5 shrink-0";
          statusBtn.innerHTML = `<span>🩺</span> <span class="hidden xs:inline">Wie geht's dir?</span><span class="xs:hidden">Status</span>`;
        }
      }

      // LLM Badge
      const llmBadge = document.getElementById('llm-status-badge');
      if (llmBadge) {
        if (appState.llm && appState.llm.available) {
          const mName = (appState.llm.model || '').split('/').pop() || 'LLM';
          llmBadge.className = "text-[10px] font-mono px-2 py-0.5 rounded-full bg-teal-500/10 text-teal-300 border border-teal-500/30 flex items-center gap-1";
          llmBadge.innerHTML = `<span class="w-1.5 h-1.5 rounded-full bg-teal-400 inline-block animate-pulse"></span> ${mName} (LM Studio)`;
          llmBadge.title = `Host: ${appState.llm.base_url} · Modell: ${appState.llm.model}`;
        } else {
          llmBadge.className = "text-[10px] font-mono px-2 py-0.5 rounded-full bg-slate-800 text-slate-400 border border-slate-700";
          llmBadge.innerText = "Offline (Rule-based Fallback)";
          llmBadge.title = "Kein LLM erreichbar - deterministischer Keyword-Parser aktiv.";
        }
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

      // Chat-Historie aus DB
      renderChatHistory();

      // Mini-Wochenansicht (Dynamisch an aktuelle Kalenderwoche gekoppelt)
      renderWeekDots();

      // Screen 3: Wochenplan Carousel
      renderWochenplanCarousel();

      // Screen 4: Makrozyklus & Timeline
      renderMakrozyklus();

      // Screen 5: Profil & Zonen
      renderProfileAndZones();
    }

    function renderChatHistory() {
      const chat = document.getElementById('chat-messages');
      if (!chat) return;

      const defaultWelcome = `
        <div class="bg-slate-800/80 rounded-2xl rounded-tl-sm p-3 max-w-[90%] sm:max-w-[88%] text-slate-300 break-words">
          Wie war dein Training heute? Erzähl mir frei von Distanz, Gefühl (<span class="text-teal-300 cursor-pointer underline decoration-dotted" onclick="showTermHelp('rpe', event)" title="Rating of Perceived Exertion (1=sehr leicht, 10=maximal)">RPE 1-10 ℹ</span>) oder etwaigen Schmerzen / Symptomen.
        </div>
      `;

      if (!appState.recent_checkins || appState.recent_checkins.length === 0) {
        chat.innerHTML = defaultWelcome;
        return;
      }

      let html = defaultWelcome;
      appState.recent_checkins.forEach(c => {
        const userText = c.raw_input || 'Check-in eingereicht';
        const dateStr = c.timestamp ? c.timestamp.slice(0, 16).replace('T', ' ') : '';
        html += `
          <div class="bg-teal-700/80 text-white rounded-2xl rounded-tr-sm p-2.5 max-w-[85%] self-end text-xs break-words shadow">
            <div class="font-medium">${userText}</div>
            <div class="text-[9px] text-teal-200/70 text-right mt-1 font-mono">${dateStr}</div>
          </div>
        `;

        const statusUpper = (c.completion_status || 'completed').toUpperCase();
        const rpeInfo = c.perceived_rpe ? ` · RPE ${c.perceived_rpe}/10` : '';

        html += `
          <div class="bg-slate-800 rounded-2xl rounded-tl-sm p-3 max-w-[92%] text-slate-200 text-xs border border-slate-700">
            <div>✅ Workout verarbeitet: <strong>${statusUpper}</strong>${rpeInfo}</div>
          </div>
        `;
      });

      chat.innerHTML = html;
      chat.scrollTop = chat.scrollHeight;
    }

    function renderPMCChart(series) {
      const ctx = document.getElementById('pmcChart');
      const emptyNotice = document.getElementById('pmc-empty-notice');
      if (!ctx) return;

      if (!series || series.length === 0) {
        if (emptyNotice) emptyNotice.classList.remove('hidden');
        if (pmcChartInstance) {
          pmcChartInstance.destroy();
          pmcChartInstance = null;
        }
        return;
      }

      if (emptyNotice) emptyNotice.classList.add('hidden');

      const labels = series.map(p => p.date.slice(5));
      const ctlData = series.map(p => p.ctl);
      const atlData = series.map(p => p.atl);
      const tsbData = series.map(p => p.tsb);

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
      if (!container) return;
      container.innerHTML = '';
      const days = ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So'];

      const today = new Date();
      const currentDay = today.getDay();
      const distToMonday = (currentDay + 6) % 7;
      const monday = new Date(today);
      monday.setDate(today.getDate() - distToMonday);

      const weekDates = [];
      for (let i = 0; i < 7; i++) {
        const d = new Date(monday);
        d.setDate(monday.getDate() + i);
        weekDates.push(d.toISOString().slice(0, 10));
      }

      const todayStr = today.toISOString().slice(0, 10);

      // Filter workouts of current calendar week
      let currentWeekWorkouts = (appState.workouts || []).filter(w => weekDates.includes(w.date));

      // Fallback if workouts are outside current week: pick active week in plan
      if (currentWeekWorkouts.length === 0 && appState.weeks && appState.weeks.length > 0) {
        const curWeekObj = appState.weeks[activeCarouselWeekIndex] || appState.weeks[0];
        currentWeekWorkouts = (appState.workouts || []).filter(w => w.week_id === curWeekObj.id);
      }

      days.forEach((dayName, idx) => {
        const dateStr = weekDates[idx];
        const isToday = (todayStr === dateStr);

        let wo = currentWeekWorkouts.find(w => w.date === dateStr);
        if (!wo) {
          wo = currentWeekWorkouts.find(w => w.day_of_week === (idx + 1));
        }

        const col = document.createElement('div');
        col.className = `flex flex-col items-center gap-1 p-1.5 sm:p-2 rounded-xl bg-slate-900/60 border ${isToday ? 'border-teal-500/60 ring-1 ring-teal-500/30 bg-teal-950/20' : 'border-slate-800/60'} min-w-0 transition`;

        let dotColor = 'bg-slate-700';
        let statusText = 'Rest';
        if (wo) {
          statusText = formatWorkoutMetric(wo.metric_primary, wo.metric_unit);
          if (wo.status === 'completed') dotColor = 'bg-emerald-400';
          else if (wo.workout_type === 'rest') dotColor = 'bg-rose-500';
          else dotColor = 'bg-teal-400 ring-2 ring-teal-500/20';
        }

        col.innerHTML = `
          <div class="flex items-center gap-0.5">
            <span class="text-[10px] sm:text-[11px] font-semibold ${isToday ? 'text-teal-300 font-bold' : 'text-slate-400'}">${dayName}</span>
            ${isToday ? '<span class="w-1 h-1 rounded-full bg-teal-400"></span>' : ''}
          </div>
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

        // Zone Help Mapping
        let zoneHelpKey = null;
        const lowerK = k.toLowerCase();
        const zoneName = z.name || k;
        const lowerName = zoneName.toLowerCase();

        if (lowerK.includes('easy') || lowerK === 'e' || lowerName.includes('easy')) zoneHelpKey = 'epace';
        else if (lowerK.includes('marathon') || lowerK === 'm' || lowerName.includes('marathon')) zoneHelpKey = 'mpace';
        else if (lowerK.includes('threshold') || lowerK === 't' || lowerName.includes('schwelle') || lowerName.includes('threshold')) zoneHelpKey = 'tpace';
        else if (lowerK.includes('interval') || lowerK === 'i' || lowerName.includes('interval')) zoneHelpKey = 'ipace';
        else if (lowerK.includes('repetition') || lowerK === 'r' || lowerName.includes('repetition')) zoneHelpKey = 'rpace';
        else if (curSport === 'cycling') zoneHelpKey = 'ftp';
        else if (curSport === 'swimming') zoneHelpKey = 'css';
        else if (curSport === 'strength') zoneHelpKey = 'dup';

        const helpBtn = zoneHelpKey ? ` <button type="button" onclick="showTermHelp('${zoneHelpKey}', event)" class="inline-flex w-3.5 h-3.5 rounded-full bg-slate-800 hover:bg-teal-500 text-slate-400 hover:text-white items-center justify-center text-[8px] font-bold transition align-middle ml-1 shrink-0" title="Erklärung zu dieser Zone">?</button>` : '';

        tr.innerHTML = `
          <td class="py-2.5 font-bold text-teal-400 whitespace-nowrap">${zoneName}${helpBtn}</td>
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
          const goalTitle = (p.goal_type || '').replace('_', ' ').toUpperCase();
          const displayName = p.name || `${goalTitle} (${p.sport_type})`;

          const item = document.createElement('div');
          item.className = `p-3.5 rounded-xl border ${cardBorder} flex flex-col sm:flex-row sm:items-center justify-between gap-3 text-xs transition`;
          item.innerHTML = `
            <div class="flex items-start sm:items-center gap-3 min-w-0">
              <span class="text-2xl shrink-0 p-1.5 rounded-lg bg-slate-900 border border-slate-800">${sportIcon}</span>
              <div class="min-w-0">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="font-bold text-white text-sm truncate">${displayName}</span>
                  <button onclick="renamePlanAction('${p.id}', '${(p.name || '').replace(/'/g, "\\'")}')" title="Namen dieses Plans bearbeiten" class="text-slate-400 hover:text-teal-400 p-0.5 transition">
                    ✏️
                  </button>
                  ${isSelected ? '<span class="px-2 py-0.5 rounded bg-teal-500/20 text-teal-300 font-bold text-[10px] shrink-0">AKTIV AUSGEWÄHLT</span>' : ''}
                  ${isArchived ? '<span class="px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 font-semibold text-[10px] shrink-0">📦 ARCHIVIERT</span>' : ''}
                </div>
                <div class="text-[11px] text-slate-400 mt-0.5">
                  <span class="text-slate-300 font-semibold uppercase">${goalTitle} (${p.sport_type})</span> · Zieltag: <strong class="text-slate-300 font-mono">${p.target_date}</strong> · Basis ${formatSportVolume(p.base_weekly_volume, p.sport_type, true)}
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

                <button onclick="openPlanEditModal('${p.id}')" title="Plan-Eckdaten bearbeiten (Zieldatum, Basisvolumen, VDOT...)" class="p-1.5 px-2 rounded-lg bg-teal-500/10 hover:bg-teal-500/20 text-teal-300 border border-teal-500/30 transition text-[11px] flex items-center gap-1">
                  <span>⚙️</span> <span class="hidden xs:inline">Eckdaten</span>
                </button>

                <button onclick="renamePlanAction('${p.id}', '${(p.name || '').replace(/'/g, "\\'")}')" title="Plan umbenennen" class="p-1.5 px-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 transition text-[11px] flex items-center gap-1">
                  <span>✏️</span> <span class="hidden xs:inline">Umbenennen</span>
                </button>

                ${!isArchived ? `
                  <button onclick="archivePlanAction('${p.id}')" title="Plan archivieren (pausieren)" class="p-1.5 px-2 rounded-lg bg-amber-500/10 hover:bg-amber-500/20 text-amber-300 border border-amber-500/30 transition text-[11px] flex items-center gap-1">
                    <span>📦</span> <span class="hidden xs:inline">Archivieren</span>
                  </button>
                ` : `
                  <button onclick="unarchivePlanAction('${p.id}')" title="Plan reaktivieren" class="p-1.5 px-2 rounded-lg bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 transition text-[11px] flex items-center gap-1">
                    <span>🌱</span> <span class="hidden xs:inline">Reaktivieren</span>
                  </button>
                `}

                <button onclick="deletePlanAction('${p.id}', '${displayName.replace(/'/g, "\\'")}')" title="Plan endgültig löschen" class="p-1.5 px-2 rounded-lg bg-rose-500/10 hover:bg-rose-500/20 text-rose-300 border border-rose-500/30 transition text-[11px] flex items-center gap-1">
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

    // Modal controls: "Wie geht's dir?" (Dynamic State-Aware Modal)
    let statusSubView = 'main'; // 'main', 'pain_form', 'fatigue_form'

    function openStatusModal() {
      statusSubView = 'main';
      renderStatusModalContent();
      document.getElementById('status-modal').classList.remove('hidden');
    }

    function closeStatusModal() {
      document.getElementById('status-modal').classList.add('hidden');
    }

    function renderStatusModalContent() {
      const body = document.getElementById('status-modal-body');
      const title = document.getElementById('status-modal-title');
      const subtitle = document.getElementById('status-modal-subtitle');
      if (!body) return;

      const isSick = !!appState.is_sick_mode;
      const isPain = !!appState.is_pain_mode;

      if (isSick) {
        title.innerHTML = '<span>🤒</span> Krankheitsmodus aktiv';
        subtitle.innerText = 'Du bist aktuell krank gemeldet. Alle anstehenden Workouts sind als Ruhetage pausiert.';
        body.innerHTML = `
          <div class="flex flex-col gap-2.5">
            <button onclick="submitStatusUpdate('recovery', 'Wieder gesund und symptomfrei!')" class="p-3.5 rounded-xl bg-slate-950/80 border border-emerald-500/40 hover:border-emerald-400 hover:bg-emerald-950/30 text-left transition flex items-start gap-3 group">
              <span class="text-2xl">🌱</span>
              <div>
                <div class="text-sm font-semibold text-emerald-400 group-hover:text-emerald-300">Wieder gesund (Recovery)</div>
                <div class="text-xs text-slate-300 mt-0.5">Startet sanften Wiederaufbau: 1. Training 50% Volumen, 4 Tage kein Tempotraining.</div>
              </div>
            </button>

            <button onclick="closeStatusModal()" class="p-3 rounded-xl bg-slate-950/50 border border-slate-700 hover:border-slate-500 text-left transition flex items-start gap-3 group">
              <span class="text-xl">⏸️</span>
              <div>
                <div class="text-sm font-semibold text-slate-300 group-hover:text-white">Weiterhin schonen</div>
                <div class="text-xs text-slate-400 mt-0.5">Behält die Ruhephase bei, bis dein Körper wieder voll belastbar ist.</div>
              </div>
            </button>
          </div>
        `;
        return;
      }

      if (isPain) {
        title.innerHTML = '<span>🩹</span> Schonmodus aktiv';
        const detailText = appState.pain_detail ? ` (${appState.pain_detail})` : '';
        subtitle.innerText = `Aktuelle Einheiten sind wegen Beschwerden geschont oder pausiert${detailText}.`;
        
        if (statusSubView === 'pain_form') {
          renderPainForm(body, title, subtitle, true);
          return;
        }

        body.innerHTML = `
          <div class="flex flex-col gap-2.5">
            <button onclick="submitStatusUpdate('pain_resolved', 'Schmerzen vollständig abgeklungen')" class="p-3.5 rounded-xl bg-slate-950/80 border border-teal-500/40 hover:border-teal-400 hover:bg-teal-950/30 text-left transition flex items-start gap-3 group">
              <span class="text-2xl">✨</span>
              <div>
                <div class="text-sm font-semibold text-teal-400 group-hover:text-teal-300">Schmerzfrei (Pain Resolved)</div>
                <div class="text-xs text-slate-300 mt-0.5">Beendet die Schonphase, reaktiviert geplante Grundlageneinheiten.</div>
              </div>
            </button>

            <button onclick="statusSubView = 'pain_form'; renderStatusModalContent();" class="p-3 rounded-xl bg-slate-950/60 border border-amber-500/30 hover:border-amber-500 hover:bg-amber-950/20 text-left transition flex items-start gap-3 group">
              <span class="text-xl">📊</span>
              <div>
                <div class="text-sm font-semibold text-amber-400 group-hover:text-amber-300">Schmerzlevel anpassen</div>
                <div class="text-xs text-slate-400 mt-0.5">Intensität oder betroffene Körperregion aktualisieren.</div>
              </div>
            </button>

            <button onclick="submitStatusUpdate('sickness', 'Krank gemeldet (Infekt/Fieber)')" class="p-3 rounded-xl bg-slate-950/60 border border-rose-500/30 hover:border-rose-500 hover:bg-rose-950/20 text-left transition flex items-start gap-3 group">
              <span class="text-xl">🤒</span>
              <div>
                <div class="text-sm font-semibold text-rose-400 group-hover:text-rose-300">Zusätzlich krank geworden</div>
                <div class="text-xs text-slate-400 mt-0.5">Aktiviert vollen Krankheitsmodus und pausiert alle Einheiten.</div>
              </div>
            </button>
          </div>
        `;
        return;
      }

      // Normal state (weder krank noch Schmerzmodus)
      if (statusSubView === 'pain_form') {
        renderPainForm(body, title, subtitle, false);
        return;
      }

      if (statusSubView === 'fatigue_form') {
        renderFatigueForm(body, title, subtitle);
        return;
      }

      title.innerHTML = `<span>🩺</span> Wie geht's dir heute?`;
      subtitle.innerText = 'Wähle deinen aktuellen Status für eine bedarfsgerechte Anpassung des Trainingsplans:';
      body.innerHTML = `
        <div class="flex flex-col gap-2.5">
          <button onclick="submitStatusUpdate('fit', 'Topfit und voll belastbar')" class="p-3 rounded-xl bg-slate-950/70 border border-emerald-500/30 hover:border-emerald-500 hover:bg-emerald-950/20 text-left transition flex items-start gap-3 group">
            <span class="text-xl">⚡</span>
            <div>
              <div class="text-sm font-semibold text-emerald-400 group-hover:text-emerald-300">Topfit & Volle Energie</div>
              <div class="text-xs text-slate-400 mt-0.5">Fühle mich ausgeruht und bereit für alle geplanten Trainingsreize.</div>
            </div>
          </button>

          <button onclick="statusSubView = 'fatigue_form'; renderStatusModalContent();" class="p-3 rounded-xl bg-slate-950/70 border border-amber-500/30 hover:border-amber-500 hover:bg-amber-950/20 text-left transition flex items-start gap-3 group">
            <span class="text-xl">💤</span>
            <div>
              <div class="text-sm font-semibold text-amber-400 group-hover:text-amber-300">Erschöpft / Schlechter Schlaf (Fatigue)</div>
              <div class="text-xs text-slate-400 mt-0.5">Schwere Beine, wenig Schlaf oder hoher Stress (Drosselung nächste 24-48h).</div>
            </div>
          </button>

          <button onclick="statusSubView = 'pain_form'; renderStatusModalContent();" class="p-3 rounded-xl bg-slate-950/70 border border-orange-500/30 hover:border-orange-500 hover:bg-orange-950/20 text-left transition flex items-start gap-3 group">
            <span class="text-xl">🩹</span>
            <div>
              <div class="text-sm font-semibold text-orange-400 group-hover:text-orange-300">Schmerzen / Zwicken (Pain)</div>
              <div class="text-xs text-slate-400 mt-0.5">Zwicken oder akuter Schmerz in Knie, Sehne oder Muskel.</div>
            </div>
          </button>

          <button onclick="submitStatusUpdate('sickness', 'Krank gemeldet (Infekt/Fieber)')" class="p-3 rounded-xl bg-slate-950/70 border border-rose-500/30 hover:border-rose-500 hover:bg-rose-950/20 text-left transition flex items-start gap-3 group">
            <span class="text-xl">🤒</span>
            <div>
              <div class="text-sm font-semibold text-rose-400 group-hover:text-rose-300">Krank / Erkältet (Sickness)</div>
              <div class="text-xs text-slate-400 mt-0.5">Aktiviert Krankheitsmodus und wandelt anstehende Workouts in Ruhetage um.</div>
            </div>
          </button>
        </div>
      `;
    }

    function renderFatigueForm(container, title, subtitle) {
      title.innerHTML = '<span>💤</span> Erschöpfung erfassen';
      subtitle.innerText = 'Wie stark ist die heutige Erschöpfung?';
      container.innerHTML = `
        <div class="flex flex-col gap-2.5">
          <button onclick="submitStatusUpdate('readiness', 'Leichte Erschöpfung / müde Beine', 5)" class="p-3 rounded-xl bg-slate-950/70 border border-amber-500/30 hover:border-amber-500 hover:bg-amber-950/20 text-left transition flex items-start gap-3 group">
            <span class="text-lg">🥱</span>
            <div>
              <div class="text-sm font-semibold text-amber-300">Leichte Müdigkeit (5–6 / 10)</div>
              <div class="text-xs text-slate-400 mt-0.5">Drosselt Tempo-Einheiten der nächsten 24h vorsorglich auf Zone Easy.</div>
            </div>
          </button>

          <button onclick="submitStatusUpdate('readiness', 'Akute Erschöpfung / Schlafdefizit', 8)" class="p-3 rounded-xl bg-slate-950/70 border border-orange-500/30 hover:border-orange-500 hover:bg-orange-950/20 text-left transition flex items-start gap-3 group">
            <span class="text-lg">🛌</span>
            <div>
              <div class="text-sm font-semibold text-orange-300">Akute Erschöpfung (7–9 / 10)</div>
              <div class="text-xs text-slate-400 mt-0.5">Drosselt Tempo- und Intervalleinheiten der nächsten 48h auf Zone Easy.</div>
            </div>
          </button>

          <button type="button" onclick="statusSubView = 'main'; renderStatusModalContent();" class="mt-2 text-xs text-slate-400 hover:text-white flex items-center gap-1 self-start">
            ← Zurück zur Auswahl
          </button>
        </div>
      `;
    }

    function renderPainForm(container, title, subtitle, isAdjusting = false) {
      title.innerHTML = isAdjusting ? '<span>📊</span> Schmerzlevel anpassen' : '<span>🩹</span> Beschwerden erfassen';
      subtitle.innerText = 'Gib die betroffene Stelle und Schmerzintensität (1–10) an:';
      container.innerHTML = `
        <div class="flex flex-col gap-3.5">
          <div>
            <label class="block text-xs font-semibold text-slate-300 mb-1.5">Betroffene Region / Gelenk</label>
            <select id="pain-location-select" class="w-full bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-xs text-white focus:outline-none focus:border-teal-400">
              <option value="Knie">Knie</option>
              <option value="Achillessehne">Achillessehne</option>
              <option value="Schienbein">Schienbein (Shin Splints)</option>
              <option value="Fußsohle / Plantarfaszie">Fußsohle / Plantarfaszie</option>
              <option value="Wade / Oberschenkel">Wade / Oberschenkel</option>
              <option value="Hüfte / IT-Band">Hüfte / IT-Band</option>
              <option value="Rücken / LWS">Rücken / LWS</option>
              <option value="Schulter / Nacken">Schulter / Nacken</option>
              <option value="Sonstige Beschwerden">Sonstige Beschwerden</option>
            </select>
          </div>

          <div>
            <div class="flex justify-between items-center mb-1.5">
              <label class="text-xs font-semibold text-slate-300">Schmerzintensität (1–10)</label>
              <span id="pain-severity-val" class="font-mono text-xs font-bold text-amber-400">4 / 10 (Strukturell)</span>
            </div>
            <input type="range" id="pain-severity-slider" min="1" max="10" value="4" step="1" oninput="updatePainSeverityLabel(this.value)" class="w-full accent-teal-400">
            <div class="flex justify-between text-[10px] text-slate-500 mt-1">
              <span>1–3: Zwicken (Niggle)</span>
              <span>4–6: Spürbar / Pause</span>
              <span>7–10: Akut</span>
            </div>
          </div>

          <div id="pain-impact-hint" class="p-2.5 rounded-xl bg-slate-950/70 border border-slate-800 text-[11px] text-slate-300">
            ℹ️ <strong>Auswirkung:</strong> 72h Pause der Einheiten & anschließende Schonung.
          </div>

          <div class="flex items-center justify-between pt-1">
            <button type="button" onclick="statusSubView = 'main'; renderStatusModalContent();" class="text-xs text-slate-400 hover:text-white">
              ← Zurück
            </button>
            <button type="button" onclick="submitPainForm()" class="px-4 py-2 rounded-xl bg-orange-600 hover:bg-orange-500 text-white font-semibold text-xs transition shadow-lg">
              Schonung anwenden
            </button>
          </div>
        </div>
      `;
    }

    function updatePainSeverityLabel(val) {
      val = parseInt(val, 10);
      const lbl = document.getElementById('pain-severity-val');
      const hint = document.getElementById('pain-impact-hint');
      if (!lbl) return;

      if (val <= 3) {
        lbl.className = 'font-mono text-xs font-bold text-yellow-400';
        lbl.innerText = `${val} / 10 (Zwicken / Niggle)`;
        if (hint) hint.innerHTML = 'ℹ️ <strong>Auswirkung:</strong> Nächste 48h nur lockere Grundlagenläufe (Zone Easy), keine harten Intervalle.';
      } else if (val <= 6) {
        lbl.className = 'font-mono text-xs font-bold text-orange-400';
        lbl.innerText = `${val} / 10 (Strukturell)`;
        if (hint) hint.innerHTML = 'ℹ️ <strong>Auswirkung:</strong> 72h Pause (Ruhetage) und anschließende Tempo-Schonung.';
      } else {
        lbl.className = 'font-mono text-xs font-bold text-rose-400';
        lbl.innerText = `${val} / 10 (Akut)`;
        if (hint) hint.innerHTML = '⚠️ <strong>Auswirkung:</strong> Sofortige 72h Zwangspause zur Vermeidung von Folgeschäden.';
      }
    }

    function submitPainForm() {
      const loc = document.getElementById('pain-location-select')?.value || 'Gelenk';
      const sev = parseInt(document.getElementById('pain-severity-slider')?.value || '4', 10);
      const note = `Schmerzmeldung: ${loc} (${sev}/10)`;
      submitStatusUpdate('pain_report', note, sev, loc);
    }

    async function submitStatusUpdate(type, note, severity = null, location = null) {
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
            location: location,
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

      const defaultName = `${p.target_event.replace('_', ' ').toUpperCase()} (${p.target_weeks || 16}W)`;
      pendingPlanParams = {
        sport: p.sport_type,
        goal: p.target_event,
        vdot: refFit,
        weeks_count: p.target_weeks || 16,
        baseline_volume: baseVol,
        name: defaultName
      };

      showSummaryView();
    }

    function previewGuidedGoal() {
      const gSport = document.getElementById('guided-sport').value;
      const gGoal = document.getElementById('guided-goal').value;
      const gWeeks = parseInt(document.getElementById('guided-weeks').value) || 16;
      const customName = document.getElementById('guided-plan-name')?.value.trim();
      const defaultName = customName || `${gGoal.replace('_', ' ').toUpperCase()} (${gWeeks}W)`;

      pendingPlanParams = {
        sport: gSport,
        goal: gGoal,
        vdot: parseFloat(document.getElementById('guided-vdot').value) || 45.0,
        weeks_count: gWeeks,
        baseline_volume: parseFloat(document.getElementById('guided-volume').value) || 30.0,
        name: defaultName
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

      const sumNameInput = document.getElementById('sum-plan-name');
      if (sumNameInput) sumNameInput.value = pendingPlanParams.name || '';

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

      const sumNameInput = document.getElementById('sum-plan-name');
      if (sumNameInput && sumNameInput.value.trim()) {
        pendingPlanParams.name = sumNameInput.value.trim();
      }

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
