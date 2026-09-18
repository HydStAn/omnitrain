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
3. **Sportwissenschaftliche Sicherheitsregeln:**
   - Verpasste Einheiten/Volumina werden niemals auf Folgetage aufgeschlagen.
   - Übererfüllung führt nicht zur automatischen Steigerung der Folgetage (Überlastungsschutz).
   - Gelenk-/Sehnenschmerz oder Krankheit erzwingen softwareseitig Ruhe- oder Entlastungstage.

```
┌─────────────────────────────────────────────────────────┐
│ 1. LLM Semantic Parser                                  │
│    - Übersetzt Freitext in generisches JSON             │
│      (Status, RPE, Symptome, Metriken)                  │
└────────────────────────────┬────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────┐
│ 2. Sport Engine Interface (Strategy Pattern)            │
└────────────┬───────────────────────────────┬────────────┘
             │                               │
             ▼                               ▼
┌──────────────────────────┐    ┌─────────────────────────┐
│ RunningStrategy          │    │ CyclingStrategy / ...   │
│ - Daniels VDOT           │    │ - Coggan TSS / FTP      │
│ - Pfitzinger Mileage     │    │ - Power Zones (Z1-Z7)   │
│ - Km-basierte Progression│    │ - Watt-/Zeit-Regeln     │
└──────────────────────────┘    └─────────────────────────┘
             │                               │
             └───────────────┬───────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────┐
│ 3. Generisches SQLite Schema                            │
│    (Plans, Weeks, Workouts, Checkin-Logs, Mutationen)   │
└─────────────────────────────────────────────────────────┘
```

## 3. Generisches Datenmodell (SQLite Schema)

* **plans**:
  * id (TEXT/UUID, PK)
  * sport_type (TEXT, z. B. 'running', 'cycling', 'strength')
  * goal_type (TEXT, z. B. 'marathon', '5k', 'ftp_builder')
  * target_date (TEXT / ISO8601)
  * base_weekly_volume (REAL, z. B. 25.0 für Lauf-km, 180 für Rad-min)
  * sessions_per_week (INTEGER, z. B. 4)
  * key_workout_day (INTEGER, 1=Mo ... 7=So, z. B. Longrun-Tag)
  * status (TEXT: 'active', 'completed', 'aborted')
* **weeks**:
  * id (TEXT/UUID, PK)
  * plan_id (FK -> plans.id)
  * week_number (INTEGER, 1..N)
  * phase (TEXT: 'base', 'build', 'peak', 'taper')
  * target_weekly_volume (REAL)
  * is_recovery_week (BOOLEAN)
* **workouts**:
  * id (TEXT/UUID, PK)
  * week_id (FK -> weeks.id)
  * date (TEXT / ISO8601)
  * workout_type (TEXT: 'easy', 'long_run', 'tempo', 'interval', 'rest')
  * metric_primary (REAL, z. B. 18.0 für km, 120 für Minuten, 5 für Sets)
  * metric_unit (TEXT, z. B. 'km', 'min', 'reps', 'tss')
  * intensity_target (TEXT, nullable, z. B. '05:30 min/km', '220 W', 'RPE 8')
  * target_duration_min (INTEGER, nullable)
  * structure_json (TEXT, nullable für Intervall-Sets, Übungslisten)
  * status (TEXT: 'planned', 'completed', 'partial', 'skipped', 'modified')
* **checkin_logs**:
  * id (TEXT/UUID, PK)
  * workout_id (FK -> workouts.id, nullable)
  * timestamp (TEXT / ISO8601)
  * raw_input (TEXT)
  * completion_status (TEXT: 'completed', 'partial', 'skipped', 'sick')
  * actual_metrics_json (TEXT, nullable, z. B. `{"metric_primary": 12.0, "unit": "km"}`)
  * perceived_rpe (INTEGER, 1..10, nullable)
  * symptoms_json (TEXT)
  * applied_mutations_json (TEXT)

## 4. LLM-Schnittstelle & JSON-Kontrakte

