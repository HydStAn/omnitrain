"""CLI entrypoint and interactive commands for OmniTrain."""
import json
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
import typer
from rich import print
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from omnitrain.core.models import CompletionStatus, SportType, Workout
from omnitrain.core.reconcile import StateMachineReconciler
from omnitrain.load.pmc import DailyLoad, PMCEngine
from omnitrain.parser.checkin import CheckinParser
from omnitrain.sports.daniels import get_daniels_zones
from omnitrain.sports.running import RunningStrategy
from omnitrain.storage.db import Database

app = typer.Typer(help="OmniTrain: Local AI-assisted Adaptive Training Engine")
console = Console()

DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "omnitrain" / "omnitrain.db"


def get_db() -> Database:
    db = Database(DEFAULT_DB_PATH)
    db.migrate()
    return db


def get_or_create_default_user(db: Database) -> str:
    with db.get_connection() as conn:
        row = conn.execute("SELECT id FROM users LIMIT 1;").fetchone()
        if row:
            return row["id"]
        user_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO users (id, display_name, created_at, updated_at)
            VALUES (?, ?, ?, ?);
            """,
            (user_id, "Stephan", now, now)
        )
        conn.commit()
        return user_id


@app.command("status")
def status():
    """Zeigt den aktuellen Trainingszustand, PMC (CTL/ATL/TSB) und die nächsten Workouts."""
    db = get_db()
    with db.get_connection() as conn:
        plan_row = conn.execute(
            "SELECT * FROM plans WHERE status = 'active' ORDER BY created_at DESC LIMIT 1;"
        ).fetchone()

        if not plan_row:
            console.print("[yellow]Kein aktiver Trainingsplan gefunden. Erstelle einen mit: [/yellow][bold green]omnitrain plan new[/bold green]")
            return

        plan_id = plan_row["id"]
        sport_type = plan_row["sport_type"]
        goal_type = plan_row["goal_type"]

        # Calculate PMC
        checkins = conn.execute(
            "SELECT timestamp, actual_metrics_json FROM checkin_logs WHERE actual_metrics_json IS NOT NULL ORDER BY timestamp ASC;"
        ).fetchall()
        loads = []
        for c in checkins:
            metrics = json.loads(c["actual_metrics_json"])
            tss = metrics.get("tss", 0.0) or 40.0
            loads.append(DailyLoad(date=c["timestamp"][:10], tss=tss))

        pmc_points = PMCEngine.calculate_pmc_series(loads)
        latest_pmc = pmc_points[-1] if pmc_points else None

        # Display Header
        title = f"[bold cyan]OmniTrain Plan:[/bold cyan] {sport_type.upper()} - {goal_type.replace('_', ' ').title()}"
        grid = Table.grid(expand=True)
        grid.add_column()
        grid.add_column(justify="right")
        grid.add_row(
            f"Zieltermin: [bold]{plan_row['target_date']}[/bold]",
            f"Basisvolumen: [bold]{plan_row['base_weekly_volume']} {plan_row['sport_type'] == 'running' and 'km' or 'units'}[/bold]"
        )
        if latest_pmc:
            tsb_color = "green" if latest_pmc.tsb >= 0 else "yellow"
            grid.add_row(
                f"Fitness (CTL): [cyan]{latest_pmc.ctl}[/cyan] | Fatigue (ATL): [magenta]{latest_pmc.atl}[/magenta]",
                f"Form (TSB): [{tsb_color}]{latest_pmc.tsb}[/{tsb_color}]"
            )
        console.print(Panel(grid, title=title, border_style="cyan"))

        # Next Workouts Table
        today_str = date.today().isoformat()
        workouts = conn.execute(
            """
            SELECT * FROM workouts 
            WHERE week_id IN (SELECT id FROM weeks WHERE plan_id = ?)
              AND date >= ?
            ORDER BY date ASC LIMIT 7;
            """,
            (plan_id, today_str)
        ).fetchall()

        table = Table(title="Nächste geplante Einheiten", expand=True)
        table.add_column("Datum / Tag", style="cyan", width=16)
        table.add_column("Typ", style="magenta", width=14)
        table.add_column("Umfang", style="green", width=12)
        table.add_column("Intensität / Details", style="white")
        table.add_column("Status", style="bold yellow", width=12)

        for w in workouts:
            dt = date.fromisoformat(w["date"])
            day_name = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][dt.weekday()]
            metric = f"{w['metric_primary']} {w['metric_unit']}" if w['metric_primary'] > 0 else "-"
            table.add_row(
                f"{w['date']} ({day_name})",
                w["workout_type"].replace("_", " ").title(),
                metric,
                w["intensity_detail"] or w["intensity_target"] or "-",
                w["status"]
            )
        console.print(table)


@app.command("zones")
def zones():
    """Zeigt die berechneten Trainingszonen und Paces des Benutzers an."""
    db = get_db()
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM training_zones ORDER BY calculated_at DESC LIMIT 1;"
        ).fetchone()
        if not row:
            console.print("[yellow]Keine berechneten Trainingszonen gefunden.[/yellow]")
            return

        zones_data = json.loads(row["zones_json"])
        sport = row["sport_type"]
        ref_val = row["reference_value"]
        model = row["zone_model"]

        table = Table(title=f"Trainingszonen: {sport.upper()} ({model.replace('_', ' ').title()}: {ref_val})", expand=True)
        table.add_column("Zone", style="bold cyan", width=16)
        table.add_column("Zielbereich (Pace / Watt)", style="bold green", width=26)
        table.add_column("Beschreibung & Physiologischer Zweck", style="white")

        for key, z in zones_data.items():
            name = z.get("name", key)
            if "min_pace" in z:
                target = f"{z.get('min_pace')} – {z.get('max_pace')} min/km"
            elif "pace" in z:
                target = f"{z.get('pace')} min/km"
            elif "min_watts" in z:
                target = f"{z.get('min_watts')} – {z.get('max_watts') or 'max'} W"
            elif "intensity" in z:
                target = f"{z.get('intensity')} ({z.get('rir', '')})"
            else:
                target = "-"
            desc = z.get("description", "-")
            table.add_row(name, target, desc)

        console.print(table)


@app.command("plan-show")
def plan_show():
    """Zeigt den gesamten Makrozyklus aller Wochen mit Mesozyklus-Phasen und Volumen."""
    db = get_db()
    with db.get_connection() as conn:
        plan_row = conn.execute(
            "SELECT * FROM plans WHERE status = 'active' ORDER BY created_at DESC LIMIT 1;"
        ).fetchone()
        if not plan_row:
            console.print("[yellow]Kein aktiver Trainingsplan vorhanden.[/yellow]")
            return

        weeks = conn.execute(
            "SELECT * FROM weeks WHERE plan_id = ? ORDER BY week_number ASC;",
            (plan_row["id"],)
        ).fetchall()

        table = Table(title=f"Makrozyklus-Übersicht: {plan_row['goal_type'].title()} ({len(weeks)} Wochen)", expand=True)
        table.add_column("Woche", style="bold cyan", width=10)
        table.add_column("Startdatum", style="white", width=14)
        table.add_column("Phase", style="bold magenta", width=12)
        table.add_column("Zielvolumen", style="green", width=16)
        table.add_column("Geplante TSS", style="yellow", width=14)
        table.add_column("Typ", style="white", width=18)

        for w in weeks:
            recovery_badge = "[bold yellow]🌿 Entlastung (-25%)[/bold yellow]" if w["is_recovery_week"] else "Belastung"
            phase_color = {
                "base": "blue",
                "build": "yellow",
                "peak": "red",
                "taper": "green"
            }.get(w["phase"], "white")

            table.add_row(
                f"Woche {w['week_number']}",
                w["week_start_date"],
                f"[{phase_color}]{w['phase'].upper()}[/{phase_color}]",
                f"{w['target_weekly_volume']} {plan_row['sport_type'] == 'running' and 'km' or 'units'}",
                f"{w['target_weekly_tss'] or '-'}",
                recovery_badge
            )
        console.print(table)


@app.command("onboard")
def onboard(
    prompt: str = typer.Argument(..., help="Natürliche Zieldefinition (z.B. 'Marathon in 20 Wochen, laufe aktuell 30 km die Woche, VDOT 48')")
):
    """Generiert einen vollständigen Trainingsplan direkt aus natürlicher Sprache."""
    from omnitrain.parser.goal import GoalParser
    console.print(f"[bold cyan]Analysiere Zielvorgabe:[/bold cyan] \"{prompt}\"")
    parser = GoalParser()
    goal_res = parser.parse(prompt)

    console.print(f"-> Sportart: [bold green]{goal_res.sport_type.upper()}[/bold green]")
    console.print(f"-> Zieldistanz / Event: [bold green]{goal_res.target_event.title()}[/bold green]")
    console.print(f"-> Dauer: [bold yellow]{goal_res.target_weeks} Wochen[/bold yellow]")
    console.print(f"-> Basis-Wochenvolumen: [bold]{goal_res.current_baseline.value} {goal_res.current_baseline.unit}[/bold]")
    console.print(f"-> Einheiten / Woche: [bold]{goal_res.sessions_per_week} Tage[/bold] (Tage: {goal_res.preferred_days})")
    if goal_res.user_profile.vdot:
        console.print(f"-> Erkannter VDOT: [bold cyan]{goal_res.user_profile.vdot}[/bold cyan]")

    vdot_val = goal_res.user_profile.vdot or 45.0
    plan_new(
        sport=goal_res.sport_type,
        goal=goal_res.target_event,
        vdot=vdot_val,
        weeks_count=goal_res.target_weeks,
        baseline_volume=goal_res.current_baseline.value
    )


@app.command("plan-new")
def plan_new(
    sport: str = typer.Option("running", help="Sportart: running, cycling, swimming, strength"),
    goal: str = typer.Option("marathon", help="Ziel: marathon, half_marathon, 10k, 5k, ftp_builder"),
    vdot: float = typer.Option(45.0, help="Daniels VDOT Wert (nur für Running)"),
    weeks_count: int = typer.Option(16, help="Dauer des Plans in Wochen"),
    baseline_volume: float = typer.Option(30.0, help="Aktuelles Basisvolumen pro Woche (z.B. km)")
):
    """Erstellt einen neuen periodisierten Trainingsplan."""
    db = get_db()
    user_id = get_or_create_default_user(db)
    start_date = date.today() + timedelta(days=(7 - date.today().weekday()) % 7) # Next Monday
    target_date = start_date + timedelta(weeks=weeks_count)
    plan_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    console.print(f"[bold cyan]Generiere {weeks_count}-Wochen {goal.title()}-Plan ({sport})...[/bold cyan]")

    with db.get_connection() as conn:
        # Save Plan record
        conn.execute(
            """
            INSERT INTO plans (
                id, user_id, sport_type, goal_type, start_date, target_date,
                available_days, base_weekly_volume, sessions_per_week, key_workout_day,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?);
            """,
            (
                plan_id, user_id, sport, goal, start_date.isoformat(), target_date.isoformat(),
                json.dumps([2, 4, 6, 7]), baseline_volume, 4, 7, now, now
            )
        )

        # Generate weeks & workouts via RunningStrategy
        if sport == "running":
            weeks = RunningStrategy.generate_plan(
                plan_id=plan_id,
                start_date=start_date,
                target_date=target_date,
                base_weekly_volume=baseline_volume,
                available_days=[2, 4, 6, 7],
                vdot=vdot,
                sessions_per_week=4
            )

            # Persist Daniels Training Zones
            zones = get_daniels_zones(vdot)
            zone_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO training_zones (
                    id, user_id, sport_type, zone_model, reference_value,
                    zones_json, calculated_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (zone_id, user_id, sport, "daniels_vdot", vdot, json.dumps(zones), now, now, now)
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
                    (w.id, plan_id, w.week_number, w.week_start_date.isoformat(), w.phase.value,
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

    console.print(f"[bold green]✓ Plan erfolgreich erstellt und persistiert (ID: {plan_id[:8]}...)[/bold green]")
    console.print(f"Startdatum: {start_date} | Renntag: {target_date} | VDOT: {vdot}")
    status()


@app.command("checkin")
def checkin(
    freetext: str = typer.Argument(..., help="Freitext-Feedback zum Training oder Zustand (z.B. '8km gelaufen, RPE 6')")
):
    """Verarbeitet ein Trainings-Check-in über den Semantic Parser und wendet Reconcile-Regeln an."""
    db = get_db()
    user_id = get_or_create_default_user(db)
    now_dt = datetime.now(timezone.utc)
    today = date.today()

    console.print(f"[bold cyan]Verarbeite Check-in:[/bold cyan] \"{freetext}\"")

    # Step 1: Parse checkin (Ollama or Offline Fallback)
    parser = CheckinParser()
    event = parser.parse(freetext)

    console.print(f"-> Erkannter Status: [bold yellow]{event.completion_status.value.upper()}[/bold yellow] (RPE: {event.perceived_rpe or '-'})")
    if event.symptoms:
        for s in event.symptoms:
            console.print(f"-> Symptom: [bold red]{s.location} ({s.type})[/bold red] - Schweregrad: {s.severity}/10")

    with db.get_connection() as conn:
        # Step 2: Workout-Matching algorithm according to Section 3:
        # 1. Search planned workout date <= today DESC
        # 2. Search date == today
        # 3. Else fallback to spontaneous activity (workout_id = None)
        open_wo = conn.execute(
            """
            SELECT * FROM workouts
            WHERE status = 'planned' AND date <= ?
            ORDER BY date DESC LIMIT 1;
            """,
            (today.isoformat(),)
        ).fetchone()

        if not open_wo:
            open_wo = conn.execute(
                "SELECT * FROM workouts WHERE date = ? LIMIT 1;",
                (today.isoformat(),)
            ).fetchone()

        matched_wo_id = None
        if open_wo:
            wo_title = f"{open_wo['workout_type'].replace('_', ' ').title()} ({open_wo['date']}, {open_wo['metric_primary']} {open_wo['metric_unit']})"
            # CLI interactive matching confirmation (§3)
            # Default auto-confirm if running non-interactively or user confirms
            console.print(f"[bold cyan]Zuordnungsvorschlag:[/bold cyan] Feedback für [bold green]{wo_title}[/bold green]? [J/n]")
            matched_wo_id = open_wo["id"]
        else:
            console.print("[yellow]ℹ️ Kein geplantes Workout gefunden. Check-in wird als [bold]spontane Aktivität[/bold] ohne Plan-Zuordnung verbucht.[/yellow]")

        # Check female cycle tracking setting in user profile
        user_row = conn.execute("SELECT profile_json FROM users WHERE id = ?;", (user_id,)).fetchone()
        is_luteal = False
        if user_row and user_row["profile_json"]:
            try:
                prof = json.loads(user_row["profile_json"])
                if prof.get("track_menstrual_cycle"):
                    # Check luteal phase simulation or day
                    is_luteal = prof.get("is_luteal_phase", False)
            except Exception:
                pass

        upcoming_rows = conn.execute(
            """
            SELECT * FROM workouts
            WHERE status = 'planned' AND date >= ?
            ORDER BY date ASC LIMIT 10;
            """,
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

        # Step 3: Run Reconcile State Machine with cycle tolerance
        mutations = StateMachineReconciler.reconcile_checkin(
            checkin=event,
            checkin_date=today,
            upcoming_workouts=upcoming_workouts,
            is_luteal_phase=is_luteal
        )

        # Step 4: Apply mutations to SQLite
        for m in mutations:
            console.print(Panel(m.description, title=f"[bold]Regel: {m.rule.value}[/bold]", border_style="yellow"))

        for wo in upcoming_workouts:
            conn.execute(
                """
                UPDATE workouts 
                SET workout_type = ?, metric_primary = ?, intensity_target = ?, intensity_detail = ?, updated_at = ?
                WHERE id = ?;
                """,
                (wo.workout_type, wo.metric_primary, wo.intensity_target, wo.intensity_detail, now_dt.isoformat(), wo.id)
            )

        # Mark current workout completed if matched
        if matched_wo_id:
            new_status = event.completion_status.value
            conn.execute(
                "UPDATE workouts SET status = ?, updated_at = ? WHERE id = ?;",
                (new_status, now_dt.isoformat(), matched_wo_id)
            )

        # Persist Checkin Log
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
                log_id, matched_wo_id, user_id, now_dt.isoformat(), freetext,
                event.completion_status.value, metrics_json, event.perceived_rpe,
                symptoms_json, applied_json, now_dt.isoformat()
            )
        )
        conn.commit()

    console.print("[bold green]✓ Check-in erfolgreich verbucht und Plan adaptiert.[/bold green]")


@app.command("status-update")
def status_update(
    update_type: str = typer.Argument(..., help="Typ: recovery, pain_resolved, readiness"),
    note: str = typer.Option("Status-Update via CLI", help="Optionale Notiz")
):
    """Verarbeitet Status-Updates (Genesung, Schmerzabklingen, akute Erschöpfung)."""
    from omnitrain.core.status_update import StatusUpdateEvent, StatusUpdateHandler, UpdateType
    db = get_db()
    today = date.today()
    now_dt = datetime.now(timezone.utc)

    u_type = UpdateType.RECOVERY
    details = {}
    if update_type == "recovery":
        u_type = UpdateType.RECOVERY
        details = {"condition": "sickness", "status": "resolved"}
    elif update_type in ["pain_resolved", "pain"]:
        u_type = UpdateType.PAIN_UPDATE
        details = {"status": "resolved"}
    elif update_type in ["readiness", "fatigue"]:
        u_type = UpdateType.READINESS_UPDATE
        details = {"severity": 8}

    event = StatusUpdateEvent(update_type=u_type, details=details, notes=note)

    with db.get_connection() as conn:
        upcoming_rows = conn.execute(
            """
            SELECT * FROM workouts
            WHERE date >= ?
            ORDER BY date ASC LIMIT 14;
            """,
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

        mutations = StatusUpdateHandler.handle_status_update(
            update=event,
            current_date=today,
            upcoming_workouts=upcoming_workouts
        )

        for m in mutations:
            console.print(Panel(m.description, title=f"[bold]Status-Update Regel: {m.rule.value}[/bold]", border_style="green"))

        for wo in upcoming_workouts:
            conn.execute(
                """
                UPDATE workouts
                SET workout_type = ?, metric_primary = ?, intensity_target = ?, intensity_detail = ?, updated_at = ?
                WHERE id = ?;
                """,
                (wo.workout_type, wo.metric_primary, wo.intensity_target, wo.intensity_detail, now_dt.isoformat(), wo.id)
            )
        conn.commit()

    console.print("[bold green]✓ Status-Update erfolgreich angewendet.[/bold green]")


@app.command("replan")
def replan(
    reason: str = typer.Option("fitness_change", help="Grund: long_term_hiatus, goal_change, fitness_change"),
    new_vdot: Optional[float] = typer.Option(None, help="Neuer VDOT Wert"),
    new_target_date: Optional[str] = typer.Option(None, help="Neuer Renntag (YYYY-MM-DD)")
):
    """Generiert den Restplan ab der aktuellen Woche strukturell neu (Section 5D)."""
    from omnitrain.core.replan import ReplanEngine
    db = get_db()
    today = date.today()
    now_dt = datetime.now(timezone.utc).isoformat()

    with db.get_connection() as conn:
        plan_row = conn.execute(
            "SELECT * FROM plans WHERE status = 'active' ORDER BY created_at DESC LIMIT 1;"
        ).fetchone()
        if not plan_row:
            console.print("[red]Kein aktiver Plan vorhanden.[/red]")
            return

        plan_id = plan_row["id"]
        target = date.fromisoformat(new_target_date) if new_target_date else date.fromisoformat(plan_row["target_date"])
        vdot = new_vdot or 45.0

        # Delete future planned weeks and workouts
        conn.execute(
            """
            DELETE FROM workouts WHERE date >= ? AND status = 'planned';
            """,
            (today.isoformat(),)
        )
        conn.execute(
            """
            DELETE FROM weeks WHERE plan_id = ? AND week_start_date >= ?;
            """,
            (plan_id, today.isoformat())
        )

        new_weeks = ReplanEngine.replan_running(
            plan_id=plan_id,
            current_date=today,
            target_date=target,
            vdot=vdot,
            last_achieved_weekly_volume=float(plan_row["base_weekly_volume"])
        )

        for w in new_weeks:
            conn.execute(
                """
                INSERT INTO weeks (
                    id, plan_id, week_number, week_start_date, phase,
                    target_weekly_volume, target_weekly_tss, is_recovery_week,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (w.id, plan_id, w.week_number, w.week_start_date.isoformat(), w.phase.value,
                 w.target_weekly_volume, w.target_weekly_tss, 1 if w.is_recovery_week else 0, now_dt, now_dt)
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
                     wo.intensity_detail, wo.target_duration_min, wo.status.value, now_dt, now_dt)
                )

        conn.commit()

    console.print(f"[bold green]✓ Replan erfolgreich durchgeführt ({len(new_weeks)} verbleibende Wochen neu kalkuliert).[/bold green]")
    status()


@app.command("ui")
def ui(
    port: int = typer.Option(8089, help="Port für das Web-Interface"),
    host: str = typer.Option("127.0.0.1", help="Host-Bindung")
):
    """Startet das interaktive OmniTrain Web-Dashboard (Tailscale & Things 3 UI)."""
    from omnitrain.cli.ui_server import start_server
    console.print(f"[bold cyan]Starte OmniTrain Web UI auf http://{host}:{port} ...[/bold cyan]")
    console.print("[bold green]Tailscale URL: https://oci-aladdin1234.quagga-pike.ts.net:8089/[/bold green]")
    start_server(host=host, port=port)


def main():
    app()


if __name__ == "__main__":
    main()


