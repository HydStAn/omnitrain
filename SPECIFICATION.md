# System-Spezifikation: OmniTrain (Core Backend)

## 1. Vision & Zielsetzung
Entwicklung einer vollständig lokalen, entkoppelten Trainings- und State-Engine für adaptive Trainingspläne (Fokus: Ausdauer & Kraft, initial Marathon). 
Das System verarbeitet unstrukturierte Benutzereingaben (Zieldefinition, Trainingsabschlüsse, Abweichungen, Krankheit, Schmerzen) und führt diese über eine hybride Architektur aus lokalem LLM-Parser und deterministischer Regel-Engine in einen validen, sportwissenschaftlich fundierten Trainingsplan über.

Die Benutzeroberfläche (Flutter) wird später aufgesetzt. Dieses Repository implementiert ausschließlich die reine Core-Logik, die Datenhaltung (SQLite) und die lokale Inferenz-Schnittstelle.

## 2. Architektonische Leitprinzipien & Strategy Pattern
1. **Separation of Concerns & Strategy Pattern:** 
   - Das LLM berechnet keine Trainingskurven oder mathematische Progressionen.
   - Das LLM dient ausschließlich als Semantic Extraction & Classification Layer (Übersetzung von Freitext in normiertes JSON).
   - Die Rechen- und Periodisierungslogik ist über ein **Sport Engine Interface (Strategy Pattern)** entkoppelt:
     - `RunningStrategy`: Daniels VDOT, Pfitzinger Mileage, km-basierte Progression und Longrun-Deckelung.
     - `CyclingStrategy` *(zukünftig)*: Coggan FTP/TSS, Power Zones (Z1–Z7), Watt-/Zeit-Regeln.
     - `StrengthStrategy` *(zukünftig)*: RIR/RPE-Laststeuerung, Satz-/Wdh-Volumen, Deloads.
2. **Offline-First & Local-Only:** 
   - Konzipiert für lokale 1.5B–3B Modelle (z. B. Qwen 2.5 3B, Llama 3.2 3B via Ollama / llama.cpp / GGUF).
   - Alle LLM-Antworten werden über strikte JSON-Schemas (JSON-Mode / Structured Outputs) validiert.
   - **LLM-Fallback-Strategie:** Wenn das lokale LLM nicht erreichbar ist (Ollama nicht gestartet, Modell nicht geladen), fällt das System auf strukturierte CLI-Prompts zurück (direkte Abfrage der einzelnen Felder statt Freitext-Parsing). Timeout: 5 Sekunden, max. 2 Retries.
3. **Sportwissenschaftliche Sicherheitsregeln:**
   - Verpasste Einheiten/Volumina werden niemals auf Folgetage aufgeschlagen.
   - Übererfüllung führt nicht zur automatischen Steigerung der Folgetage (Überlastungsschutz).
   - Gelenk-/Sehnenschmerz oder Krankheit erzwingen softwareseitig Ruhe- oder Entlastungstage.

```
┌─────────────────────────────────────────────────────────┐
│ 1. LLM Semantic Parser                                  │
│    - Übersetzt Freitext in generisches JSON             │
│      (Status, RPE, Symptome, Metriken)                  │
│    - Fallback: Strukturierte CLI-Prompts bei Timeout    │
└────────────────────────────┬────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────┐
│ 2. Sport Engine Interface (Strategy Pattern)            │
│    - generate_plan(user, goal) → weeks + workouts       │
│    - calculate_zones(user) → training_zones             │
│    - reconcile(checkin, plan) → mutations                │
│    - replan(plan, reason) → updated plan                │
└────────────┬───────────────────────────────┬────────────┘
             │                               │
             ▼                               ▼
┌──────────────────────────┐    ┌─────────────────────────┐
│ RunningStrategy          │    │ CyclingStrategy / ...   │
│ - Daniels VDOT-Zonen     │    │ - Coggan FTP-Zonen      │
│ - Pfitzinger Mileage     │    │ - Power Zones (Z1-Z7)   │
│ - Km-basierte Progression│    │ - Watt-/Zeit-Regeln     │
│ - Pace-Zonen-Berechnung  │    │ - TSS-Berechnung        │
└──────────────────────────┘    └─────────────────────────┘
             │                               │
             └───────────────┬───────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────┐
│ 3. Generisches SQLite Schema                            │
│    (Users, Plans, Weeks, Workouts, Checkin-Logs,        │
│     Training-Zones, Mutationen)                         │
└─────────────────────────────────────────────────────────┘
```

