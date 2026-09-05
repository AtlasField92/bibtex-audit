#!/usr/bin/env python3
"""BibAudit GUI: Modern graphical user interface for auditing and reviewing BibTeX bibliographies.

Features:
- 3 execution modes: Full Automatic, Hybrid (User Feedback), Full Manual.
- Fully configurable sensitivity sliders and weights with quick presets.
- Interactive review hub with side-by-side comparative inspector (Original vs Candidate).
- Direct actions: Accept, Manually edit, Keep original, Live online search.
- BibTeX snippet preview and before/after diffs.
- Multithreaded execution with progress bar and live log console.
- Persistent decision saving and resume support.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from dataclasses import asdict
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional, Set

# Import audit engine
from bib_audit_engine import (
    DEFAULT_TIMEOUT,
    VERSION,
    AuditSettings,
    Candidate,
    DecisionStore,
    ExecutionMode,
    ReportRow,
    ScholarClient,
    SensitivitySettings,
    acceptance_reason,
    apply_candidate,
    clean_doi,
    export_bibtex_file,
    format_bibtex_authors,
    format_single_bibtex_entry,
    make_row,
    normalize,
    parse_bibtex_file,
    process_entry,
    rank_candidates,
    score_candidate,
    valid_doi,
    write_reports,
)


class BibAuditApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"BibAudit v{VERSION} — BibTeX Bibliography Audit & Review")
        self.root.geometry("1280x850")
        self.root.minsize(1050, 700)

        # Settings & state
        self.settings = AuditSettings()
        self.client: Optional[ScholarClient] = None
        self.decisions = DecisionStore(self.settings.decisions_file)
        self.database: Optional[Any] = None
        self.entries: List[Dict[str, Any]] = []
        self.used_keys: Set[str] = set()
        self.rows: List[ReportRow] = []
        self.rows_by_key: Dict[str, ReportRow] = {}

        # Threading & event management
        self.event_queue: queue.Queue = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()  # Initialized to unpaused
        self.feedback_event = threading.Event()
        self.current_feedback_decision: Optional[Dict[str, Any]] = None
        self.is_running = False

        # Configure ttk styles
        self._setup_styles()

        # Build UI
        self._build_ui()

        # Start event loop polling
        self.root.after(100, self._process_event_queue)

        # Attempt auto-detection of default files
        self._auto_detect_default_files()

    # ========================================================================
    # Styles & Theme
    # ========================================================================

    def _setup_styles(self) -> None:
        self.style = ttk.Style()
        try:
            self.style.theme_use("clam")
        except Exception:
            pass

        # Fonts
        self.font_title = ("Helvetica", 14, "bold")
        self.font_subtitle = ("Helvetica", 11, "bold")
        self.font_bold = ("Helvetica", 10, "bold")
        self.font_normal = ("Helvetica", 10)
        self.font_small = ("Helvetica", 9)
        self.font_mono = ("Menlo" if sys.platform == "darwin" else "Consolas", 10)

        # Palette
        self.color_primary = "#1976D2"
        self.color_success = "#2E7D32"
        self.color_warning = "#EF6C00"
        self.color_danger = "#C62828"
        self.color_info = "#0288D1"
        self.color_neutral = "#546E7A"
        self.color_bg_card = "#F8F9FA"

        self.style.configure(".", font=self.font_normal)
        self.style.configure("TNotebook", tabposition="n")
        self.style.configure("TNotebook.Tab", padding=(12, 6), font=self.font_bold)
        self.style.configure("Primary.TButton", font=self.font_bold, foreground="#1976D2")
        self.style.configure("Success.TButton", font=self.font_bold, foreground="#2E7D32")
        self.style.configure("Danger.TButton", font=self.font_bold, foreground="#C62828")
        self.style.configure("Support.TButton", font=self.font_bold, foreground="#D97706")
        self.style.configure("Header.TLabel", font=self.font_title, foreground="#263238")
        self.style.configure("Subheader.TLabel", font=self.font_subtitle, foreground="#37474F")
        self.style.configure("Card.TLabelframe", background=self.color_bg_card)
        self.style.configure("Card.TLabelframe.Label", font=self.font_subtitle, foreground="#1976D2")

    # ========================================================================
    # User Interface Construction
    # ========================================================================

    def _build_ui(self) -> None:
        main_container = ttk.Frame(self.root, padding=8)
        main_container.pack(fill=tk.BOTH, expand=True)

        # 1. Top bar: Files, Modes & Run Controls
        self._build_top_bar(main_container)

        # 2. Progress bar & quick KPI counters
        self._build_kpi_bar(main_container)

        # 3. Main tabbed notebook
        self.notebook = ttk.Notebook(main_container)
        self.notebook.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

        # Tab 1: Interactive Review & Decisions
        self.tab_review = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(self.tab_review, text=" 📋 Interactive Review & Decisions ")
        self._build_tab_review(self.tab_review)

        # Tab 2: Settings & Sensitivity Sliders
        self.tab_settings = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(self.tab_settings, text=" ⚙️ Settings & Sensitivity Sliders ")
        self._build_tab_settings(self.tab_settings)

        # Tab 3: BibTeX Preview & Diff
        self.tab_diff = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(self.tab_diff, text=" 🔍 BibTeX Preview & Diff ")
        self._build_tab_diff(self.tab_diff)

        # Tab 4: Console & Audit Logs
        self.tab_console = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(self.tab_console, text=" 📜 Console & Logs ")
        self._build_tab_console(self.tab_console)

        # 4. Bottom status bar
        self.status_var = tk.StringVar(value="Ready. Select a BibTeX file to begin.")
        status_bar = ttk.Label(main_container, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W, font=self.font_small)
        status_bar.pack(fill=tk.X, side=tk.BOTTOM, pady=(4, 0))

    def _build_top_bar(self, parent: ttk.Frame) -> None:
        top_frame = ttk.LabelFrame(parent, text=" 📂 Files & Execution Controls ", style="Card.TLabelframe", padding=8)
        top_frame.pack(fill=tk.X, pady=(0, 4))

        # Row 1: Source and Output File Pickers
        row1 = ttk.Frame(top_frame)
        row1.pack(fill=tk.X, pady=2)

        ttk.Label(row1, text="Source BibTeX:", font=self.font_bold).pack(side=tk.LEFT, padx=(0, 4))
        self.input_path_var = tk.StringVar()
        self.input_entry = ttk.Entry(row1, textvariable=self.input_path_var, width=38)
        self.input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        ttk.Button(row1, text="Browse...", command=self._browse_input_file).pack(side=tk.LEFT, padx=2)
        ttk.Button(row1, text="Load", command=self._load_bibtex_file).pack(side=tk.LEFT, padx=4)

        ttk.Label(row1, text="Output .bib:", font=self.font_bold).pack(side=tk.LEFT, padx=(12, 4))
        self.output_path_var = tk.StringVar(value="bib-verified.bib")
        self.output_entry = ttk.Entry(row1, textvariable=self.output_path_var, width=28)
        self.output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        ttk.Button(row1, text="Browse...", command=self._browse_output_file).pack(side=tk.LEFT, padx=2)

        # Row 2: Execution Mode & Actions
        row2 = ttk.Frame(top_frame)
        row2.pack(fill=tk.X, pady=(6, 2))

        ttk.Label(row2, text="Audit mode:", font=self.font_bold).pack(side=tk.LEFT, padx=(0, 6))

        self.mode_var = tk.StringVar(value=ExecutionMode.HYBRID.value)
        r_auto = ttk.Radiobutton(row2, text="⚡ Full Automatic", variable=self.mode_var, value=ExecutionMode.AUTO.value, command=self._on_mode_change)
        r_auto.pack(side=tk.LEFT, padx=4)
        r_hybrid = ttk.Radiobutton(row2, text="🔀 Hybrid (User Feedback)", variable=self.mode_var, value=ExecutionMode.HYBRID.value, command=self._on_mode_change)
        r_hybrid.pack(side=tk.LEFT, padx=4)
        r_manual = ttk.Radiobutton(row2, text="👤 Full Manual", variable=self.mode_var, value=ExecutionMode.MANUAL.value, command=self._on_mode_change)
        r_manual.pack(side=tk.LEFT, padx=4)

        # Action buttons
        btn_frame = ttk.Frame(row2)
        btn_frame.pack(side=tk.RIGHT)

        self.btn_start = ttk.Button(btn_frame, text="🚀 Start Audit", style="Primary.TButton", command=self._start_audit)
        self.btn_start.pack(side=tk.LEFT, padx=4)

        self.btn_pause = ttk.Button(btn_frame, text="⏸️ Pause", state=tk.DISABLED, command=self._toggle_pause)
        self.btn_pause.pack(side=tk.LEFT, padx=4)

        self.btn_stop = ttk.Button(btn_frame, text="⏹️ Stop", style="Danger.TButton", state=tk.DISABLED, command=self._stop_audit)
        self.btn_stop.pack(side=tk.LEFT, padx=4)

        self.btn_save = ttk.Button(btn_frame, text="💾 Save All", style="Success.TButton", command=self._save_all_outputs)
        self.btn_save.pack(side=tk.LEFT, padx=4)

        self.btn_support = ttk.Button(btn_frame, text="☕ Support me", style="Support.TButton", command=self._open_support_url)
        self.btn_support.pack(side=tk.LEFT, padx=4)

    def _build_kpi_bar(self, parent: ttk.Frame) -> None:
        kpi_frame = ttk.Frame(parent, padding=(0, 2))
        kpi_frame.pack(fill=tk.X, pady=2)

        # Progress
        prog_box = ttk.Frame(kpi_frame)
        prog_box.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))

        self.prog_label_var = tk.StringVar(value="Progress: 0 / 0 (0%)")
        ttk.Label(prog_box, textvariable=self.prog_label_var, font=self.font_bold).pack(anchor=tk.W)

        self.progress_bar = ttk.Progressbar(prog_box, mode="determinate", length=200)
        self.progress_bar.pack(fill=tk.X, expand=True, pady=(2, 0))

        # KPI Counters
        self.kpi_vars = {
            "total": tk.StringVar(value="Total: 0"),
            "verified": tk.StringVar(value="Verified: 0"),
            "corrected": tk.StringVar(value="Corrected: 0"),
            "to_review": tk.StringVar(value="To review: 0"),
            "not_found": tk.StringVar(value="Not found: 0"),
            "error": tk.StringVar(value="Error: 0"),
        }

        kpis_box = ttk.Frame(kpi_frame)
        kpis_box.pack(side=tk.RIGHT)

        ttk.Label(kpis_box, textvariable=self.kpi_vars["total"], font=self.font_bold, foreground=self.color_neutral).pack(side=tk.LEFT, padx=6)
        ttk.Label(kpis_box, textvariable=self.kpi_vars["verified"], font=self.font_bold, foreground=self.color_success).pack(side=tk.LEFT, padx=6)
        ttk.Label(kpis_box, textvariable=self.kpi_vars["corrected"], font=self.font_bold, foreground=self.color_primary).pack(side=tk.LEFT, padx=6)
        ttk.Label(kpis_box, textvariable=self.kpi_vars["to_review"], font=self.font_bold, foreground=self.color_warning).pack(side=tk.LEFT, padx=6)
        ttk.Label(kpis_box, textvariable=self.kpi_vars["not_found"], font=self.font_bold, foreground="#757575").pack(side=tk.LEFT, padx=6)
        ttk.Label(kpis_box, textvariable=self.kpi_vars["error"], font=self.font_bold, foreground=self.color_danger).pack(side=tk.LEFT, padx=6)

    # ========================================================================
    # Tab 1: Interactive Review & Decisions
    # ========================================================================

    def _build_tab_review(self, parent: ttk.Frame) -> None:
        # Filter and search bar
        filter_frame = ttk.Frame(parent, padding=(0, 2, 0, 6))
        filter_frame.pack(fill=tk.X)

        ttk.Label(filter_frame, text="🔍 Filter:", font=self.font_bold).pack(side=tk.LEFT, padx=(0, 4))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *args: self._apply_table_filters())
        search_entry = ttk.Entry(filter_frame, textvariable=self.search_var, width=28)
        search_entry.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Label(filter_frame, text="Status:", font=self.font_bold).pack(side=tk.LEFT, padx=(0, 4))
        self.status_filter_var = tk.StringVar(value="All")
        status_combobox = ttk.Combobox(
            filter_frame,
            textvariable=self.status_filter_var,
            values=["All", "To review", "Not found", "Corrected", "Verified", "Error"],
            state="readonly",
            width=14,
        )
        status_combobox.pack(side=tk.LEFT, padx=4)
        status_combobox.bind("<<ComboboxSelected>>", lambda e: self._apply_table_filters())

        ttk.Button(filter_frame, text="Clear filter", command=self._clear_filters).pack(side=tk.LEFT, padx=6)

        # Split pane: Treeview on left, Comparative Inspector on right
        paned = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        # Left: Entries Treeview
        left_box = ttk.Frame(paned, padding=(0, 0, 4, 0))
        paned.add(left_box, weight=3)

        tree_scroll_y = ttk.Scrollbar(left_box, orient=tk.VERTICAL)
        tree_scroll_x = ttk.Scrollbar(left_box, orient=tk.HORIZONTAL)

        columns = ("idx", "key", "status", "score", "orig_title", "res_title")
        self.tree = ttk.Treeview(
            left_box,
            columns=columns,
            show="headings",
            yscrollcommand=tree_scroll_y.set,
            xscrollcommand=tree_scroll_x.set,
            selectmode="browse",
        )
        tree_scroll_y.config(command=self.tree.yview)
        tree_scroll_x.config(command=self.tree.xview)

        self.tree.heading("idx", text="#")
        self.tree.heading("key", text="BibTeX Key")
        self.tree.heading("status", text="Status")
        self.tree.heading("score", text="Score")
        self.tree.heading("orig_title", text="Original Title")
        self.tree.heading("res_title", text="Candidate / Resolved Title")

        self.tree.column("idx", width=40, minwidth=30, anchor=tk.CENTER)
        self.tree.column("key", width=140, minwidth=100)
        self.tree.column("status", width=90, minwidth=80, anchor=tk.CENTER)
        self.tree.column("score", width=60, minwidth=50, anchor=tk.CENTER)
        self.tree.column("orig_title", width=220, minwidth=150)
        self.tree.column("res_title", width=220, minwidth=150)

        # Color tags for statuses
        self.tree.tag_configure("verified", foreground=self.color_success)
        self.tree.tag_configure("corrected", foreground=self.color_primary)
        self.tree.tag_configure("to_review", foreground=self.color_warning)
        self.tree.tag_configure("not_found", foreground="#757575")
        self.tree.tag_configure("error", foreground=self.color_danger)
        self.tree.tag_configure("in_progress", foreground="#8E24AA")

        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll_y.grid(row=0, column=1, sticky="ns")
        tree_scroll_x.grid(row=1, column=0, sticky="ew")
        left_box.rowconfigure(0, weight=1)
        left_box.columnconfigure(0, weight=1)

        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # Right: Inspector & Decision panel
        right_box = ttk.Frame(paned, padding=(4, 0, 0, 0))
        paned.add(right_box, weight=4)

        self._build_inspector_panel(right_box)

    def _build_inspector_panel(self, parent: ttk.Frame) -> None:
        header_frame = ttk.Frame(parent)
        header_frame.pack(fill=tk.X, pady=(0, 4))

        self.insp_title_var = tk.StringVar(value="Select an entry in the table")
        ttk.Label(header_frame, textvariable=self.insp_title_var, font=self.font_subtitle, foreground=self.color_primary).pack(side=tk.LEFT)

        self.insp_status_var = tk.StringVar()
        self.insp_status_lbl = ttk.Label(header_frame, textvariable=self.insp_status_var, font=self.font_bold)
        self.insp_status_lbl.pack(side=tk.RIGHT)

        # Scrollable container for inspector
        canvas = tk.Canvas(parent, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
        scroll_content = ttk.Frame(canvas)

        scroll_content.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas_window = canvas.create_window((0, 0), window=scroll_content, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(canvas_window, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 1. Original Metadata Card
        orig_box = ttk.LabelFrame(scroll_content, text=" 📄 Original BibTeX Entry ", style="Card.TLabelframe", padding=6)
        orig_box.pack(fill=tk.X, pady=4)

        self.lbl_orig_key = ttk.Label(orig_box, text="Key: -", font=self.font_bold)
        self.lbl_orig_key.pack(anchor=tk.W)
        self.lbl_orig_title = ttk.Label(orig_box, text="Title: -", wraplength=450)
        self.lbl_orig_title.pack(anchor=tk.W, pady=1)
        self.lbl_orig_author = ttk.Label(orig_box, text="Authors: -", wraplength=450)
        self.lbl_orig_author.pack(anchor=tk.W, pady=1)
        self.lbl_orig_meta = ttk.Label(orig_box, text="Year/Journal: -", wraplength=450)
        self.lbl_orig_meta.pack(anchor=tk.W, pady=1)
        self.lbl_orig_doi = ttk.Label(orig_box, text="DOI: -", font=self.font_mono)
        self.lbl_orig_doi.pack(anchor=tk.W, pady=1)

        # 2. Resolved Candidate Card
        cand_box = ttk.LabelFrame(scroll_content, text=" 🌐 Resolved Candidate (Crossref / OpenAlex / DataCite) ", style="Card.TLabelframe", padding=6)
        cand_box.pack(fill=tk.X, pady=4)

        self.lbl_cand_source = ttk.Label(cand_box, text="Source: -", font=self.font_bold)
        self.lbl_cand_source.pack(anchor=tk.W)
        self.lbl_cand_title = ttk.Label(cand_box, text="Title: -", wraplength=450)
        self.lbl_cand_title.pack(anchor=tk.W, pady=1)
        self.lbl_cand_author = ttk.Label(cand_box, text="Authors: -", wraplength=450)
        self.lbl_cand_author.pack(anchor=tk.W, pady=1)
        self.lbl_cand_meta = ttk.Label(cand_box, text="Year/Journal: -", wraplength=450)
        self.lbl_cand_meta.pack(anchor=tk.W, pady=1)
        self.lbl_cand_doi = ttk.Label(cand_box, text="DOI: -", font=self.font_mono)
        self.lbl_cand_doi.pack(anchor=tk.W, pady=1)

        # Scores & Reason
        score_box = ttk.Frame(cand_box, padding=(0, 4))
        score_box.pack(fill=tk.X)
        self.lbl_scores = ttk.Label(score_box, text="Scores: Title=- | Authors=- | Year=- | Journal=- | Overall=-", font=self.font_bold, foreground=self.color_primary)
        self.lbl_scores.pack(anchor=tk.W)

        self.lbl_warning = ttk.Label(cand_box, text="Reason: -", font=self.font_small, foreground=self.color_warning, wraplength=450)
        self.lbl_warning.pack(anchor=tk.W, pady=2)

        # 3. Decision & Action Card
        decision_box = ttk.LabelFrame(scroll_content, text=" ✍️ Decision & Actions ", style="Card.TLabelframe", padding=6)
        decision_box.pack(fill=tk.X, pady=6)

        btn_grid = ttk.Frame(decision_box)
        btn_grid.pack(fill=tk.X, pady=4)

        self.btn_accept = ttk.Button(btn_grid, text="🟢 Accept Candidate", style="Success.TButton", command=self._action_accept_candidate)
        self.btn_accept.grid(row=0, column=0, padx=3, pady=2, sticky="ew")

        self.btn_manual_edit = ttk.Button(btn_grid, text="✏️ Manually Edit", command=self._action_manual_edit)
        self.btn_manual_edit.grid(row=0, column=1, padx=3, pady=2, sticky="ew")

        self.btn_keep_orig = ttk.Button(btn_grid, text="🛡️ Keep Original", command=self._action_keep_original)
        self.btn_keep_orig.grid(row=1, column=0, padx=3, pady=2, sticky="ew")

        self.btn_search_online = ttk.Button(btn_grid, text="🔍 Search Online...", command=self._action_search_online)
        self.btn_search_online.grid(row=1, column=1, padx=3, pady=2, sticky="ew")

        btn_grid.columnconfigure(0, weight=1)
        btn_grid.columnconfigure(1, weight=1)

        # Comment / note field
        note_frame = ttk.Frame(decision_box)
        note_frame.pack(fill=tk.X, pady=4)
        ttk.Label(note_frame, text="Note / Comment:", font=self.font_small).pack(side=tk.LEFT, padx=(0, 4))
        self.note_var = tk.StringVar()
        self.note_entry = ttk.Entry(note_frame, textvariable=self.note_var)
        self.note_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Special banner for active feedback requirement
        self.feedback_banner = ttk.Frame(scroll_content, padding=6, relief=tk.RIDGE)
        self.feedback_banner_lbl = ttk.Label(
            self.feedback_banner,
            text="⚠️ Waiting for your decision to continue audit...",
            font=self.font_bold,
            foreground=self.color_warning,
        )
        self.feedback_banner_lbl.pack(pady=2)

    # ========================================================================
    # Tab 2: Settings & Sensitivity Sliders
    # ========================================================================

    def _build_tab_settings(self, parent: ttk.Frame) -> None:
        canvas = tk.Canvas(parent, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
        scroll_content = ttk.Frame(canvas)

        scroll_content.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas_window = canvas.create_window((0, 0), window=scroll_content, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(canvas_window, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Card 1: Sensitivity Presets
        preset_box = ttk.LabelFrame(scroll_content, text=" 🎯 Quick Sensitivity Presets ", style="Card.TLabelframe", padding=8)
        preset_box.pack(fill=tk.X, pady=4)

        p_row = ttk.Frame(preset_box)
        p_row.pack(fill=tk.X)
        ttk.Button(p_row, text="🛡️ Very Strict (Prudent)", command=lambda: self._apply_sensitivity_preset("strict")).pack(side=tk.LEFT, padx=4)
        ttk.Button(p_row, text="⚖️ Balanced (Standard)", command=lambda: self._apply_sensitivity_preset("standard")).pack(side=tk.LEFT, padx=4)
        ttk.Button(p_row, text="🔓 Permissive (Flexible)", command=lambda: self._apply_sensitivity_preset("permissive")).pack(side=tk.LEFT, padx=4)
        ttk.Button(p_row, text="🔄 Reset to Defaults", command=self._reset_sensitivity_defaults).pack(side=tk.RIGHT, padx=4)

        # Card 2: Similarity Threshold Sliders
        sens_box = ttk.LabelFrame(scroll_content, text=" 🎚️ Similarity and Matching Thresholds ", style="Card.TLabelframe", padding=8)
        sens_box.pack(fill=tk.X, pady=6)

        self.slider_vars: Dict[str, tk.DoubleVar] = {
            "title_strong": tk.DoubleVar(value=self.settings.sensitivity.title_strong),
            "title_good": tk.DoubleVar(value=self.settings.sensitivity.title_good),
            "title_doi_min": tk.DoubleVar(value=self.settings.sensitivity.title_doi_min),
            "doi_consistency_min": tk.DoubleVar(value=self.settings.sensitivity.doi_consistency_min),
            "author_min": tk.DoubleVar(value=self.settings.sensitivity.author_min),
            "year_min": tk.DoubleVar(value=self.settings.sensitivity.year_min),
            "container_min": tk.DoubleVar(value=self.settings.sensitivity.container_min),
            "margin_min": tk.DoubleVar(value=self.settings.sensitivity.margin_min),
        }

        sliders_def = [
            ("title_strong", "Strong Title similarity (direct acceptance):", 0.70, 1.00, 0.01),
            ("title_good", "Good Title similarity (with context confirmation):", 0.60, 1.00, 0.01),
            ("title_doi_min", "Minimum Title similarity required when DOI resolved:", 0.50, 0.95, 0.01),
            ("doi_consistency_min", "Minimum consistency between DOI and BibTeX Title:", 0.40, 0.95, 0.01),
            ("author_min", "Acceptable Author similarity threshold:", 0.20, 0.90, 0.01),
            ("year_min", "Year match threshold:", 0.30, 1.00, 0.01),
            ("container_min", "Journal / Container similarity threshold:", 0.20, 0.90, 0.01),
            ("margin_min", "Minimum score margin between 1st and 2nd candidate:", 0.001, 0.050, 0.001),
        ]

        for key, label, from_val, to_val, step in sliders_def:
            self._create_slider_row(sens_box, key, label, from_val, to_val, step)

        # Card 3: Component Weights
        weight_box = ttk.LabelFrame(scroll_content, text=" ⚖️ Global Score Component Weights ", style="Card.TLabelframe", padding=8)
        weight_box.pack(fill=tk.X, pady=6)

        self.weight_vars: Dict[str, tk.DoubleVar] = {
            "weight_title": tk.DoubleVar(value=self.settings.sensitivity.weight_title),
            "weight_author": tk.DoubleVar(value=self.settings.sensitivity.weight_author),
            "weight_year": tk.DoubleVar(value=self.settings.sensitivity.weight_year),
            "weight_container": tk.DoubleVar(value=self.settings.sensitivity.weight_container),
        }

        weights_def = [
            ("weight_title", "Title weight:", 0.10, 1.00, 0.02),
            ("weight_author", "Author weight:", 0.00, 0.50, 0.02),
            ("weight_year", "Year weight:", 0.00, 0.30, 0.01),
            ("weight_container", "Journal / Container weight:", 0.00, 0.30, 0.01),
        ]

        for key, label, from_val, to_val, step in weights_def:
            self._create_slider_row(weight_box, key, label, from_val, to_val, step, is_weight=True)

        # Card 4: Network Options & Output Files
        net_box = ttk.LabelFrame(scroll_content, text=" 🌐 Network Options, Delays & Report Files ", style="Card.TLabelframe", padding=8)
        net_box.pack(fill=tk.X, pady=6)

        # OpenAlex Key
        k_row = ttk.Frame(net_box)
        k_row.pack(fill=tk.X, pady=3)
        ttk.Label(k_row, text="OpenAlex API key (optional):", width=32).pack(side=tk.LEFT)
        self.api_key_var = tk.StringVar(value=self.settings.openalex_api_key)
        ttk.Entry(k_row, textvariable=self.api_key_var, width=32).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

        # Delay and Timeout
        d_row = ttk.Frame(net_box)
        d_row.pack(fill=tk.X, pady=3)
        ttk.Label(d_row, text="Delay between requests (seconds):", width=32).pack(side=tk.LEFT)
        self.delay_var = tk.DoubleVar(value=self.settings.delay)
        ttk.Spinbox(d_row, from_=0.0, to=5.0, increment=0.1, textvariable=self.delay_var, width=8).pack(side=tk.LEFT, padx=4)

        ttk.Label(d_row, text="Request timeout (s):").pack(side=tk.LEFT, padx=(16, 4))
        self.timeout_var = tk.IntVar(value=self.settings.timeout)
        ttk.Spinbox(d_row, from_=5, to=120, increment=5, textvariable=self.timeout_var, width=8).pack(side=tk.LEFT, padx=4)

        # Limit
        l_row = ttk.Frame(net_box)
        l_row.pack(fill=tk.X, pady=3)
        ttk.Label(l_row, text="Entry limit (0 = process all):", width=32).pack(side=tk.LEFT)
        self.limit_var = tk.IntVar(value=self.settings.limit)
        ttk.Spinbox(l_row, from_=0, to=50000, increment=10, textvariable=self.limit_var, width=8).pack(side=tk.LEFT, padx=4)

        # Overwrite Bibliographic Fields Checkbox
        self.overwrite_var = tk.BooleanVar(value=self.settings.overwrite_bibliographic_fields)
        ttk.Checkbutton(net_box, text="Overwrite existing secondary bibliographic fields (journal, pages, publisher)", variable=self.overwrite_var).pack(anchor=tk.W, pady=4)

        # Output report files
        out_box = ttk.Frame(net_box)
        out_box.pack(fill=tk.X, pady=4)
        ttk.Label(out_box, text="CSV Report:").grid(row=0, column=0, sticky="w", pady=2)
        self.csv_path_var = tk.StringVar(value="audit-report.csv")
        ttk.Entry(out_box, textvariable=self.csv_path_var, width=28).grid(row=0, column=1, sticky="ew", padx=4, pady=2)

        ttk.Label(out_box, text="Markdown Summary:").grid(row=0, column=2, sticky="w", pady=2, padx=(10, 0))
        self.md_path_var = tk.StringVar(value="audit-summary.md")
        ttk.Entry(out_box, textvariable=self.md_path_var, width=28).grid(row=0, column=3, sticky="ew", padx=4, pady=2)

        out_box.columnconfigure(1, weight=1)
        out_box.columnconfigure(3, weight=1)

        # Purge Cache & Decisions
        clean_box = ttk.Frame(net_box)
        clean_box.pack(fill=tk.X, pady=(6, 2))
        ttk.Button(clean_box, text="🗑️ Clear API Cache", command=self._clear_cache_prompt).pack(side=tk.LEFT, padx=4)
        ttk.Button(clean_box, text="🔄 Reset Saved Decisions", command=self._reset_decisions_prompt).pack(side=tk.LEFT, padx=4)

        # Support Section
        support_box = ttk.LabelFrame(scroll_content, text=" ☕ Support & Community ", style="Card.TLabelframe", padding=8)
        support_box.pack(fill=tk.X, pady=6)
        ttk.Label(support_box, text="Support the development and maintenance of BibAudit:").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(support_box, text="☕ Support me on Buy Me a Coffee", style="Support.TButton", command=self._open_support_url).pack(side=tk.LEFT, padx=4)

    def _create_slider_row(self, parent: ttk.Frame, key: str, label_text: str,
                           from_val: float, to_val: float, step: float, is_weight: bool = False) -> None:
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=2)

        ttk.Label(row, text=label_text, width=46, anchor=tk.W).pack(side=tk.LEFT)

        var = self.weight_vars[key] if is_weight else self.slider_vars[key]
        val_lbl_var = tk.StringVar(value=f"{var.get():.3f}")

        def _on_slider_move(val_str: str) -> None:
            v = float(val_str)
            var.set(v)
            val_lbl_var.set(f"{v:.3f}")
            self._sync_settings_from_ui()

        scale = ttk.Scale(row, from_=from_val, to=to_val, value=var.get(), command=_on_slider_move)
        scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)

        ttk.Label(row, textvariable=val_lbl_var, width=8, font=self.font_mono, anchor=tk.E).pack(side=tk.RIGHT)

    # ========================================================================
    # Tab 3: BibTeX Preview & Diff
    # ========================================================================

    def _build_tab_diff(self, parent: ttk.Frame) -> None:
        top = ttk.Frame(parent)
        top.pack(fill=tk.X, pady=(0, 4))
        self.diff_title_var = tk.StringVar(value="Select an entry in the 'Review' tab")
        ttk.Label(top, textvariable=self.diff_title_var, font=self.font_subtitle, foreground=self.color_primary).pack(side=tk.LEFT)
        ttk.Button(top, text="📋 Copy BibTeX", command=self._copy_diff_bibtex).pack(side=tk.RIGHT)

        paned = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        # Left: Original
        left_frame = ttk.LabelFrame(paned, text=" 📄 Original BibTeX ", style="Card.TLabelframe", padding=4)
        paned.add(left_frame, weight=1)

        self.txt_orig_bib = tk.Text(left_frame, wrap=tk.NONE, font=self.font_mono, bg="#FAFAFA")
        sb_orig_y = ttk.Scrollbar(left_frame, orient=tk.VERTICAL, command=self.txt_orig_bib.yview)
        sb_orig_x = ttk.Scrollbar(left_frame, orient=tk.HORIZONTAL, command=self.txt_orig_bib.xview)
        self.txt_orig_bib.config(yscrollcommand=sb_orig_y.set, xscrollcommand=sb_orig_x.set)

        self.txt_orig_bib.grid(row=0, column=0, sticky="nsew")
        sb_orig_y.grid(row=0, column=1, sticky="ns")
        sb_orig_x.grid(row=1, column=0, sticky="ew")
        left_frame.rowconfigure(0, weight=1)
        left_frame.columnconfigure(0, weight=1)

        # Right: Resolved / Updated
        right_frame = ttk.LabelFrame(paned, text=" ✨ BibTeX After Processing / Resolution ", style="Card.TLabelframe", padding=4)
        paned.add(right_frame, weight=1)

        self.txt_res_bib = tk.Text(right_frame, wrap=tk.NONE, font=self.font_mono, bg="#F0FDF4")
        sb_res_y = ttk.Scrollbar(right_frame, orient=tk.VERTICAL, command=self.txt_res_bib.yview)
        sb_res_x = ttk.Scrollbar(right_frame, orient=tk.HORIZONTAL, command=self.txt_res_bib.xview)
        self.txt_res_bib.config(yscrollcommand=sb_res_y.set, xscrollcommand=sb_res_x.set)

        self.txt_res_bib.grid(row=0, column=0, sticky="nsew")
        sb_res_y.grid(row=0, column=1, sticky="ns")
        sb_res_x.grid(row=1, column=0, sticky="ew")
        right_frame.rowconfigure(0, weight=1)
        right_frame.columnconfigure(0, weight=1)

    def _copy_diff_bibtex(self) -> None:
        text = self.txt_res_bib.get("1.0", tk.END).strip()
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.status_var.set("BibTeX snippet copied to clipboard.")

    # ========================================================================
    # Tab 4: Console & Logs
    # ========================================================================

    def _build_tab_console(self, parent: ttk.Frame) -> None:
        ctrl_frame = ttk.Frame(parent)
        ctrl_frame.pack(fill=tk.X, pady=(0, 4))

        ttk.Label(ctrl_frame, text="Live execution log:", font=self.font_bold).pack(side=tk.LEFT)
        ttk.Button(ctrl_frame, text="Clear console", command=self._clear_console).pack(side=tk.RIGHT, padx=4)

        self.txt_console = tk.Text(parent, wrap=tk.WORD, font=self.font_mono, bg="#1E1E1E", fg="#F8F8F2")
        sb_console = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=self.txt_console.yview)
        self.txt_console.config(yscrollcommand=sb_console.set)

        # Color tags
        self.txt_console.tag_configure("INFO", foreground="#66D9EF")
        self.txt_console.tag_configure("SUCCESS", foreground="#A6E22E")
        self.txt_console.tag_configure("WARNING", foreground="#FD971F")
        self.txt_console.tag_configure("ERROR", foreground="#F92672")
        self.txt_console.tag_configure("STEP", foreground="#AE81FF", font=(self.font_mono[0], self.font_mono[1], "bold"))

        self.txt_console.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb_console.pack(side=tk.RIGHT, fill=tk.Y)

    def _log_console(self, msg: str, level: str = "INFO") -> None:
        timestamp = time.strftime("%H:%M:%S")
        formatted = f"[{timestamp}] {msg}\n"
        self.txt_console.insert(tk.END, formatted, level)
        self.txt_console.see(tk.END)

    def _clear_console(self) -> None:
        self.txt_console.delete("1.0", tk.END)

    # ========================================================================
    # File Management & Auto-Detection
    # ========================================================================

    def _auto_detect_default_files(self) -> None:
        cwd = Path.cwd()
        candidates = list(cwd.glob("*.bib.txt")) + list(cwd.glob("*.bib"))
        candidates = [c for c in candidates if c.name not in ("bib-verified.bib", "bib-verifie.bib", "out.bib")]
        if candidates:
            first = candidates[0]
            self.input_path_var.set(str(first))
            self._load_bibtex_file(silent=True)

    def _browse_input_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Select BibTeX File",
            filetypes=[("BibTeX Files", "*.bib *.bib.txt"), ("All Files", "*.*")],
        )
        if path:
            self.input_path_var.set(path)
            self._load_bibtex_file()

    def _browse_output_file(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save Verified BibTeX File",
            defaultextension=".bib",
            filetypes=[("BibTeX Files", "*.bib"), ("All Files", "*.*")],
        )
        if path:
            self.output_path_var.set(path)

    def _load_bibtex_file(self, silent: bool = False) -> None:
        path_str = self.input_path_var.get().strip()
        if not path_str:
            if not silent:
                messagebox.showwarning("Missing File", "Please specify a BibTeX file.")
            return

        file_path = Path(path_str)
        database, error = parse_bibtex_file(file_path)
        if error or not database:
            if not silent:
                messagebox.showerror("Read Error", f"Unable to load BibTeX file:\n{error}")
            self._log_console(f"Load error: {error}", "ERROR")
            return

        self.database = database
        self.entries = database.entries
        self.used_keys = {entry.get("ID", "") for entry in self.entries}
        self.rows.clear()
        self.rows_by_key.clear()

        # Reload decisions
        self._sync_settings_from_ui()
        self.decisions = DecisionStore(self.settings.decisions_file)

        # Initialize display rows
        for idx, entry in enumerate(self.entries, 1):
            key = entry.get("ID", f"[no_key_{idx}]")
            row = ReportRow(
                key=key,
                entry_type=entry.get("ENTRYTYPE", ""),
                status="pending",
                source="",
                confidence="",
                original_doi=clean_doi(entry.get("doi", "")),
                resolved_doi="",
                title_score="",
                author_score="",
                year_score="",
                changes="",
                warnings="",
                original_title=entry.get("title", ""),
                resolved_title="",
                entry_ref=entry,
            )
            self.rows.append(row)
            self.rows_by_key[key] = row

        self._refresh_table()
        self._update_kpi_counts()

        msg = f"{len(self.entries)} entries loaded from {file_path.name}."
        if self.decisions.count() > 0:
            msg += f" ({self.decisions.count()} previous decision(s) detected)"
        self.status_var.set(msg)
        self._log_console(msg, "SUCCESS")

    # ========================================================================
    # Settings Synchronization & Presets
    # ========================================================================

    def _sync_settings_from_ui(self) -> None:
        self.settings.input_file = Path(self.input_path_var.get().strip() or "references.bib")
        self.settings.output_file = Path(self.output_path_var.get().strip() or "bib-verified.bib")
        self.settings.report_file = Path(self.csv_path_var.get().strip() or "audit-report.csv")
        self.settings.summary_file = Path(self.md_path_var.get().strip() or "audit-summary.md")

        try:
            self.settings.mode = ExecutionMode(self.mode_var.get())
        except Exception:
            self.settings.mode = ExecutionMode.HYBRID

        self.settings.openalex_api_key = self.api_key_var.get().strip()
        self.settings.delay = max(0.0, self.delay_var.get())
        self.settings.timeout = max(5, self.timeout_var.get())
        self.settings.limit = max(0, self.limit_var.get())
        self.settings.overwrite_bibliographic_fields = self.overwrite_var.get()

        # Sensitivity thresholds
        sens = self.settings.sensitivity
        for k, var in self.slider_vars.items():
            if hasattr(sens, k):
                setattr(sens, k, var.get())

        # Weights
        for k, var in self.weight_vars.items():
            if hasattr(sens, k):
                setattr(sens, k, var.get())

    def _on_mode_change(self) -> None:
        mode_val = self.mode_var.get()
        if mode_val == ExecutionMode.AUTO.value:
            self.status_var.set("Full Automatic mode enabled: all entries are resolved automatically without interruption.")
        elif mode_val == ExecutionMode.HYBRID.value:
            self.status_var.set("Hybrid mode enabled: auto-resolves reliable entries, prompts for ambiguous matches.")
        elif mode_val == ExecutionMode.MANUAL.value:
            self.status_var.set("Full Manual mode enabled: confirmation requested for every modified entry.")

    def _apply_sensitivity_preset(self, preset: str) -> None:
        self.settings.sensitivity.apply_preset(preset)
        self._update_ui_from_sensitivity()
        self.status_var.set(f"Sensitivity preset '{preset.capitalize()}' applied.")
        self._log_console(f"Sensitivity configured to '{preset.capitalize()}' mode.", "INFO")

    def _reset_sensitivity_defaults(self) -> None:
        self.settings.sensitivity.reset_defaults()
        self._update_ui_from_sensitivity()
        self.status_var.set("Sensitivity thresholds reset to default values.")

    def _update_ui_from_sensitivity(self) -> None:
        sens = self.settings.sensitivity
        for k, var in self.slider_vars.items():
            if hasattr(sens, k):
                var.set(getattr(sens, k))
        for k, var in self.weight_vars.items():
            if hasattr(sens, k):
                var.set(getattr(sens, k))

    def _clear_cache_prompt(self) -> None:
        if messagebox.askyesno("Clear Cache", "Do you really want to clear the local API query cache?"):
            if self.client:
                self.client.clear_cache()
            elif self.settings.cache_file.exists():
                try:
                    self.settings.cache_file.unlink()
                except Exception as exc:
                    messagebox.showerror("Error", str(exc))
            self._log_console("API Cache cleared.", "INFO")
            self.status_var.set("API cache reset.")

    def _reset_decisions_prompt(self) -> None:
        if messagebox.askyesno("Reset Decisions", "Do you really want to clear all saved manual decisions?"):
            self.decisions.clear()
            self._log_console("Manual decisions reset.", "WARNING")
            self.status_var.set("Decisions store reset.")

    def _open_support_url(self) -> None:
        webbrowser.open_new_tab("https://buymeacoffee.com/atlasfield92")

    # ========================================================================
    # Audit Execution (Background Thread)
    # ========================================================================

    def _start_audit(self) -> None:
        if not self.entries:
            messagebox.showwarning("No Entries", "Please load a BibTeX file first.")
            return

        if self.is_running:
            return

        self._sync_settings_from_ui()
        self.client = ScholarClient(
            api_key=self.settings.openalex_api_key,
            timeout=self.settings.timeout,
            delay=self.settings.delay,
            cache_path=self.settings.cache_file,
        )

        self.is_running = True
        self.stop_event.clear()
        self.pause_event.set()
        self.btn_start.config(state=tk.DISABLED)
        self.btn_pause.config(state=tk.NORMAL, text="⏸️ Pause")
        self.btn_stop.config(state=tk.NORMAL)

        # Launch worker thread
        self.worker_thread = threading.Thread(target=self._audit_worker, daemon=True)
        self.worker_thread.start()
        self._log_console(f"Audit started in '{self.settings.mode.value.upper()}' mode...", "STEP")

    def _toggle_pause(self) -> None:
        if not self.is_running:
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.btn_pause.config(text="▶️ Resume")
            self.status_var.set("Audit paused.")
            self._log_console("Audit paused.", "WARNING")
        else:
            self.pause_event.set()
            self.btn_pause.config(text="⏸️ Pause")
            self.status_var.set("Audit resumed.")
            self._log_console("Audit resumed.", "INFO")

    def _stop_audit(self) -> None:
        if not self.is_running:
            return
        self.stop_event.set()
        self.pause_event.set()
        self.feedback_event.set()  # Unblock if waiting on user decision
        self.status_var.set("Stopping audit...")
        self._log_console("Audit stop requested by user.", "WARNING")

    def _audit_worker(self) -> None:
        limit = self.settings.limit or len(self.entries)
        target_entries = self.entries[:limit]
        total = len(target_entries)

        try:
            for idx, entry in enumerate(target_entries, 1):
                if self.stop_event.is_set():
                    break

                # Pause handling
                self.pause_event.wait()
                if self.stop_event.is_set():
                    break

                key = entry.get("ID", f"[no_key_{idx}]")
                self.event_queue.put(("entry_start", (idx, total, key)))

                try:
                    # 1. Check for previously saved decision
                    saved_decision = self.decisions.get(entry)
                    if saved_decision and self.settings.mode != ExecutionMode.AUTO:
                        row = process_entry(
                            entry, self.client, self.settings.overwrite_bibliographic_fields,
                            self.used_keys, self.settings.sensitivity
                        )
                        if "fields" in saved_decision:
                            old_k = entry.get("ID", "")
                            entry.clear()
                            entry.update(saved_decision["fields"])
                            new_k = entry.get("ID", "")
                            if old_k and new_k and old_k != new_k:
                                self.used_keys.discard(old_k)
                                self.used_keys.add(new_k)
                        row.key = entry.get("ID", key)
                        row.status = saved_decision.get("status", row.status)
                        if saved_decision.get("changes"):
                            row.changes = " | ".join(saved_decision["changes"])
                        note = saved_decision.get("note", "")
                        if note:
                            row.warnings = (row.warnings + " | " if row.warnings else "") + "Resumed decision: " + note
                        self.rows_by_key[key] = row
                        self.event_queue.put(("entry_done", (idx, total, row, "previous decision restored")))
                        continue

                    # 2. Standard analysis
                    row = process_entry(
                        entry, self.client, self.settings.overwrite_bibliographic_fields,
                        self.used_keys, self.settings.sensitivity
                    )

                    # 3. Mode-specific evaluation
                    require_feedback = False
                    if self.settings.mode == ExecutionMode.MANUAL:
                        require_feedback = True
                    elif self.settings.mode == ExecutionMode.HYBRID:
                        if row.status in ("to review", "not found", "à revoir", "non trouvé"):
                            require_feedback = True

                    if require_feedback and not self.stop_event.is_set():
                        self.feedback_event.clear()
                        self.current_feedback_decision = None
                        self.event_queue.put(("feedback_needed", (idx, total, row, entry)))

                        # Wait for user decision
                        self.feedback_event.wait()

                        if self.current_feedback_decision:
                            dec = self.current_feedback_decision
                            choice = dec.get("choice", "")
                            if choice == "quit":
                                break
                            elif choice == "accept":
                                candidate = row.candidate
                                if candidate:
                                    changes = apply_candidate(entry, candidate, self.settings.overwrite_bibliographic_fields)
                                    row.status = "corrected" if changes else "verified"
                                    row.changes = " | ".join(changes)
                                note = dec.get("note", "")
                                if note:
                                    row.warnings = (row.warnings + " | " if row.warnings else "") + "Feedback: " + note
                                self.decisions.record(key, choice="a", status=row.status,
                                                      changes=row.changes.split(" | ") if row.changes else [],
                                                      note=note, entry=entry)
                            elif choice == "keep":
                                row.status = "kept"
                                note = dec.get("note", "Original kept")
                                if note:
                                    row.warnings = (row.warnings + " | " if row.warnings else "") + "Feedback: " + note
                                self.decisions.record(key, choice="k", status="kept", changes=[], note=note, entry=entry)
                            elif choice == "edit":
                                row.status = "manually corrected"
                                changes = dec.get("changes", [])
                                if changes:
                                    row.changes = " | ".join(changes)
                                note = dec.get("note", "")
                                if note:
                                    row.warnings = (row.warnings + " | " if row.warnings else "") + "Feedback: " + note
                                self.decisions.record(key, choice="m", status=row.status,
                                                      changes=changes, note=note, entry=entry)

                    self.rows_by_key[key] = row
                    self.event_queue.put(("entry_done", (idx, total, row, None)))

                except Exception as exc:
                    err_row = make_row(
                        entry, key, "error", None,
                        clean_doi(entry.get("doi", "")), entry.get("title", ""),
                        [], [str(exc)]
                    )
                    self.rows_by_key[key] = err_row
                    self.event_queue.put(("entry_done", (idx, total, err_row, f"Error: {exc}")))

        finally:
            if self.client:
                self.client.save_cache()
            self.decisions.save()
            self.event_queue.put(("audit_finished", self.stop_event.is_set()))

    # ========================================================================
    # UI Event Queue Processing
    # ========================================================================

    def _process_event_queue(self) -> None:
        try:
            while True:
                event_type, data = self.event_queue.get_nowait()
                if event_type == "entry_start":
                    idx, total, key = data
                    pct = int((idx / total) * 100) if total else 0
                    self.prog_label_var.set(f"Progress: {idx} / {total} ({pct}%)")
                    self.progress_bar["value"] = pct
                    self.status_var.set(f"[{idx}/{total}] Auditing '{key}'...")
                    self._update_tree_item(key, status="in progress")

                elif event_type == "entry_done":
                    idx, total, row, info = data
                    self._update_tree_item_from_row(row)
                    self._update_kpi_counts()
                    lvl = "SUCCESS" if row.status in ("verified", "corrected", "validated", "vérifié", "corrigé") else ("WARNING" if row.status in ("to review", "not found", "à revoir", "non trouvé") else "ERROR")
                    msg = f"[{idx}/{total}] {row.key} -> {row.status.upper()}"
                    if info:
                        msg += f" ({info})"
                    self._log_console(msg, lvl)

                elif event_type == "feedback_needed":
                    idx, total, row, entry = data
                    self._highlight_feedback_entry(row, idx, total)

                elif event_type == "audit_finished":
                    was_stopped = data
                    self.is_running = False
                    self.btn_start.config(state=tk.NORMAL)
                    self.btn_pause.config(state=tk.DISABLED, text="⏸️ Pause")
                    self.btn_stop.config(state=tk.DISABLED)
                    self.feedback_banner.pack_forget()

                    if was_stopped:
                        self.status_var.set("Audit interrupted. Progress and decisions have been saved.")
                        self._log_console("Audit stopped by user.", "WARNING")
                    else:
                        self.status_var.set("Audit completed successfully.")
                        self._log_console("Audit completed successfully on all target entries.", "STEP")
                        messagebox.showinfo("Audit Complete", "The audit is finished! You can review entries or save the outputs.")

        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._process_event_queue)

    def _highlight_feedback_entry(self, row: ReportRow, idx: int, total: int) -> None:
        self.notebook.select(self.tab_review)
        self._select_tree_row_by_key(row.key)
        self.feedback_banner_lbl.config(
            text=f"⚠️ Entry [{idx}/{total}] '{row.key}' ({row.status.upper()}) requires your decision:"
        )
        self.feedback_banner.pack(fill=tk.X, pady=6, before=self.btn_accept.master.master)

    # ========================================================================
    # Table Display & Filtering
    # ========================================================================

    def _refresh_table(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for idx, row in enumerate(self.rows, 1):
            tag = self._get_status_tag(row.status)
            self.tree.insert(
                "",
                tk.END,
                iid=row.key,
                values=(
                    idx,
                    row.key,
                    row.status,
                    row.confidence or "-",
                    row.original_title,
                    row.resolved_title or "-",
                ),
                tags=(tag,),
            )

    def _update_tree_item(self, key: str, status: str) -> None:
        if self.tree.exists(key):
            tag = self._get_status_tag(status)
            self.tree.set(key, "status", status)
            self.tree.item(key, tags=(tag,))

    def _update_tree_item_from_row(self, row: ReportRow) -> None:
        if self.tree.exists(row.key):
            tag = self._get_status_tag(row.status)
            self.tree.set(row.key, "status", row.status)
            self.tree.set(row.key, "score", row.confidence or "-")
            self.tree.set(row.key, "orig_title", row.original_title)
            self.tree.set(row.key, "res_title", row.resolved_title or "-")
            self.tree.item(row.key, tags=(tag,))

    def _get_status_tag(self, status: str) -> str:
        s = status.lower()
        if "verified" in s or "vérifié" in s or "valid" in s:
            return "verified"
        if "corrected" in s or "corrigé" in s:
            return "corrected"
        if "to review" in s or "revoir" in s:
            return "to_review"
        if "not found" in s or "non trouvé" in s or "none" in s:
            return "not_found"
        if "error" in s or "erreur" in s:
            return "error"
        if "in progress" in s or "en cours" in s:
            return "in_progress"
        return ""

    def _apply_table_filters(self) -> None:
        query = normalize(self.search_var.get())
        status_filter = self.status_filter_var.get().lower()

        self.tree.delete(*self.tree.get_children())
        for idx, row in enumerate(self.rows, 1):
            if status_filter != "all" and status_filter != "tous":
                if status_filter not in row.status.lower():
                    continue

            if query:
                full_text = normalize(f"{row.key} {row.original_title} {row.resolved_title} {row.original_doi} {row.resolved_doi}")
                if query not in full_text:
                    continue

            tag = self._get_status_tag(row.status)
            self.tree.insert(
                "",
                tk.END,
                iid=row.key,
                values=(
                    idx,
                    row.key,
                    row.status,
                    row.confidence or "-",
                    row.original_title,
                    row.resolved_title or "-",
                ),
                tags=(tag,),
            )

    def _clear_filters(self) -> None:
        self.search_var.set("")
        self.status_filter_var.set("All")
        self._refresh_table()

    def _select_tree_row_by_key(self, key: str) -> None:
        if self.tree.exists(key):
            self.tree.selection_set(key)
            self.tree.see(key)
            self._load_row_into_inspector(self.rows_by_key.get(key))

    def _on_tree_select(self, event: Any) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        key = sel[0]
        row = self.rows_by_key.get(key)
        if row:
            self._load_row_into_inspector(row)

    def _load_row_into_inspector(self, row: Optional[ReportRow]) -> None:
        if not row:
            return

        entry = row.entry_ref or {}
        cand = row.candidate

        # Header
        self.insp_title_var.set(f"Key: {row.key} [{row.entry_type or entry.get('ENTRYTYPE', 'misc')}]")
        self.insp_status_var.set(f"Status: {row.status.upper()}")

        # Status color
        tag = self._get_status_tag(row.status)
        col = (
            self.color_success if tag == "verified" else (
                self.color_primary if tag == "corrected" else (
                    self.color_warning if tag == "to_review" else (
                        self.color_danger if tag == "error" else "#555"
                    )
                )
            )
        )
        self.insp_status_lbl.config(foreground=col)

        # Original
        self.lbl_orig_key.config(text=f"Key: {row.key}")
        self.lbl_orig_title.config(text=f"Title: {row.original_title or '[absent]'}")
        self.lbl_orig_author.config(text=f"Authors: {entry.get('author', '[absent]')}")
        container = entry.get("journal") or entry.get("booktitle") or "[absent]"
        self.lbl_orig_meta.config(text=f"Year: {entry.get('year', '[absent]')} | Journal/Book: {container}")
        self.lbl_orig_doi.config(text=f"DOI: {row.original_doi or '[absent]'}")

        # Candidate
        if cand:
            self.lbl_cand_source.config(text=f"Source: {cand.source}")
            self.lbl_cand_title.config(text=f"Title: {cand.title or '[absent]'}")
            self.lbl_cand_author.config(text=f"Authors: {cand.formatted_authors or '[absent]'}")
            self.lbl_cand_meta.config(text=f"Year: {cand.year or '[absent]'} | Journal/Book: {cand.container or '[absent]'}")
            self.lbl_cand_doi.config(text=f"DOI: {cand.doi or '[absent]'}")
            self.lbl_scores.config(
                text=f"Scores: Title={cand.title_score:.2f} | Authors={cand.author_score:.2f} | Year={cand.year_score:.2f} | Journal={cand.container_score:.2f} | Overall={cand.score:.3f}"
            )
            self.lbl_warning.config(text=f"Reason: {row.warnings or '-'}")
            self.btn_accept.config(state=tk.NORMAL)
        else:
            self.lbl_cand_source.config(text=f"Source: {row.source or '[none]'}")
            self.lbl_cand_title.config(text=f"Title: {row.resolved_title or '[no candidate]'}")
            self.lbl_cand_author.config(text="Authors: [none]")
            self.lbl_cand_meta.config(text="Year/Journal: [none]")
            self.lbl_cand_doi.config(text=f"DOI: {row.resolved_doi or '[none]'}")
            self.lbl_scores.config(text="Scores: -")
            self.lbl_warning.config(text=f"Reason: {row.warnings or 'No candidate found.'}")
            self.btn_accept.config(state=tk.DISABLED)

        # Existing note
        dec = self.decisions.get(entry) if entry else None
        self.note_var.set(dec.get("note", "") if dec else "")

        # Update Diff tab
        self._update_diff_tab(row)

    def _update_diff_tab(self, row: ReportRow) -> None:
        self.diff_title_var.set(f"BibTeX Comparison for `{row.key}`")
        self.txt_orig_bib.delete("1.0", tk.END)
        self.txt_res_bib.delete("1.0", tk.END)

        if row.entry_ref:
            raw_orig = format_single_bibtex_entry(row.entry_ref)
            self.txt_orig_bib.insert("1.0", raw_orig)

            if row.candidate:
                import copy
                mock_entry = copy.deepcopy(row.entry_ref)
                apply_candidate(mock_entry, row.candidate, self.settings.overwrite_bibliographic_fields)
                raw_res = format_single_bibtex_entry(mock_entry)
            else:
                raw_res = raw_orig
            self.txt_res_bib.insert("1.0", raw_res)

    def _update_kpi_counts(self) -> None:
        counts: Dict[str, int] = {"total": len(self.rows), "verified": 0, "corrected": 0, "to_review": 0, "not_found": 0, "error": 0}
        for r in self.rows:
            tag = self._get_status_tag(r.status)
            if tag == "verified":
                counts["verified"] += 1
            elif tag == "corrected":
                counts["corrected"] += 1
            elif tag == "to_review":
                counts["to_review"] += 1
            elif tag == "not_found":
                counts["not_found"] += 1
            elif tag == "error":
                counts["error"] += 1

        self.kpi_vars["total"].set(f"Total: {counts['total']}")
        self.kpi_vars["verified"].set(f"Verified: {counts['verified']}")
        self.kpi_vars["corrected"].set(f"Corrected: {counts['corrected']}")
        self.kpi_vars["to_review"].set(f"To review: {counts['to_review']}")
        self.kpi_vars["not_found"].set(f"Not found: {counts['not_found']}")
        self.kpi_vars["error"].set(f"Error: {counts['error']}")

    # ========================================================================
    # User Actions (Feedback / Manual Decisions)
    # ========================================================================

    def _get_current_selected_row(self) -> Optional[ReportRow]:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Selection", "Please select an entry in the table.")
            return None
        return self.rows_by_key.get(sel[0])

    def _action_accept_candidate(self) -> None:
        row = self._get_current_selected_row()
        if not row or not row.candidate:
            messagebox.showwarning("Action Unavailable", "No valid candidate to accept for this entry.")
            return

        note = self.note_var.get().strip()
        if self.is_running and not self.feedback_event.is_set():
            self.current_feedback_decision = {"choice": "accept", "note": note}
            self.feedback_banner.pack_forget()
            self.feedback_event.set()
        else:
            entry = row.entry_ref or {}
            changes = apply_candidate(entry, row.candidate, self.settings.overwrite_bibliographic_fields)
            row.status = "corrected" if changes else "verified"
            row.changes = " | ".join(changes)
            if note:
                row.warnings = (row.warnings + " | " if row.warnings else "") + "Feedback: " + note
            self.decisions.record(row.key, choice="a", status=row.status, changes=changes, note=note, entry=entry)
            self._update_tree_item_from_row(row)
            self._load_row_into_inspector(row)
            self._update_kpi_counts()
            self.status_var.set(f"Candidate accepted for '{row.key}'.")
            self._log_console(f"Entry '{row.key}' updated by candidate acceptance.", "SUCCESS")

    def _action_keep_original(self) -> None:
        row = self._get_current_selected_row()
        if not row:
            return

        note = self.note_var.get().strip() or "Original kept by user decision"
        if self.is_running and not self.feedback_event.is_set():
            self.current_feedback_decision = {"choice": "keep", "note": note}
            self.feedback_banner.pack_forget()
            self.feedback_event.set()
        else:
            entry = row.entry_ref or {}
            row.status = "kept"
            row.warnings = (row.warnings + " | " if row.warnings else "") + "Feedback: " + note
            self.decisions.record(row.key, choice="k", status="kept", changes=[], note=note, entry=entry)
            self._update_tree_item_from_row(row)
            self._load_row_into_inspector(row)
            self._update_kpi_counts()
            self.status_var.set(f"Original kept for '{row.key}'.")
            self._log_console(f"Entry '{row.key}' marked as kept.", "INFO")

    def _action_manual_edit(self) -> None:
        row = self._get_current_selected_row()
        if not row:
            return

        entry = row.entry_ref or {}
        cand = row.candidate

        # Manual edit dialog
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Manually Edit Entry: {row.key}")
        dialog.geometry("620x520")
        dialog.transient(self.root)
        dialog.grab_set()

        fields_frame = ttk.Frame(dialog, padding=12)
        fields_frame.pack(fill=tk.BOTH, expand=True)

        fields = [
            ("key", "BibTeX Key (ID):", entry.get("ID", row.key)),
            ("title", "Title:", (cand.title if cand else "") or entry.get("title", "")),
            ("author", "BibTeX Authors:", (cand.formatted_authors if cand else "") or entry.get("author", "")),
            ("year", "Year:", str((cand.year if cand else "") or entry.get("year", ""))),
            ("journal", "Journal / Booktitle:", (cand.container if cand else "") or entry.get("journal") or entry.get("booktitle", "")),
            ("doi", "DOI:", (cand.doi if cand else "") or entry.get("doi", "")),
            ("volume", "Volume:", (cand.volume if cand else "") or entry.get("volume", "")),
            ("pages", "Pages:", (cand.pages if cand else "") or entry.get("pages", "")),
        ]

        entries_vars: Dict[str, tk.StringVar] = {}

        for idx, (f_key, f_label, f_val) in enumerate(fields):
            ttk.Label(fields_frame, text=f_label, font=self.font_bold).grid(row=idx, column=0, sticky="w", pady=4)
            var = tk.StringVar(value=str(f_val))
            entries_vars[f_key] = var
            e = ttk.Entry(fields_frame, textvariable=var, width=45)
            e.grid(row=idx, column=1, sticky="ew", padx=6, pady=4)

        fields_frame.columnconfigure(1, weight=1)

        # Note
        ttk.Label(fields_frame, text="Comment / Note:", font=self.font_bold).grid(row=len(fields), column=0, sticky="w", pady=6)
        note_var = tk.StringVar(value=self.note_var.get())
        ttk.Entry(fields_frame, textvariable=note_var, width=45).grid(row=len(fields), column=1, sticky="ew", padx=6, pady=6)

        def _on_save() -> None:
            changes: List[str] = []
            for f_key, _, _ in fields:
                new_val = entries_vars[f_key].get().strip()
                if f_key == "doi":
                    new_val = clean_doi(new_val)
                old_val = str(entry.get(f_key, "") if f_key != "key" else entry.get("ID", "")).strip()
                if new_val and normalize(new_val) != normalize(old_val):
                    changes.append(f"{f_key}: {old_val or '[absent]'} -> {new_val}")
                    if f_key == "key":
                        entry["ID"] = new_val
                    else:
                        entry[f_key] = new_val

            note = note_var.get().strip()

            if self.is_running and not self.feedback_event.is_set():
                self.current_feedback_decision = {"choice": "edit", "changes": changes, "note": note}
                self.feedback_banner.pack_forget()
                self.feedback_event.set()
            else:
                row.status = "manually corrected" if changes else "kept"
                if changes:
                    row.changes = " | ".join(changes)
                if note:
                    row.warnings = (row.warnings + " | " if row.warnings else "") + "Feedback: " + note
                self.decisions.record(row.key, choice="m", status=row.status, changes=changes, note=note, entry=entry)
                self._update_tree_item_from_row(row)
                self._load_row_into_inspector(row)
                self._update_kpi_counts()

            dialog.destroy()
            self._log_console(f"Entry '{row.key}' updated manually.", "SUCCESS")

        btn_box = ttk.Frame(dialog, padding=10)
        btn_box.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(btn_box, text="💾 Save Changes", style="Primary.TButton", command=_on_save).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn_box, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT, padx=4)

    def _action_search_online(self) -> None:
        row = self._get_current_selected_row()
        if not row:
            return

        entry = row.entry_ref or {}
        if not self.client:
            self.client = ScholarClient(
                api_key=self.settings.openalex_api_key,
                timeout=self.settings.timeout,
                delay=self.settings.delay,
                cache_path=self.settings.cache_file,
            )

        # Targeted online search window
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Manual Online Search — {row.key}")
        dialog.geometry("750x550")
        dialog.transient(self.root)
        dialog.grab_set()

        top_f = ttk.Frame(dialog, padding=10)
        top_f.pack(fill=tk.X)

        ttk.Label(top_f, text="Title or DOI to search:", font=self.font_bold).pack(side=tk.LEFT, padx=(0, 4))
        q_var = tk.StringVar(value=row.original_doi or row.original_title)
        q_entry = ttk.Entry(top_f, textvariable=q_var, width=45)
        q_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

        res_tree = ttk.Treeview(
            dialog,
            columns=("source", "doi", "year", "title", "score"),
            show="headings",
            selectmode="browse",
        )
        res_tree.heading("source", text="Source")
        res_tree.heading("doi", text="DOI")
        res_tree.heading("year", text="Year")
        res_tree.heading("title", text="Title")
        res_tree.heading("score", text="Score")

        res_tree.column("source", width=80, anchor=tk.CENTER)
        res_tree.column("doi", width=140)
        res_tree.column("year", width=60, anchor=tk.CENTER)
        res_tree.column("title", width=360)
        res_tree.column("score", width=60, anchor=tk.CENTER)

        found_candidates: List[Candidate] = []

        def _do_search() -> None:
            query = q_var.get().strip()
            if not query:
                return
            res_tree.delete(*res_tree.get_children())
            found_candidates.clear()

            self.status_var.set("Searching APIs...")
            raw_cands = self.client.search_query(query, count=10)
            ranked = rank_candidates(entry, raw_cands, self.settings.sensitivity)
            found_candidates.extend(ranked)

            for idx, c in enumerate(ranked):
                res_tree.insert(
                    "",
                    tk.END,
                    iid=str(idx),
                    values=(c.source, c.doi, c.year or "-", c.title, f"{c.score:.3f}"),
                )
            self.status_var.set(f"{len(ranked)} result(s) found.")

        ttk.Button(top_f, text="🔍 Search", style="Primary.TButton", command=_do_search).pack(side=tk.LEFT, padx=4)

        res_tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)

        def _apply_selected() -> None:
            sel = res_tree.selection()
            if not sel:
                messagebox.showwarning("Selection", "Please select a candidate in the list.")
                return
            chosen_c = found_candidates[int(sel[0])]
            row.candidate = chosen_c
            row.resolved_title = chosen_c.title
            row.resolved_doi = chosen_c.doi
            row.source = chosen_c.source
            row.confidence = f"{chosen_c.score:.3f}"
            row.title_score = f"{chosen_c.title_score:.3f}"
            row.author_score = f"{chosen_c.author_score:.3f}"
            row.year_score = f"{chosen_c.year_score:.3f}"
            self._load_row_into_inspector(row)
            self._update_tree_item_from_row(row)
            dialog.destroy()
            messagebox.showinfo("Candidate Assigned", "The candidate has been assigned to the entry. You can now click 'Accept Candidate'.")

        bot_f = ttk.Frame(dialog, padding=10)
        bot_f.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(bot_f, text="✅ Assign This Candidate", style="Success.TButton", command=_apply_selected).pack(side=tk.RIGHT, padx=4)
        ttk.Button(bot_f, text="Close", command=dialog.destroy).pack(side=tk.RIGHT, padx=4)

        # Run initial search
        dialog.after(100, _do_search)

    # ========================================================================
    # Output Saving
    # ========================================================================

    def _save_all_outputs(self) -> None:
        if not self.database:
            messagebox.showwarning("Nothing to Save", "No database is currently loaded.")
            return

        self._sync_settings_from_ui()

        try:
            # 1. Save BibTeX file
            export_bibtex_file(self.database, self.settings.output_file)

            # 2. Save CSV & Markdown reports
            write_reports(self.rows, self.settings.report_file, self.settings.summary_file)

            # 3. Save decisions and cache
            self.decisions.save()
            if self.client:
                self.client.save_cache()

            msg = (
                f"Files saved successfully:\n"
                f"- Corrected BibTeX: {self.settings.output_file}\n"
                f"- CSV Report: {self.settings.report_file}\n"
                f"- Markdown Summary: {self.settings.summary_file}"
            )
            messagebox.showinfo("Save Complete", msg)
            self.status_var.set("All files have been saved successfully.")
            self._log_console(f"Files exported: {self.settings.output_file.name}, {self.settings.report_file.name}, {self.settings.summary_file.name}", "SUCCESS")

        except Exception as exc:
            messagebox.showerror("Save Error", f"Failed to save files:\n{exc}")
            self._log_console(f"Save error: {exc}", "ERROR")


def main() -> int:
    root = tk.Tk()
    app = BibAuditApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
