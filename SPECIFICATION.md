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
      - `RunningStrategy`: Daniels VDOT, Pfitzinger Mileage, km-basierte Progression, Strides und Longrun-Deckelung.
      - `CyclingStrategy` *(zukünftig)*: Coggan FTP/TSS (mit definierten Testprotokollen), NP/IF, Power Zones (Z1–Z7), CTL/ATL/TSB, Sweetspot & Polarized.
      - `StrengthStrategy` *(zukünftig)*: RIR/RPE-Laststeuerung, DUP/Linear/Block-Periodisierung, Movement-Pattern-Balancing, MEV/MAV/MRV.
      - `SwimStrategy` *(zukünftig)*: CSS mit definiertem Testprotokoll, 5-Zonen-Modell, Pull/Kick/Technik-Komponenten, SWOLF-Tracking.
2. **Offline-First & Local-Only:** 
   - Konzipiert für lokale 1.5B–3B Modelle (z. B. Qwen 2.5 3B, Llama 3.2 3B via Ollama / llama.cpp / GGUF).
   - Alle LLM-Antworten werden über strikte JSON-Schemas (JSON-Mode / Structured Outputs) validiert.
   - **LLM-Fallback-Strategie:** Wenn das lokale LLM nicht erreichbar ist (Ollama nicht gestartet, Modell nicht geladen), fällt das System auf strukturierte CLI-Prompts zurück (direkte Abfrage der einzelnen Felder statt Freitext-Parsing). Timeout: 5 Sekunden, max. 2 Retries.
3. **Wearables & Automatisierte Ingestion (API-Adapter):**
   - Das System bereitet eine Adapter-Schnittstelle (Service Layer) für zukünftige Integrationen von Apple HealthKit, Garmin Connect oder Strava vor.
   - Harte Metriken (HR, Pace, Watt, HRV) sollen perspektivisch automatisch ingestiert werden. Der LLM-gestützte Freitext-Check-in ergänzt dann primär weiche Metriken (RPE, Schmerz, Ernährung, Gefühl).
4. **Sportwissenschaftliche Sicherheitsregeln:**
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
│ RunningStrategy          │    │ CyclingStrategy         │
│ - Daniels VDOT-Zonen     │    │ - Coggan FTP-Zonen      │
│ - Pfitzinger Mileage     │    │ - NP/IF/TSS             │
│ - Strides & Progression  │    │ - Sweetspot & Polarized │
│ - Pace-Zonen-Berechnung  │    │ - CTL/ATL/TSB           │
└──────────────────────────┘    └─────────────────────────┘
┌──────────────────────────┐    ┌─────────────────────────┐
│ StrengthStrategy         │    │ SwimStrategy            │
│ - RPE/RIR Autoregulation │    │ - CSS-Zonen (5 Stufen)  │
│ - DUP/Linear/Block       │    │ - Pull/Kick/Technik     │
│ - Movement Patterns      │    │ - SWOLF-Tracking        │
│ - MEV/MAV/MRV            │    │ - Open-Water-Spezifika  │
└──────────────────────────┘    └─────────────────────────┘
             │                               │
             └───────────────┬───────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────┐
│ 3. Unified Training Load (rTSS + bTSS + sTSS + wTSS)  │
│    → CTL / ATL / TSB über alle Sportarten              │
└────────────────────────────┬────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────┐
│ 4. Generisches SQLite Schema                            │
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
  * profile_json (TEXT, nullable — erweiterbare Stammdaten, z. B. `{"vdot": 42, "ftp_watts": 220, "swim_css_100m": "01:48", "track_menstrual_cycle": true, "injury_history": []}`)
  * created_at (TEXT / ISO8601)
  * updated_at (TEXT / ISO8601)

* **training_zones**:
  * id (TEXT/UUID, PK)
  * user_id (FK -> users.id)
  * sport_type (TEXT: 'running', 'cycling', 'swimming')
  * zone_model (TEXT: 'daniels_vdot', 'coggan_ftp', 'hr_percentage', 'swim_css')
  * reference_value (REAL — z. B. VDOT 42, FTP 220, CSS 1.45)
  * zones_json (TEXT — Array von Zonen, z. B. `[{"name": "easy", "min_pace": "05:50", "max_pace": "06:20"}]`)
  * calculated_at (TEXT / ISO8601)
  * created_at (TEXT / ISO8601)
  * updated_at (TEXT / ISO8601)

* **plans**:
  * id (TEXT/UUID, PK)
  * user_id (FK -> users.id)
  * sport_type (TEXT, z. B. 'running', 'cycling', 'strength', 'multisport')
  * goal_type (TEXT, z. B. 'marathon', '5k', 'ftp_builder')
  * start_date (TEXT / ISO8601 — erster Tag des Plans, explizit gesetzt)
  * target_date (TEXT / ISO8601)
  * available_days (TEXT — JSON-Array verfügbarer Wochentage, z. B. `[2, 4, 6, 7]`)
  * base_weekly_volume (REAL, z. B. 25.0 für Lauf-km, 180 für Rad-min, 350 für TSS)
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
  * target_weekly_volume (REAL — Primärmetrik)
  * target_weekly_tss (REAL, nullable — Unified Load)
  * is_recovery_week (BOOLEAN)
  * created_at (TEXT / ISO8601)
  * updated_at (TEXT / ISO8601)

