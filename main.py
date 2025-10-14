"""
Gerenciador Firebird
Autor: MMaffi
"""

import os
import ctypes
import sys
import json
import shutil
import subprocess
import tempfile
import zipfile
import psutil
from datetime import datetime
from pathlib import Path
import threading
import logging
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext, simpledialog
import time
import schedule
from typing import Dict, List, Optional
import winreg
import winshell
from win32com.client import Dispatch

# ------- EXECUTA EM MODO ADM -------
def is_admin():
    """Verifica se o programa está sendo executado como administrador"""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

def run_as_admin():
    """Reinicia o programa com elevação de administrador"""
    if not is_admin():
        try:
            ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, " ".join(sys.argv), None, 1
            )
            sys.exit(0)
        except Exception as e:
            logging.error(f"Falha ao solicitar elevação: {e}")
            messagebox.showerror(
                "Erro de Permissão", 
                "Não foi possível executar como administrador.\n"
                "Execute o programa manualmente como Administrador."
            )
            return False
    return True

# ---------- CONFIG ----------
if getattr(sys, 'frozen', False):
    # Executável PyInstaller
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).resolve().parent

CONFIG_PATH = BASE_DIR / "config.json"
LOG_FILE = BASE_DIR / "gerenciador_firebird.log"
DEFAULT_BACKUP_DIR = BASE_DIR / "backups"
DEFAULT_KEEP_BACKUPS = 5

# ---------- LOGGING ----------
def setup_logging():
    LOG_FILE.parent.mkdir(exist_ok=True)
    
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
    
    # Formatação
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    return logger

# ---------- GERENCIADOR DE CONFIG ----------
def load_config():
    """Carrega configurações do JSON"""
    default = {
        "gbak_path": "",
        "gfix_path": "",
        "backup_dir": str(DEFAULT_BACKUP_DIR),
        "keep_backups": DEFAULT_KEEP_BACKUPS,
        "firebird_user": "SYSDBA",
        "firebird_password": "masterkey",
        "firebird_host": "localhost",
        "firebird_port": "26350",  # Porta padrão
        "auto_monitor": True,
        "monitor_interval": 30,
        "minimize_to_tray": True,
        "start_minimized": False,
        "start_with_windows": False,
        "scheduled_backups": []
    }
    
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                conf = json.load(f)
            default.update(conf)
            logging.info("Configurações carregadas com sucesso")
        except Exception as e:
            logging.error(f"Falha ao ler config.json: {e}")
    else:
        try:
            Path(default["backup_dir"]).mkdir(parents=True, exist_ok=True)
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(default, f, indent=2)
            logging.info("Arquivo de configuração criado com sucesso")
        except Exception as e:
            logging.error(f"Falha ao criar config.json: {e}")
    
    return default

def save_config(conf):
    """Salva configurações no JSON"""
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(conf, f, indent=2)
        logging.info("Configurações salvas com sucesso")
        return True
    except Exception as e:
        logging.error(f"Falha ao salvar config.json: {e}")
        return False

# ---------- AUTOMAÇÕES ----------
def find_executable(name):
    """Encontra executáveis do Firebird no sistema"""
    exe = shutil.which(name)
    if exe:
        logging.info(f"Executável encontrado no PATH: {exe}")
        return exe

    common_dirs = [
        "C:\\Program Files\\Firebird",
        "C:\\Program Files (x86)\\Firebird",
        "C:\\Firebird",
    ]
    
    for base in common_dirs:
        if os.path.exists(base):
            for root, dirs, files in os.walk(base):
                if name in files:
                    full_path = os.path.join(root, name)
                    logging.info(f"Executável encontrado: {full_path}")
                    return full_path
    
    logging.warning(f"Executável não encontrado: {name}")
    return ""

def cleanup_old_backups(backup_dir: Path, keep: int):
    """Remove backups antigos mantendo apenas os X mais recentes"""
    try:
        files = list(backup_dir.glob("*.fbk")) + list(backup_dir.glob("*.zip"))
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        
        removed_count = 0
        for old in files[keep:]:
            try:
                old.unlink()
                removed_count += 1
                logging.info(f"Backup antigo removido: {old.name}")
            except Exception as e:
                logging.warning(f"Falha ao remover {old.name}: {e}")
        
        if removed_count > 0:
            logging.info(f"Limpeza concluída: {removed_count} arquivos removidos")
            
    except Exception as e:
        logging.error(f"Erro durante limpeza de backups: {e}")

def kill_firebird_processes():
    """Mata processos do Firebird de forma segura"""
    firebird_processes = [
        "fb_inet_server.exe", "fbserver.exe", "fbguard.exe", 
        "firebird.exe", "ibserver.exe", "gbak.exe", "gfix.exe"
    ]
    
    killed_count = 0
    try:
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                proc_name = proc.info['name'].lower() if proc.info['name'] else ''
                if any(fb_proc in proc_name for fb_proc in [p.lower() for p in firebird_processes]):
                    pid = proc.info['pid']
                    proc_name = proc.info['name']
                    p = psutil.Process(pid)
                    p.terminate()
                    p.wait(timeout=5)
                    killed_count += 1
                    logging.info(f"Processo finalizado: {proc_name} (PID: {pid})")
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
                continue
                
    except Exception as e:
        logging.error(f"Erro ao finalizar processos: {e}")
        return False
    
    logging.info(f"Total de processos finalizados: {killed_count}")
    return killed_count > 0

def get_disk_space(path):
    """Retorna informações de espaço em disco"""
    try:
        usage = shutil.disk_usage(path)
        return {
            'total': usage.total,
            'used': usage.used,
            'free': usage.free,
            'free_gb': usage.free / (1024**3),
            'total_gb': usage.total / (1024**3),
            'percent_used': (usage.used / usage.total) * 100
        }
    except Exception as e:
        logging.error(f"Erro ao verificar espaço em disco: {e}")
        return None