## 3. Generisches Datenmodell (SQLite Schema)

### Schema-Versionierung
Die Datenbank enthält eine `schema_version`-Tabelle, die beim Start geprüft wird. Schema-Migrationen werden als nummerierte Python-Skripte ausgeführt (`migrations/001_initial.py`, `002_add_zones.py`, ...). Initiale Version: `1`.

```
schema_version:
  version (INTEGER, PK)
  applied_at (TEXT / ISO8601)
  description (TEXT)
```

### Kern-Tabellen

* **users**:
  * id (TEXT/UUID, PK)
  * display_name (TEXT, nullable)
  * birth_year (INTEGER, nullable)
  * resting_hr (INTEGER, nullable, Ruhepuls in bpm)
  * max_hr (INTEGER, nullable, Maximalpuls in bpm)
  * weight_kg (REAL, nullable)
  * profile_json (TEXT, nullable — erweiterbare Stammdaten, z. B. `{"vdot": 42, "ftp_watts": 220, "swim_css_100m": "01:48"}`)
  * created_at (TEXT / ISO8601)
  * updated_at (TEXT / ISO8601)

* **training_zones**:
  * id (TEXT/UUID, PK)
  * user_id (FK -> users.id)
  * sport_type (TEXT: 'running', 'cycling', 'swimming')
  * zone_model (TEXT: 'daniels_vdot', 'coggan_ftp', 'hr_percentage')
  * reference_value (REAL — z. B. VDOT 42, FTP 220, Max-HR 185)
  * zones_json (TEXT — Array von Zonen, z. B. `[{"name": "easy", "min_pace": "05:50", "max_pace": "06:20"}, {"name": "threshold", "min_pace": "04:55", "max_pace": "05:05"}]`)
  * calculated_at (TEXT / ISO8601)
  * created_at (TEXT / ISO8601)
  * updated_at (TEXT / ISO8601)

* **plans**:
  * id (TEXT/UUID, PK)
  * user_id (FK -> users.id)
  * sport_type (TEXT, z. B. 'running', 'cycling', 'strength')
  * goal_type (TEXT, z. B. 'marathon', '5k', 'ftp_builder')
  * start_date (TEXT / ISO8601 — erster Tag des Plans, explizit gesetzt)
  * target_date (TEXT / ISO8601)
  * available_days (TEXT — JSON-Array verfügbarer Wochentage, z. B. `[2, 4, 6, 7]` für Di/Do/Sa/So)
  * base_weekly_volume (REAL, z. B. 25.0 für Lauf-km, 180 für Rad-min)
  * sessions_per_week (INTEGER, z. B. 4)
  * key_workout_day (INTEGER, 1=Mo ... 7=So, z. B. Longrun-Tag)
  * status (TEXT: 'active', 'completed', 'aborted')
  * created_at (TEXT / ISO8601)
  * updated_at (TEXT / ISO8601)

* **weeks**:
  * id (TEXT/UUID, PK)
  * plan_id (FK -> plans.id)
  * week_number (INTEGER, 1..N)
  * week_start_date (TEXT / ISO8601)
  * phase (TEXT: 'base', 'build', 'peak', 'taper')
  * target_weekly_volume (REAL)
  * is_recovery_week (BOOLEAN)
  * created_at (TEXT / ISO8601)
  * updated_at (TEXT / ISO8601)