* **workouts**:
  * id (TEXT/UUID, PK)
  * week_id (FK -> weeks.id)
  * sport_type (TEXT — z. B. 'running', 'strength', 'cycling')
  * date (TEXT / ISO8601)
  * day_of_week (INTEGER, 1=Mo ... 7=So)
  * workout_type (TEXT: 'easy', 'long_run', 'tempo', 'interval', 'rest', 'strength_upper', 'strength_lower', 'cross_training')
  * metric_primary (REAL, z. B. 18.0 für km, 120 für Minuten, 5 für Sets)
  * metric_unit (TEXT, z. B. 'km', 'min', 'reps', 'tss')
  * intensity_target (TEXT, nullable — Referenz auf Trainingszone, z. B. 'easy', 'threshold', 'Z4', 'RPE 8')
  * intensity_detail (TEXT, nullable — berechnete Zielwerte, z. B. '05:30–05:50 min/km', '200–220 W')
  * target_duration_min (INTEGER, nullable)
  * structure_json (TEXT, nullable — Intervall-Sets, Übungslisten, z. B. `{"intervals": [{"reps": 6, "distance_m": 800, "zone": "interval"}]}`)
  * status (TEXT: 'planned', 'completed', 'partial', 'skipped', 'medical_skip', 'modified')
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
  * environmental_factors_json (TEXT, nullable — Wetter, Hitze, Höhe, z. B. `{"temp_c": 30, "elevation_m": 1500}`)
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
  "completion_reason": "time_constraint",
  "actual_metrics": {
    "metric_primary": 12.0,
    "unit": "km",
    "tss": 85.4,
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
  "notes": "Musste bei km 12 abbrechen, keine Zeit mehr. Leichtes Stechen im rechten Knie."
}
```
*(Anmerkung: `actual_metrics` ist sportartenspezifisch. Bei Radsport z.B. `"np_watts": 215, "tss": 120`, beim Schwimmen `"swolf": 38`)*

### C. Status-Update Parser
* **System-Prompt Ziel:** Verarbeitet Zustandsänderungen ausserhalb von Workout-Check-ins (Genesungsmeldung, Schmerzupdate, Zieländerung, Readiness/Schlaf).
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
  "update_type": "readiness_update",
  "details": {
    "factor": "sleep_deprivation",
    "severity": 8,
    "notes": "Jetlag aus Asien, habe letzte Nacht nur 3h geschlafen."
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
* **Strides (Lauf-ABC / neuromuskuläre Aktivierung):**
  * 4–6 × 80–100 m kontrollierte Beschleunigungen (Zone R), am Ende von Easy Runs (Tag 1 oder Tag 3).
  * Kein eigenes Volumen — zählen nicht zum Wochenumfang, aber als Workout-Komponente in `structure_json` abgebildet.
  * Zweck: Laufökonomie, Rekrutierung schneller Muskelfasern, Erhalt der Spritzigkeit ohne Ermüdungsrisiko.
  * Referenz: Daniels (*Running Formula*), Pfitzinger (*Advanced Marathoning*), Fitzgerald (*80/20 Running*).
* **Adaptive Volumen-Steigerungsrate & Alters-Skalierung:**
  * Die pauschale 8–10 %-Regel (§5B) wird nach aktuellem Wochenvolumen differenziert:
    * < 30 km/Woche: max. 8 % (Einsteiger, höheres relatives Verletzungsrisiko)
    * 30–60 km/Woche: 8–10 % (Standard)
    * \> 60 km/Woche: 5–8 % (erfahrene Läufer, absolute km-Zunahme bereits gross)
  * **Alters-Anpassung:** Bei Athleten > 40 Jahre (und verstärkt ab > 50) wird die Steigerungsrate und das Basis-Wochenvolumen konservativer skaliert, da Sehnen und Muskeln signifikant mehr Zeit für die Regeneration benötigen.
  * Gespeichert als Konfiguration in der `RunningStrategy`, nicht als User-Eingabe.

### C. Reconcile- & Mutations-Regelwerk (Zustandsautomat)

#### Prioritäts-Matrix (höchste Priorität zuerst)
```
1. SICK       — Krankheit (höchste Priorität, überschreibt alles)
2. PAIN       — Struktureller Schmerz (Gelenk, Sehne, Knochen)
3. FATIGUE    — Akute Erschöpfung / Jetlag / Schlafmangel
4. RPE_HIGH   — Übermässige Ermüdung (RPE >= 8 bei Easy-Workout)
5. PARTIAL    — Teilweise absolviert
6. MISSED     — Nicht absolviert (keine Aktion nötig, km verfallen)
7. OVERPERFORMED — Mehr als geplant (keine Aktion, Plan bleibt)
8. COMPLETED  — Planmässig absolviert (keine Mutation)
```

Bei mehreren gleichzeitigen Signalen (z. B. `SICK` + `PAIN`) greift die höchstpriorisierte Regel.

#### Regel 1: Status SICK
* **Auslöser:** Check-in mit `sickness_reported = true` oder `completion_status = 'sick'`.
* **Klassifizierung (LLM-Aufgabe):** Unterscheidung zwischen systemisch ("Fieber", "Gliederschmerzen", "below the neck") und lokal ("Schnupfen", "above the neck").
* **Aktion:**
  * Bei systemischem Infekt: Alle geplanten Workouts werden zwingend in `rest` umgewandelt, **bis ein expliziter Status-Update (`status: 'resolved'`) eingeht.**
  * Bei lokalem Infekt ohne Fieber: Option auf leichte Aktivität (Zone 1) zur Erhaltung, kein Tempotraining.
* **Wiedereinstieg nach systemischer Genesung:**
  * Erstes Training bei maximal 50 % des geplanten Volumens, zwingend Zone 1.
  * Kein Tempotraining (`tempo`, `interval`) in den ersten 4 Tagen nach Genesung.
  * Ab Tag 5: schrittweise Rückkehr zum regulären Plan.

#### Regel 2: Symptom PAIN (Gelenk, Sehne, Fuß, Schienbein)
* **Auslöser:** Check-in mit `symptoms[].type` in `['joint_pain', 'tendon_pain', 'shin_pain', 'bone_pain']`.
* **Aktion je nach Schmerzart & Lokalisierung:**
  * **Knochenschmerz (z. B. shin_pain):** Strikte Entlastung. Bei Severity >= 4 sofortige Zwangspause bis explizites ärztliches "Go" (Status-Update).
  * **Sehnenschmerz (z. B. tendon_pain, Achillessehne):** Reagiert oft schlecht auf komplette Ruhe. Volumen/Intensität wird drastisch reduziert, Plyometrie/Sprints gestrichen, aber leichtes Lastmanagement bleibt erhalten.
  * **Gelenkschmerz (z. B. joint_pain, Knie):** Severity >= 4 erzwingt 48–72h Pause. Bei Severity 1-3 (Niggle) keine Pause, aber Ersatz von Tempo durch Easy in den nächsten 48h.
* **Muskelschmerz (`type: 'muscle_soreness'`)** mit `severity < 6` löst keine Zwangspause aus (Active Recovery).
* **LLM Human-in-the-Loop:** Bei Erkennung von Schmerz ab Severity 4 fragt die UI sicherheitshalber nach ("Ich habe verstanden: Stechender Knieschmerz (6/10). Korrekt?").

#### Regel 3: FATIGUE / LOW READINESS
* **Auslöser:** Status-Update `readiness_update` (z.B. Jetlag, Schlafmangel, extrem gestresst) mit `severity >= 7`.
* **Aktion:** Nächste 48 Stunden: Umwandlung aller High-Intensity-Workouts (Tempo, Intervalle, Kraft Maximalkraft) in Active Recovery / Easy Workouts (Zone 1-2). Volumen bleibt erhalten, Intensität sinkt.

#### Regel 4: Ernährung & Hydration (RED-S Prävention)
* **Auslöser:** Geplante Long Runs (>90 min) oder extreme TSS-Wochen.
* **Aktion:** Die Engine gibt proaktive Warnungen oder Tipps für die Kohlenhydratzufuhr (g/h) und Hydratation aus.
* **Check-in Kontext:** Fehlende Verpflegung kann als Kontext für hohe RPE-Ausreißer ("Bonking") erfasst werden, um zu verhindern, dass die Engine mangelnde Fitness annimmt, wenn es an Treibstoff fehlte.

#### Regel 5: RPE-Überwachung
* **Auslöser:** Easy-Workout mit `perceived_rpe >= 8`.
* **Aktion:** Reduktion des Volumens der Folgewoche um 15 %.
* **Ausnahme (Female Cycle):** Wenn `track_menstrual_cycle: true` und der User sich in der späten Luteal- / frühen Menstruationsphase befindet (häufig höherer RPE/Puls), wird die RPE-Toleranz um +1 erhöht.
  * *Proaktive Anpassung:* Statt nur reaktiv die RPE-Toleranz zu erhöhen, reduziert die Engine in dieser Zyklusphase proaktiv die Intensität leicht und passt Temperatur- oder Pulszonen nach oben an, um der veränderten Physiologie (Körperkerntemperatur, Substratnutzung) gerecht zu werden.

#### Regel 6: PARTIAL / MISSED
* **Partial - Time Constraint:** Abbruch wegen Zeitmangel. Keine Aktion, verpasstes Volumen verfällt ersatzlos.
* **Partial - Exhaustion:** Abbruch wegen Erschöpfung. Zählt als `RPE_HIGH` (Regel 4) und senkt das Volumen der Folgewoche.
* **Missed:** Verpasste Einheiten verfallen. Kein Erhöhen der Folgetage oder des nächsten Longruns (Schutz vor "Aufhol-Verletzungen").

#### Regel 7: OVERPERFORMED
* Keine automatische Steigerung der Folgetage; Plan läuft unverändert weiter.

### D. Replan-Konzept (Plan-Neugenerierung)
Punktuelle Mutationen (§5C) reichen nicht aus, wenn der Plan strukturell ungültig wird. Ein **Replan** wird ausgelöst bei:
* **Langzeitausfall:** Mehr als 2 zusammenhängende Wochen ohne Training (Krankheit, Verletzung). Der Restplan wird ab der aktuellen Woche mit reduziertem Basisvolumen neu generiert.
* **Zieländerung:** Neues Zieldatum (`goal_change` Status-Update), andere Zieldistanz, anderer Wettkampf.
* **Signifikante Fitness-Änderung:** Neuer VDOT/FTP nach Wettkampf oder Leistungstest → Zonen und Paces werden neu berechnet, offene Workouts erhalten aktualisierte `intensity_detail`-Werte.
* **Goal Feasibility Check:** Fällt ein Athlet so lange aus, dass die verbleibende Zeit bis zum Wettkampf nicht mehr ausreicht, um das nötige Volumen sicher zu erreichen, eskaliert die Engine den Replan. Sie schlägt proaktiv ein Downgrade des Ziels (z. B. Halbmarathon statt Marathon) oder ein neues Zieldatum vor.

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
4. **Unified Training Load (sportartübergreifende Belastungssteuerung):**
   * Jede Strategy berechnet einen sportartspezifischen **Training Stress Score (TSS)** pro Workout:
     * Laufen: **rTSS** (basierend auf Pace / Functional Threshold Pace)
     * Radsport: **bTSS** (basierend auf NP / FTP, nach Coggan)
     * Schwimmen: **sTSS** (basierend auf Pace / CSS)
     * Kraft: **wTSS** (Approximation aus RPE × Dauer)
   * Der Gesamt-TSS pro Tag/Woche wird über alle Sportarten aggregiert und ermöglicht die Berechnung von **CTL** (Chronic Training Load), **ATL** (Acute Training Load) und **TSB** (Training Stress Balance) als zentrale Steuerungsgrössen.
   * Diese Metriken werden in `actual_metrics_json` erfasst und in der Statistik-Ansicht visualisiert.
5. **Konfigurierbarer Mesozyklus:**
   * Standard: 3:1 (3 Wochen Steigerung, 1 Woche Entlastung).
   * Konfigurierbar in `plans.profile_json` als `{"mesocycle_ratio": "3:1"}` — unterstützte Varianten: `2:1` (ältere/verletzungsanfällige Athleten), `3:1` (Standard), `4:1` (junge/erfahrene Athleten mit hoher Belastbarkeit).

## 9. Sportarten- & Zielkatalog (Referenz)

### 🏃 Laufsport (`RunningStrategy`)

| Ziel (`goal_type`) | Planstruktur | Primärmetrik | Zonenmodell |
|---------------------|-------------|-------------|-------------|
| `5k` | 8–12 Wochen | km | Daniels VDOT |
| `10k` | 10–14 Wochen | km | Daniels VDOT |
| `half_marathon` | 12–16 Wochen | km | Daniels VDOT |
| `marathon` | 16–24 Wochen | km | Daniels VDOT |
| `ultra_50k` / `ultra_100k` | 20–30 Wochen | km + Höhenmeter | VDOT + Zeitbasiert |
| `base_building` | Fortlaufend (zyklisch) | km | VDOT |
| `return_to_running` | 6–12 Wochen | km + Minuten | Konservativ, HR-basiert |
| `couch_to_5k` | 8–10 Wochen | Minuten (Walk/Run) | Puls / RPE |

Besonderheiten Ultra: Longrun-Cap in Zeit statt km (z. B. max. 4h statt max. 34 km), Vertikalmeter als sekundäre Metrik.

### 🚴 Radsport (`CyclingStrategy`)

| Ziel (`goal_type`) | Planstruktur | Primärmetrik | Zonenmodell |
|---------------------|-------------|-------------|-------------|
| `ftp_builder` | 8–12 Wochen | TSS / Minuten | Coggan FTP-Zonen (Z1–Z7) |
| `gran_fondo` | 12–20 Wochen | TSS / km | Coggan FTP |
| `crit_race` | 8–12 Wochen | TSS + Intervalle | FTP + anaerobe Kapazität |
| `mtb_endurance` | 12–16 Wochen | Stunden + Höhenmeter | FTP / HR |
| `base_endurance` | Fortlaufend | TSS / Woche | FTP |
| `zwift_training` | 8–12 Wochen | TSS / Watt | FTP (Indoor) |

Besonderheiten: Sweetspot-Blöcke (88–94 % FTP), Over-Under-Intervalle, Periodisierung nach CTL/ATL/TSB, Indoor/Outdoor-Differenzierung.

#### FTP-Testprotokolle (Pflicht vor Zonenberechnung)
FTP (Functional Threshold Power) muss vor der Zonenberechnung bestimmt werden. Es werden zwei Standard-Protokolle unterstützt:
* **20-Minuten-Test:** 20 min All-Out auf Ergometer/Strasse. FTP = Durchschnittsleistung × 0.95.
* **Ramp-Test (Step-Test):** Beginnend bei ~100 W, alle 1 min +20 W bis Erschöpfung. FTP = 75 % der letzten voll absolvierten Stufe.
* **Ergebnis:** Wird in `training_zones.reference_value` persistiert; Zonen werden automatisch berechnet.
* **Re-Test-Empfehlung:** Alle 6–8 Wochen oder nach signifikantem Trainingsblock / Wettkampf.
* **Indoor/Outdoor-Korrektur:** Indoor-FTP liegt typisch 5–10 % unter Outdoor-FTP. Bei Bedarf zwei separate Zonenprofile.

#### Normalized Power (NP), Intensity Factor (IF) \u0026 TSS-Berechnung
* **Normalized Power (NP):** Gewichteter Durchschnitt der Leistung, der variable Intensität berücksichtigt (30-s rolling average, 4. Potenz). Besser als Durchschnittsleistung für ungleichmässige Belastungen.
* **Intensity Factor (IF):** `IF = NP / FTP`. Werte: <0.75 = Recovery, 0.75–0.85 = Endurance, 0.85–0.95 = Tempo, 0.95–1.05 = Threshold, >1.05 = VO2max+.
* **TSS-Formel:** `TSS = (Dauer_in_Sekunden × NP × IF) / (FTP × 3600) × 100`.
* **Abbildung in DB:** NP und IF werden in `actual_metrics_json` erfasst: `{"np_watts": 210, "if": 0.91, "tss": 85}`.

#### CTL-Steigerungsrate (Safety Constraint)
Analog zur 10 %-Regel im Laufsport gilt für die Rad-Periodisierung:
* **Maximale CTL-Steigerung:** 5–7 TSS/Tag pro Woche (Empfehlung nach Coggan/Allen).
* **Überschreitung:** Löst bei der Plan-Generierung eine Warnung aus und begrenzt automatisch das geplante Wochenvolumen.

#### Polarized Training (Seiler-Modell, alternative Plan-Variante)
* Neben dem Sweetspot-basierten Default-Ansatz wird **Polarized Training** nach Stephen Seiler als konfigurierbare Variante unterstützt.
* **Verteilung:** ~80 % des Trainingsvolumens in Z1–Z2 (unter LT1), ~20 % in Z4+ (über LT2), minimale Zeit in Z3 (Sweetspot/Tempo).
* **Evidenz:** Meta-Analysen (Stöggl \u0026 Sperlich, 2014) zeigen bei erfahrenen Ausdauerathleten vergleichbare oder bessere Ergebnisse als Threshold-Training.
* **Konfiguration:** Über `plans.profile_json` als `{"training_distribution": "polarized"}` (Default: `"sweetspot"`).

### 🏋️ Kraftsport (`StrengthStrategy`)

| Ziel (`goal_type`) | Planstruktur | Primärmetrik | Intensitätsmodell |
|---------------------|-------------|-------------|-------------------|
| `hypertrophy` | 8–16 Wochen | Tonnage (kg × Wdh) | RIR / RPE |
| `strength_5x5` | 12–16 Wochen | 1RM-Prozentsätze | % 1RM |
| `powerlifting_meet` | 12–20 Wochen (Peaking) | SBD (Squat/Bench/Dead) | Wilks / % 1RM |
| `bodyweight_fitness` | Fortlaufend | Wdh-Progressionen | RPE |
| `general_fitness` | Fortlaufend (zyklisch) | Sätze × Wdh | RPE / RIR |
| `rehab_strength` | 6–12 Wochen | Sätze bei niedrigem Gewicht | Schmerzfreier ROM |

Besonderheiten: Deload-Wochen alle 3–4 Wochen (-40 % Volumen), Übungslisten in `structure_json`, Split-Varianten (Push/Pull/Legs, Upper/Lower, Ganzkörper), Autoregulation via RPE/RIR.

#### Periodisierungsmodell
Die `StrengthStrategy` verwendet standardmässig **Daily Undulating Periodization (DUP)** (Zourdos et al., 2016) — die aktuell best-evidenzierte Methode für simultane Kraft- und Hypertrophie-Entwicklung:
* **Prinzip:** Innerhalb einer Woche variieren Intensität und Volumen pro Trainingstag (z. B. Mo: Hypertrophie 3×10 @RPE 7, Mi: Kraft 5×3 @RPE 8, Fr: Power/Metabolic 4×6 @RPE 7.5).
* **Alternative Modelle** (über `plans.profile_json` konfigurierbar):
  * **Linear:** Klassische Phasen — Hypertrophie (4 Wo) → Kraft (4 Wo) → Peaking (2 Wo). Besser für Einsteiger.
  * **Block:** Issurin-Modell — konzentrierte Blöcke (2–4 Wochen) mit einem Hauptfokus. Gut für fortgeschrittene Powerlifter.
* **Default für Goal-Typen:**
  * `hypertrophy` / `general_fitness` / `bodyweight_fitness` → DUP
  * `strength_5x5` / `rehab_strength` → Linear
  * `powerlifting_meet` → Block (mit Peaking)

#### Wiederholungsbereiche pro Ziel
| Ziel | Rep-Range | Sätze | Intensität | Pause |
|------|-----------|-------|-----------|-------|
| Maximalkraft | 1–5 Wdh | 3–6 | 85–100 % 1RM / RPE 8–10 | 3–5 min |
| Hypertrophie | 6–12 Wdh | 3–5 | 65–80 % 1RM / RPE 7–9 | 60–120 s |
| Kraft-Ausdauer | 12–20 Wdh | 2–4 | 50–65 % 1RM / RPE 6–8 | 30–60 s |
| Metabolic / Power | 3–6 Wdh (explosiv) | 3–5 | 50–70 % 1RM | 2–3 min |

Diese Bereiche werden in `structure_json` als Constraints pro Übung hinterlegt und für die Autoregulation via RPE/RIR herangezogen.

#### Übungs-Kategorisierung (Movement Patterns)
Compound-Übungen werden nach Bewegungsmuster klassifiziert, um ein automatisches Balancing der Split-Pläne zu ermöglichen:
* **Squat** (Kniebeuge-Pattern): Back Squat, Front Squat, Goblet Squat, Split Squat
* **Hinge** (Hüftgelenk-Pattern): Deadlift, Romanian DL, Hip Thrust, Good Morning
* **Vertical Push:** Overhead Press, Push Press, Dumbbell Shoulder Press
* **Vertical Pull:** Pull-Up, Chin-Up, Lat Pulldown
* **Horizontal Push:** Bench Press, Incline Press, Push-Up
* **Horizontal Pull:** Barbell Row, Cable Row, Dumbbell Row
* **Carry / Core:** Farmer's Walk, Plank, Pallof Press, Turkish Get-Up

Die Engine stellt sicher, dass jeder Split pro Woche alle 7 Patterns mindestens 1× enthält (ausser `rehab_strength`, wo selektiv gearbeitet wird).

#### Volumen-Landmarken (nach Renaissance Periodization)
* **MEV (Minimum Effective Volume):** Minimales Satzvolumen pro Muskelgruppe und Woche, um Fortschritt zu erzielen (typisch 6–10 Sätze).
* **MAV (Maximum Adaptive Volume):** Optimaler Bereich für maximale Adaptation (typisch 12–20 Sätze).
* **MRV (Maximum Recoverable Volume):** Obergrenze, ab der die Erholung nicht mehr ausreicht (typisch 20–25+ Sätze).
* Die Engine generiert Pläne im MAV-Bereich und reduziert auf MEV in Deload-Wochen.

#### Relative Stärke-Koeffizienten
Für Powerlifting-Vergleiche werden **DOTS Points** (IPF-Standard, Nachfolger von Wilks) und optional **GL Points** (Goodlift) unterstützt. Wilks bleibt als Legacy-Option verfügbar.

### 🏊 Schwimmen (`SwimStrategy`)

| Ziel (`goal_type`) | Planstruktur | Primärmetrik | Zonenmodell |
|---------------------|-------------|-------------|-------------|
| `open_water_5k` | 12–16 Wochen | Meter | CSS (Critical Swim Speed) |
| `pool_1500m` | 8–12 Wochen | Meter | CSS-Zonen |
| `triathlon_swim_leg` | Teil eines Tri-Plans | Meter + Zeit | CSS |
| `learn_to_swim` | Fortlaufend | Minuten + Technikdrills | RPE |

Besonderheiten: Technikdrills als eigenständiger Workout-Typ, Zugfrequenz-Metriken, Intervalle in Bahnen (25m/50m).

#### CSS-Testprotokoll (Pflicht vor Zonenberechnung)
Die CSS wird aus zwei Zeitschwimm-Tests berechnet:
* **Testset:** 400 m Zeitschwimmen (t₄₀₀) + 5 min Pause + 200 m Zeitschwimmen (t₂₀₀), jeweils All-Out.
* **Formel:** `CSS = (400 − 200) / (t₄₀₀ − t₂₀₀)` → Ergebnis in m/s, umgerechnet in Pace pro 100 m.
* **Beispiel:** t₄₀₀ = 6:30 (390s), t₂₀₀ = 3:00 (180s) → CSS = 200 / 210 = 0.952 m/s → **1:45 min/100m**.
* **Re-Test-Empfehlung:** Alle 6–8 Wochen oder nach signifikantem Trainingsblock.

#### CSS-Zonenmodell
| Zone | Name | Pace (relativ zu CSS) | Einsatz |
|------|------|----------------------|---------|
| Z1 | Recovery | CSS + 15–20 s/100m | Aktive Erholung, Technikfokus |
| Z2 | Endurance | CSS + 5–10 s/100m | Aerobe Grundlage, hohe Umfänge |
| Z3 | Threshold (CSS) | CSS ± 3 s/100m | Schwellenarbeit, Haupttraining |
| Z4 | VO2max | CSS − 5–10 s/100m | Kurze Intervalle (100–200 m) |
| Z5 | Sprint | CSS − 15+ s/100m | Maximalsprints (25–50 m) |

#### Trainingskomponenten (Workout-Typen)
Schwimmtraining besteht aus mehr als reinem Kraul-Schwimmen. Folgende Workout-Typen werden als eigenständige Einheiten oder Blöcke innerhalb eines Workouts unterstützt:
* **Main Set:** Normales Schwimmen (Freestyle), Intervalle nach CSS-Zonen.
* **Pull-Set:** Mit Pullbuoy (Oberkörper-Fokus, Beinauftrieb eliminiert). Typisch 20–30 % des Trainingsumfangs.
* **Kick-Set:** Mit Schwimmbrett (Beinarbeit isoliert). Typisch 10–15 % des Trainingsumfangs.
* **Paddles-Set:** Mit Handpaddles (Kraftentwicklung Oberkörper). Erst ab fortgeschrittenem Niveau, max. 20 % des Umfangs.
* **Technik-Drills:** Einarmig, Catch-Up, Fingertip-Drag, Sculling etc. Als strukturierte Liste in `structure_json`.
* **Abbildung in DB:** Über `structure_json`, z. B. `{"blocks": [{"type": "warmup", "meters": 400, "zone": "Z1"}, {"type": "pull", "meters": 600, "zone": "Z2"}, {"type": "main", "intervals": [{"reps": 8, "meters": 100, "zone": "Z4", "rest_sec": 20}]}, {"type": "kick", "meters": 200, "zone": "Z2"}]}`.

#### Effizienzmetrik SWOLF
* **Definition:** SWOLF = Zuganzahl + Zeit (in Sekunden) pro Bahn. Niedrigerer Wert = effizienter.
* **Tracking:** Als optionale Metrik in `actual_metrics_json` erfassbar: `{"swolf": 38, "stroke_count": 14, "time_per_25m": 24}`.
* **Verwendung:** Langzeit-Trendanalyse der Schwimmeffizienz, kein Steuerungs-Input für die Engine.

#### Open-Water-Spezifika (für `open_water_5k` / Triathlon-Schwimmen)
* **Sighting-Drills:** Periodisch in Main-Sets integriert (alle 6–8 Züge Kopf heben).
* **Drafting-Übungen:** Schwimmen im Windschatten als spezifischer Trainingsinhalt.
* **Neoprenanzug-Einfluss:** CSS im Neopren typisch 3–5 s/100m schneller — separate Zonen-Berechnung bei Bedarf.

### 🏆 Multi-Sport / Hybrid

| Ziel (`goal_type`) | Sportarten-Mix | Besonderheit |
|---------------------|---------------|-------------|
| `triathlon_sprint` | Swim + Bike + Run | 8–12 Wochen, Multi-Sport-Tage, kumulierte Ermüdung |
| `triathlon_olympic` | Swim + Bike + Run | 12–16 Wochen, asymmetrisches Tapering |
| `ironman_70.3` | Swim + Bike + Run | 16–24 Wochen, Brickworkouts |
| `ironman` | Swim + Bike + Run | 24–36 Wochen, strenge Periodisierung |
| `hyrox` | Run + Functional Fitness | 12–16 Wochen, Lauf-/Kraft-Wechsel |
| `obstacle_race` | Run + Kraft + Grip | 12–16 Wochen, gemischte Metrik |
| `duathlon` | Run + Bike | 10–16 Wochen |
| `swimrun` | Swim + Run | 12–20 Wochen |

Multi-Sport-Tage werden in der DB als mehrere Workouts am selben `date` mit unterschiedlichem `sport_type` abgebildet.

#### Cross-Sport-Belastungsäquivalenz (Unified Training Load)
Für die korrekte Steuerung kumulierter Ermüdung über Sportarten hinweg wird eine einheitliche Belastungswährung benötigt:
* **rTSS (Run Training Stress Score):** Berechnet aus Pace-zu-FTP-Ratio (oder HR-basiert bei fehlendem Pace-Sensor). Formel analog Cycling-TSS, wobei Functional Threshold Pace (FTPa) die Referenz bildet.
* **sTSS (Swim Training Stress Score):** Berechnet aus CSS-Ratio — `sTSS = (Dauer × (NP / CSS)² × 100) / 3600`.
* **bTSS (Bike Training Stress Score):** Identisch mit Coggan-TSS (siehe CyclingStrategy).
* **Gesamt-TSS (Kardiovaskulär vs. Strukturell):** Einfaches Aufsummieren reicht im Multi-Sport nicht, da Laufen hohe strukturelle/mechanische Ermüdung erzeugt, Rad/Schwimmen fast keine. Die Engine berechnet zwei getrennte Belastungsvektoren:
  1. *Metabolischer/Kardiovaskulärer Stress* (CTL auf Basis aller TSS)
  2. *Struktureller/Orthopädischer Load* (Stark gewichtet auf Lauf-km und Krafttraining)
* **Wöchentliche TSS-Caps:** Empfohlen nach Trainingsstatus (z. B. Sprint-Tri: 300–500 TSS/Woche, Ironman: 700–1200 TSS/Woche).

#### Sportarten-Priorisierung (Limiter-Konzept)
Nach Joe Friels *The Triathlete's Training Bible* wird die Disziplin identifiziert, die den grössten Performance-Engpass darstellt:
* **Onboarding-Frage:** „Welche Disziplin ist dein Limiter?" (oder Ableitung aus Benchmark-Zeiten).
* **Einfluss auf Volumenverteilung:** Der Limiter erhält proportional mehr Trainingsvolumen (z. B. 40 % Limiter, 35 % Stärke, 25 % dritte Disziplin).
* **Gespeichert in:** `plans.profile_json` als `{"limiter": "swim", "strength": "bike", "volume_split": [40, 35, 25]}`.

#### Transition-Training (T1 / T2)
* **T1 (Swim → Bike)** und **T2 (Bike → Run)** werden als eigenständige Workout-Komponenten in `structure_json` abgebildet.
* **Brick Workouts** enthalten T2-Blöcke: `{"brick": [{"sport": "cycling", "duration_min": 60, "zone": "Z3"}, {"transition": "T2", "target_min": 3}, {"sport": "running", "duration_min": 20, "zone": "E"}]}`.
* **Transition-Praxis:** Ab 8 Wochen vor dem Wettkampf mindestens 1× pro Woche ein Brick-Workout.

### 🧘 Ergänzende / Randkategorien

| Ziel | Machbarkeit | Anmerkung |
|------|-------------|-----------|
| Rudern (Ergometer/Wasser) | ✅ Gut | Ähnlich Radsport: Watt-basiert, Split-Zeiten |
| CrossFit / Functional Fitness | ⚠️ Eingeschränkt | WODs sind semi-randomisiert; Periodisierung möglich bei strukturierten Programmen |
| Yoga / Mobility | ⚠️ Eingeschränkt | Keine Volumen-Progression, aber als Recovery-Tag/Cross-Training integrierbar |
| Wandern / Bergsteigen | ✅ Gut | Höhenmeter + Zeit als Metrik, ähnlich Ultra |
| Kampfsport | ⚠️ Eingeschränkt | Ausdauer-/Kraft-Periodisierung ja, Techniktraining zu skill-basiert |

## 10. GUI-Vision (Flutter App — spätere Phase)

### Design-Philosophie
- **Minimalistisch-funktional:** Klarheit von Things 3, Fokussiertheit von Nike Run Club, Tiefe von TrainingPeaks.
- **Dark Mode first** (Sportler schauen oft morgens früh oder abends auf den Plan).
- **Conversational Check-in** als zentrales UX-Element: Der Benutzer *redet* mit der App statt Formulare auszufüllen.
- **Kein Gamification:** Keine Badges, Streaks oder Social-Vergleich. Die App ist ein ruhiger, kompetenter Coach — kein Fitness-Influencer.

### Farbschema & Visuelles System
- **Primärfarbe:** Kräftiges Electric Blue oder Teal — suggeriert Dynamik und Technologie.
- **Akzente pro Sportart:** Jede Sportart hat eine subtile Akzentfarbe (Laufen = Orange, Rad = Grün, Kraft = Rot, Schwimmen = Cyan). Diese färbt Workout-Karten, Zonen-Badges und Graphen.
- **Typografie:** Klar, serifenlos, mit monospaced Pace-/Watt-Werten (ähnlich Strava).

### Screens

#### Screen 1: Dashboard / Heute
Der erste Screen nach dem Öffnen. Zeigt den heutigen Tag im Kontext der Woche.
- **Oben:** Kompakte Statusleiste — aktuelle Trainingsphase (z. B. "Build Phase · Woche 8/24"), Wochen-Fortschrittsring (3 von 4 Workouts erledigt).
- **Mitte: Heutige Workout-Karte** (prominentestes Element):
  - Workout-Typ-Icon + Name ("Tempo Run")
  - Zielwerte: "12.0 km · Zone T · 04:55–05:05 min/km"
  - Grosser "Check-in"-Button
  - Falls Ruhetag: beruhigende Anzeige ("Regeneration 🌿 · Nächstes Workout: morgen")
- **Darunter:** Mini-Wochenansicht — 7 kleine Kreise (Mo–So), farbig nach Status:
  - Ausgefüllt grün = erledigt, orange = teilweise, Umriss = geplant, grau = Ruhetag, rot pulsierend = Zwangspause (SICK/PAIN).
- **Unten:** Optionaler Insight-Streifen: "Dein Wochenvolumen: 42 von 48 km (88 %)" oder "RPE-Trend: stabil bei 5.2".

#### Screen 2: Conversational Check-in
Öffnet sich nach Tippen auf den Check-in-Button. Das Herzstück der App.
- **Chat-Interface:** Sieht aus wie ein Messenger. Oben steht das geplante Workout als Kontext-Karte.
- Der Benutzer tippt Freitext: *"Heute 10 km gelaufen, letzter Kilometer war zäh. Leichtes Ziehen im linken Knie, RPE so 7."*
- Die App zeigt nach dem LLM-Parsing eine **strukturierte Zusammenfassung** als Antwort-Bubble:
  - ✅ 10.0 km (Ziel: 12.0 km) → Partial
  - 💪 RPE: 7
  - ⚠️ Knie links · leichter Schmerz · Schweregrad 3
- **"Stimmt das so?"** mit Bestätigen / Korrigieren-Buttons.
- Nach Bestätigung: Wenn Mutationen ausgelöst werden, freundliche Erklärung:
  - *"Wegen des Knieschmerzes habe ich dein Tempo-Workout am Donnerstag in einen Ruhetag umgewandelt."*
  - Vorher-/Nachher-Vergleich als Karten-Carousel.

#### Screen 3: Wochenplan
Kalenderartige Übersicht der aktuellen und nächsten Wochen.
- **Horizontales Scrolling** durch die Wochen (swipe links/rechts).
- Jeder Tag ist eine Karte mit: Workout-Typ (Icon + Name), Metriken ("18 km · Easy"), Status-Badge.
- Aktuelle Woche prominent, vergangene leicht ausgegraut.
- Am oberen Rand: Phase-Label + Mesozyklus-Indikator ("Woche 3 von 4 vor Deload").

#### Screen 4: Planübersicht (Makrozyklus)
Bird's-Eye-View des gesamten Trainingsplans.
- **Vertikale Zeitleiste** mit farbigen Phasen-Blöcken: Base (Blau) → Build (Orange) → Peak (Rot) → Taper (Grün) → Race Day (Gold-Stern).
- Jede Woche ist eine horizontale Zeile mit: Wochennummer + Datum, Volumen-Balken, Recovery-Markierung.
- Aktueller Standort durch "Du bist hier"-Marker hervorgehoben.
- Volumen-Kurve als dezenter Graph über dem Ganzen (Wellenbewegung der Mesozyklen).

#### Screen 5: Profil & Zonen
Einstellungen und Leistungsdaten des Benutzers.
- Persönliche Daten: Alter, Gewicht, Ruhepuls, Max-Puls.
- **Biomarker & Zyklen:** Option für Female Cycle Tracking (passt Engine-Sensitivität für RPE/HR an).
- Leistungswerte pro Sportart: VDOT (Laufen), FTP (Rad), CSS (Schwimmen), 1RM-Werte (Kraft).
- Historischer Verlauf: Graphische Darstellung der Baseline-Werte über die Zeit (z.B. FTP-Entwicklung).
- Berechnete Zonen als Tabelle (Zonenname, Pace-/Watt-Bereich, HR-Bereich).
- Button "Zonen neu berechnen" (nach neuem Wettkampf/Test).
- Aktive Pläne: Liste mit Sport-Icon, Zielname und Countdown ("Marathon Zürich · noch 16 Wochen").

#### Screen 6: Onboarding-Flow
Erstmaliges Einrichten oder neuen Plan erstellen.
- **Conversational statt Formular:** Benutzer beschreibt Ziel in Freitext, App parsed via LLM und zeigt Zusammenfassung zur Bestätigung.
- Falls LLM offline: Fallback auf geführte Einzelabfragen (Dropdown für Sportart, Slider für Volumen etc.).
- Am Ende: Animation, der Plan "baut sich auf" (Wochen fliegen als Karten rein).

#### Screen 7: Status-Updates & Genesungs-Tracker
Für Phasen abseits normaler Check-ins.
- **"Wie geht's dir?"-Button** auf dem Dashboard (immer sichtbar).
- Bei Krankheit: App aktiviert SICK-Modus, zeigt Timeline ("Krank seit Mo, 15.09. · Tag 3"), pausiert alle Workouts.
- Bei Genesungsmeldung: App zeigt Rückkehrplan ("Erstes Workout auf 50 % reduziert, kein Tempo bis Freitag").
- Schmerztagebuch: Täglicher Kurz-Check "Wie ist das Knie heute? (1–10)" mit Verlaufsgraph.
- Readiness-Update: Erfassung von Schlafqualität, Stress oder Jetlag.

#### Screen 8: Statistiken & Trends
Langfristige Auswertung.
- **Performance Management Chart (PMC):** Das Herzstück für Multi-Sport. Liniendiagramme für **CTL** (Chronic Training Load / Fitness, blau), **ATL** (Acute Training Load / Fatigue, pink) und **TSB** (Training Stress Balance / Form, gelb).
- Wochenvolumen-Graph: Balkendiagramm Soll vs. Ist über den Planverlauf, inkl. TSS-Erreichung.
- RPE-Trend: Liniengraph der durchschnittlichen RPE pro Woche (Frühwarnung bei steigendem Trend).
- Consistency Score: Prozent der absolvierten Workouts (motivierend, aber ohne Gamification-Druck).
- Symptom-History: Zeitstrahl mit Schmerz-/Krankheitsereignissen.

### Navigation & UX-Prinzipien
- **Bottom Tab Bar:** 4 Tabs — Heute · Plan · Statistiken · Profil.
- **Floating Action Button:** "Neuer Check-in" (immer erreichbar, auch für spontane Aktivitäten).
- **Pull-to-Refresh** auf dem Dashboard aktualisiert Mutationen.
- **Notifications (optional):** Sanfte Erinnerung am Trainingsmorgen ("Heute steht ein Tempo-Lauf an: 10 km · Zone T"). Keine Push-Notification-Flut.

