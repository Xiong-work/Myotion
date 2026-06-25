# Myotion

Myotion is a desktop application for biomechanics and movement analysis, with emphasis on EMG processing, kinematic analysis, 3D visualization, and interactive plotting. It is designed for research and clinical workflows.

---

## Setup

**Requirements:** Windows, Anaconda

1. Clone the repository.
2. Open an Anaconda PowerShell and run:

```console
cd %PATH_OF_THIS_PROJECT%
conda env create --name accmov --file=accmov-win.yaml
```

---

## Run

```console
conda activate accmov
python main.py
```

---

## Key Features

- **Data import** — C3D and MAT file support, including combined kinematics + EMG trials
- **EMG analysis** — preprocessing, filtering, rectification, envelope, normalization, segmentation, frequency analysis
- **Kinematic analysis** — trial inspection, event creation/editing, parameter calculation
- **3D visualization** — marker trajectories, body rendering, force plate display
- **Interactive plotting** — embedded R Shiny server for flexible data visualization
- **Structured workflows** — explicit crop/event state shared between kinematics and EMG

---

## Project Structure

```text
Myotion/
├── main.py                  # Application entry point
├── rserver.py               # Embedded R Shiny server manager
├── configuration.ui         # Qt Designer: configuration dialog
├── emg_config.ui            # Qt Designer: EMG config dialog
├── emg_import.ui            # Qt Designer: EMG import dialog
├── main.ui / login.ui       # Qt Designer: main window / login
├── accmov-win.yaml          # Conda environment (Windows)
│
├── modules/
│   ├── app_functions.py     # App logic and GUI actions
│   ├── app_settings.py      # App settings and configuration
│   ├── ui_main.py           # Main window UI wiring
│   ├── ui_functions.py      # Shared UI utility functions
│   ├── ui_configuration.py  # Configuration dialog logic
│   ├── ui_emg_config.py     # EMG config dialog logic
│   ├── emg_import.py        # EMG import workflow
│   │
│   ├── pyMotion/            # Data processing engine
│   │   └── core/
│   │       ├── trial.py         # Trial data model
│   │       ├── workspace.py     # Workspace/session management
│   │       ├── emg.py           # EMG processing logic
│   │       ├── kinematic.py     # Kinematic data handling
│   │       ├── c3d.py           # C3D file parser
│   │       ├── mat.py           # MAT file parser
│   │       ├── xml.py           # XML file parser
│   │       ├── freq_analysis.py # Frequency-domain analysis
│   │       ├── advance_analysis.py
│   │       ├── statistic.py
│   │       ├── report.py
│   │       └── ...
│   │
│   └── kinematics/          # Trial viewer and 3D rendering
│       ├── renderwidget.py  # OpenGL render widget
│       ├── renderer.py      # Scene renderer
│       ├── bodyrender.py    # Body/marker rendering
│       ├── playbarwidget.py # Playback controls
│       ├── controller.py    # Viewer interaction
│       └── ...
│
├── widgets/                 # Custom Qt widgets
│   ├── emg_pipeline_panel.py
│   ├── custometreewidget.py
│   └── ...
│
├── themes/                  # Qt stylesheets and theming
├── images/                  # Icons and graphical assets
├── shiny/                   # R Shiny app for interactive plots
├── test/                    # Test scripts and sample data
└── script/                  # Dev utilities (translations, releases)
```

---

## Architecture Overview

### Kinematics workflow

Acts as the trial-level inspection surface (Mokka-like). Handles trial loading, synchronized marker/EMG/force-plate visibility, event creation/editing, and local filtering/plotting.

### EMG workflow

Focused on preprocessing, visualization, time-domain and frequency-domain analysis, and downstream metrics. Consumes crop/event metadata from the kinematics workflow when available.

### Shared trial state

Minimal and explicit: trial identity, sync relationship, event list, crop intervals, sampling metadata. No hidden coupling between modules.