* **workouts**:
  * id (TEXT/UUID, PK)
  * week_id (FK -> weeks.id)
  * sport_type (TEXT — z. B. 'running', 'strength'; ermöglicht Multi-Sport-Tage innerhalb eines Plans)
  * date (TEXT / ISO8601)
  * day_of_week (INTEGER, 1=Mo ... 7=So)
  * workout_type (TEXT: 'easy', 'long_run', 'tempo', 'interval', 'rest', 'strength_upper', 'strength_lower', 'cross_training')
  * metric_primary (REAL, z. B. 18.0 für km, 120 für Minuten, 5 für Sets)
  * metric_unit (TEXT, z. B. 'km', 'min', 'reps', 'tss')
  * intensity_target (TEXT, nullable — Referenz auf Trainingszone, z. B. 'easy', 'threshold', 'Z4', 'RPE 8')
  * intensity_detail (TEXT, nullable — berechnete Zielwerte, z. B. '05:30–05:50 min/km', '200–220 W')
  * target_duration_min (INTEGER, nullable)
  * structure_json (TEXT, nullable — Intervall-Sets, Übungslisten, z. B. `{"intervals": [{"reps": 6, "distance_m": 800, "zone": "interval"}]}`)
  * status (TEXT: 'planned', 'completed', 'partial', 'skipped', 'modified')
  * created_at (TEXT / ISO8601)
  * updated_at (TEXT / ISO8601)

* **checkin_logs**:
  * id (TEXT/UUID, PK)
  * workout_id (FK -> workouts.id, nullable — explizit zugeordnet oder per Matching-Algorithmus)
  * user_id (FK -> users.id)
  * timestamp (TEXT / ISO8601)
  * raw_input (TEXT)
  * completion_status (TEXT: 'completed', 'partial', 'skipped', 'sick')
  * actual_metrics_json (TEXT, nullable, z. B. `{"metric_primary": 12.0, "unit": "km"}`)
  * perceived_rpe (INTEGER, 1..10, nullable)
  * symptoms_json (TEXT)
  * applied_mutations_json (TEXT — inkl. Timestamps: `[{"rule": "pain_rest_48h", "affected_workouts": [...], "applied_at": "..."}]`)
  * created_at (TEXT / ISO8601)

### Workout-Zuordnung bei Check-in (Matching-Algorithmus)
Wenn ein Check-in ohne explizite Workout-ID eingeht, wird das Workout wie folgt zugeordnet:
1. Suche das nächste Workout mit `status = 'planned'` und `date <= heute`, sortiert nach `date DESC`.
2. Falls kein offenes Workout gefunden wird, Suche mit `date = heute` (auch wenn bereits abgeschlossen → neuer Log-Eintrag).
3. Falls kein Match: Check-in wird als **"spontane Aktivität"** ohne `workout_id` gespeichert (Freilauf / ungeplantes Training).

Im CLI wird vor der Zuordnung eine Bestätigung angezeigt: *"Feedback für: Easy Run (Di, 12.0 km)? [j/n/andere]"*.

## 4. LLM-Schnittstelle & JSON-Kontrakte

### A. Onboarding / Goal Parser
* **System-Prompt Ziel:** Extrahiert Zielparameter aus Freitext.
* **JSON-Schema:**
```json
{
  "sport_type": "running",
  "target_event": "marathon",
  "target_weeks": 24,
  "current_baseline": {"value": 25.0, "unit": "km_per_week"},
  "sessions_per_week": 4,
  "preferred_days": [2, 4, 6, 7],
  "key_session_day": 7,
  "fitness_level": "beginner",
  "user_profile": {
    "age": 35,
    "resting_hr": 52,
    "max_hr": null,
    "recent_race": null,
    "weight_kg": 78
  },
  "constraints": []
}
```