### A. Onboarding / Goal Parser
* **System-Prompt Ziel:** Extrahiert Zielparameter aus Freitext.
* **JSON-Schema:**
```json
{
  "sport_type": "running",
  "target_event": "marathon",
  "target_weeks": 24,
  "current_weekly_volume": 25.0,
  "volume_unit": "km",
  "sessions_per_week": 4,
  "preferred_long_run_day": 7,
  "fitness_level": "beginner"
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

## 5. Deterministische Logik & RunningStrategy (Referenz-Implementierung)

### A. Initialer Plan-Generator (RunningStrategy)
* **Volumen-Progression:** Steigerung des wöchentlichen Volumens um maximal 8–10 % gegenüber der Vorwoche.
* **Mesozyklus-Muster:** 3 Wochen progressive Steigerung, 1 Woche Entlastung (-25 % Volumen).
* **Tapering:** Letzte 3 Wochen vor dem Wettkampf: -20 %, -40 %, -60 % des Peak-Volumens bei Beibehaltung kurzer Reize.
* **Wochen-Aufteilung (Beispiel 4 Tage Laufen):**
  * Tag 1 (Di): Easy Run (20 % Wochenumfang)
  * Tag 2 (Do): Tempo / Schwellenlauf (20 % Wochenumfang)
  * Tag 3 (Sa): Easy Run (25 % Wochenumfang)
  * Tag 4 (So): Longrun (35 % Wochenumfang, maximal 32–34 km)

### B. Reconcile- & Mutations-Regelwerk (Invarianten)
* **Status SICK:**
  * Sofortige Umwandlung der nächsten N Tage in `rest`.
  * Wiedereinstieg nach Genesung: Erstes Training bei maximal 50 % des geplanten Volumens, kein Tempotraining in den ersten 4 Tagen nach Genesung.
* **Symptom PAIN (Gelenk, Sehne, Fuß, Schienbein):**
  * Mindestens 48–72 Stunden zwingende Trainingspause (Umwandlung in `rest`).
  * Streichung von Tempo-/Intervalltraining für die laufende Woche.
* **Status PARTIAL / MISSED:**
  * Verpasste Einheiten/Volumina verfallen ersatzlos.
  * Kein Erhöhen der Folgetage oder des nächsten Longruns.
* **Status OVERPERFORMED (mehr Distanz / Wiederholungen):**
  * Keine automatische Steigerung der Folgetage; Plan läuft unverändert weiter, um Überlastung zu verhindern.
* **RPE-Überwachung:**
  * Wenn bei einem `easy`-Workout RPE >= 8 gemeldet wird: Reduktion des Volumens der Folgewoche um 15 %.

## 6. Ziel dieser Phase & Deliverables
* **Headless CLI-Applikation:** Keine UI erforderlich.
* **Generierung:** Initiales Erzeugen und persistentes Abspeichern eines 24-Wochen-Plans in SQLite via `RunningStrategy`.
* **Interaktive Schleife:**
  * Konsolenausgabe des aktuellen Plans / der nächsten Einheiten.
  * Eingabeaufforderung für Freitext-Logs.
  * Aufruf des lokalen LLM zur Extraktion des generischen JSON-Check-ins.
  * Anwendung der Mutationsregeln auf die SQLite-Datenbank.
  * Vorher-/Nachher-Anzeige der modifizierten Trainingstage im Terminal.

## 7. Erweiterbarkeit: Andere Laufziele & Distanzen (Multi-Goal)
Die `RunningStrategy` unterstützt über austauschbare Ziel-Profile (`GoalProfile`) beliebige Laufziele:
* `5k`: Fokus auf $v\text{VO}_2\text{max}$-Intervalle, Longrun-Deckelung bei 12–16 km, Tapering 7–10 Tage.
* `10k`: Fokus auf Schwellenläufe (Threshold) und Laktattoleranz, Longrun-Deckelung bei 14–20 km, Tapering 10–14 Tage.
* `half_marathon`: Fokus auf Laktatschwelle und progressive Dauerläufe, Longrun-Deckelung bei 18–24 km, Tapering 14 Tage.
* `marathon`: Fokus auf aerobe Effizienz und Glykogenökonomie, Longrun-Deckelung bei 32–34 km, Tapering 21 Tage (3 Wochen).
* `base_building`: Zyklisches Grundlagentraining ohne Tapering/Wettkampfpeak.

## 8. Abstraktion & Multi-Sport-Fähigkeit (Cross-Sport Architecture)
Das Kernprinzip trennt Daten- und Prompt-Modell von der Rechen-Engine:
1. **Gemeinsamkeiten aller Sportarten:**
   * Schutz- und Regenerationslogik: Krankheit (`SICK`) erzwingt Pause und degressiven Wiedereinstieg; Schmerz (`PAIN`) verlangt Deload der betroffenen Struktur.
   * Keine Kompensation verpasster Einheiten durch Überlastung an Folgetagen.
   * Periodisierung in Mesozyklen (typischerweise 3:1 oder 4:1 Belastung zu Entlastung).
2. **Sportartspezifische Belastungsmetriken:**
   * **Laufsport:** Distanz (`km`), Pace, RPE (`RunningStrategy`).
   * **Radsport:** Zeit (`min`), Leistung (`Watt`, `% FTP`), TSS (`CyclingStrategy`).
   * **Kraftsport (Hypertrophie / Powerlifting):** Sätze, Wiederholungen, Last (`kg`), RIR / RPE (`StrengthStrategy`).
   * **Schwimmen:** Distanz (`m`), Intervalle, CSS (`SwimStrategy`).
   * **Triathlon / Hyrox:** Kumuliertes Multi-Disziplin-Belastungsmanagement.
3. **Zukunftssicheres Datenmodell (Polymorphe Workouts):**
   * Pläne und Workouts unterstützen `sport_type`, `metric_primary`, `metric_unit` und `intensity_target`.
   * Detail-Parameter für Nicht-Lauf-Disziplinen werden über flexible JSON-Payloads (`structure_json` / `actual_metrics_json`) abgebildet, ohne das Tabellenschema brechen zu müssen.