# ------------ APP PRINCIPAL ------------
class GerenciadorFirebirdApp(tk.Tk):
    def __init__(self):
        super().__init__()
        
        self.logger = setup_logging()

        self.dev_buffer = ""
        self.dev_mode = False
        self.scheduled_jobs = []
        self.schedule_thread = None
        self.schedule_running = False
        self.tray_icon = None

        self.bind_all("<F12>", self._toggle_dev_mode)
        self.bind_all("<Key>", self._capture_secret_key)
        
        try:
            self.conf = load_config()
            self._setup_ui()
            self._start_background_tasks()
            self._start_scheduler()
            
            # Verifica e sincroniza a configuração de inicialização com Windows
            current_startup_setting = self.conf.get("start_with_windows", False)
            actual_startup_status = self.is_in_startup()
            
            if current_startup_setting != actual_startup_status:
                self.log("🔄 Sincronizando configuração de inicialização com Windows...", "info")
                self.apply_startup_setting(current_startup_setting)
            
            # Inicia minimizado se configurado
            if self.conf.get("start_minimized", False):
                self.after(1000, self.minimize_to_tray)
            
            self.logger.info("Gerenciador Firebird iniciado com sucesso")
            
        except Exception as e:
            self.logger.critical(f"Falha crítica ao iniciar aplicação: {e}")
            messagebox.showerror("Erro Fatal", f"Falha ao iniciar aplicação:\n{e}")
            sys.exit(1)

    def _setup_ui(self):
        """Configura interface do usuário"""
        self.title("Gerenciador Firebird")
        
        # Ícone da aplicação
        icon_path = BASE_DIR / "images" / "icon.ico"
        if icon_path.exists():
            self.iconbitmap(str(icon_path))

        self.geometry("900x750+100+50")
        self.minsize(800, 700)
        self.configure(bg="#f5f5f5")
        
        self.task_running = False
        
        # Configura fechamento para minimizar para bandeja
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        
        self._create_main_interface()

    def _create_main_interface(self):
        """Cria interface com abas"""
        # Header
        header_frame = ttk.Frame(self)
        header_frame.pack(pady=10, fill="x", padx=10)

        header_frame.columnconfigure(0, weight=1)
        header_frame.columnconfigure(1, weight=0)

        header = ttk.Label(
            header_frame, 
            text="Gerenciador Firebird",
            font=("Arial", 16, "bold")
        )
        header.grid(row=0, column=0, sticky="w")

        controls_frame = ttk.Frame(header_frame)
        controls_frame.grid(row=0, column=1, sticky="e")

        # Botão minimizar para bandeja
        tray_btn = ttk.Button(
            controls_frame,
            text=" ⤵️",
            width=3,
            command=self.minimize_to_tray,
            cursor="hand2"
        )
        tray_btn.pack(side="left", padx=2)

        # Botão configurações
        config_btn = ttk.Button(
            controls_frame,
            text="⚙️ Configurações",
            command=self.config_window,
            cursor="hand2"
        )
        config_btn.pack(side="left", padx=2)

        # Abas
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=5)
        
        # Cria todas as abas
        self._create_dashboard_tab()
        self._create_monitor_tab()
        self._create_scheduler_tab()
        self._create_tools_tab()
        
        # Footer
        self._create_footer()

    def _create_dashboard_tab(self):
        """Cria aba principal"""
        dashboard_frame = ttk.Frame(self.notebook)
        self.notebook.add(dashboard_frame, text="Principal")
        
        # Botões de ação
        btn_frame = ttk.LabelFrame(dashboard_frame, text="Ações", padding=10)
        btn_frame.pack(pady=5, padx=10, fill="x")

        self.btn_backup = ttk.Button(
            btn_frame, 
            text="📦 Gerar Backup",
            cursor="hand2",
            command=self.backup
        )
        self.btn_restore = ttk.Button(
            btn_frame, 
            text="♻️ Restaurar Backup",
            cursor="hand2",
            command=self.restore
        )
        self.btn_verify = ttk.Button(
            btn_frame, 
            text="🩺 Verificar Integridade",
            cursor="hand2",
            command=self.verify
        )
        self.btn_kill = ttk.Button(
            btn_frame, 
            text="🔥 Matar Instâncias",
            cursor="hand2", 
            command=self.kill
        )

        # Layout dos botões
        self.btn_backup.grid(row=0, column=0, padx=5, pady=5, sticky="ew")
        self.btn_restore.grid(row=0, column=1, padx=5, pady=5, sticky="ew")
        self.btn_verify.grid(row=0, column=2, padx=5, pady=5, sticky="ew")
        self.btn_kill.grid(row=0, column=3, padx=5, pady=5, sticky="ew")
        
        for i in range(4):
            btn_frame.columnconfigure(i, weight=1)

        # Status
        status_frame = ttk.Frame(dashboard_frame)
        status_frame.pack(pady=5, fill="x", padx=10)
        
        self.status_label = ttk.Label(
            status_frame, 
            text="Pronto para iniciar operações.",
            foreground="gray",
            font=("Arial", 9)
        )
        self.status_label.pack()

        # Barra de progresso
        self.progress = ttk.Progressbar(
            dashboard_frame, 
            mode="indeterminate", 
            length=500
        )
        self.progress.pack(pady=5)

        # Log
        log_frame = ttk.LabelFrame(dashboard_frame, text="Log de Execução", padding=10)
        log_frame.pack(padx=10, pady=10, fill="both", expand=True)

        self.output = scrolledtext.ScrolledText(log_frame, height=15)
        self.output.pack(fill="both", expand=True)
      
        self.output.tag_config("success", foreground="green")
        self.output.tag_config("error", foreground="red")
        self.output.tag_config("warning", foreground="orange")
        self.output.tag_config("info", foreground="blue")
        self.output.tag_config("debug", foreground="gray")

        self.log("✅ Aplicativo iniciado. Selecione uma ação acima.", "success")

    def _create_monitor_tab(self):
        """Cria aba de monitoramento"""
        monitor_frame = ttk.Frame(self.notebook)
        self.notebook.add(monitor_frame, text="Monitor")
        
        # Status do servidor
        server_frame = ttk.LabelFrame(monitor_frame, text="Status do Servidor", padding=10)
        server_frame.pack(fill="x", padx=10, pady=5)
        
        self.server_status = ttk.Label(server_frame, text="🔄 Verificando status...")
        self.server_status.pack(anchor="w")
        
        # Espaço em disco
        disk_frame = ttk.LabelFrame(monitor_frame, text="Espaço em Disco", padding=10)
        disk_frame.pack(fill="x", padx=10, pady=5)
        
        self.disk_status = ttk.Label(disk_frame, text="🔄 Calculando espaço...")
        self.disk_status.pack(anchor="w")
        
        # Processos
        processes_frame = ttk.LabelFrame(monitor_frame, text="Processos do Firebird", padding=10)
        processes_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        # Treeview para processos
        self.processes_tree = ttk.Treeview(processes_frame, columns=("PID", "Nome", "Status"), show="headings")
        self.processes_tree.heading("PID", text="PID")
        self.processes_tree.heading("Nome", text="Nome do Processo")
        self.processes_tree.heading("Status", text="Status")
        self.processes_tree.column("PID", width=80)
        self.processes_tree.column("Nome", width=200)
        self.processes_tree.column("Status", width=100)
        
        scrollbar = ttk.Scrollbar(processes_frame, orient="vertical", command=self.processes_tree.yview)
        self.processes_tree.configure(yscrollcommand=scrollbar.set)
        
        self.processes_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        # Botões de controle
        control_frame = ttk.Frame(monitor_frame)
        control_frame.pack(fill="x", padx=10, pady=5)
        
        ttk.Button(control_frame, text="🔄 Atualizar",cursor="hand2", command=self.refresh_monitor).pack(side="left", padx=5)
        ttk.Button(control_frame, text="📊 Relatório de Sistema", cursor="hand2", command=self.generate_system_report).pack(side="left", padx=5)

    def _create_scheduler_tab(self):
        """Cria aba de agendamento"""
        sched_frame = ttk.Frame(self.notebook)
        self.notebook.add(sched_frame, text="Agendador")
        
        # Formulário de agendamento
        form_frame = ttk.LabelFrame(sched_frame, text="Novo Agendamento", padding=15)
        form_frame.pack(fill="x", padx=10, pady=10)
        
        # Banco de dados
        ttk.Label(form_frame, text="Banco de dados:").grid(row=0, column=0, sticky="w", pady=8)
        self.sched_db_var = tk.StringVar()
        self.sched_db_entry = ttk.Entry(form_frame, textvariable=self.sched_db_var, width=40)
        self.sched_db_entry.grid(row=0, column=1, padx=5)
        ttk.Button(form_frame, text="...", width=3, command=self.pick_sched_db).grid(row=0, column=2)
        
        # Nome do agendamento
        ttk.Label(form_frame, text="Nome do agendamento:").grid(row=1, column=0, sticky="w", pady=8)
        self.sched_name_var = tk.StringVar()
        ttk.Entry(form_frame, textvariable=self.sched_name_var, width=40).grid(row=1, column=1, padx=5)
        
        # Frequência
        ttk.Label(form_frame, text="Frequência:").grid(row=2, column=0, sticky="w", pady=8)
        self.sched_freq_var = tk.StringVar(value="Diário")
        freq_combo = ttk.Combobox(form_frame, textvariable=self.sched_freq_var, 
                                 values=["Diário", "Semanal", "Mensal"], state="readonly")
        freq_combo.grid(row=2, column=1, padx=5, sticky="w")
        
        # Horário
        ttk.Label(form_frame, text="Horário (HH:MM):").grid(row=3, column=0, sticky="w", pady=8)
        self.sched_time_var = tk.StringVar(value="02:00")
        ttk.Entry(form_frame, textvariable=self.sched_time_var, width=10).grid(row=3, column=1, padx=5, sticky="w")
        
        # Compactar backup
        ttk.Label(form_frame, text="Compactar backup:").grid(row=4, column=0, sticky="w", pady=8)
        self.sched_compress_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(form_frame, variable=self.sched_compress_var).grid(row=4, column=1, sticky="w", padx=5)
        
        # Botão de agendamento
        ttk.Button(form_frame, text="➕ Agendar Backup", 
                  cursor="hand2",
                  command=self.schedule_backup).grid(row=5, column=1, pady=15, sticky="w")
        
        # Lista de agendamentos
        list_frame = ttk.LabelFrame(sched_frame, text="Agendamentos Ativos", padding=10)
        list_frame.pack(fill="both", expand=True, padx=10, pady=10)
        
        self.schedules_tree = ttk.Treeview(list_frame, columns=("Nome", "Banco", "Frequência", "Horário", "Compactar"), show="headings")
        self.schedules_tree.heading("Nome", text="Nome")
        self.schedules_tree.heading("Banco", text="Banco de Dados")
        self.schedules_tree.heading("Frequência", text="Frequência")
        self.schedules_tree.heading("Horário", text="Horário")
        self.schedules_tree.heading("Compactar", text="Compactar")
        
        self.schedules_tree.column("Nome", width=120)
        self.schedules_tree.column("Banco", width=150)
        self.schedules_tree.column("Frequência", width=80)
        self.schedules_tree.column("Horário", width=60)
        self.schedules_tree.column("Compactar", width=60)
        
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.schedules_tree.yview)
        self.schedules_tree.configure(yscrollcommand=scrollbar.set)
        
        self.schedules_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        # Botões de controle
        control_frame = ttk.Frame(list_frame)
        control_frame.pack(fill="x", pady=5)
        
        ttk.Button(control_frame, text="🗑️ Remover Selecionado",
                  cursor="hand2", 
                  command=self.remove_schedule).pack(pady=2)
        
        ttk.Button(control_frame, text="🔄 Recarregar Agendamentos",
                  cursor="hand2",
                  command=self.load_schedules).pack(pady=2)
        
        # Carrega agendamentos salvos
        self.load_schedules()

    def _create_tools_tab(self):
        """Cria aba de ferramentas avançadas"""
        tools_frame = ttk.Frame(self.notebook)
        self.notebook.add(tools_frame, text="Ferramentas")
        
        # Frame de ferramentas
        tools_grid = ttk.Frame(tools_frame, padding=20)
        tools_grid.pack(fill="both", expand=True)
        
        # Otimização
        optimize_btn = ttk.Button(
            tools_grid, 
            text="🔧 Otimizar Banco",
            cursor="hand2", 
            command=self.optimize_database,
            width=20
        )
        optimize_btn.grid(row=0, column=0, padx=10, pady=10, sticky="ew")
        
        # Correção de Banco
        repair_btn = ttk.Button(
            tools_grid, 
            text="🔩 Corrigir Banco",
            cursor="hand2", 
            command=self.repair_database,
            width=20
        )
        repair_btn.grid(row=0, column=1, padx=10, pady=10, sticky="ew")
        
        # Migração
        migrate_btn = ttk.Button(
            tools_grid, 
            text="🔄 Migrar Banco",
            cursor="hand2", 
            command=self.migrate_database,
            width=20
        )
        migrate_btn.grid(row=1, column=0, padx=10, pady=10, sticky="ew")
        
        # Relatório
        report_btn = ttk.Button(
            tools_grid, 
            text="📊 Gerar Relatório",
            cursor="hand2", 
            command=self.generate_system_report,
            width=20
        )
        report_btn.grid(row=1, column=1, padx=10, pady=10, sticky="ew")
        
        # Verificar espaço
        space_btn = ttk.Button(
            tools_grid, 
            text="💾 Verificar Espaço",
            cursor="hand2", 
            command=self.check_disk_space,
            width=20
        )
        space_btn.grid(row=2, column=0, padx=10, pady=10, sticky="ew")
        
        # Importar configurações
        import_btn = ttk.Button(
            tools_grid, 
            text="📥 Importar Config",
            cursor="hand2", 
            command=self.import_config,
            width=20
        )
        import_btn.grid(row=2, column=1, padx=10, pady=10, sticky="ew")

        # Exportar configurações
        export_btn = ttk.Button(
            tools_grid, 
            text="📤 Exportar Config",
            cursor="hand2", 
            command=self.export_config,
            width=20
        )
        export_btn.grid(row=3, column=0, padx=10, pady=10, sticky="ew")
        
        # Configurar colunas
        tools_grid.columnconfigure(0, weight=1)
        tools_grid.columnconfigure(1, weight=1)

    def _create_footer(self):
        """Cria rodapé da aplicação"""
        footer_frame = tk.Frame(self, bg="#f5f5f5", relief="ridge", borderwidth=1)
        footer_frame.pack(side="bottom", fill="x")
        
        APP_VERSION = "2025.10.13.1714"

        footer_left = tk.Label(
            footer_frame,
            text="© 2025 MMaffi. Todos os direitos reservados.",
            font=("Arial", 9),
            bg="#f5f5f5",
            fg="gray",
            anchor="w"
        )
        footer_left.pack(side="left", padx=10, pady=3)

        footer_right = tk.Label(
            footer_frame,
            text=f"Versão {APP_VERSION}",
            font=("Arial", 9),
            bg="#f5f5f5",
            fg="gray",
            anchor="e"
        )
        footer_right.pack(side="right", padx=10, pady=3)

    # ---------- SISTEMA DE BANDEJA ----------
    def create_tray_icon(self):
        """Cria ícone na bandeja do sistema"""
        try:
            import pystray
            from PIL import Image
            import threading
            
            # Tenta carregar um arquivo de imagem
            icon_paths = [
                BASE_DIR / "images" / "icon.ico"
            ]
            
            image = None
            for icon_path in icon_paths:
                if icon_path.exists():
                    try:
                        image = Image.open(icon_path)
                        # Redimensiona para tamanho padrão da bandeja
                        image = image.resize((32, 32), Image.Resampling.LANCZOS)
                        self.log(f"📌 Ícone da bandeja carregado: {icon_path}", "info")
                        break
                    except Exception as e:
                        continue
            
            # Se não encontrou arquivo cria ícone padrão
            if image is None:
                from PIL import ImageDraw
                image = Image.new('RGB', (32, 32), color='#2c3e50')
                draw = ImageDraw.Draw(image)
                
                draw.text((10, 6), "F", fill="white", font=None)
                self.log("📌 Usando ícone padrão da bandeja", "info")
            
            # Menu do ícone
            menu = pystray.Menu(
                pystray.MenuItem("Abrir Gerenciador Firebird", self.restore_from_tray),
                pystray.MenuItem("Sair", self.quit_application)
            )
            
            # Cria o ícone
            self.tray_icon = pystray.Icon("gerenciador_firebird", image, "Gerenciador Firebird", menu)
            
            # Inicia o ícone em uma thread separada
            def run_tray():
                try:
                    self.tray_icon.run()
                except Exception as e:
                    self.log(f"❌ Erro no ícone da bandeja: {e}", "error")
            
            tray_thread = threading.Thread(target=run_tray, daemon=True)
            tray_thread.start()
            
            self.log("📌 Ícone da bandeja criado", "info")
            
        except ImportError:
            self.log("⚠️ Biblioteca pystray não encontrada. Instale com: pip install pystray pillow", "warning")
            self.tray_icon = None

    def minimize_to_tray(self):
        """Minimiza o programa para a bandeja do sistema"""
        if self.conf.get("minimize_to_tray", True):
            self.withdraw()
            self.create_tray_icon()
            self.log("📌 Programa minimizado para bandeja do sistema", "info")
        else:
            self.iconify()

    def restore_from_tray(self, icon=None, item=None):
        """Restaura o programa da bandeja"""
        if self.tray_icon:
            self.tray_icon.stop()
            self.tray_icon = None
        
        self.deiconify()
        self.state('normal')
        self.lift()
        self.focus_force()
        self.log("🔄 Programa restaurado da bandeja", "info")

    def quit_application(self, icon=None, item=None):
        """Fecha o aplicativo completamente"""
        if self.tray_icon:
            self.tray_icon.stop()
        
        self.schedule_running = False
        self.quit()
        self.destroy()

    def on_close(self):
        """Lida com o fechamento da janela"""
        if self.conf.get("minimize_to_tray", True):
            self.minimize_to_tray()
        else:
            self.quit_application()

    def _start_background_tasks(self):
        """Inicia tarefas em background"""
        if self.conf.get("auto_monitor", True):
            self.after(5000, self.auto_refresh_monitor)

    def _start_scheduler(self):
        """Inicia o agendador de backups"""
        self.schedule_running = True
        self.schedule_thread = threading.Thread(target=self._schedule_worker, daemon=True)
        self.schedule_thread.start()
        self.log("🕒 Agendador de backups iniciado", "info")

    def _schedule_worker(self):
        """Worker thread para executar agendamentos"""
        while self.schedule_running:
            try:
                schedule.run_pending()
            except Exception as e:
                self.log(f"❌ Erro no agendador: {e}", "error")
            time.sleep(60)  # Verifica a cada minuto

    # ---------- INICIALIZAÇÃO COM WINDOWS ----------
    def toggle_startup(self, enabled):
        self.apply_startup_setting(enabled)

    def apply_startup_setting(self, enabled):
        """Aplica a configuração de inicialização com Windows"""
        try:
            if enabled:
                self.add_to_startup()
            else:
                self.remove_from_startup()
        except Exception as e:
            self.log(f"❌ Erro ao configurar inicialização com Windows: {e}", "error")

    def add_to_startup(self):
        """Adiciona o programa à inicialização do Windows"""
        try:
            # Usando winshell
            startup_folder = winshell.startup()
            script_path = sys.executable if getattr(sys, 'frozen', False) else sys.argv[0]
            
            # Cria o atalho
            shortcut_path = os.path.join(startup_folder, "Gerenciador Firebird.lnk")
            
            shell = Dispatch('WScript.Shell')
            shortcut = shell.CreateShortCut(shortcut_path)
            shortcut.Targetpath = script_path
            shortcut.WorkingDirectory = os.path.dirname(script_path)
            shortcut.Description = "Gerenciador Firebird"
            
            # Adiciona argumento para iniciar minimizado se configurado
            if self.conf.get("start_minimized", False):
                shortcut.Arguments = "--minimized"
            
            shortcut.save()
            
            self.log("✅ Programa adicionado à inicialização do Windows", "success")
            return True
            
        except Exception as e:
            self.log(f"❌ Erro ao adicionar à inicialização: {e}", "error")

            return self._add_to_startup_registry()

    def _add_to_startup_registry(self):
        """Método alternativo usando registro do Windows"""
        try:
            script_path = sys.executable if getattr(sys, 'frozen', False) else sys.argv[0]
            
            # Adiciona argumento para iniciar minimizado se configurado
            if self.conf.get("start_minimized", False):
                script_path = f'"{script_path}" --minimized'
            else:
                script_path = f'"{script_path}"'
            
            key = winreg.HKEY_CURRENT_USER
            subkey = r"Software\Microsoft\Windows\CurrentVersion\Run"
            
            with winreg.OpenKey(key, subkey, 0, winreg.KEY_SET_VALUE) as reg_key:
                winreg.SetValueEx(reg_key, "Gerenciador Firebird", 0, winreg.REG_SZ, script_path)
            
            self.log("✅ Programa adicionado à inicialização via registro", "success")
            return True
            
        except Exception as e:
            self.log(f"❌ Erro ao adicionar ao registro: {e}", "error")
            return False

    def remove_from_startup(self):
        """Remove o programa da inicialização do Windows"""
        try:
            # Remove atalho da pasta Inicializar
            startup_folder = winshell.startup()
            shortcut_path = os.path.join(startup_folder, "Gerenciador Firebird.lnk")
            
            if os.path.exists(shortcut_path):
                os.remove(shortcut_path)
                self.log("✅ Programa removido da inicialização (atalho)", "success")
            
            # Remove do registro
            self._remove_from_startup_registry()
            
            return True
            
        except Exception as e:
            self.log(f"❌ Erro ao remover da inicialização: {e}", "error")
            return False

    def _remove_from_startup_registry(self):
        """Remove do registro do Windows"""
        try:
            key = winreg.HKEY_CURRENT_USER
            subkey = r"Software\Microsoft\Windows\CurrentVersion\Run"
            
            with winreg.OpenKey(key, subkey, 0, winreg.KEY_SET_VALUE) as reg_key:
                try:
                    winreg.DeleteValue(reg_key, "Gerenciador Firebird")
                    self.log("✅ Programa removido da inicialização (registro)", "success")
                except FileNotFoundError:
                    pass
                    
        except Exception as e:
            self.log(f"❌ Erro ao remover do registro: {e}", "error")

    def is_in_startup(self):
        try:
            # Verifica no registro
            key = winreg.HKEY_CURRENT_USER
            subkey = r"Software\Microsoft\Windows\CurrentVersion\Run"
            
            with winreg.OpenKey(key, subkey, 0, winreg.KEY_READ) as reg_key:
                try:
                    winreg.QueryValueEx(reg_key, "Gerenciador Firebird")
                    return True
                except FileNotFoundError:
                    pass
            
            # Verifica na pasta Inicializar
            startup_folder = winshell.startup()
            shortcut_path = os.path.join(startup_folder, "Gerenciador Firebird.lnk")
            return os.path.exists(shortcut_path)
            
        except Exception:
            return False

    # ---------- UTILIDADES ----------
    def log(self, msg, tag="info"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_entry = f"[{timestamp}] {msg}\n"
        
        self.output.insert(tk.END, log_entry, tag)
        self.output.see(tk.END)

        if tag == "error":
            self.logger.error(msg)
        elif tag == "warning":
            self.logger.warning(msg)
        elif tag == "success":
            self.logger.info(msg)
        else:
            self.logger.info(msg)

    def set_status(self, text, color="gray"):
        """Atualiza status da aplicação"""
        self.status_label.config(text=text, foreground=color)
        self.update_idletasks()

    def disable_buttons(self):
        """Desabilita todos os botões durante operações"""
        buttons = [self.btn_backup, self.btn_restore, self.btn_verify, self.btn_kill]
        for btn in buttons:
            btn.state(["disabled"])

    def enable_buttons(self):
        """Reabilita todos os botões"""
        buttons = [self.btn_backup, self.btn_restore, self.btn_verify, self.btn_kill]
        for btn in buttons:
            btn.state(["!disabled"])

    def _toggle_dev_mode(self, event=None):
        """Ativa/desativa o modo dev"""
        if not self.dev_mode:
            self.dev_mode = True
            self.dev_buffer = ""

            # Timer de 3 segundos para cancelar automaticamente
            self.dev_timer = self.after(3000, self._cancel_dev_mode)
            return

        if hasattr(self, "dev_timer"):
            self.after_cancel(self.dev_timer)
            del self.dev_timer

        if self.dev_buffer.strip().lower() == "script":
            self.open_script_console()

        self.dev_mode = False
        self.dev_buffer = ""

    def _cancel_dev_mode(self):
        self.dev_mode = False
        self.dev_buffer = ""

    def _capture_secret_key(self, event):
        if self.dev_mode and event.keysym != "F12":
            if event.keysym == "Return":
                return
            elif event.keysym == "BackSpace":
                self.dev_buffer = self.dev_buffer[:-1]
            else:
                self.dev_buffer += event.char

    # ---------- EXECUÇÃO DE COMANDOS ----------
    def run_command(self, cmd, on_finish=None):
        """Executa comandos em thread separada"""
        def worker():
            self.task_running = True
            self.disable_buttons()
            self.progress.start(10)
            self.set_status("Executando operação...", "blue")

            try:
                self.log(f"Executando comando: {' '.join(cmd)}", "debug")

                CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors='replace',
                    creationflags=CREATE_NO_WINDOW
                )

                for line in iter(process.stdout.readline, ''):
                    if line.strip():
                        self.log(line.strip(), "info")

                process.stdout.close()
                return_code = process.wait()

                if return_code == 0:
                    self.set_status("✅ Operação concluída com sucesso!", "green")
                    self.log("✔️ Comando executado com sucesso.", "success")
                    self.bell()
                else:
                    self.set_status("⚠️ Ocorreu um erro. Veja o log abaixo.", "red")
                    self.log(f"⚠️ Comando retornou código de erro: {return_code}", "error")

            except FileNotFoundError:
                error_msg = "Erro: Arquivo executável não encontrado. Verifique as configurações."
                self.log(error_msg, "error")
                self.set_status("❌ Executável não encontrado.", "red")
            except Exception as e:
                error_msg = f"Erro inesperado: {str(e)}"
                self.log(error_msg, "error")
                self.set_status("❌ Falha inesperada.", "red")
            finally:
                self.progress.stop()
                self.enable_buttons()
                self.task_running = False
                if on_finish:
                    self.after(100, on_finish)

        threading.Thread(target=worker, daemon=True).start()

    def _get_connection_string(self):
        """Retorna a string de conexão com host e porta"""
        host = self.conf.get("firebird_host", "localhost")
        port = self.conf.get("firebird_port", "26350")
        return f"{host}/{port}"

    def _get_service_mgr_string(self):
        """Retorna a string de conexão para service_mgr com porta"""
        host = self.conf.get("firebird_host", "localhost")
        port = self.conf.get("firebird_port", "26350")
        return f"{host}/{port}:service_mgr"

    # ---------- FUNÇÕES PRINCIPAIS ----------
    def backup(self):
        """Gera backup do banco de dados"""
        gbak = self.conf.get("gbak_path") or find_executable("gbak.exe")
        if not gbak:
            messagebox.showerror("Erro", "gbak.exe não encontrado. Configure o caminho nas configurações.")
            return
        
        self.conf["gbak_path"] = gbak
        save_config(self.conf)

        db = filedialog.askopenfilename(
            title="Selecione o banco de dados (.fdb)", 
            filetypes=[("Firebird Database", "*.fdb"), ("Todos os arquivos", "*.*")]
        )
        if not db:
            return

        backup_dir = Path(self.conf.get("backup_dir", BASE_DIR / "backups"))
        backup_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        db_name = Path(db).stem
        name = f"backup_{db_name}_{timestamp}.fbk"
        backup_path = backup_dir / name

        compress = messagebox.askyesno(
            "Compactar Backup", 
            "Deseja compactar o backup após gerar?\n\n"
            "✅ Compactado: Economiza espaço\n"
            "❌ Não compactado: Restauração mais rápida"
        )

        # Constrói comando gbak geração
        cmd = [
            gbak, "-b", 
            "-se", self._get_service_mgr_string(),
            db, 
            str(backup_path), 
            "-user", self.conf.get("firebird_user", "SYSDBA"), 
            "-pass", self.conf.get("firebird_password", "masterkey")
        ]

        self.log(f"🟦 Iniciando backup: {db} -> {backup_path}", "info")
        self.log(f"🔌 Conectando em: {self._get_service_mgr_string()}", "info")
        self.set_status("Gerando backup, por favor aguarde...", "blue")

        def after_backup():
            if compress and backup_path.exists():
                try:
                    zip_path = backup_path.with_suffix(".zip")
                    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                        z.write(backup_path, arcname=backup_path.name)
                    backup_path.unlink()
                    self.log(f"🟩 Backup compactado: {zip_path}", "success")
                except Exception as e:
                    self.log(f"Erro ao compactar backup: {e}", "error")
            
            # Limpa backups antigos
            keep_count = int(self.conf.get("keep_backups", DEFAULT_KEEP_BACKUPS))
            cleanup_old_backups(backup_dir, keep_count)
            
            self.logger.info(f"Backup finalizado com sucesso: {db}")

        self.run_command(cmd, on_finish=after_backup)

    def execute_scheduled_backup(self, db_path, schedule_name, compress=True):
        """Executa um backup agendado"""
        try:
            gbak = self.conf.get("gbak_path") or find_executable("gbak.exe")
            if not gbak or not os.path.exists(db_path):
                self.log(f"❌ Backup agendado '{schedule_name}' falhou: Banco não encontrado", "error")
                return

            backup_dir = Path(self.conf.get("backup_dir", DEFAULT_BACKUP_DIR))
            backup_dir.mkdir(parents=True, exist_ok=True)
            
            timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
            db_name = Path(db_path).stem
            name = f"backup_{db_name}_{timestamp}.fbk"
            backup_path = backup_dir / name

            self.log(f"🕒 Executando backup agendado: {schedule_name}", "info")
            self.log(f"🔌 Conectando em: {self._get_service_mgr_string()}", "info")

            cmd = [
                gbak, "-b", 
                "-se", self._get_service_mgr_string(),
                db_path, 
                str(backup_path), 
                "-user", self.conf.get("firebird_user", "SYSDBA"), 
                "-pass", self.conf.get("firebird_password", "masterkey")
            ]

            def run_scheduled_backup():
                try:
                    process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors='replace',
                        creationflags=subprocess.CREATE_NO_WINDOW
                    )

                    output, _ = process.communicate()
                    return_code = process.wait()

                    if return_code == 0:
                        if compress and backup_path.exists():
                            zip_path = backup_path.with_suffix(".zip")
                            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                                z.write(backup_path, arcname=backup_path.name)
                            backup_path.unlink()
                        
                        keep_count = int(self.conf.get("keep_backups", DEFAULT_KEEP_BACKUPS))
                        cleanup_old_backups(backup_dir, keep_count)
                        
                        self.log(f"✅ Backup agendado '{schedule_name}' concluído com sucesso", "success")
                    else:
                        self.log(f"❌ Backup agendado '{schedule_name}' falhou: {output}", "error")

                except Exception as e:
                    self.log(f"❌ Erro no backup agendado '{schedule_name}': {e}", "error")

            # Executa em thread separada para não travar a interface
            threading.Thread(target=run_scheduled_backup, daemon=True).start()

        except Exception as e:
            self.log(f"❌ Erro ao executar backup agendado '{schedule_name}': {e}", "error")

    def restore(self):
        """Restaura backup para banco de dados"""
        gbak = self.conf.get("gbak_path") or find_executable("gbak.exe")
        if not gbak:
            messagebox.showerror("Erro", "gbak.exe não encontrado. Configure o caminho nas configurações.")
            return
        
        self.conf["gbak_path"] = gbak
        save_config(self.conf)

        bkp = filedialog.askopenfilename(
            title="Selecione o arquivo de backup", 
            filetypes=[("Backup Files", "*.fbk *.zip"), ("Todos os arquivos", "*.*")]
        )
        if not bkp:
            return

        tmpdir = None
        actual_backup = bkp
        extracted_files = []
        
        # Extrai se for arquivo ZIP
        if bkp.lower().endswith(".zip"):
            try:
                # Extrai caso for ZIP
                zip_path = Path(bkp)
                extract_dir = zip_path.parent / f"{zip_path.stem}_extracted"
                extract_dir.mkdir(exist_ok=True)
                
                self.log(f"Extraindo arquivo ZIP para: {extract_dir}", "info")
                
                with zipfile.ZipFile(bkp, "r") as z:
                    z.extractall(extract_dir)
                
                fbks = list(extract_dir.glob("*.fbk"))
                if not fbks:
                    messagebox.showerror("Erro", "Nenhum arquivo .fbk encontrado dentro do ZIP.")
                    shutil.rmtree(extract_dir, ignore_errors=True)
                    return
                
                actual_backup = str(fbks[0])
                extracted_files.append(extract_dir)
                self.log(f"Arquivo extraído: {actual_backup}", "success")
                
            except Exception as e:
                messagebox.showerror("Erro", f"Falha ao extrair arquivo ZIP: {e}")
                if extract_dir.exists():
                    shutil.rmtree(extract_dir, ignore_errors=True)
                return

        dest = filedialog.asksaveasfilename(
            title="Salvar banco restaurado como...",
            defaultextension=".fdb",
            filetypes=[("Firebird Database", "*.fdb")]
        )
        if not dest:
            # Limpa arquivos extraídos se o usuário cancelar
            for item in extracted_files:
                if Path(item).exists():
                    if Path(item).is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        Path(item).unlink(missing_ok=True)
            return

        # Constrói comando gbak restauração
        cmd = [
            gbak, "-c", 
            "-se", self._get_service_mgr_string(),
            actual_backup, 
            dest, 
            "-user", self.conf.get("firebird_user", "SYSDBA"), 
            "-pass", self.conf.get("firebird_password", "masterkey"),
            "-rep"
        ]

        self.log(f"🟦 Restaurando backup: {actual_backup} -> {dest}", "info")
        self.log(f"🔌 Conectando em: {self._get_service_mgr_string()}", "info")
        self.set_status("Restaurando banco, aguarde...", "blue")

        def cleanup_extracted():
            """Limpa arquivos extraídos após a restauração"""
            for item in extracted_files:
                if Path(item).exists():
                    try:
                        if Path(item).is_dir():
                            shutil.rmtree(item, ignore_errors=True)
                            self.log(f"🗑️ Pasta de extração removida: {item}", "info")
                        else:
                            Path(item).unlink(missing_ok=True)
                            self.log(f"🗑️ Arquivo temporário removido: {item}", "info")
                    except Exception as e:
                        self.log(f"⚠️ Erro ao remover arquivos extraídos {item}: {e}", "warning")

        self.run_command(cmd, on_finish=cleanup_extracted)

    def verify(self):
        """Verifica integridade do banco e oferece correção se necessário"""
        gfix = self.conf.get("gfix_path") or find_executable("gfix.exe")
        if not gfix:
            messagebox.showerror("Erro", "gfix.exe não encontrado. Configure o caminho nas configurações.")
            return
        
        self.conf["gfix_path"] = gfix
        save_config(self.conf)

        db = filedialog.askopenfilename(
            title="Selecione o banco de dados para verificação", 
            filetypes=[("Firebird Database", "*.fdb"), ("Todos os arquivos", "*.*")]
        )
        if not db:
            return

        cmd = [
            gfix, "-v", "-full", 
            db, 
            "-user", self.conf.get("firebird_user", "SYSDBA"), 
            "-pass", self.conf.get("firebird_password", "masterkey")
        ]

        self.log(f"🩺 Verificando integridade: {db}", "info")
        self.set_status("Executando verificação completa...", "blue")
        
        def after_verify():
            """Callback após verificação"""
            self._run_verify_with_output(cmd, db)
        
        self.run_command(cmd, on_finish=after_verify)

    def _run_verify_with_output(self, cmd, db_path):
        def worker():
            try:
                self.log("📋 Analisando resultado da verificação...", "info")
                
                CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors='replace',
                    creationflags=CREATE_NO_WINDOW
                )

                output_lines = []
                for line in iter(process.stdout.readline, ''):
                    if line.strip():
                        output_lines.append(line.strip())
                        self.log(line.strip(), "info")

                process.stdout.close()
                return_code = process.wait()

                output_text = "\n".join(output_lines)
                
                # Analisa se há erros
                has_correctable_errors = self._analyze_verify_output(output_text)
                
                if has_correctable_errors:
                    self.after(0, lambda: self._offer_correction(db_path, output_text))
                else:
                    if return_code == 0:
                        self.after(0, lambda: self.set_status("✅ Verificação concluída - Sem erros encontrados", "green"))
                        self.log("✅ Verificação concluída - Sem erros encontrados", "success")
                    else:
                        self.after(0, lambda: self.set_status("⚠️ Verificação concluída com erros", "orange"))

            except Exception as e:
                self.after(0, lambda: self.log(f"❌ Erro na análise: {e}", "error"))

        threading.Thread(target=worker, daemon=True).start()

    def _analyze_verify_output(self, output_text):
        """Analisa erros"""
        # Padrões de erros que podem ser corrigidos com gfix
        correctable_patterns = [
            "corrupt",
            "damage",
            "broken",
            "checksum error",
            "checksum mismatch",
            "validation error",
            "structural error",
            "index is broken",
            "transaction inventory page is corrupt",
            "bad checksum",
            "page is used twice",
            "wrong page type",
            "orphan node",
            "missing index node",
            "blob not found"
        ]
        
        output_lower = output_text.lower()
        for pattern in correctable_patterns:
            if pattern in output_lower:
                self.log(f"🔍 Erro corrigível detectado: {pattern}", "warning")
                return True
        
        return False

    def _offer_correction(self, db_path, verify_output):
        """Oferece opção de correção quando erros são detectados"""
        db_name = Path(db_path).name
        
        # Cria janela personalizada
        correction_win = tk.Toplevel(self)
        correction_win.title("Correção de Erros Detectados")
        correction_win.geometry("600x400")
        correction_win.resizable(True, True)
        correction_win.transient(self)
        correction_win.grab_set()
        
        # Centraliza
        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 300
        y = self.winfo_y() + (self.winfo_height() // 2) - 200
        correction_win.geometry(f"+{x}+{y}")
        
        # Ícone
        icon_path = BASE_DIR / "images" / "icon.ico"
        if icon_path.exists():
            correction_win.iconbitmap(str(icon_path))
        
        # Frame principal
        main_frame = ttk.Frame(correction_win, padding=15)
        main_frame.pack(fill="both", expand=True)
        
        # Título
        ttk.Label(main_frame, 
                text="🚨 ERROS DETECTADOS NO BANCO DE DADOS",
                font=("Arial", 12, "bold"),
                foreground="red").pack(pady=(0, 10))
        
        ttk.Label(main_frame,
                text=f"Banco: {db_name}",
                font=("Arial", 10, "bold")).pack(pady=(0, 5))
        
        # Aviso
        warning_frame = ttk.LabelFrame(main_frame, text="⚠️ AVISO DE SEGURANÇA", padding=10)
        warning_frame.pack(fill="x", pady=10)
        
        warning_text = (
            "Foram detectados erros no banco de dados que PODEM ser corrigidos automaticamente.\n\n"
            "🚨 É EXTREMAMENTE RECOMENDADO criar uma cópia de segurança do banco antes \n"
            "de prosseguir com a correção, pois o processo pode ser irreversível.\n\n"
            "Deseja criar um backup de segurança agora?"
        )
        
        ttk.Label(warning_frame, text=warning_text, justify="left").pack()
        
        # Detalhes dos erros
        details_frame = ttk.LabelFrame(main_frame, text="📋 Detalhes dos Erros Detectados", padding=10)
        details_frame.pack(fill="both", expand=True, pady=10)
        
        details_text = scrolledtext.ScrolledText(details_frame, height=8, wrap=tk.WORD)
        details_text.pack(fill="both", expand=True)
        details_text.insert("1.0", verify_output)
        details_text.config(state="disabled")
        
        # Frame de botões
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill="x", pady=15)
        
        def create_backup_and_fix():
            """Cria backup e depois executa correção"""
            correction_win.destroy()
            self._create_safety_backup(db_path, lambda: self._execute_correction(db_path))
        
        def fix_without_backup():
            """Executa correção sem backup"""
            if not messagebox.askyesno(
                "Confirmação de Risco",
                "⚠️ ALTO RISCO ⚠️\n\n"
                "Você está prestes a executar uma correção sem backup de segurança.\n"
                "Esta operação pode corromper permanentemente o banco de dados.\n\n"
                "Tem certeza que deseja continuar SEM backup?",
                icon=messagebox.WARNING
            ):
                return
            
            correction_win.destroy()
            self._execute_correction(db_path)
        
        def cancel_operation():
            """Cancela a operação"""
            correction_win.destroy()
            self.log("❌ Correção cancelada pelo usuário", "warning")
        
        # Botões
        ttk.Button(btn_frame, 
                text="💾 Criar Backup e Corrigir",
                command=create_backup_and_fix,
                cursor="hand2").pack(side="left", padx=5)
        
        ttk.Button(btn_frame,
                text="⚡ Corrigir sem Backup (RISCO)",
                command=fix_without_backup,
                cursor="hand2").pack(side="left", padx=5)
        
        ttk.Button(btn_frame,
                text="❌ Cancelar",
                command=cancel_operation,
                cursor="hand2").pack(side="right", padx=5)

    def _create_safety_backup(self, db_path, on_complete):
        """Cria um backup de segurança"""
        gbak = self.conf.get("gbak_path") or find_executable("gbak.exe")
        if not gbak:
            messagebox.showerror("Erro", "gbak.exe não encontrado para criar backup de segurança.")
            return
        
        backup_dir = Path(self.conf.get("backup_dir", DEFAULT_BACKUP_DIR))
        safety_dir = backup_dir / "safety_backups"
        safety_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        db_name = Path(db_path).stem
        backup_name = f"safety_backup_{db_name}_{timestamp}.fbk"
        backup_path = safety_dir / backup_name
        
        self.log(f"🛡️ Criando backup de segurança: {backup_path}", "info")
        self.log(f"🔌 Conectando em: {self._get_service_mgr_string()}", "info")
        
        cmd = [
            gbak, "-b", 
            "-se", self._get_service_mgr_string(),
            db_path, 
            str(backup_path), 
            "-user", self.conf.get("firebird_user", "SYSDBA"), 
            "-pass", self.conf.get("firebird_password", "masterkey")
        ]
        
        def after_backup():
            self.log(f"✅ Backup de segurança criado: {backup_path}", "success")
            on_complete()
        
        self.run_command(cmd, on_finish=after_backup)

    def _execute_correction(self, db_path):
        """Executa o comando de correção do banco"""
        gfix = self.conf.get("gfix_path") or find_executable("gfix.exe")
        if not gfix:
            messagebox.showerror("Erro", "gfix.exe não encontrado.")
            return
        
        self.log("🔧 Iniciando correção do banco de dados...", "warning")
        
        # Comando de correção
        cmd = [
            gfix, "-mend", "-ig",
            db_path,
            "-user", self.conf.get("firebird_user", "SYSDBA"),
            "-pass", self.conf.get("firebird_password", "masterkey")
        ]
        
        self.log(f"⚙️ Comando de correção: {' '.join(cmd)}", "info")
        self.set_status("Executando correção do banco...", "orange")
        
        def after_correction():
            """Callback após correção"""
            self.log("✅ Correção concluída. Verificando resultado...", "info")
            
            # Executa nova verificação para confirmar correção
            verify_cmd = [
                gfix, "-v", "-full", 
                db_path, 
                "-user", self.conf.get("firebird_user", "SYSDBA"), 
                "-pass", self.conf.get("firebird_password", "masterkey")
            ]
            
            def after_reverify():
                self.set_status("✅ Processo de correção finalizado", "green")
                messagebox.showinfo(
                    "Correção Concluída", 
                    "O processo de correção foi finalizado.\n\n"
                    "Verifique o log para detalhes sobre o resultado da operação."
                )
            
            self.run_command(verify_cmd, on_finish=after_reverify)
        
        self.run_command(cmd, on_finish=after_correction)

    def repair_database(self):
        """Executa correção completa do banco de dados"""
        gfix = self.conf.get("gfix_path") or find_executable("gfix.exe")
        if not gfix:
            messagebox.showerror("Erro", "gfix.exe não encontrado. Configure o caminho nas configurações.")
            return
        
        self.conf["gfix_path"] = gfix
        save_config(self.conf)

        db = filedialog.askopenfilename(
            title="Selecione o banco de dados para correção", 
            filetypes=[("Firebird Database", "*.fdb"), ("Todos os arquivos", "*.*")]
        )
        if not db:
            return

        # Pergunta se deseja criar backup de segurança
        response = messagebox.askyesno(
            "Correção de Banco - Backup de Segurança",
            "🚨 CORREÇÃO DE BANCO DE DADOS 🚨\n\n"
            "Esta operação tentará corrigir erros estruturais no banco.\n\n"
            "É EXTREMAMENTE RECOMENDADO criar um backup de segurança\n"
            "antes de prosseguir, pois a correção pode ser irreversível.\n\n"
            "Deseja criar um backup de segurança agora?",
            icon=messagebox.WARNING
        )
        
        if response:
            # Cria backup de segurança antes da correção
            self._create_safety_backup(db, lambda: self._execute_advanced_repair(db))
        else:
            # Pergunta confirmação para prosseguir sem backup
            if messagebox.askyesno(
                "Confirmação de Risco",
                "⚠️ ALTO RISCO ⚠️\n\n"
                "Você está prestes a executar uma correção sem backup de segurança.\n"
                "Esta operação pode corromper permanentemente o banco de dados.\n\n"
                "Tem certeza que deseja continuar SEM backup?",
                icon=messagebox.WARNING
            ):
                self._execute_advanced_repair(db)

    def _execute_advanced_repair(self, db_path):
        """Executa correção avançada do banco"""
        gfix = self.conf.get("gfix_path") or find_executable("gfix.exe")
        if not gfix:
            return
        
        self.log("🛠️ Iniciando correção avançada do banco...", "warning")
        self.set_status("Executando correção avançada...", "orange")
        
        # Sequência de comandos de correção
        repair_commands = [
            {
                "name": "Limpeza de transações",
                "cmd": [gfix, "-sweep", db_path, "-user", self.conf["firebird_user"], "-pass", self.conf["firebird_password"]]
            },
            {
                "name": "Correção de índices",
                "cmd": [gfix, "-mend", "-ignore", db_path, "-user", self.conf["firebird_user"], "-pass", self.conf["firebird_password"]]
            },
            {
                "name": "Validação completa",
                "cmd": [gfix, "-validate", "-full", db_path, "-user", self.conf["firebird_user"], "-pass", self.conf["firebird_password"]]
            },
            {
                "name": "Correção de páginas",
                "cmd": [gfix, "-mend", "-ig", db_path, "-user", self.conf["firebird_user"], "-pass", self.conf["firebird_password"]]
            }
        ]
        
        def run_next_command(index=0):
            if index < len(repair_commands):
                command_info = repair_commands[index]
                self.log(f"🔧 Executando: {command_info['name']}", "info")
                
                def after_command():
                    self.log(f"✅ {command_info['name']} concluído", "success")
                    run_next_command(index + 1)
                
                self.run_command(command_info['cmd'], after_command)
            else:
                self.log("✅ Correção avançada concluída!", "success")
                self.set_status("Correção avançada concluída", "green")
                
                # Executa verificação final
                verify_cmd = [
                    gfix, "-v", "-full", 
                    db_path, 
                    "-user", self.conf.get("firebird_user", "SYSDBA"), 
                    "-pass", self.conf.get("firebird_password", "masterkey")
                ]
                
                def after_final_verify():
                    messagebox.showinfo(
                        "Correção Concluída",
                        "✅ Correção avançada do banco concluída!\n\n"
                        "Todos os procedimentos de correção foram executados.\n"
                        "Verifique o log para detalhes sobre o resultado."
                    )
                
                self.run_command(verify_cmd, on_finish=after_final_verify)
        
        # Inicia a sequência de correção
        run_next_command()

    def kill(self):
        """Finaliza processos do Firebird"""
        self.log("🚫 Iniciando finalização de processos do Firebird...", "warning")
        self.set_status("Finalizando processos do Firebird...", "orange")
        
        def kill_processes():
            success = kill_firebird_processes()
            self.after(0, lambda: self._on_kill_complete(success))
        
        threading.Thread(target=kill_processes, daemon=True).start()

    def _on_kill_complete(self, success):
        """Callback após finalizar processos"""
        if success:
            self.set_status("✅ Processos do Firebird finalizados!", "green")
            self.log("✅ Todos os processos do Firebird foram finalizados com sucesso.", "success")
        else:
            self.set_status("ℹ️ Nenhum processo do Firebird encontrado ou erro ao finalizar.", "blue")
            self.log("ℹ️ Nenhum processo do Firebird em execução ou erro ao finalizar.", "info")

    # ---------- MONITORAMENTO ----------
    def refresh_monitor(self):
        """Atualiza informações do monitor"""
        try:
            # Atualiza status do servidor
            self._update_server_status()
            
            # Atualiza espaço em disco
            self._update_disk_space()
            
            # Atualiza lista de processos
            self._update_processes_list()
            
        except Exception as e:
            self.log(f"❌ Erro ao atualizar monitor: {e}", "error")

    def _update_server_status(self):
        """Atualiza status do servidor Firebird"""
        try:
            # Verifica se o serviço está rodando
            firebird_processes = []
            for proc in psutil.process_iter(['name']):
                if proc.info['name'] and any(fb in proc.info['name'].lower() 
                                           for fb in ['firebird', 'fb_inet', 'fbserver']):
                    firebird_processes.append(proc.info['name'])
            
            if firebird_processes:
                status = f"✅ Online - Processos: {', '.join(set(firebird_processes))}"
                port = self.conf.get("firebird_port", "26350")
                status += f" (Porta: {port})"
            else:
                status = "❌ Offline - Nenhum processo encontrado"
                
            self.server_status.config(text=status)
            
        except Exception as e:
            self.server_status.config(text=f"❌ Erro: {str(e)}")

    def _update_disk_space(self):
        """Atualiza informações de espaço em disco"""
        try:
            backup_dir = Path(self.conf.get("backup_dir", DEFAULT_BACKUP_DIR))
            disk_info = get_disk_space(backup_dir)
            
            if disk_info:
                status = (f"💾 Total: {disk_info['total_gb']:.1f}GB | "
                         f"Livre: {disk_info['free_gb']:.1f}GB | "
                         f"Usado: {disk_info['percent_used']:.1f}%")
                
                if disk_info['free_gb'] < 1:
                    status += " ⚠️ ESPAÇO CRÍTICO"
                elif disk_info['free_gb'] < 5:
                    status += " ⚠️ Espaço limitado"
                    
                self.disk_status.config(text=status)
            else:
                self.disk_status.config(text="❌ Erro ao verificar espaço")
                
        except Exception as e:
            self.disk_status.config(text=f"❌ Erro: {str(e)}")

    def _update_processes_list(self):
        """Atualiza lista de processos do Firebird"""
        try:
            # Limpa lista atual
            for item in self.processes_tree.get_children():
                self.processes_tree.delete(item)
            
            # Adiciona processos
            firebird_processes = [
                "fb_inet_server.exe", "fbserver.exe", "fbguard.exe", 
                "firebird.exe", "ibserver.exe", "gbak.exe", "gfix.exe"
            ]
            
            for proc in psutil.process_iter(['pid', 'name', 'status']):
                try:
                    proc_name = proc.info['name'].lower() if proc.info['name'] else ''
                    if any(fb_proc in proc_name for fb_proc in [p.lower() for p in firebird_processes]):
                        self.processes_tree.insert("", "end", values=(
                            proc.info['pid'],
                            proc.info['name'],
                            proc.info['status']
                        ))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                    
        except Exception as e:
            self.log(f"❌ Erro ao atualizar processos: {e}", "error")

    def auto_refresh_monitor(self):
        """Atualização automática do monitor"""
        if self.conf.get("auto_monitor", True):
            self.refresh_monitor()
            interval = int(self.conf.get("monitor_interval", 30)) * 1000
            self.after(interval, self.auto_refresh_monitor)

    # ---------- AGENDAMENTO ----------
    def pick_sched_db(self):
        """Seleciona banco para agendamento"""
        db = filedialog.askopenfilename(
            title="Selecione o banco para agendamento",
            filetypes=[("Firebird Database", "*.fdb")]
        )
        if db:
            self.sched_db_var.set(db)

    def schedule_backup(self):
        """Cria novo agendamento de backup"""
        if not all([self.sched_db_var.get(), self.sched_name_var.get(), self.sched_time_var.get()]):
            messagebox.showerror("Erro", "Preencha todos os campos obrigatórios.")
            return
        
        try:
            # Valida horário
            time_str = self.sched_time_var.get()
            hours, minutes = map(int, time_str.split(':'))
            if not (0 <= hours <= 23 and 0 <= minutes <= 59):
                raise ValueError("Horário inválido")
        except:
            messagebox.showerror("Erro", "Horário inválido. Use o formato HH:MM (ex: 14:30)")
            return

        schedule_data = {
            "name": self.sched_name_var.get(),
            "database": self.sched_db_var.get(),
            "frequency": self.sched_freq_var.get(),
            "time": self.sched_time_var.get(),
            "compress": self.sched_compress_var.get()
        }

        # Adiciona à configuração
        if "scheduled_backups" not in self.conf:
            self.conf["scheduled_backups"] = []
        
        self.conf["scheduled_backups"].append(schedule_data)
        save_config(self.conf)

        # Adiciona à lista visual
        self.schedules_tree.insert("", "end", values=(
            schedule_data["name"],
            Path(schedule_data["database"]).name,
            schedule_data["frequency"],
            schedule_data["time"],
            "Sim" if schedule_data["compress"] else "Não"
        ))

        # Configura o agendamento
        self._setup_schedule(schedule_data)
        
        # Limpa campos
        self.sched_name_var.set("")
        self.sched_db_var.set("")
        self.sched_time_var.set("02:00")
        self.sched_compress_var.set(True)
        
        self.log(f"📅 Agendamento criado: {schedule_data['name']}", "success")
        messagebox.showinfo("Sucesso", f"Agendamento '{schedule_data['name']}' criado com sucesso!")

    def _setup_schedule(self, schedule_data):
        """Configura o agendamento na biblioteca schedule"""
        try:
            # Remove agendamentos existentes com o mesmo nome
            schedule.clear(schedule_data["name"])
            
            # Configura o agendamento baseado na frequência
            job = None
            if schedule_data["frequency"] == "Diário":
                job = schedule.every().day.at(schedule_data["time"]).do(
                    self.execute_scheduled_backup,
                    schedule_data["database"],
                    schedule_data["name"],
                    schedule_data["compress"]
                ).tag(schedule_data["name"])
            
            elif schedule_data["frequency"] == "Semanal":
                job = schedule.every().monday.at(schedule_data["time"]).do(
                    self.execute_scheduled_backup,
                    schedule_data["database"],
                    schedule_data["name"],
                    schedule_data["compress"]
                ).tag(schedule_data["name"])
            
            elif schedule_data["frequency"] == "Mensal":
                # Agenda para o primeiro dia de cada mês
                job = schedule.every(30).days.at(schedule_data["time"]).do(
                    self.execute_scheduled_backup,
                    schedule_data["database"],
                    schedule_data["name"],
                    schedule_data["compress"]
                ).tag(schedule_data["name"])
            
            if job:
                self.log(f"🕒 Agendamento configurado: {schedule_data['name']} - {schedule_data['frequency']} às {schedule_data['time']}", "info")
                
        except Exception as e:
            self.log(f"❌ Erro ao configurar agendamento '{schedule_data['name']}': {e}", "error")

    def load_schedules(self):
        """Carrega agendamentos salvos"""
        try:
            # Limpa a lista visual
            for item in self.schedules_tree.get_children():
                self.schedules_tree.delete(item)
            
            # Limpa agendamentos existentes
            schedule.clear()
            
            # Carrega da configuração
            scheduled_backups = self.conf.get("scheduled_backups", [])
            for schedule_data in scheduled_backups:
                # Adiciona à lista visual
                self.schedules_tree.insert("", "end", values=(
                    schedule_data["name"],
                    Path(schedule_data["database"]).name,
                    schedule_data["frequency"],
                    schedule_data["time"],
                    "Sim" if schedule_data.get("compress", True) else "Não"
                ))
                
                # Configura o agendamento
                self._setup_schedule(schedule_data)
            
            self.log(f"📅 {len(scheduled_backups)} agendamentos carregados", "info")
            
        except Exception as e:
            self.log(f"❌ Erro ao carregar agendamentos: {e}", "error")

    def remove_schedule(self):
        """Remove agendamento selecionado"""
        selection = self.schedules_tree.selection()
        if not selection:
            messagebox.showwarning("Aviso", "Selecione um agendamento para remover.")
            return
        
        for item in selection:
            values = self.schedules_tree.item(item, "values")
            schedule_name = values[0]
            
            # Remove da configuração
            if "scheduled_backups" in self.conf:
                self.conf["scheduled_backups"] = [
                    s for s in self.conf["scheduled_backups"] 
                    if s["name"] != schedule_name
                ]
                save_config(self.conf)
            
            # Remove da lista visual
            self.schedules_tree.delete(item)
            
            # Remove do agendador
            schedule.clear(schedule_name)
            
            self.log(f"🗑️ Agendamento removido: {schedule_name}", "info")
        
        messagebox.showinfo("Sucesso", "Agendamento removido com sucesso!")

    # ---------- FERRAMENTAS AVANÇADAS ----------
    def optimize_database(self):
        """Executa operações de otimização no banco"""
        gfix = self.conf.get("gfix_path") or find_executable("gfix.exe")
        if not gfix:
            messagebox.showerror("Erro", "gfix.exe não encontrado.")
            return
        
        db = filedialog.askopenfilename(title="Selecione o banco para otimizar")
        if not db:
            return
        
        self.log("🔧 Iniciando otimização do banco...", "info")
        
        # Comandos de otimização
        commands = [
            [gfix, "-sweep", db, "-user", self.conf["firebird_user"], "-pass", self.conf["firebird_password"]],
            [gfix, "-mend", db, "-user", self.conf["firebird_user"], "-pass", self.conf["firebird_password"]],
        ]
        
        def run_next_command(index=0):
            if index < len(commands):
                self.run_command(commands[index], lambda: run_next_command(index + 1))
            else:
                self.log("✅ Otimização concluída com sucesso!", "success")
        
        run_next_command()

    def migrate_database(self):
        """Migra banco entre versões do Firebird"""
        gbak = self.conf.get("gbak_path") or find_executable("gbak.exe")
        if not gbak:
            messagebox.showerror("Erro", "gbak.exe não encontrado.")
            return
        
        source_db = filedialog.askopenfilename(title="Selecione o banco para migrar")
        if not source_db:
            return
        
        target_version = simpledialog.askstring("Migração", "Versão destino (2.5, 3.0, 4.0):")
        if not target_version:
            return
        
        backup_dir = Path(self.conf.get("backup_dir", DEFAULT_BACKUP_DIR))
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_file = backup_dir / f"migration_backup_{timestamp}.fbk"
        migrated_file = backup_dir / f"migrated_v{target_version}_{Path(source_db).name}"
        
        self.log(f"🔄 Iniciando migração para v{target_version}...", "info")
        self.log(f"🔌 Conectando em: {self._get_service_mgr_string()}", "info")
        
        # Backup
        backup_cmd = [
            gbak, "-b", 
            "-se", self._get_service_mgr_string(),
            source_db, str(backup_file),
            "-user", self.conf["firebird_user"], "-pass", self.conf["firebird_password"]
        ]
        
        # Restauração
        restore_cmd = [
            gbak, "-c", 
            "-se", self._get_service_mgr_string(),
            str(backup_file), str(migrated_file),
            "-user", self.conf["firebird_user"], "-pass", self.conf["firebird_password"], "-rep"
        ]
        
        def after_backup():
            self.log("✅ Backup para migração concluído", "success")
            self.run_command(restore_cmd, after_restore)
        
        def after_restore():
            self.log(f"✅ Migração concluída: {migrated_file}", "success")
            # Limpa backup temporário
            try:
                backup_file.unlink()
            except:
                pass
        
        self.run_command(backup_cmd, after_backup)

    def generate_system_report(self):
        """Gera relatório detalhado do sistema"""
        try:
            report = []
            report.append("=" * 50)
            report.append("RELATÓRIO DO SISTEMA GERENCIADOR FIREBIRD")
            report.append(f"Gerado em: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
            report.append("=" * 50)
            
            # Informações do sistema
            report.append("\n📊 INFORMAÇÕES DO SISTEMA:")
            report.append(f"- Diretório base: {BASE_DIR}")
            report.append(f"- Diretório de backups: {self.conf.get('backup_dir', 'Não definido')}")
            
            # Configurações Firebird
            report.append(f"\n🔥 CONFIGURAÇÕES FIREBIRD:")
            report.append(f"- Host: {self.conf.get('firebird_host', 'localhost')}")
            report.append(f"- Porta: {self.conf.get('firebird_port', '26350')}")
            report.append(f"- Usuário: {self.conf.get('firebird_user', 'SYSDBA')}")
            
            # Espaço em disco
            backup_dir = Path(self.conf.get("backup_dir", DEFAULT_BACKUP_DIR))
            disk_info = get_disk_space(backup_dir)
            if disk_info:
                report.append(f"\n💾 ESPAÇO EM DISCO:")
                report.append(f"- Total: {disk_info['total_gb']:.1f} GB")
                report.append(f"- Livre: {disk_info['free_gb']:.1f} GB")
                report.append(f"- Usado: {disk_info['percent_used']:.1f}%")
            
            # Processos Firebird
            fb_processes = self._get_firebird_processes()
            report.append(f"\n🔥 PROCESSOS FIREBIRD: {len(fb_processes)} encontrados")
            for proc in fb_processes:
                report.append(f"  - {proc['name']} (PID: {proc['pid']})")
            
            # Backups
            backup_files = list(Path(self.conf.get("backup_dir", DEFAULT_BACKUP_DIR)).glob("*.fbk")) + \
                          list(Path(self.conf.get("backup_dir", DEFAULT_BACKUP_DIR)).glob("*.zip"))
            report.append(f"\n📦 BACKUPS: {len(backup_files)} arquivos")
            if backup_files:
                latest = max(backup_files, key=lambda f: f.stat().st_mtime)
                report.append(f"- Último backup: {latest.name}")
                report.append(f"  Gerado em: {datetime.fromtimestamp(latest.stat().st_mtime).strftime('%d/%m/%Y %H:%M')}")
            
            # Agendamentos
            scheduled_backups = self.conf.get("scheduled_backups", [])
            report.append(f"\n🕒 AGENDAMENTOS: {len(scheduled_backups)} configurados")
            for sched in scheduled_backups:
                report.append(f"- {sched['name']}: {sched['frequency']} às {sched['time']}")
            
            # Inicialização com Windows
            startup_status = "Sim" if self.conf.get("start_with_windows", False) else "Não"
            report.append(f"\n🪟 INICIALIZAÇÃO COM WINDOWS: {startup_status}")
            
            # Salva relatório
            report_path = BASE_DIR / f"relatorio_sistema_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(report))
            
            self.log(f"📊 Relatório gerado: {report_path}", "success")
            messagebox.showinfo("Relatório", f"Relatório salvo em:\n{report_path}")
            
        except Exception as e:
            self.log(f"❌ Erro ao gerar relatório: {e}", "error")

    def _get_firebird_processes(self):
        """Retorna lista de processos do Firebird"""
        processes = []
        firebird_procs = ["fb_inet_server.exe", "fbserver.exe", "fbguard.exe", "firebird.exe", "ibserver.exe"]
        
        for proc in psutil.process_iter(['pid', 'name']):
            if proc.info['name'] and any(fb in proc.info['name'].lower() for fb in [p.lower() for p in firebird_procs]):
                processes.append({
                    'pid': proc.info['pid'],
                    'name': proc.info['name']
                })
        
        return processes

    def check_disk_space(self):
        """Verifica e alerta sobre espaço em disco"""
        backup_dir = Path(self.conf.get("backup_dir", DEFAULT_BACKUP_DIR))
        disk_info = get_disk_space(backup_dir)
        
        if disk_info:
            free_gb = disk_info['free_gb']
            
            if free_gb < 1:
                msg = f"🚨 ESPAÇO CRÍTICO! Apenas {free_gb:.1f}GB livres!"
                self.log(msg, "error")
                messagebox.showwarning("Espaço em Disco", msg)
            elif free_gb < 5:
                msg = f"⚠️ Espaço limitado: {free_gb:.1f}GB livres"
                self.log(msg, "warning")
                messagebox.showwarning("Espaço em Disco", msg)
            else:
                msg = f"✅ Espaço suficiente: {free_gb:.1f}GB livres"
                self.log(msg, "success")
                messagebox.showinfo("Espaço em Disco", msg)
        else:
            messagebox.showerror("Erro", "Não foi possível verificar o espaço em disco.")

    def export_config(self):
        """Exporta configurações para arquivo"""
        config_file = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("Todos os arquivos", "*.*")]
        )
        if config_file:
            try:
                with open(config_file, 'w', encoding='utf-8') as f:
                    json.dump(self.conf, f, indent=2, ensure_ascii=False)
                self.log(f"📤 Configurações exportadas: {config_file}", "success")
                messagebox.showinfo("Exportar", "Configurações exportadas com sucesso!")
            except Exception as e:
                self.log(f"❌ Erro ao exportar configurações: {e}", "error")
                messagebox.showerror("Erro", f"Falha ao exportar:\n{e}")

    def import_config(self):
        """Importa configurações de arquivo"""
        config_file = filedialog.askopenfilename(
            filetypes=[("JSON files", "*.json"), ("Todos os arquivos", "*.*")]
        )
        if config_file:
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    new_conf = json.load(f)
                
                keep_keys = ['backup_dir', 'gbak_path', 'gfix_path', 'firebird_host', 'firebird_port']
                for key in keep_keys:
                    if key in self.conf:
                        new_conf[key] = self.conf[key]
                
                self.conf.update(new_conf)
                save_config(self.conf)
                
                # Recarrega agendamentos
                self.load_schedules()
                
                self.log("📥 Configurações importadas com sucesso", "success")
                messagebox.showinfo("Importar", 
                                  "Configurações importadas com sucesso!\n"
                                  "Agendamentos recarregados.")
                                  
            except Exception as e:
                self.log(f"❌ Erro ao importar configurações: {e}", "error")
                messagebox.showerror("Erro", f"Falha ao importar:\n{e}")

    # ---------- CONFIGURAÇÕES ----------
    def config_window(self):
        """Janela de configurações"""
        win = tk.Toplevel(self)
        win.title("Configurações do Sistema")
        win.geometry("500x650")
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()

        # Centraliza
        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 250
        y = self.winfo_y() + (self.winfo_height() // 2) - 325
        win.geometry(f"+{x}+{y}")

        # Ícone
        icon_path = BASE_DIR / "images" / "icon.ico"
        if icon_path.exists():
            win.iconbitmap(str(icon_path))

        notebook = ttk.Notebook(win)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)

        # Aba Firebird
        firebird_frame = ttk.Frame(notebook, padding=10)
        notebook.add(firebird_frame, text="Firebird")

        ttk.Label(firebird_frame, text="Local do gbak.exe:").grid(row=0, column=0, sticky="w", pady=8)
        gbak_var = tk.StringVar(value=self.conf.get("gbak_path", ""))
        gbak_entry = ttk.Entry(firebird_frame, textvariable=gbak_var, width=40)
        gbak_entry.grid(row=0, column=1, padx=5)
        ttk.Button(firebird_frame, text="...", width=3, 
                  command=lambda: self.pick_exe(gbak_var, "gbak.exe")).grid(row=0, column=2)

        ttk.Label(firebird_frame, text="Local do gfix.exe:").grid(row=1, column=0, sticky="w", pady=8)
        gfix_var = tk.StringVar(value=self.conf.get("gfix_path", ""))
        gfix_entry = ttk.Entry(firebird_frame, textvariable=gfix_var, width=40)
        gfix_entry.grid(row=1, column=1, padx=5)
        ttk.Button(firebird_frame, text="...", width=3,
                  command=lambda: self.pick_exe(gfix_var, "gfix.exe")).grid(row=1, column=2)

        ttk.Label(firebird_frame, text="Pasta de backups:").grid(row=2, column=0, sticky="w", pady=8)
        backup_var = tk.StringVar(value=self.conf.get("backup_dir", ""))
        backup_entry = ttk.Entry(firebird_frame, textvariable=backup_var, width=40)
        backup_entry.grid(row=2, column=1, padx=5)
        ttk.Button(firebird_frame, text="...", width=3,
                  command=lambda: self.pick_dir(backup_var)).grid(row=2, column=2)

        ttk.Label(firebird_frame, text="Host do Firebird:").grid(row=3, column=0, sticky="w", pady=8)
        host_var = tk.StringVar(value=self.conf.get("firebird_host", "localhost"))
        ttk.Entry(firebird_frame, textvariable=host_var, width=40).grid(row=3, column=1, padx=5)

        ttk.Label(firebird_frame, text="Porta do Firebird:").grid(row=4, column=0, sticky="w", pady=8)
        port_var = tk.StringVar(value=self.conf.get("firebird_port", "26350"))
        ttk.Entry(firebird_frame, textvariable=port_var, width=40).grid(row=4, column=1, padx=5)

        ttk.Label(firebird_frame, text="Usuário:").grid(row=5, column=0, sticky="w", pady=8)
        user_var = tk.StringVar(value=self.conf.get("firebird_user", "SYSDBA"))
        ttk.Entry(firebird_frame, textvariable=user_var, width=40).grid(row=5, column=1, padx=5)

        ttk.Label(firebird_frame, text="Senha:").grid(row=6, column=0, sticky="w", pady=8)
        pass_var = tk.StringVar(value=self.conf.get("firebird_password", "masterkey"))
        ttk.Entry(firebird_frame, textvariable=pass_var, width=40, show="*").grid(row=6, column=1, padx=5)

        ttk.Label(firebird_frame, text="Qtd. backups a manter:").grid(row=7, column=0, sticky="w", pady=8)
        keep_var = tk.IntVar(value=self.conf.get("keep_backups", DEFAULT_KEEP_BACKUPS))
        ttk.Spinbox(firebird_frame, from_=1, to=100, textvariable=keep_var, width=10).grid(row=7, column=1, sticky="w", padx=5)

        # Aba Sistema
        system_frame = ttk.Frame(notebook, padding=10)
        notebook.add(system_frame, text="Sistema")

        ttk.Label(system_frame, text="Monitoramento automático:").grid(row=0, column=0, sticky="w", pady=8)
        monitor_var = tk.BooleanVar(value=self.conf.get("auto_monitor", True))
        ttk.Checkbutton(system_frame, variable=monitor_var).grid(row=0, column=1, sticky="w", padx=5)

        ttk.Label(system_frame, text="Intervalo (segundos):").grid(row=1, column=0, sticky="w", pady=8)
        interval_var = tk.IntVar(value=self.conf.get("monitor_interval", 30))
        ttk.Spinbox(system_frame, from_=10, to=300, textvariable=interval_var, width=10).grid(row=1, column=1, sticky="w", padx=5)

        # Comportamento
        ttk.Label(system_frame, text="Minimizar para bandeja:").grid(row=2, column=0, sticky="w", pady=8)
        tray_var = tk.BooleanVar(value=self.conf.get("minimize_to_tray", True))
        ttk.Checkbutton(system_frame, variable=tray_var).grid(row=2, column=1, sticky="w", padx=5)

        ttk.Label(system_frame, text="Iniciar minimizado:").grid(row=3, column=0, sticky="w", pady=8)
        start_min_var = tk.BooleanVar(value=self.conf.get("start_minimized", False))
        ttk.Checkbutton(system_frame, variable=start_min_var).grid(row=3, column=1, sticky="w", padx=5)

        # Iniciar com Windows
        ttk.Label(system_frame, text="Iniciar com Windows:").grid(row=4, column=0, sticky="w", pady=8)
        startup_var = tk.BooleanVar(value=self.conf.get("start_with_windows", False))
        startup_cb = ttk.Checkbutton(system_frame, variable=startup_var, 
                                    command=lambda: self.toggle_startup(startup_var.get()))
        startup_cb.grid(row=4, column=1, sticky="w", padx=5)

        # Botões
        btn_frame = ttk.Frame(win)
        btn_frame.pack(pady=10)

        def save_all_config():
            self.conf.update({
                "gbak_path": gbak_var.get(),
                "gfix_path": gfix_var.get(),
                "backup_dir": backup_var.get(),
                "firebird_host": host_var.get(),
                "firebird_port": port_var.get(),
                "firebird_user": user_var.get(),
                "firebird_password": pass_var.get(),
                "keep_backups": keep_var.get(),
                "auto_monitor": monitor_var.get(),
                "monitor_interval": interval_var.get(),
                "minimize_to_tray": tray_var.get(),
                "start_minimized": start_min_var.get(),
                "start_with_windows": startup_var.get()
            })
            
            if save_config(self.conf):
                # Aplica a configuração de inicialização com Windows
                self.apply_startup_setting(startup_var.get())
                messagebox.showinfo("Configurações", "Configurações salvas com sucesso!")
                win.destroy()
            else:
                messagebox.showerror("Erro", "Falha ao salvar configurações!")

        ttk.Button(btn_frame, text="💾 Salvar Tudo", 
                  command=save_all_config,
                  cursor="hand2").pack(side="left", padx=10)
        
        ttk.Button(btn_frame, text="❌ Cancelar", 
                  command=win.destroy,
                  cursor="hand2").pack(side="left", padx=10)

    def pick_exe(self, var, exe_name):
        """Seleciona executável"""
        path = filedialog.askopenfilename(
            title=f"Selecione {exe_name}", 
            filetypes=[("Executável", "*.exe"), ("Todos os arquivos", "*.*")]
        )
        if path:
            var.set(path)

    def pick_dir(self, var):
        """Seleciona diretório"""
        path = filedialog.askdirectory(title="Selecione diretório")
        if path:
            var.set(path)

    # ---------- CONSOLE DE DESENVOLVIMENTO ----------
    def open_script_console(self):
        """Abre console de desenvolvimento"""
        win = tk.Toplevel(self)
        win.title("Console de Desenvolvimento")
        win.geometry("700x500")
        win.minsize(600, 400)

        # Centraliza
        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 350
        y = self.winfo_y() + (self.winfo_height() // 2) - 250
        win.geometry(f"+{x}+{y}")

        # Ícone
        icon_path = BASE_DIR / "images" / "icon.ico"
        if icon_path.exists():
            win.iconbitmap(str(icon_path))

        win.transient(self)
        win.grab_set()
        win.focus_force()

        ttk.Label(win, text="Console de Desenvolvimento - Execute código Python:").pack(pady=5)

        text = scrolledtext.ScrolledText(win, height=15, width=80, font=("Consolas", 10))
        text.pack(padx=10, pady=5, fill="both", expand=True)

        output = scrolledtext.ScrolledText(win, height=8, width=80, font=("Consolas", 10), bg="#111", fg="#0f0")
        output.pack(padx=10, pady=5, fill="both", expand=True)

        def run_script(event=None):
            code = text.get("1.0", tk.END).strip()
            output.delete("1.0", tk.END)
            if not code:
                return
            try:
                local_vars = {
                    'app': self,
                    'config': self.conf,
                    'Path': Path,
                    'tk': tk,
                    'ttk': ttk,
                    'messagebox': messagebox,
                    'filedialog': filedialog
                }
                exec(code, globals(), local_vars)
                output.insert(tk.END, "✅ Execução concluída com sucesso.\n")
            except Exception as e:
                output.insert(tk.END, f"❌ Erro: {e}\n")

        # Botão executar
        ttk.Button(win, text="▶️ Executar Script", command=run_script, cursor="hand2").pack(pady=5)

        # Atalho Shift + Enter
        text.bind("<Shift-Return>", run_script)

        self.log("🧩 Console de desenvolvimento aberto.", "info")

        def on_close():
            self.dev_mode = False
            self.dev_buffer = ""
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)

    def __del__(self):
        """Destrutor - para o agendador"""
        self.schedule_running = False

# ---------- MAIN ----------
if __name__ == "__main__":
    try:
        # Verificar permissões de administrador
        if not is_admin():
            response = messagebox.askyesno(
                "Permissão de Administrador",
                "Este programa requer permissões de administrador para \n"
                "gerenciar processos do Firebird.\n\n"
                "Deseja executar como administrador?",
                icon=messagebox.WARNING
            )
            if response:
                if not run_as_admin():
                    sys.exit(1)
            else:
                messagebox.showinfo(
                    "Informação",
                    "Algumas funcionalidades podem não funcionar \n"
                    "sem permissões de administrador."
                )
        
        # Iniciar aplicação
        app = GerenciadorFirebirdApp()
        app.mainloop()
        
    except Exception as e:
        print(f"Erro fatal: {e}")
        messagebox.showerror("Erro Fatal", f"Falha ao iniciar aplicação:\n{e}")
        sys.exit(1)