### B. Generischer Check-in & Feedback Parser
* **System-Prompt Ziel:** Verarbeitet Freitext-Rückmeldungen sportartenunabhängig (Erfüllung, RPE, Krankheit, Schmerzlokalisation).
* **JSON-Schema:**
```json
{
  "event_type": "workout_checkin",
  "completion_status": "partial",
  "actual_metrics": {
    "metric_primary": 12.0,
    "unit": "km",
    "completed_units": null
  },
  "perceived_rpe": 8,
  "sickness_reported": false,
  "sickness_days": 0,
  "symptoms": [
    {
      "location": "knee_right",
      "side": "right",
      "type": "joint_pain",
      "severity": 6
    }
  ],
  "notes": "Musste bei km 12 abbrechen wegen Stechen im rechten Knie."
}
```

### C. Status-Update Parser
* **System-Prompt Ziel:** Verarbeitet Zustandsänderungen ausserhalb von Workout-Check-ins (Genesungsmeldung, Schmerzupdate, Zielanderung).
* **JSON-Schema:**
```json
{
  "event_type": "status_update",
  "update_type": "recovery",
  "details": {
    "condition": "sickness",
    "status": "resolved",
    "notes": "Fühle mich wieder fit, Fieber seit 2 Tagen weg."
  }
}
```
```json
{
  "event_type": "status_update",
  "update_type": "pain_update",
  "details": {
    "location": "knee_right",
    "status": "improving",
    "severity": 2,
    "notes": "Knie nur noch leicht bei Treppen."
  }
}
```
```json
{
  "event_type": "status_update",
  "update_type": "goal_change",
  "details": {
    "change": "target_date_shifted",
    "new_target_date": "2027-04-20",
    "reason": "Wettkampf verschoben"
  }
}
```

## 5. Deterministische Logik & RunningStrategy (Referenz-Implementierung)

### A. Zonenberechnung (RunningStrategy)
Die `RunningStrategy` berechnet Pace-Zonen nach Daniels' VDOT-Tabelle aus dem User-Profil:
* **Eingabe:** VDOT-Wert (aus Wettkampfzeit oder manuelle Eingabe) oder geschätzt aus `fitness_level`.
* **Ausgegebene Zonen:**
  * Easy (E): Grundlage, ca. 65–75 % VO2max
  * Marathon (M): Zielrenntempo
  * Threshold (T): Laktatschwelle, ca. 83–88 % VO2max
  * Interval (I): VO2max-Arbeit, ca. 95–100 % VO2max
  * Repetition (R): Schnelligkeit, kürzer als I
* **Ergebnis:** Werden als `training_zones`-Eintrag persistiert und für `workouts.intensity_detail` verwendet.

### B. Initialer Plan-Generator (RunningStrategy)
* **Start & Scheduling:**
  * `start_date` wird explizit gesetzt (Default: nächster Montag nach Plan-Erstellung).
  * Workouts werden auf die in `plans.available_days` definierten Wochentage verteilt.
  * Die Strategy-Methode `distribute_workouts_to_weekdays(available_days)` ordnet Workout-Typen den verfügbaren Tagen zu.
* **Volumen-Progression:** Steigerung des wöchentlichen Volumens um maximal 8–10 % gegenüber der Vorwoche.
* **Mesozyklus-Muster:** 3 Wochen progressive Steigerung, 1 Woche Entlastung (-25 % Volumen).
* **Tapering:** Letzte 3 Wochen vor dem Wettkampf: -20 %, -40 %, -60 % des Peak-Volumens bei Beibehaltung kurzer Reize.
* **Wochen-Aufteilung (Beispiel 4 Tage Laufen):**
  * Tag 1 (Di): Easy Run (20 % Wochenumfang) → Zone E
  * Tag 2 (Do): Tempo / Schwellenlauf (20 % Wochenumfang) → Zone T
  * Tag 3 (Sa): Easy Run (25 % Wochenumfang) → Zone E
  * Tag 4 (So): Longrun (35 % Wochenumfang, maximal 32–34 km) → Zone E/M

### C. Reconcile- & Mutations-Regelwerk (Zustandsautomat)

#### Prioritäts-Matrix (höchste Priorität zuerst)
```
1. SICK       — Krankheit (höchste Priorität, überschreibt alles)
2. PAIN       — Struktureller Schmerz (Gelenk, Sehne, Knochen)
3. RPE_HIGH   — Übermässige Ermüdung (RPE >= 8 bei Easy-Workout)
4. PARTIAL    — Teilweise absolviert
5. MISSED     — Nicht absolviert (keine Aktion nötig, km verfallen)
6. OVERPERFORMED — Mehr als geplant (keine Aktion, Plan bleibt)
7. COMPLETED  — Planmässig absolviert (keine Mutation)
```

Bei mehreren gleichzeitigen Signalen (z. B. `SICK` + `PAIN`) greift die höchstpriorisierte Regel.

#### Regel 1: Status SICK
* **Auslöser:** Check-in mit `sickness_reported = true` oder `completion_status = 'sick'`.
* **Aktion:** Alle geplanten Workouts werden in `rest` umgewandelt, **bis ein expliziter Status-Update (`event_type: 'status_update'`, `update_type: 'recovery'`, `status: 'resolved'`) eingeht.**
* **Wiedereinstieg nach Genesung:**
  * Erstes Training bei maximal 50 % des geplanten Volumens.
  * Kein Tempotraining (`tempo`, `interval`) in den ersten 4 Tagen nach Genesung.
  * Ab Tag 5: schrittweise Rückkehr zum regulären Plan.

#### Regel 2: Symptom PAIN (Gelenk, Sehne, Fuß, Schienbein)
* **Auslöser:** Check-in mit `symptoms[].type` in `['joint_pain', 'tendon_pain', 'shin_pain', 'bone_pain']` und `severity >= 4`.
* **Aktion:**
  * Mindestens 48–72 Stunden zwingende Trainingspause (Umwandlung in `rest`).
  * Streichung von Tempo-/Intervalltraining für die laufende Woche.
  * Bei `severity >= 7`: Pause bis expliziter Status-Update (`pain_update`, `status: 'resolved'` oder `status: 'improving'` mit `severity <= 3`).
* **Muskelschmerz (`type: 'muscle_soreness'`)** mit `severity < 6` löst keine Zwangspause aus (normaler Trainingsreiz).

#### Regel 3: RPE-Überwachung
* **Auslöser:** Easy-Workout mit `perceived_rpe >= 8`.
* **Aktion:** Reduktion des Volumens der Folgewoche um 15 %.

#### Regel 4: PARTIAL / MISSED
* Verpasste Einheiten/Volumina verfallen ersatzlos.
* Kein Erhöhen der Folgetage oder des nächsten Longruns.

#### Regel 5: OVERPERFORMED
* Keine automatische Steigerung der Folgetage; Plan läuft unverändert weiter.

### D. Replan-Konzept (Plan-Neugenerierung)
Punktuelle Mutationen (§5C) reichen nicht aus, wenn der Plan strukturell ungültig wird. Ein **Replan** wird ausgelöst bei:
* **Langzeitausfall:** Mehr als 2 zusammenhängende Wochen ohne Training (Krankheit, Verletzung). Der Restplan wird ab der aktuellen Woche mit reduziertem Basisvolumen neu generiert.
* **Zieländerung:** Neues Zieldatum (`goal_change` Status-Update), andere Zieldistanz, anderer Wettkampf.
* **Signifikante Fitness-Änderung:** Neuer VDOT/FTP nach Wettkampf oder Leistungstest → Zonen und Paces werden neu berechnet, offene Workouts erhalten aktualisierte `intensity_detail`-Werte.

Beim Replan gilt:
* Bereits absolvierte Wochen/Workouts bleiben unverändert (`status != 'planned'`).
* Die letzte absolvierte Woche dient als neue Baseline für die Volumenberechnung.
* Die Strategy-Methode `replan(plan, reason, current_week)` erzeugt die verbleibenden Wochen und Workouts neu.

## 6. Ziel dieser Phase & Deliverables
* **Headless CLI-Applikation:** Keine UI erforderlich.
* **Generierung:** Initiales Erzeugen und persistentes Abspeichern eines Plans in SQLite via `RunningStrategy`, inklusive Zonenberechnung.
* **Interaktive Schleife:**
  * Konsolenausgabe des aktuellen Plans / der nächsten Einheiten (mit berechneten Pace-Zonen).
  * Eingabeaufforderung für Freitext-Logs.
  * Aufruf des lokalen LLM zur Extraktion des generischen JSON-Check-ins (Fallback auf strukturierte Prompts bei Timeout).
  * Workout-Zuordnung mit Bestätigung.
  * Anwendung der Mutationsregeln (Zustandsautomat) auf die SQLite-Datenbank.
  * Vorher-/Nachher-Anzeige der modifizierten Trainingstage im Terminal.
* **Status-Updates:** Verarbeitung von Genesungsmeldungen, Schmerzupdates und Zieländerungen.

## 7. Erweiterbarkeit: Andere Laufziele & Distanzen (Multi-Goal)
Die `RunningStrategy` unterstützt über austauschbare Ziel-Profile (`GoalProfile`) beliebige Laufziele:
* `5k`: Fokus auf VO2max-Intervalle (Zone I/R), Longrun-Deckelung bei 12–16 km, Tapering 7–10 Tage.
* `10k`: Fokus auf Schwellenläufe (Zone T) und Laktattoleranz, Longrun-Deckelung bei 14–20 km, Tapering 10–14 Tage.
* `half_marathon`: Fokus auf Laktatschwelle (Zone T/M) und progressive Dauerläufe, Longrun-Deckelung bei 18–24 km, Tapering 14 Tage.
* `marathon`: Fokus auf aerobe Effizienz (Zone E/M) und Glykogenökonomie, Longrun-Deckelung bei 32–34 km, Tapering 21 Tage (3 Wochen).
* `base_building`: Zyklisches Grundlagentraining ohne Tapering/Wettkampfpeak.

## 8. Abstraktion & Multi-Sport-Fähigkeit (Cross-Sport Architecture)
Das Kernprinzip trennt Daten- und Prompt-Modell von der Rechen-Engine:
1. **Gemeinsamkeiten aller Sportarten:**
   * Schutz- und Regenerationslogik: Krankheit (`SICK`) erzwingt Pause und degressiven Wiedereinstieg; Schmerz (`PAIN`) verlangt Deload der betroffenen Struktur.
   * Keine Kompensation verpasster Einheiten durch Überlastung an Folgetagen.
   * Periodisierung in Mesozyklen (typischerweise 3:1 oder 4:1 Belastung zu Entlastung).
   * Zustandsautomat mit Prioritäts-Matrix für Reconcile-Entscheidungen.
2. **Sportartspezifische Belastungsmetriken & Zonenmodelle:**
   * **Laufsport:** Distanz (`km`), Pace, Daniels VDOT-Zonen (`RunningStrategy`).
   * **Radsport:** Zeit (`min`), Leistung (`Watt`, `% FTP`), TSS, Coggan FTP-Zonen (`CyclingStrategy`).
   * **Kraftsport (Hypertrophie / Powerlifting):** Sätze, Wiederholungen, Last (`kg`), RIR / RPE (`StrengthStrategy`).
   * **Schwimmen:** Distanz (`m`), Intervalle, CSS (`SwimStrategy`).
   * **Triathlon / Hyrox:** Multi-Sport-Tage via `workouts.sport_type`, kumuliertes Belastungsmanagement.
3. **Zukunftssicheres Datenmodell (Polymorphe Workouts):**
   * Pläne und Workouts unterstützen `sport_type`, `metric_primary`, `metric_unit`, `intensity_target` und `intensity_detail`.
   * Detail-Parameter für Nicht-Lauf-Disziplinen werden über flexible JSON-Payloads (`structure_json` / `actual_metrics_json`) abgebildet, ohne das Tabellenschema brechen zu müssen.
   * Multi-Sport-Tage werden durch mehrere Workouts am selben Datum mit unterschiedlichem `sport_type` abgebildet.
