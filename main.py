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
import patoolib
from patoolib.util import PatoolError
import rarfile
import py7zr
import psutil
from datetime import datetime, timedelta
from pathlib import Path
import threading
import logging
from logging.handlers import RotatingFileHandler
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext, simpledialog, Label
import time
import schedule
from typing import Dict, List, Optional
import winreg
import winshell
from win32com.client import Dispatch
import hashlib
import hmac
import copy

from app_core import (
    atomic_write_json,
    cleanup_old_backups as cleanup_backups_core,
    compress_backup,
    redact_command,
    safe_extract_tar,
    safe_extract_archive,
    safe_extract_zip,
    validate_archive_members,
    validate_password,
)
from secure_store import PREFIX as SECRET_PREFIX, protect_secret, unprotect_secret

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
    DATA_DIR = Path(os.getenv("LOCALAPPDATA", BASE_DIR)) / "GerenciadorFirebird"
else:
    BASE_DIR = Path(__file__).resolve().parent
    DATA_DIR = BASE_DIR

DATA_DIR.mkdir(parents=True, exist_ok=True)

# Em builds instalados, dados mutáveis ficam no perfil do usuário. Migra versões
# antigas que gravavam ao lado do executável sempre que isso for possível.
if DATA_DIR != BASE_DIR:
    for legacy_name in ("config.json", "users.json"):
        legacy_path = BASE_DIR / legacy_name
        current_path = DATA_DIR / legacy_name
        if legacy_path.exists() and not current_path.exists():
            try:
                shutil.copy2(legacy_path, current_path)
            except OSError:
                pass

CONFIG_PATH = DATA_DIR / "config.json"
LOG_FILE = DATA_DIR / "gerenciador_firebird.log"
DEFAULT_BACKUP_DIR = DATA_DIR / "backups"
DEFAULT_KEEP_BACKUPS = 5
REPORTS_DIR = DATA_DIR / "Relatórios"

# Constantes para controle de versão
APP_VERSION = "2025.12.29.1110"
VERSION_CHECK_URL = "https://raw.githubusercontent.com/MMaffi/gerenciador_firebird/main/version.json"

# Opções disponíveis de pageSize
PAGE_SIZE_OPTIONS = [
    "1024",  
    "2048",    
    "4096",   
    "8192",
    "16384", 
]

# ---------- SISTEMA DE USUÁRIOS ----------
USER_ROLES = {
    "admin": "Administrador",
    "operator": "Operador", 
    "viewer": "Visualizador"
}

USER_PERMISSIONS = {
    "admin": [
        "backup", "restore", "verify", "repair", "sweep", "optimize",
        "migrate", "recalculate_indexes", "generate_reports", "kill_processes",
        "manage_schedules", "manage_users", "system_config", "export_import",
        "sql_console", "all_tools"
    ],
    "operator": [
        "backup", "restore", "verify", "sweep", "generate_reports",
        "kill_processes", "manage_schedules", "sql_console"
    ],
    "viewer": [
        "generate_reports", "view_monitor"
    ]
}

DEFAULT_USERS = {
    "admin": {
        "password": "admin",
        "role": "admin",
        "full_name": "Administrador Principal",
        "email": "admin@admin.com",
        "created_at": None,
        "last_login": None,
        "active": True,
        "must_change_password": True
    }
}

# ---------- LOGGING ----------
def cleanup_old_logs(log_file_path, max_days):
    """Remove logs antigos"""
    try:
        if not log_file_path.exists():
            return
        
        cutoff_date = datetime.now() - timedelta(days=max_days)
        
        with open(log_file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        new_lines = []
        for line in lines:
            if len(line) >= 19:
                try:
                    log_date_str = line[:19]
                    log_date = datetime.strptime(log_date_str, '%Y-%m-%d %H:%M:%S')
                    if log_date >= cutoff_date:
                        new_lines.append(line)
                except ValueError:
                    new_lines.append(line)
            else:
                new_lines.append(line)
        
        with open(log_file_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
            
        logging.info(f"Limpeza de logs concluída. Mantidos logs dos últimos {max_days} dias")
        
    except Exception as e:
        logging.error(f"Erro ao limpar logs antigos: {e}")

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

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    return logger

# ---------- GERENCIADOR DE USUÁRIOS ----------
class UserManager:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.users_file = config_path.parent / "users.json"
        self.current_user = None
        self.load_users()
    
    def load_users(self):
        """Carrega usuários do arquivo"""
        if self.users_file.exists():
            try:
                with open(self.users_file, 'r', encoding='utf-8') as f:
                    self.users = json.load(f)
            except:
                logging.exception("Arquivo de usuários inválido; a autenticação foi bloqueada")
                raise RuntimeError("users.json está inválido. Restaure o arquivo antes de iniciar.")
        else:
            self.users = copy.deepcopy(DEFAULT_USERS)
            self._hash_default_passwords()
            self.save_users()
    
    def _hash_default_passwords(self):
        """Converte senhas padrão para hash"""
        for username, user_data in self.users.items():
            if not user_data.get('password', '').startswith('$2b$'):
                user_data['password'] = self.hash_password(user_data['password'])
    
    def hash_password(self, password: str) -> str:
        """Gera hash da senha usando bcrypt"""
        try:
            import bcrypt
            return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        except ImportError as exc:
            raise RuntimeError("A dependência bcrypt é obrigatória para autenticação segura") from exc
    
    def verify_password(self, password: str, hashed: str) -> bool:
        """Verifica se a senha corresponde ao hash"""
        if hashed.startswith(('$2a$', '$2b$', '$2y$')):
            import bcrypt
            return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))
        # Compatibilidade temporária com hashes antigos; são atualizados no login.
        legacy_hash = hashlib.sha256(f"{password}salt".encode()).hexdigest()
        return hmac.compare_digest(legacy_hash, hashed)
    
    def authenticate(self, username: str, password: str) -> bool:
        """Autentica um usuário ativo."""
        if not username or not password:
            return False
        
        # AUTENTICAÇÃO NORMAL
        if username in self.users and self.users[username]['active']:
            if self.verify_password(password, self.users[username]['password']):
                if not self.users[username]['password'].startswith(('$2a$', '$2b$', '$2y$')):
                    self.users[username]['password'] = self.hash_password(password)
                if username == "admin" and password == "admin":
                    self.users[username]["must_change_password"] = True
                self.users[username]['last_login'] = datetime.now().isoformat()
                self.save_users()
                self.current_user = {
                    'username': username,
                    'role': self.users[username]['role'],
                    'full_name': self.users[username]['full_name'],
                    'must_change_password': self.users[username].get('must_change_password', False)
                }
                return True
        
        return False
    
    def has_permission(self, permission: str) -> bool:
        """Verifica se usuário atual tem permissão"""
        if not self.current_user:
            return False
        
        # Verificação normal
        user_role = self.current_user['role']
        return permission in USER_PERMISSIONS.get(user_role, [])
    
    def create_user(self, username: str, password: str, role: str, full_name: str, email: str = "") -> bool:
        """Cria novo usuário"""
        password_ok, _ = validate_password(password)
        if username in self.users or role not in USER_ROLES or not password_ok:
            return False
        
        self.users[username] = {
            'password': self.hash_password(password),
            'role': role,
            'full_name': full_name,
            'email': email,
            'created_at': datetime.now().isoformat(),
            'last_login': None,
            'active': True
        }
        
        return self.save_users()
    
    def update_user(self, username: str, **kwargs) -> bool:
        """Atualiza dados do usuário"""
        if username not in self.users:
            return False
        
        for key, value in kwargs.items():
            if key in ['password', 'role', 'full_name', 'email', 'active']:
                if key == 'password' and value:
                    if not validate_password(value)[0]:
                        return False
                    self.users[username]['password'] = self.hash_password(value)
                else:
                    self.users[username][key] = value
        
        return self.save_users()
    
    def delete_user(self, username: str) -> bool:
        """Remove usuário"""
        if username == self.current_user['username']:
            return False
        
        # Verifica se é o último admin
        admin_count = sum(1 for u in self.users.values() if u['role'] == 'admin' and u['active'])
        if self.users[username]['role'] == 'admin' and admin_count <= 1:
            return False
        
        del self.users[username]
        return self.save_users()
    
    def save_users(self) -> bool:
        """Salva usuários no arquivo"""
        try:
            atomic_write_json(self.users_file, self.users)
            return True
        except Exception as exc:
            logging.error("Falha ao salvar usuários: %s", exc)
            return False
    
    def get_users_list(self) -> List[Dict]:
        """Retorna lista de usuários"""
        users_list = []
        for username, data in self.users.items():
            users_list.append({
                'username': username,
                'role': data['role'],
                'full_name': data['full_name'],
                'email': data.get('email', ''),
                'created_at': data.get('created_at', ''),
                'last_login': data.get('last_login', ''),
                'active': data.get('active', True)
            })
        return users_list

    def change_password(self, username: str, new_password: str) -> bool:
        """Altera a senha de um usuário"""
        if username not in self.users or not validate_password(new_password)[0]:
            return False
        
        self.users[username]['password'] = self.hash_password(new_password)
        self.users[username]['must_change_password'] = False
        return self.save_users()

    def get_user_details(self, username: str) -> Optional[Dict]:
        if username in self.users:
            user_data = self.users[username].copy()
            user_data['username'] = username
            if 'password' in user_data:
                del user_data['password']
            return user_data
        return None

# ---------- VERIFICAÇÃO DE ATUALIZAÇÕES ----------
def check_for_updates(conf):
    """Verifica se há uma nova versão disponível SEMPRE ao iniciar"""
    try:
        # Verifica se o usuário ignorou esta versão
        ignored_version = conf.get("ignored_version")
        
        conf["last_update_check"] = datetime.now().isoformat()
        save_config(conf)
        
        import urllib.request
        import json as json_lib
        
        response = urllib.request.urlopen(VERSION_CHECK_URL, timeout=10)
        data = json_lib.loads(response.read().decode())
        
        latest_version = data.get("latest_version")
        download_url = data.get("download_url")
        release_notes = data.get("release_notes", "")
        
        if (latest_version and 
            latest_version != APP_VERSION and 
            latest_version != ignored_version):
            return {
                "current_version": APP_VERSION,
                "latest_version": latest_version,
                "download_url": download_url,
                "release_notes": release_notes
            }
        
        return None
        
    except Exception as e:
        logging.error(f"Erro ao verificar atualizações: {e}")
        return None

def check_for_updates_thread_safe(conf):
    """Versão thread-safe para verificar atualizações"""
    try:
        # Verifica se o usuário ignorou esta versão
        ignored_version = conf.get("ignored_version")
        
        import urllib.request
        import json as json_lib
        
        response = urllib.request.urlopen(VERSION_CHECK_URL, timeout=10)
        data = json_lib.loads(response.read().decode())
        
        latest_version = data.get("latest_version")
        download_url = data.get("download_url")
        release_notes = data.get("release_notes", "")
        
        if (latest_version and 
            latest_version != APP_VERSION and 
            latest_version != ignored_version):
            return {
                "current_version": APP_VERSION,
                "latest_version": latest_version,
                "download_url": download_url,
                "release_notes": release_notes
            }
        
        return None  # Retorna None quando já está na versão mais recente
        
    except Exception as e:
        logging.error(f"Erro ao verificar atualizações: {e}")
        return None
    
def check_updates_in_thread(conf, callback):
    """Verifica atualizações em uma thread separada"""
    def worker():
        try:
            # Atualiza o timestamp da última verificação
            conf["last_update_check"] = datetime.now().isoformat()
            # Salva em uma cópia para evitar problemas de thread
            conf_copy = conf.copy()
            
            update_info = check_for_updates_thread_safe(conf_copy)
            callback(update_info)
        except Exception as e:
            logging.error(f"Erro na thread de verificação de atualizações: {e}")
            callback(None)
    
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

# ---------- GERENCIADOR DE CONFIG ----------
def find_firebird_executables(firebird_path):
    """Encontra automaticamente os executáveis do Firebird na pasta especificada"""
    executables = {
        'gbak_path': '',
        'gfix_path': '',
        'gstat_path': '',
        'isql_path': ''
    }
    
    if not firebird_path or not os.path.exists(firebird_path):
        return executables
    
    # Lista de executáveis para procurar
    exe_files = ['gbak.exe', 'gfix.exe', 'gstat.exe', 'isql.exe']
    
    # Procura recursivamente na pasta do Firebird
    for root, dirs, files in os.walk(firebird_path):
        for file in files:
            if file.lower() in exe_files:
                full_path = os.path.join(root, file)
                if file.lower() == 'gbak.exe':
                    executables['gbak_path'] = full_path
                elif file.lower() == 'gfix.exe':
                    executables['gfix_path'] = full_path
                elif file.lower() == 'gstat.exe':
                    executables['gstat_path'] = full_path
                elif file.lower() == 'isql.exe':
                    executables['isql_path'] = full_path
    
    return executables

def load_config():
    """Carrega configurações do JSON"""
    default = {
        "firebird_path": "",
        "gbak_path": "",
        "gfix_path": "",
        "gstat_path": "",
        "isql_path": "",
        "backup_dir": str(DEFAULT_BACKUP_DIR),
        "keep_backups": DEFAULT_KEEP_BACKUPS,
        "firebird_user": "SYSDBA",
        "firebird_password": "masterkey",
        "firebird_host": "localhost",
        "firebird_port": "26350",
        "page_size": "8192",
        "auto_monitor": True,
        "monitor_interval": 30,
        "minimize_to_tray": True,
        "start_with_windows": False,
        "scheduled_backups": [],
        "log_retention_days": 30,
        "last_update_check": None,
        "ignored_version": None,
        "last_user": "",
        "auto_login": False,
        "auto_login_user": "",
        "auto_login_password": ""
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
            persisted_default = default.copy()
            if persisted_default.get("firebird_password"):
                persisted_default["firebird_password"] = protect_secret(
                    persisted_default["firebird_password"]
                )
            atomic_write_json(CONFIG_PATH, persisted_default)
            logging.info("Arquivo de configuração criado com sucesso")
        except Exception as e:
            logging.error(f"Falha ao criar config.json: {e}")

    stored_password = default.get("firebird_password", "")
    if isinstance(stored_password, str) and stored_password.startswith(SECRET_PREFIX):
        try:
            default["firebird_password"] = unprotect_secret(stored_password)
        except Exception as exc:
            logging.error("Não foi possível desbloquear a senha do Firebird: %s", exc)
            default["firebird_password"] = ""
    
    # Se o caminho do Firebird estiver configurado, busca os executáveis automaticamente
    if default.get("firebird_path") and os.path.exists(default["firebird_path"]):
        executables = find_firebird_executables(default["firebird_path"])
        
        # Atualiza apenas se os executáveis não estiverem configurados manualmente
        for exe_name, exe_path in executables.items():
            if exe_path and (not default.get(exe_name) or not os.path.exists(default[exe_name])):
                default[exe_name] = exe_path
                logging.info(f"Executável {exe_name} encontrado automaticamente: {exe_path}")
    
    # Executa limpeza de logs ao carregar configurações
    try:
        cleanup_old_logs(LOG_FILE, default.get("log_retention_days", 30))
    except Exception as e:
        logging.error(f"Erro na limpeza inicial de logs: {e}")
    
    return default

def save_config(conf):
    """Salva configurações no JSON"""
    try:
        persisted = conf.copy()
        firebird_password = persisted.get("firebird_password", "")
        if firebird_password:
            persisted["firebird_password"] = protect_secret(firebird_password)
        atomic_write_json(CONFIG_PATH, persisted)
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
        removed = cleanup_backups_core(backup_dir, keep)
        for old_file in removed:
            logging.info(f"Backup antigo removido: {old_file.name}")
        if removed:
            logging.info(f"Limpeza concluída: {len(removed)} arquivos removidos")
            
    except Exception as e:
        logging.error(f"Erro durante limpeza de backups: {e}")

def get_disk_space(path):
    """Retorna informações de espaço em disco"""
    try:
        path = Path(path) if isinstance(path, str) else path
        
        if not path.exists():
            path = path.parent if path.parent.exists() else Path.cwd()
        
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
        logging.error(f"Erro ao verificar espaço em disco para {path}: {e}")
        return None

def open_file_with_default_app(file_path):
    """Abre arquivo com programa padrão do sistema"""
    try:
        if os.name == 'nt':
            os.startfile(file_path)
        elif sys.platform == 'darwin':
            subprocess.run(['open', file_path])
        else:
            subprocess.run(['xdg-open', file_path])
        return True
    except Exception as e:
        logging.error(f"Erro ao abrir arquivo {file_path}: {e}")
        return False

# ---------- PROTEÇÃO DE CREDENCIAIS ----------
def simple_encrypt(text: str, key: str = "firebird_manager_key") -> str:
    """Protege texto para o usuário atual do Windows."""
    return protect_secret(text)

def simple_decrypt(encrypted_text: str, key: str = "firebird_manager_key") -> str:
    """Desprotege texto e mantém leitura do formato legado."""
    return unprotect_secret(encrypted_text, key)

# ------------ APP PRINCIPAL ------------
class GerenciadorFirebirdApp(tk.Tk):
    def __init__(self):
        super().__init__()

        self.logger = setup_logging()
        
        # Sistema de usuários
        self.user_manager = UserManager(CONFIG_PATH)
        self.current_user = None
        
        # === Configura a janela como tela de login inicialmente ===
        self._setup_login_window()
        
        self.dev_buffer = ""
        self.dev_mode = False
        self.scheduled_jobs = []
        self.schedule_thread = None
        self.schedule_running = False
        self.tray_icon = None
        self._tray_last_click = 0.0

        self.bind_all("<F12>", self._toggle_dev_mode)
        self.bind_all("<Key>", self._capture_secret_key)
        
        # Carrega configurações
        self.conf = load_config()

        # Verificação de atualizações
        self.update_check_in_progress = False
        
        # Verifica se deve fazer login automático
        if self.conf.get("auto_login", False):
            auto_user = self.conf.get("auto_login_user", "")
            auto_password_encrypted = self.conf.get("auto_login_password", "")
            
            if auto_user and auto_password_encrypted:
                try:
                    auto_password = simple_decrypt(auto_password_encrypted)
                    if self.user_manager.authenticate(auto_user, auto_password):
                        self.current_user = self.user_manager.current_user
                        if not self.current_user.get("must_change_password"):
                            self._destroy_login_and_setup_main()
                            return
                        self.conf["auto_login"] = False
                        self.conf["auto_login_password"] = ""
                        save_config(self.conf)
                except Exception as e:
                    self.logger.error(f"Erro no login automático: {e}")
        
        # Se não fez login automático, mostra tela de login
        self.show_login_screen()

    def _setup_login_window(self):
        """Configura a janela principal como tela de login"""
        self.title("Login - Gerenciador Firebird")
        self.geometry("400x450")  
        self.resizable(False, False)
        
        # Centraliza na tela
        self.update_idletasks()
        width = 400
        height = 450
        x = (self.winfo_screenwidth() // 2) - (width // 2)
        y = (self.winfo_screenheight() // 2) - (height // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")
        
        # Ícone
        icon_path = BASE_DIR / "images" / "icon.ico"
        if icon_path.exists():
            self.iconbitmap(str(icon_path))
        
        # Remove o comportamento padrão de fechar
        self.protocol("WM_DELETE_WINDOW", self.quit_application)

    def _destroy_login_and_setup_main(self):
        """Destroi a tela de login e configura a janela principal"""
        for widget in self.winfo_children():
            widget.destroy()
        
        # Reconfigura a janela para o sistema principal
        self._setup_main_window()
        
        # === Força o redimensionamento ===
        self.update_idletasks()
        
        # Continua a inicialização do sistema
        self._continue_initialization()

    def _setup_main_window(self):
        """Configura a janela principal do sistema"""
        self.title("Gerenciador Firebird")
        self.geometry("900x750+100+50")
        self.minsize(800, 700)
        self.configure(bg="#f5f5f5")
        
        # === Permitir redimensionamento ===
        self.resizable(True, True)
        
        # Ícone
        icon_path = BASE_DIR / "images" / "icon.ico"
        if icon_path.exists():
            self.iconbitmap(str(icon_path))
        
        # Configura fechamento para minimizar para bandeja
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def show_login_screen(self):
        """Exibe tela de login na janela principal"""
        for widget in self.winfo_children():
            widget.destroy()
        
        # Frame principal
        main_frame = ttk.Frame(self, padding=30)
        main_frame.pack(fill="both", expand=True)
        
        # Título
        ttk.Label(
            main_frame,
            text="🔐 Gerenciador Firebird",
            font=("Arial", 16, "bold")
        ).pack(pady=(0, 30))
        
        ttk.Label(
            main_frame,
            text="Faça login para continuar",
            font=("Arial", 10),
            foreground="gray"
        ).pack(pady=(0, 20))
        
        # Campos de login
        ttk.Label(main_frame, text="Usuário:", font=("Arial", 9, "bold")).pack(anchor="w", pady=(10, 5))
        username_var = tk.StringVar()
        username_entry = ttk.Entry(main_frame, textvariable=username_var, width=30, font=("Arial", 10))
        username_entry.pack(fill="x", pady=(0, 15))
        
        ttk.Label(main_frame, text="Senha:", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 5))
        password_var = tk.StringVar()
        password_entry = ttk.Entry(main_frame, textvariable=password_var, show="•", width=30, font=("Arial", 10))
        password_entry.pack(fill="x", pady=(0, 20))
        
        # Checkbox salvar login
        auto_login_var = tk.BooleanVar(value=self.conf.get("auto_login", False))
        auto_login_cb = ttk.Checkbutton(
            main_frame, 
            variable=auto_login_var,
            text="Lembrar login"
        )
        auto_login_cb.pack(anchor="w", pady=(0, 20))
        
        # Status do login
        login_status = ttk.Label(main_frame, text="", foreground="red", font=("Arial", 9))
        login_status.pack(pady=(0, 10))
        
        def attempt_login():
            username = username_var.get().strip()
            password = password_var.get()
            
            if not username or not password:
                login_status.config(text="Preencha usuário e senha")
                return
            
            if self.user_manager.authenticate(username, password):
                self.current_user = self.user_manager.current_user

                if self.current_user.get("must_change_password"):
                    changed_password = self._force_password_change(username)
                    if not changed_password:
                        login_status.config(text="É necessário definir uma nova senha para continuar")
                        return
                    password = changed_password
                    self.current_user["must_change_password"] = False
                
                # Salva o último usuário logado
                self.conf["last_user"] = username
                save_config(self.conf)
                
                if auto_login_var.get():
                    self.conf["auto_login"] = True
                    self.conf["auto_login_user"] = username
                    # Criptografa a senha antes de salvar
                    encrypted_password = simple_encrypt(password)
                    self.conf["auto_login_password"] = encrypted_password
                else:
                    # Remove login automático
                    self.conf["auto_login"] = False
                    self.conf["auto_login_user"] = ""
                    self.conf["auto_login_password"] = ""
                
                save_config(self.conf)
                
                # Destroi a tela de login e cria a principal
                self._destroy_login_and_setup_main()
                
            else:
                login_status.config(text="Usuário ou senha inválidos")
                password_entry.delete(0, tk.END)
        
        # Botões
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill="x", pady=20)
        
        ttk.Button(
            btn_frame,
            text="🔐 Entrar",
            command=attempt_login,
            cursor="hand2"
        ).pack(side="left", padx=(0, 10))
        
        ttk.Button(
            btn_frame,
            text="❌ Sair",
            command=self.quit_application,
            cursor="hand2"
        ).pack(side="right")
        
        # Enter para logar
        password_entry.bind("<Return>", lambda e: attempt_login())
        
        # CARREGA O ÚLTIMO USUÁRIO LOGADO
        last_user = self.conf.get("last_user", "")
        if last_user and not self.conf.get("auto_login", False):
            username_var.set(last_user)
            password_entry.focus()
        else:
            username_entry.focus()

    def _force_password_change(self, username):
        """Exige a troca da credencial inicial antes de liberar a aplicação."""
        messagebox.showwarning(
            "Troca de senha obrigatória",
            "Esta conta ainda usa uma senha inicial. Defina uma senha segura para continuar."
        )
        new_password = simpledialog.askstring(
            "Nova senha", "Digite a nova senha (mínimo de 8 caracteres):", show="•", parent=self
        )
        if new_password is None:
            return False
        valid, validation_message = validate_password(new_password)
        if not valid:
            messagebox.showerror("Senha inválida", validation_message)
            return False
        confirmation = simpledialog.askstring(
            "Confirmar senha", "Digite a nova senha novamente:", show="•", parent=self
        )
        if new_password != confirmation:
            messagebox.showerror("Senha inválida", "As senhas não coincidem.")
            return False
        if not self.user_manager.change_password(username, new_password):
            messagebox.showerror("Erro", "Não foi possível salvar a nova senha.")
            return None
        return new_password

    def _continue_initialization(self):
        """Continua a inicialização após login bem-sucedido"""
        try:
            self._setup_ui()
            self._start_background_tasks()
            self._start_scheduler()
            
            # Atualiza interface com permissões do usuário
            self._update_ui_permissions()
            
            # Log de acesso
            self.logger.info(f"Usuário {self.current_user['username']} ({self.current_user['role']}) logou no sistema")
            self.log(f"👤 Usuário: {self.current_user['full_name']} ({self.current_user['role']})", "success")
            
            # Configurações de inicialização...
            current_startup_setting = self.conf.get("start_with_windows", False)
            actual_startup_status = self.is_in_startup()
            
            if current_startup_setting != actual_startup_status:
                self.log("🔄 Sincronizando configuração de inicialização com Windows...", "info")
                self.apply_startup_setting(current_startup_setting)
            
            self.logger.info("Gerenciador Firebird iniciado com sucesso")
            
            # Inicia verificação de atualizações em thread separada
            self.after(1000, self.check_and_notify_update_in_thread)
            
        except Exception as e:
            self.logger.critical(f"Falha crítica ao iniciar aplicação: {e}")
            messagebox.showerror("Erro Fatal", f"Falha ao iniciar aplicação:\n{e}")
            sys.exit(1)

    def _update_ui_permissions(self):
        """Atualiza interface baseado nas permissões do usuário"""
        user_role = self.current_user['role']
        
        # Atualiza título da janela com info do usuário
        role_display = USER_ROLES.get(user_role, user_role)
        self.title(f"Gerenciador Firebird - {self.current_user['full_name']} ({role_display})")
        
        # Adiciona botão de gerenciar usuários se for admin
        if self.user_manager.has_permission("manage_users"):
            for widget in self.winfo_children():
                if isinstance(widget, ttk.Frame):
                    for child in widget.winfo_children():
                        if isinstance(child, ttk.Frame):
                            # Adiciona botão de usuários
                            users_btn = ttk.Button(
                                child,
                                text="👥 Usuários",
                                command=self.manage_users,
                                cursor="hand2"
                            )
                            users_btn.pack(side="left", padx=2)

    def on_login_close(self):
        """Encerra programa"""
        self._logging_off = False
        self.quit()
        self.destroy()
        sys.exit(0)

    def logoff(self):
        """Faz logoff do usuário atual e volta para tela de login"""
        if not messagebox.askyesno("Confirmar Logoff", "Deseja realmente sair da aplicação?"):
            return

        self._logging_off = True

        try:
            # Salva o último usuário
            if self.current_user:
                self.conf["last_user"] = self.current_user['username']

            # Remove login automático
            self.conf["auto_login"] = False
            self.conf["auto_login_user"] = ""
            self.conf["auto_login_password"] = ""
            save_config(self.conf)

            # Para o agendador e outras tasks de background
            try:
                self.stop_scheduler()
            except Exception:
                pass

            try:
                self.state("normal")
            except Exception:
                pass

            for widget in self.winfo_children():
                try:
                    widget.destroy()
                except Exception:
                    pass

            # Reset do usuário
            self.current_user = None
            self.user_manager.current_user = None

            login_w, login_h = 400, 450
            self.resizable(False, False)
            try:
                self.minsize(login_w, login_h)
            except Exception:
                pass

            # Centraliza a janela na tela
            x = (self.winfo_screenwidth() // 2) - (login_w // 2)
            y = (self.winfo_screenheight() // 2) - (login_h // 2)
            self.geometry(f"{login_w}x{login_h}+{x}+{y}")

            self.title("Login - Gerenciador Firebird")
            icon_path = BASE_DIR / "images" / "icon.ico"
            if icon_path.exists():
                try:
                    self.iconbitmap(str(icon_path))
                except Exception:
                    pass

            self.update_idletasks()

            try:
                self.show_login_screen()
                self.protocol("WM_DELETE_WINDOW", self.on_login_close)
            finally:
                self._logging_off = False

        except Exception as e:
            self.logger.exception("Erro durante logoff: %s", e)
            try:
                self._setup_login_window()
                self.show_login_screen()
            except Exception:
                pass

    def check_permission(self, permission: str, show_message: bool = True) -> bool:
        """Verifica permissão e mostra mensagem se necessário"""
        if self.user_manager.has_permission(permission):
            return True
        
        if show_message:
            messagebox.showwarning(
                "Permissão Negada",
                f"Você não tem permissão para executar esta ação.\n\n"
                f"Permissão requerida: {permission}\n"
                f"Seu nível: {USER_ROLES.get(self.current_user['role'], self.current_user['role'])}"
            )
        return False
    
    def check_and_notify_update_in_thread(self):
        """Verifica atualizações em thread separada para não bloquear a UI"""
        if self.update_check_in_progress:
            return  # Já está verificando, não faz nada
        
        try:
            self.update_check_in_progress = True
            self.log("🔍 Verificando atualizações em segundo plano...", "info")
            
            def update_callback(update_info):
                """Callback chamado quando a verificação de atualizações termina"""
                self.update_check_in_progress = False
                
                if update_info:
                    # Só exibe uma única notificação
                    self.after(0, lambda: self.show_update_notification(update_info))
                    # Log ÚNICO de nova versão encontrada
                    self.log(f"📢 Nova versão disponível: {update_info['latest_version']}", "info")
                else:
                    # Log único quando está na versão mais recente
                    self.log("✅ Você está na versão mais recente", "success")
                    # Salva a data da última verificação
                    self.conf["last_update_check"] = datetime.now().isoformat()
                    save_config(self.conf)
            
            # Chama a verificação em thread separada
            check_updates_in_thread(self.conf, update_callback)
            
        except Exception as e:
            self.update_check_in_progress = False
            self.log(f"⚠️ Verificação de atualização falhou: {e}", "debug")

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

        # Botão abrir pasta de backups
        backup_folder_btn = ttk.Button(
            controls_frame,
            text="📁 Backups",
            command=self.open_backup_folder,
            cursor="hand2"
        )
        backup_folder_btn.pack(side="left", padx=2)

        # Botão verificar atualizações
        update_btn = ttk.Button(
            controls_frame,
            text="🔄 Verificar Atualizações",
            command=self.check_update_manual,
            cursor="hand2"
        )
        update_btn.pack(side="left", padx=2)

        # Botão configurações
        config_btn = ttk.Button(
            controls_frame,
            text="⚙️ Configurações",
            command=self.config_window,
            cursor="hand2"
        )
        config_btn.pack(side="left", padx=2)

        # Botão de logoff
        logoff_btn = ttk.Button(
            controls_frame,
            text="🚪 Sair",
            command=self.logoff,
            cursor="hand2"
        )
        logoff_btn.pack(side="left", padx=2)

        # Botão de usuários (adicionado dinamicamente)
        self.users_btn = None

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

        # Adiciona binding para detectar mudança de aba
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_change)

    def _on_tab_change(self, event=None):
        """Callback quando o usuário muda de aba"""
        current_tab = self.notebook.index(self.notebook.select())
        self.current_tab_index = current_tab

    def switch_to_main_tab(self):
        """Muda para a aba Principal"""
        try:
            self.notebook.select(0)
            self.update_idletasks()
        except Exception as e:
            print(f"Erro ao mudar para aba principal: {e}")    

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

        # Layout dos botões
        self.btn_backup.grid(row=0, column=0, padx=5, pady=5, sticky="ew")
        self.btn_restore.grid(row=0, column=1, padx=5, pady=5, sticky="ew")
        self.btn_verify.grid(row=0, column=2, padx=5, pady=5, sticky="ew")
        
        for i in range(3):
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
            mode="determinate", 
            length=500
        )
        self.progress.pack(pady=5)
        self.progress["value"] = 0

        # Log
        log_frame = ttk.LabelFrame(dashboard_frame, text="Log de Execução", padding=10)
        log_frame.pack(padx=10, pady=10, fill="both", expand=True)

        # Frame para controles do log
        log_controls_frame = ttk.Frame(log_frame)
        log_controls_frame.pack(fill="x", pady=(0, 5))
        
        # Botão limpar logs da tela
        self.btn_clear_logs = ttk.Button(
            log_controls_frame,
            text="Limpar tela de Logs",
            cursor="hand2",
            command=self.clear_screen_logs,
            width=30
        )
        self.btn_clear_logs.pack(side="right", padx=5)

        self.output = scrolledtext.ScrolledText(log_frame, height=15)
        self.output.pack(fill="both", expand=True)
      
        self.output.tag_config("success", foreground="green")
        self.output.tag_config("error", foreground="red")
        self.output.tag_config("warning", foreground="orange")
        self.output.tag_config("info", foreground="blue")
        self.output.tag_config("debug", foreground="gray")

        self.log("✅ Aplicativo iniciado. Selecione uma ação acima.", "success")

    def clear_screen_logs(self):
        """Limpa os logs visíveis na tela"""
        self.output.delete("1.0", tk.END)
        self.set_status("✅ Logs da tela limpos com sucesso", "green")
    
    def _create_monitor_tab(self):
        """Cria aba de monitoramento"""
        monitor_frame = ttk.Frame(self.notebook)
        self.notebook.add(monitor_frame, text="Monitor")
        
        # Frame superior com informações do sistema
        top_frame = ttk.Frame(monitor_frame)
        top_frame.pack(fill="x", padx=10, pady=5)
        
        # Status do servidor
        server_frame = ttk.LabelFrame(top_frame, text="Status do Servidor Firebird", padding=10)
        server_frame.pack(side="left", fill="x", expand=True, padx=5)
        
        self.server_status = ttk.Label(server_frame, text="🔄 Verificando status...")
        self.server_status.pack(anchor="w")
        
        # Espaço em disco
        disk_frame = ttk.LabelFrame(top_frame, text="Espaço em Disco", padding=10)
        disk_frame.pack(side="left", fill="x", expand=True, padx=5)
        
        self.disk_status = ttk.Label(disk_frame, text="🔄 Calculando espaço...")
        self.disk_status.pack(anchor="w")
        
        # Frame principal
        main_frame = ttk.Frame(monitor_frame)
        main_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        # Frame de pesquisa
        search_frame = ttk.LabelFrame(main_frame, text="Pesquisar Processos", padding=10)
        search_frame.pack(fill="x", padx=5, pady=5)
        
        ttk.Label(search_frame, text="Pesquisar:").pack(side="left", padx=5)
        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(search_frame, textvariable=self.search_var, width=40)
        search_entry.pack(side="left", padx=5)
        
        # Botões de pesquisa
        search_btn_frame = ttk.Frame(search_frame)
        search_btn_frame.pack(side="left", padx=10)
        
        ttk.Button(search_btn_frame, text="🔍 Pesquisar", 
                cursor="hand2", command=self._refresh_all_processes).pack(side="left", padx=2)
        ttk.Button(search_btn_frame, text="🔄 Atualizar Tudo",
                cursor="hand2", command=self._refresh_all_processes).pack(side="left", padx=2)
        
        # Lista de todos os processos
        all_processes_frame = ttk.LabelFrame(main_frame, text="Todos os Processos do Sistema", padding=10)
        all_processes_frame.pack(fill="both", expand=True, padx=5, pady=5)
        
        # Treeview para todos os processos
        self.all_processes_tree = ttk.Treeview(all_processes_frame, 
                                            columns=("PID", "Nome", "Usuário", "Status"), 
                                            show="headings",
                                            selectmode="extended")
        
        # Configurar os cabeçalhos com função de ordenação
        self.all_processes_tree.heading("PID", text="PID", command=lambda: self._sort_treeview("PID"))
        self.all_processes_tree.heading("Nome", text="Nome do Processo", command=lambda: self._sort_treeview("Nome"))
        self.all_processes_tree.heading("Usuário", text="Usuário", command=lambda: self._sort_treeview("Usuário"))
        self.all_processes_tree.heading("Status", text="Status", command=lambda: self._sort_treeview("Status"))

        self.sort_order = {
            "PID": False,
            "Nome": False, 
            "Usuário": False,
            "Status": False
        }
        
        self.all_processes_tree.column("PID", width=80)
        self.all_processes_tree.column("Nome", width=250)
        self.all_processes_tree.column("Usuário", width=150)
        self.all_processes_tree.column("Status", width=100)
        
        # Scrollbars
        v_scrollbar = ttk.Scrollbar(all_processes_frame, orient="vertical", command=self.all_processes_tree.yview)
        h_scrollbar = ttk.Scrollbar(all_processes_frame, orient="horizontal", command=self.all_processes_tree.xview)
        self.all_processes_tree.configure(yscrollcommand=v_scrollbar.set, xscrollcommand=h_scrollbar.set)
        
        self.all_processes_tree.pack(side="left", fill="both", expand=True)
        v_scrollbar.pack(side="right", fill="y")
        h_scrollbar.pack(side="bottom", fill="x")
        
        self.sort_order = {}
        
        # Status dos processos
        self.process_status_label = ttk.Label(main_frame, text="🔄 Carregando processos...")
        self.process_status_label.pack(anchor="w", padx=10, pady=2)
        
        # Botões de ação
        action_frame = ttk.Frame(main_frame)
        action_frame.pack(fill="x", padx=5, pady=10)
        
        ttk.Button(action_frame, 
                text="🔥 Finalizar Selecionados",
                command=self._kill_selected_processes,
                cursor="hand2").pack(side="left", padx=5)
        
        ttk.Button(action_frame,
                text="🎯 Finalizar por PID",
                command=self._kill_by_pid,
                cursor="hand2").pack(side="left", padx=5)

        self.search_job = None
        def on_search_change(*args):
            if self.search_job:
                self.after_cancel(self.search_job)
            self.search_job = self.after(500, self._refresh_all_processes)
        
        self.search_var.trace_add("write", on_search_change)
        
        # Atalhos de teclado
        self.all_processes_tree.bind("<Delete>", lambda e: self._kill_selected_processes())
        self.all_processes_tree.bind("<F5>", lambda e: self._refresh_all_processes())

    def _sort_treeview(self, column):
        """Ordena o treeview pela coluna clicada"""
        try:
            current_reverse = self.sort_order.get(column, False)
            
            items = [(self.all_processes_tree.set(item, column), item) for item in self.all_processes_tree.get_children('')]
            
            if column == "PID":
                try:
                    items.sort(key=lambda x: int(x[0]) if x[0].isdigit() else float('inf'), reverse=current_reverse)
                except:
                    items.sort(key=lambda x: x[0], reverse=current_reverse)
            else:
                items.sort(key=lambda x: x[0].lower() if x[0] else "", reverse=current_reverse)
            
            # Reorganiza os itens na nova ordem
            for index, (_, item) in enumerate(items):
                self.all_processes_tree.move(item, '', index)
            
            new_reverse = not current_reverse
            self.sort_order[column] = new_reverse

            self._update_column_heading(column, new_reverse)
            
        except Exception as e:
            self.log(f"❌ Erro ao ordenar coluna {column}: {e}", "error")

    def _update_column_heading(self, column, reverse):
        """Atualiza o cabeçalho"""
        for col in ["PID", "Nome", "Usuário", "Status"]:
            current_text = self.all_processes_tree.heading(col, "text")

            clean_text = current_text.replace(" ▲", "").replace(" ▼", "")
            self.all_processes_tree.heading(col, text=clean_text)

        base_text = ""
        if column == "PID":
            base_text = "PID"
        elif column == "Nome":
            base_text = "Nome do Processo"
        elif column == "Usuário":
            base_text = "Usuário"
        elif column == "Status":
            base_text = "Status"
        
        arrow = " ▼" if reverse else " ▲"
        self.all_processes_tree.heading(column, text=base_text + arrow)

    def _refresh_all_processes(self):
        """Atualiza lista de todos os processos do sistema"""
        try:
            selected_items = self.all_processes_tree.selection()
            selected_pids = [self.all_processes_tree.item(item, "values")[0] for item in selected_items]

            for item in self.all_processes_tree.get_children():
                self.all_processes_tree.delete(item)
            
            search_term = self.search_var.get().lower()
            
            process_count = 0
            all_processes = []
            
            for proc in psutil.process_iter(['pid', 'name', 'username', 'status']):
                try:
                    proc_info = proc.info
                    proc_name = proc_info['name'] or ''
                    proc_user = proc_info['username'] or ''
                    proc_status = proc_info['status'] or 'Unknown'
                    
                    if search_term and search_term not in proc_name.lower():
                        continue
                    
                    all_processes.append((
                        str(proc_info['pid']),
                        proc_name,
                        proc_user,
                        proc_status
                    ))
                    process_count += 1
                    
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            
            sorted_column = None
            reverse_order = False
            
            for column, is_reverse in self.sort_order.items():
                if is_reverse is not None:
                    sorted_column = column
                    reverse_order = is_reverse
                    break
            
            if sorted_column:
                if sorted_column == "PID":
                    all_processes.sort(key=lambda x: int(x[0]) if x[0].isdigit() else float('inf'), reverse=reverse_order)
                elif sorted_column == "Nome":
                    all_processes.sort(key=lambda x: x[1].lower(), reverse=reverse_order)
                elif sorted_column == "Usuário":
                    all_processes.sort(key=lambda x: x[2].lower(), reverse=reverse_order)
                elif sorted_column == "Status":
                    all_processes.sort(key=lambda x: x[3].lower(), reverse=reverse_order)
            
            for process_data in all_processes:
                item = self.all_processes_tree.insert("", "end", values=process_data)
                
                if process_data[0] in selected_pids:
                    self.all_processes_tree.selection_add(item)
            
            self.process_status_label.config(text=f"✅ {process_count} processos encontrados")
            
        except Exception as e:
            self.process_status_label.config(text=f"❌ Erro ao carregar processos: {e}")

    def _create_scheduler_tab(self):
        """Cria aba de agendamento"""
        sched_frame = ttk.Frame(self.notebook)
        self.notebook.add(sched_frame, text="Agendador")
        
        # Frame principal com grid
        main_frame = ttk.Frame(sched_frame, padding=10)
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)
        
        # Frame de controles
        controls_frame = ttk.Frame(main_frame)
        controls_frame.pack(fill="x", pady=(0, 10))
        
        # Botão para adicionar novo agendamento
        add_btn = ttk.Button(
            controls_frame,
            text="➕ Novo Agendamento",
            cursor="hand2",
            command=self._open_new_schedule_window,
            width=25
        )
        add_btn.pack(side="left", padx=5)
        
        # Botão editar
        edit_btn = ttk.Button(
            controls_frame,
            text="✏️ Editar Selecionado",
            cursor="hand2",
            command=self.edit_schedule,
            width=25
        )
        edit_btn.pack(side="left", padx=5)
        
        # Botão excluir
        delete_btn = ttk.Button(
            controls_frame,
            text="🗑️ Excluir Selecionado",
            cursor="hand2",
            command=self.remove_schedule,
            width=25
        )
        delete_btn.pack(side="left", padx=5)
        
        # Botão recarregar
        reload_btn = ttk.Button(
            controls_frame,
            text="🔄 Recarregar",
            cursor="hand2",
            command=self.load_schedules,
            width=25
        )
        reload_btn.pack(side="left", padx=5)
        
        # Lista de agendamentos
        list_frame = ttk.LabelFrame(main_frame, text="Agendamentos Ativos", padding=10)
        list_frame.pack(fill="both", expand=True)
        
        # Treeview para agendamentos
        self.schedules_tree = ttk.Treeview(
            list_frame, 
            columns=("Nome", "Banco", "Frequência", "Horário", "Compactar", "Próxima Execução"), 
            show="headings",
            height=12
        )
        
        # Configurar cabeçalhos
        self.schedules_tree.heading("Nome", text="Nome")
        self.schedules_tree.heading("Banco", text="Banco de Dados")
        self.schedules_tree.heading("Frequência", text="Frequência")
        self.schedules_tree.heading("Horário", text="Horário")
        self.schedules_tree.heading("Compactar", text="Compactar")
        self.schedules_tree.heading("Próxima Execução", text="Próxima Execução")
        
        # Configurar colunas
        self.schedules_tree.column("Nome", width=150)
        self.schedules_tree.column("Banco", width=200)
        self.schedules_tree.column("Frequência", width=100)
        self.schedules_tree.column("Horário", width=80)
        self.schedules_tree.column("Compactar", width=80)
        self.schedules_tree.column("Próxima Execução", width=150)
        
        # Scrollbars
        v_scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.schedules_tree.yview)
        h_scrollbar = ttk.Scrollbar(list_frame, orient="horizontal", command=self.schedules_tree.xview)
        self.schedules_tree.configure(yscrollcommand=v_scrollbar.set, xscrollcommand=h_scrollbar.set)
        
        self.schedules_tree.pack(side="left", fill="both", expand=True)
        v_scrollbar.pack(side="right", fill="y")
        h_scrollbar.pack(side="bottom", fill="x")
        
        # Status
        self.schedule_status = ttk.Label(main_frame, text="Carregando agendamentos...", foreground="gray")
        self.schedule_status.pack(pady=5)
        
        # Carrega agendamentos salvos
        self.load_schedules()

    def _open_new_schedule_window(self):
        """janela para novo agendamento"""
        if not self.check_permission("manage_schedules"):
            return
            
        win = tk.Toplevel(self)
        win.title("Novo Agendamento")
        win.geometry("500x550")
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()
        
        # Centraliza
        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 250
        y = self.winfo_y() + (self.winfo_height() // 2) - 225
        win.geometry(f"+{x}+{y}")
        
        # Ícone
        icon_path = BASE_DIR / "images" / "icon.ico"
        if icon_path.exists():
            win.iconbitmap(str(icon_path))
        
        # Frame principal
        main_frame = ttk.Frame(win, padding=20)
        main_frame.pack(fill="both", expand=True)
        
        ttk.Label(main_frame, text="Novo Agendamento", font=("Arial", 14, "bold")).pack(pady=(0, 20))
        
        # Nome do agendamento
        ttk.Label(main_frame, text="Nome do agendamento:*", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
        sched_name_var = tk.StringVar()
        sched_name_entry = ttk.Entry(main_frame, textvariable=sched_name_var, width=40, font=("Arial", 10))
        sched_name_entry.pack(fill="x", pady=(0, 10))
        sched_name_entry.focus()
        
        # Banco de dados
        ttk.Label(main_frame, text="Banco de dados:*", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
        sched_db_var = tk.StringVar()
        db_frame = ttk.Frame(main_frame)
        db_frame.pack(fill="x", pady=(0, 10))
        sched_db_entry = ttk.Entry(db_frame, textvariable=sched_db_var, width=35, font=("Arial", 10))
        sched_db_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(db_frame, text="📁", width=3, 
                command=lambda: self._pick_schedule_db(sched_db_var)).pack(side="left", padx=5)
        
        # Frequência
        ttk.Label(main_frame, text="Frequência:*", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
        sched_freq_var = tk.StringVar(value="Diário")
        freq_combo = ttk.Combobox(main_frame, textvariable=sched_freq_var, 
                                values=["Diário", "Semanal", "Mensal"], 
                                state="readonly", width=20, font=("Arial", 10))
        freq_combo.pack(fill="x", pady=(0, 10))
        
        # Frame para opções específicas da frequência
        freq_options_frame = ttk.Frame(main_frame)
        freq_options_frame.pack(fill="x", pady=(0, 10))
        
        # Horário
        ttk.Label(main_frame, text="Horário (HH:MM):*", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
        
        # Frame para o campo de horário
        time_frame = ttk.Frame(main_frame)
        time_frame.pack(anchor="w", pady=(0, 10))
        
        # Função de validação dos campos de hora/minuto
        def validate_time_input(new_value):
            """Permite apenas até 2 dígitos numéricos"""
            if new_value == "":
                return True
            if len(new_value) > 2:
                return False
            return new_value.isdigit()
        
        vcmd = (self.register(validate_time_input), "%P")
        
        # Horas
        hour_var = tk.StringVar(value="02")
        hour_entry = ttk.Entry(
            time_frame,
            textvariable=hour_var,
            width=3,
            font=("Arial", 10),
            justify="center",
            validate="key",
            validatecommand=vcmd
        )
        hour_entry.pack(side="left")
        
        ttk.Label(time_frame, text=":", font=("Arial", 10, "bold")).pack(side="left", padx=2)
        
        # Minutos
        minute_var = tk.StringVar(value="00")
        minute_entry = ttk.Entry(
            time_frame,
            textvariable=minute_var,
            width=3,
            font=("Arial", 10),
            justify="center",
            validate="key",
            validatecommand=vcmd
        )
        minute_entry.pack(side="left")
        
        # Tooltip
        time_tooltip = ttk.Label(main_frame, text="Formato: HH:MM (24 horas). Ex: 14:30, 02:00, 23:45", 
                                foreground="gray", font=("Arial", 8))
        time_tooltip.pack(anchor="w", pady=(0, 10))
        
        # Compactar backup
        compress_frame = ttk.Frame(main_frame)
        compress_frame.pack(fill="x", pady=10)
        sched_compress_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(compress_frame, variable=sched_compress_var, 
                        text="Compactar backup após gerar (recomendado)").pack(anchor="w")
        
        # Botões
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill="x", pady=20)
        
        def create_schedule():
            """Cria o novo agendamento"""
            # Validações
            if not sched_name_var.get().strip():
                messagebox.showerror("Erro", "Digite um nome para o agendamento.")
                sched_name_entry.focus()
                return
                
            if not sched_db_var.get().strip():
                messagebox.showerror("Erro", "Selecione um banco de dados.")
                return
                
            hour_str = hour_var.get().strip()
            minute_str = minute_var.get().strip()
            
            if not hour_str or not minute_str:
                messagebox.showerror("Erro", "Preencha horas e minutos.")
                hour_entry.focus()
                return
                
            if not hour_str.isdigit() or not minute_str.isdigit():
                messagebox.showerror("Erro", "Horas e minutos devem conter apenas números.")
                hour_entry.focus()
                return
                
            if len(hour_str) > 2 or len(minute_str) > 2:
                messagebox.showerror("Erro", "Horas e minutos devem ter no máximo 2 dígitos.")
                hour_entry.focus()
                return
                
            try:
                hours_int = int(hour_str)
                minutes_int = int(minute_str)
                
                if not (0 <= hours_int <= 23):
                    raise ValueError("Hora deve estar entre 00 e 23")
                if not (0 <= minutes_int <= 59):
                    raise ValueError("Minutos devem estar entre 00 e 59")
                    
            except ValueError as e:
                messagebox.showerror("Erro", f"Horário inválido: {e}")
                hour_entry.focus()
                return
            
            # Formata para 2 dígitos
            hour_final = f"{hours_int:02d}"
            minute_final = f"{minutes_int:02d}"
            
            # Prepara dados do agendamento
            schedule_data = {
                "name": sched_name_var.get().strip(),
                "database": sched_db_var.get().strip(),
                "frequency": sched_freq_var.get(),
                "hour": int(hour_final),
                "minute": int(minute_final),
                "compress": sched_compress_var.get()
            }
            
            frequency = sched_freq_var.get()
            if frequency == "Semanal":
                if hasattr(self, 'sched_weekday_var'):
                    schedule_data["weekday"] = self.sched_weekday_var.get()
                else:
                    messagebox.showerror("Erro", "Selecione um dia da semana para o agendamento semanal.")
                    return
            elif frequency == "Mensal":
                if hasattr(self, 'sched_monthday_var'):
                    schedule_data["monthday"] = self.sched_monthday_var.get()
                else:
                    messagebox.showerror("Erro", "Selecione um dia do mês para o agendamento mensal.")
                    return
            
            # Adiciona à configuração
            if "scheduled_backups" not in self.conf:
                self.conf["scheduled_backups"] = []
            
            existing_names = [s["name"] for s in self.conf["scheduled_backups"]]
            if schedule_data["name"] in existing_names:
                messagebox.showerror("Erro", f"Já existe um agendamento com o nome '{schedule_data['name']}'.")
                sched_name_entry.focus()
                return
            
            self.conf["scheduled_backups"].append(schedule_data)
            
            if save_config(self.conf):
                win.destroy()
                self.load_schedules()
                self.log(f"📅 Agendamento criado: {schedule_data['name']}", "success")
                messagebox.showinfo("Sucesso", f"Agendamento '{schedule_data['name']}' criado com sucesso!")
            else:
                messagebox.showerror("Erro", "Erro ao salvar agendamento.")
        
        def cancel_creation():
            win.destroy()
        
        ttk.Button(btn_frame, text="💾 Criar Agendamento", 
                command=create_schedule,
                cursor="hand2").pack(side="left", padx=5)
        
        ttk.Button(btn_frame, text="❌ Cancelar", 
                command=cancel_creation,
                cursor="hand2").pack(side="right", padx=5)
        
        # Configurar opções iniciais de frequência
        self._update_new_schedule_freq_options(freq_options_frame, sched_freq_var.get())
        
        freq_combo.bind('<<ComboboxSelected>>', 
                        lambda e: self._update_new_schedule_freq_options(freq_options_frame, sched_freq_var.get()))

    def _update_new_schedule_freq_options(self, options_frame, frequency):
        """Atualiza opções de frequência"""
        # Limpa frame anterior
        for widget in options_frame.winfo_children():
            widget.destroy()
        
        if frequency == "Diário":
            # Para diário
            ttk.Label(options_frame, text="O backup será executado diariamente no horário selecionado.",
                     foreground="gray", font=("Arial", 9)).pack(anchor="w")
            
        elif frequency == "Semanal":
            # Para semanal
            ttk.Label(options_frame, text="Dia da semana:*", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
            self.sched_weekday_var = tk.StringVar(value="Segunda")
            weekday_combo = ttk.Combobox(options_frame, textvariable=self.sched_weekday_var,
                                       values=["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"],
                                       state="readonly", width=15, font=("Arial", 10))
            weekday_combo.pack(anchor="w", pady=(0, 5))
            
        elif frequency == "Mensal":
            # Para mensal
            ttk.Label(options_frame, text="Dia do mês:*", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
            self.sched_monthday_var = tk.StringVar(value="1")
            monthday_combo = ttk.Combobox(options_frame, textvariable=self.sched_monthday_var,
                                        values=[str(i) for i in range(1, 32)], state="readonly", width=5, font=("Arial", 10))
            monthday_combo.pack(anchor="w", pady=(0, 5))
            ttk.Label(options_frame, text="(1-31)", foreground="gray", font=("Arial", 9)).pack(anchor="w")

    def _pick_schedule_db(self, var):
        """Seleciona banco para agendamento"""
        db = filedialog.askopenfilename(
            title="Selecione o banco para agendamento",
            filetypes=[("Firebird Database", "*.fdb")]
        )
        if db:
            var.set(db)

    def _create_tools_tab(self):
        """Cria aba de ferramentas avançadas"""
        tools_frame = ttk.Frame(self.notebook)
        self.notebook.add(tools_frame, text="Ferramentas")
        
        # Frame principal
        main_frame = ttk.Frame(tools_frame)
        main_frame.pack(fill="both", expand=True, padx=15, pady=15)
        
        # ===== OPERAÇÕES DO BANCO DE DADOS =====
        maintenance_frame = ttk.LabelFrame(main_frame, text="🔧 Operações do Banco de dados", padding=15)
        maintenance_frame.pack(fill="x", pady=(0, 20))
        
        # Container para centralizar os botões
        maintenance_container = ttk.Frame(maintenance_frame)
        maintenance_container.pack(expand=True, fill="x")
        
        # Linha 1
        row1_frame = ttk.Frame(maintenance_container)
        row1_frame.pack(pady=10)
        
        optimize_btn = ttk.Button(
            row1_frame, 
            text="🔧 Otimizar Banco",
            cursor="hand2", 
            command=self.optimize_database,
            width=30
        )
        optimize_btn.pack(side="left", padx=8, pady=5)
        
        repair_btn = ttk.Button(
            row1_frame, 
            text="🔩 Corrigir Banco",
            cursor="hand2", 
            command=self.repair_database,
            width=30
        )
        repair_btn.pack(side="left", padx=8, pady=5)
        
        sweep_btn = ttk.Button(
            row1_frame, 
            text="🧹 Limpar Banco (Sweep)",
            cursor="hand2", 
            command=self.sweep_database,
            width=30
        )
        sweep_btn.pack(side="left", padx=8, pady=5)
        
        # Linha 2
        row2_frame = ttk.Frame(maintenance_container)
        row2_frame.pack(pady=10)
        
        recalc_indexes_btn = ttk.Button(
            row2_frame, 
            text="📊 Recalcular Índices",
            cursor="hand2", 
            command=self.recalculate_indexes,
            width=30
        )
        recalc_indexes_btn.pack(side="left", padx=8, pady=5)

        migrate_btn = ttk.Button(
            row2_frame, 
            text="🔄 Migrar Banco",
            cursor="hand2", 
            command=self.migrate_database,
            width=30
        )
        migrate_btn.pack(side="left", padx=8, pady=5)
        
        # ===== RELATÓRIOS =====
        migration_frame = ttk.LabelFrame(main_frame, text="🔄 Relatórios", padding=15)
        migration_frame.pack(fill="x", pady=(0, 20))
        
        # Container para centralizar os botões
        migration_container = ttk.Frame(migration_frame)
        migration_container.pack(expand=True, fill="x")
        
        # Linha 1
        row_reports = ttk.Frame(migration_container)
        row_reports.pack(pady=10)
        
        gstat_report_btn = ttk.Button(
            row_reports, 
            text="📈 Relatório Banco (GSTAT)",
            cursor="hand2", 
            command=self.generate_gstat_report,
            width=30
        )
        gstat_report_btn.pack(side="left", padx=8, pady=5)

        report_btn = ttk.Button(
            row_reports, 
            text="📋 Relatório Sistema",
            cursor="hand2", 
            command=self.generate_system_report,
            width=30
        )
        report_btn.pack(side="left", padx=8, pady=5)

        space_btn = ttk.Button(
            row_reports, 
            text="💾 Verificar Espaço em Disco",
            cursor="hand2", 
            command=self.check_disk_space,
            width=30
        )
        space_btn.pack(side="left", padx=8, pady=5)
        
        # ===== CONFIGURAÇÕES =====
        config_frame = ttk.LabelFrame(main_frame, text="⚙️ Configurações e Utilitários", padding=15)
        config_frame.pack(fill="x", pady=(0, 20))
        
        # Container para centralizar os botões
        config_container = ttk.Frame(config_frame)
        config_container.pack(expand=True, fill="x")
        
        # Linha 1
        row_config = ttk.Frame(config_container)
        row_config.pack(pady=10)
        
        export_btn = ttk.Button(
            row_config, 
            text="📤 Exportar Configurações",
            cursor="hand2", 
            command=self.export_config,
            width=30
        )
        export_btn.pack(side="left", padx=8, pady=5)
        
        import_btn = ttk.Button(
            row_config, 
            text="📥 Importar Configurações",
            cursor="hand2", 
            command=self.import_config,
            width=30
        )
        import_btn.pack(side="left", padx=8, pady=5)
        
        # Linha 2
        row_config2 = ttk.Frame(config_container)
        row_config2.pack(pady=10)

        compress_btn = ttk.Button(
            row_config2, 
            text="🗜️ Compactar Arquivos",
            cursor="hand2", 
            command=self.compress_files,
            width=30
        )
        compress_btn.pack(side="left", padx=8, pady=5)
        
        extract_btn = ttk.Button(
            row_config2, 
            text="📦 Descompactar Arquivos",
            cursor="hand2", 
            command=self.extract_files,
            width=30
        )
        extract_btn.pack(side="left", padx=8, pady=5)
        
        # Centralizar
        for container in [maintenance_container, migration_container, config_container]:
            container.pack_configure(anchor="center")
        
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(0, weight=1)

    def _create_footer(self):
        """Cria rodapé da aplicação"""
        footer_frame = tk.Frame(self, bg="#f5f5f5", relief="ridge", borderwidth=1)
        footer_frame.pack(side="bottom", fill="x")

        def abrir_janela_versao(event):
            # Criar janela de info versão
            janela_versao = tk.Toplevel()
            janela_versao.title("Informações da Versão")
            janela_versao.geometry("350x450")
            janela_versao.resizable(False, False)
            
            # Centraliza
            self.update_idletasks()
            x = self.winfo_x() + (self.winfo_width() // 2) - 175
            y = self.winfo_y() + (self.winfo_height() // 2) - 225
            janela_versao.geometry(f"+{x}+{y}")

            # Ícone
            icon_path = BASE_DIR / "images" / "icon.ico"
            if icon_path.exists():
                janela_versao.iconbitmap(str(icon_path))
            
            main_frame = tk.Frame(janela_versao)
            main_frame.pack(fill="both", expand=True, padx=20, pady=10)
            
            # Frame para a versão
            versao_frame = tk.Frame(main_frame)
            versao_frame.pack(anchor="n", fill="x", pady=10)
            
            # Label da versão
            tk.Label(
                versao_frame,
                text=f"Versão: {APP_VERSION}",
                font=("Arial", 12, "bold"),
            ).pack(expand=True)
            
            # Frame para o botão copiar
            copiar_frame = tk.Frame(versao_frame)
            copiar_frame.pack(fill="x", pady=5)
            
            # Botão copiar versão
            btn_copiar = ttk.Button(
                copiar_frame,
                text="📋 Copiar Versão",
                cursor="hand2",
                width=15
            )
            btn_copiar.pack(anchor="center")
            
            # Frame para os tópicos
            topicos_frame = tk.Frame(main_frame)
            topicos_frame.pack(fill="both", expand=True, pady=10)
            
            # Tópicos/Especificações da versão
            especificacoes = [
                            "✓ Restore com suporte a novos tipos de compactações de arquivo",
                            "✓ Novas funções de compactar e descompactar arquivos"
                        ]
            
            for especificacao in especificacoes:
                tk.Label(
                    topicos_frame,
                    text=especificacao,
                    font=("Arial", 9),
                    anchor="w",
                    justify="left"
                ).pack(fill="x", pady=2)
            
            # Frame para o botão fechar
            button_frame = tk.Frame(main_frame)
            button_frame.pack(side="bottom", fill="x", pady=10)
            
            # Botão fechar
            ttk.Button(
                button_frame,
                text="❌ Fechar",
                command=janela_versao.destroy,
                cursor="hand2"
            ).pack(anchor="center")

            def copiar_versao():
                janela_versao.clipboard_clear()
                janela_versao.clipboard_append(APP_VERSION)
                janela_versao.update()
                
                btn_copiar.config(text="✅ Copiado!")
                
                janela_versao.after(2000, lambda: btn_copiar.config(text="📋 Copiar Versão"))

            btn_copiar.config(command=copiar_versao)

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
            text=f"Versão: {APP_VERSION}",
            font=("Arial", 9),
            bg="#f5f5f5",
            fg="gray",
            anchor="e"
        )
        footer_right.pack(side="right", padx=10, pady=3)

        footer_right.bind("<Double-Button-1>", abrir_janela_versao)

    # ---------- SISTEMA DE VERIFICAÇÃO DE ATUALIZAÇÕES ----------
    def check_and_notify_update(self):
        try:
            update_info = check_for_updates(self.conf)
            
            if update_info:
                self.show_update_notification(update_info)
            else:
                if self.dev_mode:
                    self.log("✅ Você está na versão mais recente", "info")
                    
        except Exception as e:
            self.log(f"⚠️ Verificação de atualização falhou: {e}", "debug")

    def check_update_manual(self):
        """Verificação manual de atualizações em thread separada"""
        if self.update_check_in_progress:
            messagebox.showinfo("Aviso", "Já há uma verificação de atualização em andamento.")
            return
        
        self.update_check_in_progress = True
        self.log("🔍 Verificando atualizações manualmente...", "info")
        self.set_status("Verificando atualizações...", "blue")
        
        def update_callback(update_info):
            """Callback para verificação manual"""
            self.update_check_in_progress = False
            
            if update_info:
                # Só exibe uma notificação
                self.after(0, lambda: self.show_update_notification(update_info))
                self.set_status("✅ Nova versão disponível!", "green")
                # Log ÚNICO
                self.log(f"📢 Nova versão disponível: {update_info['latest_version']}", "info")
            else:
                self.after(0, lambda: messagebox.showinfo("Verificação de Atualização", 
                                                        "✅ Você está usando a versão mais recente!"))
                self.set_status("Pronto", "gray")
                # Log único
                self.log("✅ Você está na versão mais recente", "success")
                # Salva a data da última verificação
                self.conf["last_update_check"] = datetime.now().isoformat()
                save_config(self.conf)
        
        # Chama a verificação em thread separada
        check_updates_in_thread(self.conf, update_callback)

    def show_update_notification(self, update_info):
        """Mostra janela de notificação de atualização"""
        update_win = tk.Toplevel(self)
        update_win.title("📢 Atualização Disponível!")
        update_win.geometry("600x500")
        update_win.resizable(True, True)
        update_win.transient(self)
        update_win.grab_set()
        
        # Centraliza
        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 300
        y = self.winfo_y() + (self.winfo_height() // 2) - 200
        update_win.geometry(f"+{x}+{y}")
        
        # Ícone
        icon_path = BASE_DIR / "images" / "icon.ico"
        if icon_path.exists():
            update_win.iconbitmap(str(icon_path))
        
        # Frame principal
        main_frame = ttk.Frame(update_win, padding=20)
        main_frame.pack(fill="both", expand=True)
        
        # Cabeçalho
        header_frame = ttk.Frame(main_frame)
        header_frame.pack(fill="x", pady=(0, 15))
        
        ttk.Label(
            header_frame,
            text="🎉 NOVA VERSÃO DISPONÍVEL!",
            font=("Arial", 16, "bold"),
            foreground="green"
        ).pack()
        
        ttk.Label(
            header_frame,
            text="Uma versão mais recente do Gerenciador Firebird está disponível para download",
            font=("Arial", 10),
            foreground="gray"
        ).pack(pady=5)
        
        # Informações da versão
        info_frame = ttk.LabelFrame(main_frame, text="📋 Informações da Versão", padding=15)
        info_frame.pack(fill="x", pady=10)
        
        ttk.Label(
            info_frame,
            text=f"Versão atual: {update_info['current_version']}",
            font=("Arial", 10)
        ).pack(anchor="w")
        
        ttk.Label(
            info_frame,
            text=f"Nova versão: {update_info['latest_version']}",
            font=("Arial", 10, "bold")
        ).pack(anchor="w", pady=5)
        
        # Notas de release
        if update_info.get('release_notes'):
            notes_frame = ttk.LabelFrame(main_frame, text="📝 Novidades desta versão", padding=15)
            notes_frame.pack(fill="both", expand=True, pady=10)
            
            notes_text = scrolledtext.ScrolledText(notes_frame, height=6, wrap=tk.WORD)
            notes_text.pack(fill="both", expand=True)
            notes_text.insert("1.0", update_info['release_notes'])
            notes_text.config(state="disabled")
    
        # Botões
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill="x", pady=15)
        
        def download_update():
            try:
                import webbrowser
                webbrowser.open(update_info['download_url'])
                
                Label(update_win, text="Após baixar, execute o instalador manualmente. Fechando aplicação em 5 segundos...", 
                    fg="green", font=("Arial", 10), justify='center').pack(pady=10)
                
                update_win.after(5000, lambda: [update_win.destroy(), sys.exit(0)])
                
            except Exception as e:
                messagebox.showerror("Erro", f"Não foi possível abrir o link de download:\n{e}")
        
        def remind_later():
            """Fecha e lembra depois"""
            self.conf["last_update_check"] = None
            save_config(self.conf)
            update_win.destroy()
        
        def skip_version():
            # Marca esta versão como ignorada
            self.conf["ignored_version"] = update_info['latest_version']
            save_config(self.conf)
            update_win.destroy()
        
        # Só mostra botão de download se houver URL
        if update_info.get('download_url'):
            ttk.Button(
                btn_frame,
                text="⬇️ Baixar Agora",
                command=download_update,
                cursor="hand2"
            ).pack(side="left", padx=5)
        
        ttk.Button(
            btn_frame,
            text="⏰ Lembrar Depois",
            command=remind_later,
            cursor="hand2"
        ).pack(side="left", padx=5)
        
        ttk.Button(
            btn_frame,
            text="🚫 Ignorar Esta Versão",
            command=skip_version,
            cursor="hand2"
        ).pack(side="right", padx=5)
        
        # Foca na janela
        update_win.focus_force()

    # ------- Sistema de Compactação de Descompactação
    def compress_files(self):
        """Compacta arquivos ou pastas para ZIP"""
        if not self.check_permission("export_import"):
            return
            
        items = filedialog.askopenfilenames(
            title="Selecione arquivos para compactar",
            filetypes=[("Todos os arquivos", "*.*")]
        )
        
        if not items:
            return
            
        zip_path = filedialog.asksaveasfilename(
            title="Salvar arquivo ZIP como...",
            defaultextension=".zip",
            filetypes=[("Arquivos ZIP", "*.zip")]
        )
        
        if not zip_path:
            return
            
        zip_path = Path(zip_path)

        # Muda para aba Principal antes de começar
        self.switch_to_main_tab()
        
        # Cria janela de progresso
        progress_win = tk.Toplevel(self)
        progress_win.title("Compactando arquivos...")
        progress_win.geometry("400x200")
        progress_win.resizable(False, False)
        progress_win.transient(self)
        progress_win.grab_set()
        
        # Centraliza
        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 200
        y = self.winfo_y() + (self.winfo_height() // 2) - 90
        progress_win.geometry(f"+{x}+{y}")
        
        # Ícone
        icon_path = BASE_DIR / "images" / "icon.ico"
        if icon_path.exists():
            progress_win.iconbitmap(str(icon_path))
        
        # Frame principal
        main_frame = ttk.Frame(progress_win, padding=20)
        main_frame.pack(fill="both", expand=True)
        
        # Mensagem
        ttk.Label(main_frame, 
                 text="🗜️ Compactando arquivos...",
                 font=("Arial", 10, "bold")).pack(pady=10)
        
        progress_label = ttk.Label(main_frame, 
                                 text=f"Processando {len(items)} arquivo(s)...",
                                 font=("Arial", 9))
        progress_label.pack(pady=5)
        
        # Barra de progresso
        progress_bar = ttk.Progressbar(main_frame, 
                                     mode='indeterminate',
                                     length=300)
        progress_bar.pack(pady=10)
        progress_bar.start(10)
        
        # Variável de controle para cancelamento
        cancel_var = threading.Event()
        
        def cancel_compression():
            """Cancela a compactação"""
            cancel_var.set()
            progress_win.destroy()
            self.log("❌ Compactação cancelada pelo usuário", "warning")
        
        # Botão de cancelar
        cancel_btn = ttk.Button(main_frame, 
                               text="❌ Cancelar",
                               command=cancel_compression)
        cancel_btn.pack(pady=5)
        
        # Configura o botão X da janela para cancelar também
        def on_window_close():
            cancel_compression()
        
        progress_win.protocol("WM_DELETE_WINDOW", on_window_close)
        
        def compress_worker():
            """Thread para compactação"""
            try:
                self.log(f"🗜️ Iniciando compactação de {len(items)} arquivo(s) para {zip_path.name}", "info")
                
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                    for i, item_path in enumerate(items, 1):
                        # Verifica se o usuário cancelou
                        if cancel_var.is_set():
                            break
                        
                        item = Path(item_path)
                        
                        self.after(0, lambda idx=i: progress_label.config(
                            text=f"Compactando {idx}/{len(items)}: {item.name}"
                        ))
                        
                        if item.is_file():
                            zf.write(item_path, arcname=item.name)
                        elif item.is_dir():
                            for root, dirs, files in os.walk(item_path):
                                # Verifica novamente a cada pasta
                                if cancel_var.is_set():
                                    break
                                
                                for file in files:
                                    # Verifica a cada arquivo
                                    if cancel_var.is_set():
                                        break
                                    
                                    file_path = os.path.join(root, file)
                                    arcname = os.path.relpath(file_path, item.parent)
                                    zf.write(file_path, arcname=arcname)
                
                # Se foi cancelado, remove o arquivo ZIP incompleto
                if cancel_var.is_set():
                    try:
                        if zip_path.exists():
                            zip_path.unlink()
                            self.log(f"🗑️ Arquivo ZIP incompleto removido: {zip_path.name}", "info")
                    except Exception as e:
                        self.log(f"⚠️ Não foi possível remover arquivo incompleto: {e}", "warning")
                    
                    self.after(0, lambda: [
                        self.log("❌ Compactação cancelada pelo usuário", "warning")
                    ])
                    return
                
                # Se não foi cancelado, mostra sucesso
                self.after(0, lambda: [
                    progress_win.destroy(),
                    self.log(f"✅ Compactação concluída: {zip_path.name}", "success"),
                    messagebox.showinfo("Compactação Concluída", 
                                      f"✅ {len(items)} arquivo(s) compactado(s) com sucesso!\n\n"
                                      f"Arquivo ZIP: {zip_path.name}")
                ])
                
            except Exception as e:
                # Se houve erro, tenta remover o arquivo ZIP incompleto
                if zip_path.exists():
                    try:
                        zip_path.unlink()
                        self.log(f"🗑️ Arquivo ZIP incompleto removido após erro: {zip_path.name}", "info")
                    except:
                        pass
                
                if not cancel_var.is_set():  # Só mostra erro se não foi cancelado
                    self.after(0, lambda: [
                        progress_win.destroy(),
                        self.log(f"❌ Erro na compactação: {e}", "error"),
                        messagebox.showerror("Erro na Compactação", 
                                           f"Falha ao compactar arquivos:\n{e}")
                    ])
        
        # Inicia thread de compactação
        threading.Thread(target=compress_worker, daemon=True).start()

    def extract_files(self):
        """Descompacta arquivos"""
        if not self.check_permission("export_import"):
            return
            
        # Seleciona arquivo
        archive_path = filedialog.askopenfilename(
            title="Selecione arquivo para descompactar",
            filetypes=[
                ("Arquivos Compactados", "*.zip *.rar *.7z *.tar.gz *.tar.bz2 *.tgz *.tbz2"),
                ("Todos os arquivos", "*.*")
            ]
        )
        
        if not archive_path:
            return
            
        archive_path = Path(archive_path)
        
        # Seleciona pasta de destino
        dest_dir = filedialog.askdirectory(
            title="Selecione pasta de destino para extração"
        )
        
        if not dest_dir:
            return
            
        dest_dir = Path(dest_dir)

        # Muda para aba Principal antes de começar
        self.switch_to_main_tab()
        
        # Cria janela de progresso
        progress_win = tk.Toplevel(self)
        progress_win.title("Descompactando arquivos...")
        progress_win.geometry("400x250")
        progress_win.resizable(False, False)
        progress_win.transient(self)
        progress_win.grab_set()
        
        # Centraliza
        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 200
        y = self.winfo_y() + (self.winfo_height() // 2) - 100
        progress_win.geometry(f"+{x}+{y}")
        
        # Ícone
        icon_path = BASE_DIR / "images" / "icon.ico"
        if icon_path.exists():
            progress_win.iconbitmap(str(icon_path))
        
        # Frame principal
        main_frame = ttk.Frame(progress_win, padding=20)
        main_frame.pack(fill="both", expand=True)
        
        # Mensagem
        ttk.Label(main_frame, 
                 text="📦 Descompactando arquivo...",
                 font=("Arial", 10, "bold")).pack(pady=10)
        
        progress_label = ttk.Label(main_frame, 
                                 text=f"Arquivo: {archive_path.name}",
                                 font=("Arial", 9))
        progress_label.pack(pady=5)
        
        file_type_label = ttk.Label(main_frame, 
                                  text="Detectando tipo de arquivo...",
                                  font=("Arial", 8),
                                  foreground="gray")
        file_type_label.pack(pady=2)
        
        # Barra de progresso
        progress_bar = ttk.Progressbar(main_frame, 
                                     mode='indeterminate',
                                     length=300)
        progress_bar.pack(pady=10)
        progress_bar.start(10)
        
        # Botão cancelar
        cancel_var = threading.Event()
        
        def cancel_extraction():
            cancel_var.set()
            progress_win.destroy()
            self.log("❌ Extração cancelada pelo usuário", "warning")
        
        ttk.Button(main_frame, 
                  text="❌ Cancelar Extração",
                  command=cancel_extraction).pack(pady=5)
        
        def count_initial_files(directory):
            """Conta arquivos em um diretório"""
            count = 0
            if directory.exists():
                for root, dirs, files in os.walk(directory):
                    count += len(files)
            return count
        
        def extract_worker():
            """Thread para extração"""
            try:
                initial_count = count_initial_files(dest_dir)
                
                file_type = self._detect_archive_type(str(archive_path))
                self.after(0, lambda: file_type_label.config(
                    text=f"Tipo: {file_type}"
                ))
                
                self.log(f"📦 Iniciando extração de {archive_path.name} ({file_type})", "info")
                
                self.after(0, lambda: progress_label.config(
                    text="Validando e extraindo arquivo..."
                ))
                safe_extract_archive(archive_path, dest_dir)
                
                if cancel_var.is_set():
                    return
                
                final_count = count_initial_files(dest_dir)
                
                extracted_count = final_count - initial_count
                
                if extracted_count < 0:
                    extracted_count = 0
                    try:
                        if archive_path.suffix.lower() == '.zip':
                            with zipfile.ZipFile(archive_path, 'r') as zf:
                                for info in zf.infolist():
                                    if not info.is_dir():
                                        extracted_count += 1
                    except:
                        extracted_count = 0
                
                # Finaliza na thread principal
                self.after(0, lambda: [
                    progress_win.destroy(),
                    self.log(f"✅ Extração concluída: {extracted_count} arquivo(s) extraído(s)", "success"),
                    messagebox.showinfo("Extração Concluída", 
                                      f"✅ Arquivo descompactado com sucesso!\n\n"
                                      f"Arquivo: {archive_path.name}\n"
                                      f"Destino: {dest_dir}\n"
                                      f"Arquivos extraídos: {extracted_count}")
                ])
                
            except Exception as e:
                if not cancel_var.is_set():
                    self.after(0, lambda: [
                        progress_win.destroy(),
                        self.log(f"❌ Erro na extração: {e}", "error"),
                        messagebox.showerror("Erro na Extração", 
                                           f"Falha ao descompactar arquivo:\n{e}")
                    ])
        
        # Inicia thread de extração
        threading.Thread(target=extract_worker, daemon=True).start()

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
                        break
                    except Exception as e:
                        continue
            
            # Se não encontrou arquivo cria ícone padrão
            if image is None:
                from PIL import ImageDraw
                image = Image.new('RGB', (32, 32), color='#2c3e50')
                draw = ImageDraw.Draw(image)
                
                draw.text((10, 6), "F", fill="white", font=None)
            
            # Menu do ícone
            menu = pystray.Menu(
                pystray.MenuItem(
                    "Abrir Gerenciador Firebird",
                    self.restore_from_tray
                ),
                # O backend Windows do pystray chama o item padrão a cada
                # clique esquerdo. Mantê-lo invisível permite reconhecer dois
                # cliques sem alterar o menu acionado pelo botão direito.
                pystray.MenuItem(
                    "Abrir",
                    self._handle_tray_left_click,
                    default=True,
                    visible=False
                ),
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
            
            
        except ImportError:
            self.log("⚠️ Biblioteca pystray não encontrada. Instale com: pip install pystray pillow", "warning")
            self.tray_icon = None

    def minimize_to_tray(self):
        """Minimiza o programa para a bandeja do sistema"""
        if self.conf.get("minimize_to_tray", True):
            self.withdraw()
            self._tray_last_click = 0.0
            if self.tray_icon is None:
                self.create_tray_icon()
        else:
            self.iconify()

    def restore_from_tray(self, icon=None, item=None):
        """Restaura o programa da bandeja"""
        active_icon = icon or self.tray_icon
        if active_icon:
            active_icon.stop()
        self.tray_icon = None
        self._tray_last_click = 0.0
        # Callbacks do pystray não rodam na thread do Tkinter.
        self.after(0, self._show_window_from_tray)

    def _handle_tray_left_click(self, icon=None, item=None):
        """Restaura a janela após um duplo clique esquerdo na bandeja."""
        now = time.monotonic()
        try:
            double_click_interval = ctypes.windll.user32.GetDoubleClickTime() / 1000.0
        except Exception:
            double_click_interval = 0.5

        if 0 < now - self._tray_last_click <= double_click_interval:
            self._tray_last_click = 0.0
            self.restore_from_tray(icon, item)
        else:
            self._tray_last_click = now

    def _show_window_from_tray(self):
        """Restaura e traz a janela para frente na thread da interface."""
        self.deiconify()
        self.state('normal')
        self.lift()
        self.attributes('-topmost', True)
        self.after_idle(lambda: self.attributes('-topmost', False))
        self.focus_force()

    def quit_application(self, icon=None, item=None):
        """Fecha o aplicativo completamente"""
        if self.tray_icon:
            self.tray_icon.stop()
        
        self.schedule_running = False
        self.quit()
        self.destroy()

    def on_close(self):
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
            time.sleep(60)

    def stop_scheduler(self):
        """Para o agendador"""
        self.schedule_running = False
        if self.schedule_thread and self.schedule_thread.is_alive():
            self.schedule_thread.join(timeout=5)
        self.log("🛑 Agendador de backups parado", "info")

    def __del__(self):
        self.stop_scheduler()

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
            shortcut.save()
            
            self.log("✅ Programa adicionado à inicialização do Windows", "success")
            return True
            
        except Exception as e:
            self.log(f"❌ Erro ao adicionar à inicialização: {e}", "error")

            return self._add_to_startup_registry()

    def _add_to_startup_registry(self):
        try:
            script_path = sys.executable if getattr(sys, 'frozen', False) else sys.argv[0]
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

    def open_backup_folder(self):
        """Abre a pasta de backups padrão"""
        try:
            backup_dir = Path(self.conf.get("backup_dir", DEFAULT_BACKUP_DIR))
            
            # Verifica se o diretório existe, se não cria
            if not backup_dir.exists():
                backup_dir.mkdir(parents=True, exist_ok=True)
                self.log(f"📁 Pasta de backups criada: {backup_dir}", "info")
            
            # Abre no explorador de arquivos
            if open_file_with_default_app(backup_dir):
                self.log(f"📁 Pasta de backups aberta: {backup_dir}", "success")
            else:
                self.log(f"❌ Não foi possível abrir a pasta: {backup_dir}", "error")
                messagebox.showerror(
                    "Erro", 
                    f"Não foi possível abrir a pasta de backups:\n{backup_dir}"
                )
                
        except Exception as e:
            error_msg = f"❌ Erro ao abrir pasta de backups: {e}"
            self.log(error_msg, "error")
            messagebox.showerror("Erro", error_msg)

    def disable_buttons(self):
        """Desabilita todos os botões durante operações"""
        buttons = [self.btn_backup, self.btn_restore, self.btn_verify]
        for btn in buttons:
            btn.state(["disabled"])

    def enable_buttons(self):
        """Reabilita todos os botões"""
        buttons = [self.btn_backup, self.btn_restore, self.btn_verify]
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

        if self.dev_buffer.strip().lower() == "sql":
            self.open_sql_console()

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
    def run_command(self, cmd, on_finish=None, show_progress=True, on_complete=None):
        """Executa comandos em thread separada"""
        self.task_running = True
        self.disable_buttons()
        if show_progress:
            self.switch_to_main_tab()
            self._start_progress_animation()

        def complete(success):
            if show_progress:
                self._stop_progress_animation()
            self.enable_buttons()
            self.task_running = False
            if success and on_finish:
                on_finish()
            if on_complete:
                on_complete()

        def worker():
            success = False
            try:
                self.log(f"Executando comando: {redact_command(cmd)}", "debug")

                CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors='replace',
                    creationflags=CREATE_NO_WINDOW,
                    bufsize=1,
                    universal_newlines=True
                )

                output_lines = []
                while True:
                    line = process.stdout.readline()
                    if not line and process.poll() is not None:
                        break
                    if line.strip():
                        output_lines.append(line.strip())
                        self.after(100, lambda l=line.strip(): self.log(l, "info"))

                process.stdout.close()
                return_code = process.wait()

                if return_code == 0:
                    success = True
                    self.after(0, lambda: self.set_status("✅ Operação concluída com sucesso!", "green"))
                    self.after(0, lambda: self.log("✔️ Comando executado com sucesso.", "success"))
                    self.after(0, self.bell)
                else:
                    self.after(0, lambda: self.set_status("⚠️ Ocorreu um erro. Veja o log abaixo.", "red"))
                    self.after(0, lambda: self.log(f"⚠️ Comando retornou código de erro: {return_code}", "error"))

            except FileNotFoundError:
                error_msg = "Erro: Arquivo executável não encontrado. Verifique as configurações."
                self.after(0, lambda: self.log(error_msg, "error"))
                self.after(0, lambda: self.set_status("❌ Executável não encontrado.", "red"))
            except Exception as e:
                error_msg = f"Erro inesperado: {str(e)}"
                self.after(0, lambda: self.log(error_msg, "error"))
                self.after(0, lambda: self.set_status("❌ Falha inesperada.", "red"))
            finally:
                self.after(0, lambda: complete(success))

        threading.Thread(target=worker, daemon=True).start()

    def _start_progress_animation(self):
        """Inicia a animação da barra de progresso"""
        self.progress["mode"] = "indeterminate"
        self.progress.start(10)
        self.set_status("Executando operação...", "blue")

    def _stop_progress_animation(self):
        """Para a animação da barra de progresso"""
        self.progress.stop()
        self.progress["mode"] = "determinate"
        self.progress["value"] = 0

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
        if not self.check_permission("backup"):
            return
            
        gbak = self.conf.get("gbak_path") or find_executable("gbak.exe")
        if not gbak:
            messagebox.showerror("Erro", "gbak.exe não encontrado. Configure o caminho do Firebird nas configurações.")
            return
        
        self.conf["gbak_path"] = gbak
        save_config(self.conf)

        db = filedialog.askopenfilename(
            title="Selecione o banco de dados (.fdb)", 
            filetypes=[("Firebird Database", "*.fdb"), ("Todos os arquivos", "*.*")]
        )
        if not db:
            return

        # Verifica o tamanho do banco de dados
        try:
            db_size = os.path.getsize(db)
            db_size_gb = db_size / (1024**3)
            self.log(f"📊 Tamanho do banco: {db_size_gb:.2f} GB", "info")
        except Exception as e:
            self.log(f"⚠️ Não foi possível verificar o tamanho do banco: {e}", "warning")
            db_size_gb = 0

        backup_dir_default = Path(self.conf.get("backup_dir", DEFAULT_BACKUP_DIR))
        
        backup_path = filedialog.asksaveasfilename(
            title="Salvar backup como...",
            initialdir=backup_dir_default,
            defaultextension=".fbk",
            filetypes=[("Firebird Backup", "*.fbk"), ("Todos os arquivos", "*.*")]
        )
        
        if not backup_path:
            return

        backup_path = Path(backup_path)
        backup_dir = backup_path.parent
        
        # Verifica espaço livre no disco
        disk_info = get_disk_space(backup_dir)
        if not disk_info:
            messagebox.showerror("Erro", "Não foi possível verificar o espaço em disco.")
            return
        
        free_space_gb = disk_info['free_gb']
        
        # Estima o tamanho do backup
        estimated_backup_size_gb = db_size_gb * 0.7
        
        # Verifica se há espaço suficiente
        required_space_gb = max(estimated_backup_size_gb, 0.1)
        
        if free_space_gb < required_space_gb:
            error_msg = (
                f"🚨 ESPAÇO INSUFICIENTE PARA BACKUP!\n\n"
                f"Espaço livre no disco: {free_space_gb:.2f} GB\n"
                f"Espaço estimado necessário: {required_space_gb:.2f} GB\n"
                f"Espaço faltante: {required_space_gb - free_space_gb:.2f} GB\n\n"
                f"Libere espaço em disco antes de continuar."
            )
            self.log(f"❌ {error_msg}", "error")
            messagebox.showerror("Espaço Insuficiente", error_msg)
            return
        
        elif free_space_gb < (required_space_gb * 2):
            warning_msg = (
                f"⚠️ ESPAÇO LIMITADO NO DISCO\n\n"
                f"Espaço livre: {free_space_gb:.2f} GB\n"
                f"Espaço necessário: {required_space_gb:.2f} GB\n"
                f"Espaço restante após backup: {free_space_gb - required_space_gb:.2f} GB\n\n"
                f"Deseja continuar mesmo assim?"
            )
            self.log(f"⚠️ {warning_msg}", "warning")
            if not messagebox.askyesno("Espaço Limitado", warning_msg, icon=messagebox.WARNING):
                self.log("❌ Backup cancelado pelo usuário devido a espaço limitado", "info")
                return
        
        self.log(f"✅ Espaço em disco suficiente: {free_space_gb:.2f} GB livres", "success")
        
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
        self.log(f"💾 Espaço disponível: {free_space_gb:.2f} GB", "info")
        self.set_status("Gerando backup, por favor aguarde...", "blue")

        def after_backup():
            if compress:
                # Compactação em uma thread separada
                self._compress_backup_in_thread(backup_path)
            else:
                keep_count = int(self.conf.get("keep_backups", DEFAULT_KEEP_BACKUPS))
                cleanup_old_backups(backup_dir, keep_count)
                
            self.logger.info(f"Backup finalizado com sucesso: {db}")

        self.run_command(cmd, on_finish=after_backup)

    def _compress_backup_in_thread(self, backup_path):
        """Executa a compactação do backup em uma thread separada"""
        def compress_worker():
            try:
                self.after(0, lambda: self.set_status("Compactando backup...", "blue"))
                self.after(0, lambda: self.log("🗜️ Iniciando compactação do backup...", "info"))
                
                zip_path = backup_path.with_suffix(".zip")
                self.after(0, lambda: self.log(f"📦 Compactando: {backup_path.name} -> {zip_path.name}", "info"))
                compress_backup(backup_path, zip_path)
                
                # Atualiza a interface na thread principal
                self.after(0, lambda: self.log(f"✅ Backup compactado com sucesso: {zip_path.name}", "success"))
                self.after(0, lambda: self.set_status("Backup compactado com sucesso!", "green"))
                
            except Exception as e:
                # Em caso de erro, mantém o arquivo .fbk original
                error_msg = f"❌ Erro ao compactar backup: {e}"
                self.after(0, lambda: self.log(error_msg, "error"))
                self.after(0, lambda: self.set_status("Erro na compactação", "red"))
                
            finally:
                self.after(0, lambda: self._cleanup_old_backups_after_compress(backup_path.parent))
        
        # Inicia a thread de compactação
        threading.Thread(target=compress_worker, daemon=True).start()

    def _cleanup_old_backups_after_compress(self, backup_dir=None):
        """Limpa backups antigos após a compactação"""
        try:
            backup_dir = Path(backup_dir or self.conf.get("backup_dir", DEFAULT_BACKUP_DIR))
            keep_count = int(self.conf.get("keep_backups", DEFAULT_KEEP_BACKUPS))
            cleanup_old_backups(backup_dir, keep_count)
            self.log("🧹 Limpeza de backups antigos concluída", "info")
        except Exception as e:
            self.log(f"⚠️ Erro durante limpeza de backups: {e}", "warning")

    def execute_scheduled_backup(self, db_path, schedule_name, compress=True):
        """Executa um backup agendado"""
        try:
            gbak = self.conf.get("gbak_path") or find_executable("gbak.exe")
            if not gbak or not os.path.exists(db_path):
                self.log(f"❌ Backup agendado '{schedule_name}' falhou: Banco não encontrado", "error")
                return

            # Verifica espaço em disco antes do backup agendado
            backup_dir = Path(self.conf.get("backup_dir", DEFAULT_BACKUP_DIR))
            disk_info = get_disk_space(backup_dir)
            
            if not disk_info:
                self.log(f"❌ Backup agendado '{schedule_name}' falhou: Não foi possível verificar espaço em disco", "error")
                return
            
            free_space_gb = disk_info['free_gb']
            
            # Verifica tamanho aproximado do banco
            try:
                db_size = os.path.getsize(db_path)
                db_size_gb = db_size / (1024**3)
                required_space_gb = max(db_size_gb * 0.7, 0.1) 
            except:
                required_space_gb = 1.0
                
            if free_space_gb < required_space_gb:
                self.log(f"❌ Backup agendado '{schedule_name}' cancelado: Espaço insuficiente. Livre: {free_space_gb:.2f}GB, Necessário: ~{required_space_gb:.2f}GB", "error")
                return
                
            backup_dir.mkdir(parents=True, exist_ok=True)
            
            timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
            db_name = Path(db_path).stem
            name = f"backup_{db_name}_{timestamp}.fbk"
            backup_path = backup_dir / name

            self.log(f"🕒 Executando backup agendado: {schedule_name}", "info")
            self.log(f"🔌 Conectando em: {self._get_service_mgr_string()}", "info")
            self.log(f"💾 Espaço disponível: {free_space_gb:.2f} GB", "info")

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

                    output, _ = process.communicate()
                    return_code = process.wait()

                    if return_code == 0:
                        self.log(f"✅ Backup agendado '{schedule_name}' gerado com sucesso", "success")
                        
                        if compress:
                            # Compacta em thread separada
                            self._compress_scheduled_backup(backup_path, schedule_name)
                        else:
                            # Limpa backups antigos
                            keep_count = int(self.conf.get("keep_backups", DEFAULT_KEEP_BACKUPS))
                            cleanup_old_backups(backup_dir, keep_count)
                            self.log(f"✅ Backup agendado '{schedule_name}' finalizado", "success")
                            
                    else:
                        self.log(f"❌ Backup agendado '{schedule_name}' falhou. Código: {return_code}", "error")
                        if output:
                            self.log(f"📄 Saída do comando: {output}", "error")

                except Exception as e:
                    self.log(f"❌ Erro no backup agendado '{schedule_name}': {e}", "error")

            # Executa em thread separada
            threading.Thread(target=run_scheduled_backup, daemon=True).start()

        except Exception as e:
            self.log(f"❌ Erro ao executar backup agendado '{schedule_name}': {e}", "error")

    def _compress_scheduled_backup(self, backup_path, schedule_name):
        """Compacta backup agendado em thread separada"""
        def compress_worker():
            try:
                self.log(f"🗜️ Compactando backup agendado: {schedule_name}", "info")
                
                zip_path = backup_path.with_suffix(".zip")
                
                compress_backup(backup_path, zip_path)

                backup_dir = Path(self.conf.get("backup_dir", DEFAULT_BACKUP_DIR))
                keep_count = int(self.conf.get("keep_backups", DEFAULT_KEEP_BACKUPS))
                cleanup_old_backups(backup_dir, keep_count)
                
                self.log(f"✅ Backup agendado '{schedule_name}' compactado com sucesso: {zip_path.name}", "success")
                
            except Exception as e:
                error_msg = f"❌ Erro ao compactar backup agendado '{schedule_name}': {e}"
                self.log(error_msg, "error")
        
        # Inicia a thread de compactação
        threading.Thread(target=compress_worker, daemon=True).start()

    def restore(self):
        """Restaura backup para banco de dados"""
        if not self.check_permission("restore"):
            return
            
        gbak = self.conf.get("gbak_path") or find_executable("gbak.exe")
        if not gbak:
            messagebox.showerror("Erro", "gbak.exe não encontrado. Configure o caminho do Firebird nas configurações.")
            return
        
        self.conf["gbak_path"] = gbak
        save_config(self.conf)

        # Tipos de arquivo suportados
        bkp = filedialog.askopenfilename(
            title="Selecione o arquivo de backup", 
            filetypes=[
                ("Compatíveis", "*.fbk *.zip *.rar *.7z *.tar.gz *.tar.bz2"),
                ("Backup Files", "*.fbk"),
                ("Arquivos Compactados", "*.zip *.rar *.7z *.tar.gz *.tar.bz2"),
                ("Todos os arquivos", "*.*")
            ]
        )
        if not bkp:
            return

        self.current_backup_file = bkp
        self.extracted_files = []
        self.extraction_cancelled = False

        # Verifica se é um arquivo compactado
        compressed_extensions = ['.zip', '.rar', '.7z', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2']
        file_ext = Path(bkp).suffix.lower()
        
        # Para extensões compostas
        if file_ext in ['.gz', '.bz2']:
            if Path(bkp).suffixes[-2:] == ['.tar', '.gz']:
                file_ext = '.tar.gz'
            elif Path(bkp).suffixes[-2:] == ['.tar', '.bz2']:
                file_ext = '.tar.bz2'
        
        if any(bkp.lower().endswith(ext) for ext in compressed_extensions):
            self._extract_compressed_backup(bkp)
        else:
            self._restore_fbk_backup(bkp)

    def _extract_compressed_backup(self, bkp):
        """Extrai backup compactado"""
        try:
            # Cria janela de extração
            self._create_progress_window()
            self.update_idletasks()

            bkp_path = Path(bkp)
            file_ext = bkp_path.suffix.lower()
            
            # Para extensões compostas
            if file_ext in ['.gz', '.bz2']:
                if bkp_path.suffixes[-2:] == ['.tar', '.gz']:
                    file_ext = '.tar.gz'
                elif bkp_path.suffixes[-2:] == ['.tar', '.bz2']:
                    file_ext = '.tar.bz2'
            
            self.extract_dir = bkp_path.parent / f"{bkp_path.stem}_extracted"
            self.extract_dir.mkdir(exist_ok=True)
            
            self.log(f"📦 Iniciando extração do arquivo {file_ext.upper()}: {bkp_path.name}", "info")
            self._update_progress(f"Analisando arquivo: {bkp_path.name}")
            
            # Detecta tipo de arquivo
            file_type = self._detect_archive_type(bkp)
            self._update_progress(f"Tipo detectado: {file_type}")
            time.sleep(0.5)
            
            self._update_progress("Iniciando extração...")
            
            def extract_with_progress():
                """Extrai arquivo compactado"""
                try:
                    if self.extraction_cancelled:
                        return False
                    
                    safe_extract_archive(bkp_path, self.extract_dir)
                    self.log(f"✅ Extração validada e concluída: {bkp_path.name}", "success")
                    return True
                                
                except Exception as e:
                    self.log(f"❌ Erro durante extração: {e}", "error")
                    return False
            
            # Executa extração em thread separada
            def extraction_worker():
                success = extract_with_progress()
                self.after(0, lambda: self._after_extraction(success, bkp))
            
            threading.Thread(target=extraction_worker, daemon=True).start()
            
        except Exception as e:
            self._close_progress_window()
            messagebox.showerror("Erro", f"Falha ao extrair arquivo: {e}")
            if hasattr(self, 'extract_dir') and self.extract_dir.exists():
                shutil.rmtree(self.extract_dir, ignore_errors=True)

    def _detect_archive_type(self, filepath):
        """Detecta o tipo de arquivo compactado"""
        path = Path(filepath)
        ext = path.suffix.lower()
        
        # Verifica extensões compostas
        if len(path.suffixes) >= 2:
            if path.suffixes[-2:] == ['.tar', '.gz']:
                return 'tar.gz'
            elif path.suffixes[-2:] == ['.tar', '.bz2']:
                return 'tar.bz2'

        extension_map = {
            '.zip': 'ZIP',
            '.rar': 'RAR',
            '.7z': '7-Zip',
            '.tar': 'TAR',
            '.gz': 'GZIP',
            '.bz2': 'BZIP2',
            '.tgz': 'GZipped TAR',
            '.tbz2': 'BZIP2 TAR'
        }
        
        return extension_map.get(ext, f"Desconhecido ({ext})")

    def _extract_rar_file(self, rar_path):
        """Extrai arquivos RAR específicos"""
        try:
            safe_extract_archive(rar_path, self.extract_dir)
            return True
        except Exception as e:
            self.log(f"❌ Falha ao extrair RAR: {e}", "error")
            return False

    def _extract_7z_file(self, sevenz_path):
        """Extrai arquivos 7-Zip"""
        try:
            safe_extract_archive(sevenz_path, self.extract_dir)
            return True
        except Exception as e:
            self.log(f"❌ Falha ao extrair 7z: {e}", "error")
            return False

    def _extract_tar_file(self, tar_path):
        """Extrai arquivos TAR"""
        try:
            safe_extract_archive(tar_path, self.extract_dir)
            return True
        except Exception as e:
            self.log(f"❌ Falha ao extrair TAR: {e}", "error")
            return False

    def _create_progress_window(self):
        """Cria janela de progresso para extração"""
        self.progress_win = tk.Toplevel(self)
        self.progress_win.title("Extraindo Backup")
        self.progress_win.geometry("450x200")
        self.progress_win.resizable(False, False)
        self.progress_win.transient(self)
        self.progress_win.grab_set()
        
        # Centraliza
        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 225
        y = self.winfo_y() + (self.winfo_height() // 2) - 75
        self.progress_win.geometry(f"+{x}+{y}")
        
        # Ícone
        icon_path = BASE_DIR / "images" / "icon.ico"
        if icon_path.exists():
            self.progress_win.iconbitmap(str(icon_path))
        
        # Frame principal
        main_frame = ttk.Frame(self.progress_win, padding=20)
        main_frame.pack(fill="both", expand=True)
        
        # Mensagem
        ttk.Label(main_frame, 
                text="📦 Extraindo arquivo ZIP...",
                font=("Arial", 10, "bold")).pack(pady=10)
        
        self.progress_label = ttk.Label(main_frame, 
                                    text="Preparando extração...",
                                    font=("Arial", 9))
        self.progress_label.pack(pady=5)
        
        # Barra de progresso
        self.progress_bar = ttk.Progressbar(main_frame, 
                                        mode='indeterminate',
                                        length=350)
        self.progress_bar.pack(pady=10)
        self.progress_bar.start(10)
        
        # Botão cancelar
        cancel_btn = ttk.Button(main_frame, 
                            text="❌ Cancelar Extração",
                            command=self._cancel_extraction)
        cancel_btn.pack(pady=5)

    def _update_progress(self, message):
        """Atualiza mensagem"""
        if hasattr(self, 'progress_label') and hasattr(self, 'progress_win'):
            self.progress_label.config(text=message)
            self.progress_win.update_idletasks()

    def _close_progress_window(self):
        """Fecha janela de progresso"""
        if hasattr(self, 'progress_win'):
            self.progress_win.destroy()

    def _cancel_extraction(self):
        """Cancela a extração"""
        self.extraction_cancelled = True
        self.log("❌ Extração cancelada pelo usuário", "warning")
        self._close_progress_window()

    def _after_extraction(self, extraction_success, bkp):
        self._close_progress_window()
        
        if not extraction_success:
            if hasattr(self, 'extract_dir') and self.extract_dir.exists():
                shutil.rmtree(self.extract_dir, ignore_errors=True)
            return
        
        # Busca arquivos .fbk extraídos
        extract_dir = Path(bkp).parent / f"{Path(bkp).stem}_extracted"
        
        # Procura recursivamente por arquivos .fbk
        fbks = list(extract_dir.rglob("*.fbk"))
        
        if not fbks:
            fbks = list(extract_dir.rglob("*.fdb"))
            
        if not fbks:
            messagebox.showerror("Erro", "Nenhum arquivo .fbk ou .fdb encontrado dentro do arquivo compactado.")
            if extract_dir.exists():
                shutil.rmtree(extract_dir, ignore_errors=True)
            return
        
        actual_backup = str(fbks[0])
        self.extracted_files.append(extract_dir)
        
        self.log(f"✅ Arquivo extraído: {actual_backup}", "success")
        
        dest = filedialog.asksaveasfilename(
            title="Salvar banco restaurado como...",
            defaultextension=".fdb",
            filetypes=[("Firebird Database", "*.fdb")]
        )
        
        if not dest:
            # Limpa arquivos extraídos se o usuário cancelar
            for item in self.extracted_files:
                if Path(item).exists():
                    if Path(item).is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        Path(item).unlink(missing_ok=True)
            return
        
        self._perform_restoration(actual_backup, dest, self.extracted_files)

    def _restore_fbk_backup(self, bkp):
        """Restaura backup .fbk diretamente"""
        dest = filedialog.asksaveasfilename(
            title="Salvar banco restaurado como...",
            defaultextension=".fdb",
            filetypes=[("Firebird Database", "*.fdb")]
        )
        if not dest:
            return

        # Executa restauração
        self._perform_restoration(bkp, dest, [])

    def _perform_restoration(self, backup_path, destination_path, extracted_files):
        """Executa a restauração do backup"""
        gbak = self.conf.get("gbak_path")
        
        # Constrói comando gbak restauração
        cmd = [
            gbak, "-c", 
            "-se", self._get_service_mgr_string(),
            backup_path, 
            destination_path, 
            "-user", self.conf.get("firebird_user", "SYSDBA"), 
            "-pass", self.conf.get("firebird_password", "masterkey"),
            "-page_size", self.conf.get("page_size", "8192")
        ]

        self.log(f"🟦 Restaurando backup: {Path(backup_path).name} -> {Path(destination_path).name}", "info")
        self.log(f"🔌 Conectando em: {self._get_service_mgr_string()}", "info")
        self.log(f"📄 PageSize configurado: {self.conf.get('page_size', '8192')}", "info")
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

        self.run_command(cmd, on_complete=cleanup_extracted)

    def verify(self):
        """Verifica integridade do banco"""
        if not self.check_permission("verify"):
            return
            
        gfix = self.conf.get("gfix_path") or find_executable("gfix.exe")
        if not gfix:
            messagebox.showerror("Erro", "gfix.exe não encontrado. Configure o caminho do Firebird nas configurações.")
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
        correction_win.geometry("600x500")
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
            "de prosseguir com a correção, pois o processo pode be irreversível.\n\n"
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
            "-pass", self.conf.get("firebird_password", "masterkey"),
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
        if not self.check_permission("repair"):
            return
            
        gfix = self.conf.get("gfix_path") or find_executable("gfix.exe")
        if not gfix:
            messagebox.showerror("Erro", "gfix.exe não encontrado. Configure o caminho do Firebird nas configurações.")
            return
        
        self.conf["gfix_path"] = gfix
        save_config(self.conf)

        db = filedialog.askopenfilename(
            title="Selecione o banco de dados para correção", 
            filetypes=[("Firebird Database", "*.fdb"), ("Todos os arquivos", "*.*")]
        )
        if not db:
            return

        # Pergunta se deseja fazer limpeza antes da correção
        do_sweep = messagebox.askyesno(
            "Limpeza do Banco",
            "Deseja executar a limpeza do banco (sweep) antes da correção?\n\n"
            "✅ Com sweep: Limpa registros antigos e otimiza o banco\n"
            "❌ Sem sweep: Apenas correção de erros estruturais"
        )

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

        # Muda para aba Principal antes de começar
        self.switch_to_main_tab()
        
        if response:
            # Cria backup de segurança antes da correção
            self._create_safety_backup(db, lambda: self._execute_advanced_repair(db, do_sweep))
        else:
            if messagebox.askyesno(
                "Confirmação de Risco",
                "⚠️ ALTO RISCO ⚠️\n\n"
                "Você está prestes a executar uma correção sem backup de segurança.\n"
                "Esta operação pode corromper permanentemente o banco de dados.\n\n"
                "Tem certeza que deseja continuar SEM backup?",
                icon=messagebox.WARNING
            ):
                self._execute_advanced_repair(db, do_sweep)

    def _execute_advanced_repair(self, db_path, do_sweep=False):
        """Executa correção avançada do banco"""
        gfix = self.conf.get("gfix_path") or find_executable("gfix.exe")
        if not gfix:
            return
        
        self.log("🛠️ Iniciando correção avançada do banco...", "warning")
        self.set_status("Executando correção avançada...", "orange")
        
        repair_commands = []

        if do_sweep:
            repair_commands.append({
                "name": "Limpeza de registros antigos",
                "cmd": [gfix, "-sweep", db_path, "-user", self.conf["firebird_user"], "-pass", self.conf["firebird_password"]]
            })
        
        repair_commands.extend([
            {
                "name": "Validação completa",
                "cmd": [gfix, "-validate", "-full", db_path, "-user", self.conf["firebird_user"], "-pass", self.conf["firebird_password"]]
            },
            {
                "name": "Correção de páginas",
                "cmd": [gfix, "-mend", "-ig", db_path, "-user", self.conf["firebird_user"], "-pass", self.conf["firebird_password"]]
            }
        ])
        
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

    def sweep_database(self):
        """Executa a limpeza (sweep) do banco de dados"""
        if not self.check_permission("sweep"):
            return
            
        gfix = self.conf.get("gfix_path") or find_executable("gfix.exe")
        if not gfix:
            messagebox.showerror("Erro", "gfix.exe não encontrado. Configure o caminho do Firebird nas configurações.")
            return
        
        self.conf["gfix_path"] = gfix
        save_config(self.conf)

        db = filedialog.askopenfilename(
            title="Selecione o banco de dados para limpeza", 
            filetypes=[("Firebird Database", "*.fdb"), ("Todos os arquivos", "*.*")]
        )
        if not db:
            return

        if not messagebox.askyesno(
            "Limpeza do Banco",
            "🧹 LIMPEZA DO BANCO DE DADOS (SWEEP)\n\n"
            "Esta operação irá:\n"
            "• Limpar registros antigos\n"
            "• Remover transações obsoletas\n"
            "• Otimizar o espaço do banco\n\n"
            "Deseja continuar?",
            icon=messagebox.QUESTION
        ):
            return

        # Muda para aba Principal antes de começar
        self.switch_to_main_tab()

        cmd = [
            gfix, "-sweep",
            db,
            "-user", self.conf.get("firebird_user", "SYSDBA"),
            "-pass", self.conf.get("firebird_password", "masterkey")
        ]

        self.log(f"🧹 Iniciando limpeza do banco: {db}", "info")
        self.set_status("Executando limpeza do banco...", "blue")

        def after_sweep():
            self.log("✅ Limpeza do banco concluída com sucesso!", "success")
            messagebox.showinfo(
                "Limpeza Concluída",
                "✅ Limpeza do banco concluída com sucesso!\n\n"
                "Registros antigos foram removidos e o banco foi otimizado."
            )

        self.run_command(cmd, on_finish=after_sweep)

    # ---------- RECALCULAR ÍNDICES ----------
    def recalculate_indexes(self):
        """Recalcula todos os índices do banco de dados usando ISQL"""
        if not self.check_permission("recalculate_indexes"):
            return
            
        isql = self.conf.get("isql_path") or find_executable("isql.exe")
        if not isql:
            messagebox.showerror("Erro", "isql.exe não encontrado. Configure o caminho do Firebird nas configurações.")
            return
        
        self.conf["isql_path"] = isql
        save_config(self.conf)

        db = filedialog.askopenfilename(
            title="Selecione o banco de dados para recalcular índices",
            filetypes=[("Firebird Database", "*.fdb"), ("Todos os arquivos", "*.*")]
        )
        if not db:
            return

        if not messagebox.askyesno(
            "Recalcular Índices",
            "📊 RECALCULAR ÍNDICES DO BANCO DE DADOS\n\n"
            "Esta operação irá:\n"
            "• Recalcular estatísticas de todos os índices\n"
            "• Otimizar o desempenho das consultas\n"
            "• Melhorar a performance do banco\n\n"
            "⚠️ A operação pode demorar dependendo do tamanho do banco.\n\n"
            "Deseja continuar?",
            icon=messagebox.QUESTION
        ):
            return
        
        # Muda para aba Principal antes de começar
        self.switch_to_main_tab()

        # Cria pasta temporária
        db_path = Path(db)
        temp_dir = db_path.parent / f"temp_index_recalc_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        try:
            # Cria o diretório temporário
            temp_dir.mkdir(exist_ok=True)
            
            # Cria arquivo SQL temporário
            temp_sql_file = temp_dir / f"recalc_indexes.sql"
            
            # Script SQL
            sql_script = """
    -- Método para recálculo dos índices
    -- Força recálculo de estatísticas limpando os valores existentes
    UPDATE RDB$INDICES 
    SET RDB$STATISTICS = NULL 
    WHERE RDB$SYSTEM_FLAG = 0 
    AND RDB$INDEX_NAME NOT STARTING WITH 'RDB$';

    COMMIT;

    SELECT 'Estatísticas de índices resetadas. O Firebird irá recalculá-las automaticamente.' as RESULTADO 
    FROM RDB$DATABASE;
    """
            
            # Salva o script SQL no arquivo temporário
            with open(temp_sql_file, 'w', encoding='utf-8') as f:
                f.write(sql_script)
            
            self.log(f"📊 Iniciando recálculo de índices: {db_path.name}", "info")
            self.log(f"📁 Pasta temporária criada: {temp_dir}", "info")
            self.log(f"🔌 Conectando em: {self._get_connection_string()}", "info")
            self.set_status("Recalculando índices, aguarde...", "blue")
            
            # Comando ISQL para executar o script SQL
            cmd = [
                isql,
                self._get_connection_string() + ":" + db,
                "-user", self.conf.get("firebird_user", "SYSDBA"),
                "-pass", self.conf.get("firebird_password", "masterkey"),
                "-i", str(temp_sql_file)
            ]
            
            def cleanup_temp_files():
                """Limpa arquivos temporários"""
                try:
                    if temp_dir.exists():
                        for file in temp_dir.glob("*"):
                            try:
                                file.unlink()
                            except Exception as e:
                                self.log(f"⚠️ Não foi possível remover {file}: {e}", "warning")
                        
                        temp_dir.rmdir()
                        self.log(f"🗑️ Pasta temporária removida: {temp_dir}", "info")
                except Exception as e:
                    self.log(f"⚠️ Erro ao limpar pasta temporária: {e}", "warning")
            
            def after_recalc():
                """Callback após recálculo"""
                cleanup_temp_files()
                self.log("✅ Recálculo de índices concluído com sucesso!", "success")
                messagebox.showinfo(
                    "Recálculo Concluído",
                    "✅ Recálculo de índices concluído com sucesso!\n\n"
                    "As estatísticas dos índices foram atualizadas.\n"
                    "O desempenho das consultas deve melhorar significativamente."
                )
            
            self.run_command(cmd, on_finish=after_recalc)
            
        except Exception as e:
            self.log(f"❌ Erro ao criar script de recálculo: {e}", "error")
            # Tenta limpar mesmo em caso de erro
            try:
                if temp_dir.exists():
                    for file in temp_dir.glob("*"):
                        try:
                            file.unlink()
                        except:
                            pass
                    temp_dir.rmdir()
            except:
                pass

    # ---------- GERENCIAMENTO DE PROCESSOS ----------
    def refresh_monitor(self):
        """Atualiza informações"""
        try:
            # Atualiza status do servidor
            self._update_server_status()
            
            # Atualiza espaço em disco
            self._update_disk_space()
            
            # Atualiza lista de processos
            self._refresh_all_processes()
            
        except Exception as e:
            self.log(f"❌ Erro ao atualizar monitor: {e}", "error")

    def _update_server_status(self):
        """Atualiza status do servidor Firebird"""
        try:
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

    def _kill_selected_processes(self):
        """Finaliza processos selecionados"""
        if not self.check_permission("kill_processes"):
            return
            
        selection = self.all_processes_tree.selection()
        if not selection:
            messagebox.showwarning("Aviso", "Selecione pelo menos um processo para finalizar.")
            return
        
        selected_count = len(selection)
        if not messagebox.askyesno(
            "Confirmação",
            f"🚨 ATENÇÃO 🚨\n\n"
            f"Você está prestes a finalizar {selected_count} processo(s).\n\n"
            f"Esta operação pode causar:\n"
            f"• Perda de dados não salvos\n"
            f"• Instabilidade do sistema\n"
            f"• Falha em aplicativos\n\n"
            f"Tem certeza que deseja continuar?",
            icon=messagebox.WARNING
        ):
            return
        
        killed_count = 0
        failed_count = 0
        failed_list = []
        
        for item in selection:
            values = self.all_processes_tree.item(item, "values")
            pid = int(values[0])
            proc_name = values[1]
            
            try:
                process = psutil.Process(pid)
                
                try:
                    process.terminate()
                    process.wait(timeout=3)
                    killed_count += 1
                    self.log(f"✅ Processo finalizado: {proc_name} (PID: {pid})", "success")
                    
                except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                    try:
                        process.kill()
                        process.wait(timeout=2)
                        killed_count += 1
                        self.log(f"✅ Processo forçado: {proc_name} (PID: {pid})", "warning")
                    except:
                        failed_count += 1
                        failed_list.append(f"{proc_name} (PID: {pid})")
                        self.log(f"❌ Falha ao finalizar: {proc_name} (PID: {pid})", "error")
                        
            except Exception as e:
                failed_count += 1
                failed_list.append(f"{proc_name} (PID: {pid})")
                self.log(f"❌ Erro ao finalizar {proc_name} (PID: {pid}): {e}", "error")
        
        result_msg = f"✅ {killed_count} processo(s) finalizado(s) com sucesso!"
        if failed_count > 0:
            result_msg += f"\n❌ {failed_count} processo(s) falharam:\n" + "\n".join(failed_list)
        
        messagebox.showinfo("Resultado", result_msg)
        
        self.after(1000, self._refresh_all_processes)
        
        self.log(f"🔚 Finalização concluída: {killed_count} sucesso(s), {failed_count} falha(s)", 
                "success" if failed_count == 0 else "warning")

    def _kill_by_pid(self):
        """Finaliza processo por PID específico"""
        if not self.check_permission("kill_processes"):
            return
            
        pid_dialog = tk.Toplevel(self)
        pid_dialog.title("Finalizar por PID")
        pid_dialog.geometry("300x170")
        pid_dialog.resizable(False, False)
        pid_dialog.transient(self)
        pid_dialog.grab_set()

        # Centraliza a janela
        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 150
        y = self.winfo_y() + (self.winfo_height() // 2) - 70
        pid_dialog.geometry(f"+{x}+{y}")

        # Configura o ícone
        icon_path = BASE_DIR / "images" / "icon.ico"
        if icon_path.exists():
            pid_dialog.iconbitmap(str(icon_path))

        # Frame principal
        main_frame = ttk.Frame(pid_dialog, padding=20)
        main_frame.pack(fill="both", expand=True)

        # Label
        ttk.Label(main_frame, text="Digite o PID do processo:",
                font=("Arial", 10)).pack(pady=(0, 10))

        # Entry para o PID
        pid_var = tk.StringVar()
        pid_entry = ttk.Entry(main_frame, textvariable=pid_var, width=15, font=("Arial", 12))
        pid_entry.pack(pady=5)
        pid_entry.focus()

        # Frame para botões
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(pady=15)

        def confirm_pid():
            """Confirma o PID digitado"""
            pid_str = pid_var.get().strip()
            if not pid_str:
                messagebox.showwarning("Aviso", "Digite um PID válido.")
                return

            if not pid_str.isdigit():
                messagebox.showwarning("Aviso", "O PID deve conter apenas números.")
                pid_entry.focus()
                return

            pid = int(pid_str)
            pid_dialog.destroy()
            self._execute_kill_by_pid(pid)

        def cancel_pid():
            pid_dialog.destroy()

        # Botões
        ttk.Button(btn_frame, text="✅ Confirmar",
                command=confirm_pid, cursor="hand2").pack(side="left", padx=5)
        ttk.Button(btn_frame, text="❌ Cancelar",
                command=cancel_pid, cursor="hand2").pack(side="left", padx=5)

        # Enter confirma, ESC cancela
        pid_entry.bind("<Return>", lambda e: confirm_pid())
        pid_dialog.bind("<Escape>", lambda e: cancel_pid())

    def _execute_kill_by_pid(self, pid):
        try:
            process = psutil.Process(pid)
            proc_name = process.name()

            if not messagebox.askyesno(
                "Confirmação",
                f"Finalizar processo?\n\n"
                f"PID: {pid}\n"
                f"Nome: {proc_name}\n\n"
                f"Tem certeza?",
                icon=messagebox.WARNING
            ):
                return

            try:
                process.terminate()
                process.wait(timeout=3)
                self.log(f"✅ Processo finalizado: {proc_name} (PID: {pid})", "success")
                messagebox.showinfo("Sucesso", f"Processo {proc_name} (PID: {pid}) finalizado!")
            except:
                try:
                    process.kill()
                    process.wait(timeout=2)
                    self.log(f"✅ Processo forçado: {proc_name} (PID: {pid})", "warning")
                    messagebox.showinfo("Sucesso", f"Processo {proc_name} (PID: {pid}) forçado!")
                except Exception as e:
                    self.log(f"❌ Falha ao finalizar {proc_name} (PID: {pid}): {e}", "error")
                    messagebox.showerror("Erro", f"Falha ao finalizar processo {pid}:\n{e}")

            # Atualiza lista
            self.after(1000, self._refresh_all_processes)

        except psutil.NoSuchProcess:
            messagebox.showerror("Erro", f"Processo com PID {pid} não encontrado.")
        except Exception as e:
            messagebox.showerror("Erro", f"Erro ao acessar processo {pid}:\n{e}")

    def auto_refresh_monitor(self):
        """Atualização automática"""
        if self.conf.get("auto_monitor", True):
            self.refresh_monitor()
            interval = int(self.conf.get("monitor_interval", 30)) * 1000
            self.after(interval, self.auto_refresh_monitor)

    # ---------- AGENDAMENTO ----------
    def load_schedules(self):
        """Carrega agendamentos salvos"""
        try:
            for item in self.schedules_tree.get_children():
                self.schedules_tree.delete(item)
            
            schedule.clear()
            
            scheduled_backups = self.conf.get("scheduled_backups", [])
            
            for schedule_data in scheduled_backups:
                # Formata horário
                time_str = f"{schedule_data['hour']:02d}:{schedule_data['minute']:02d}"
                
                next_run = self._calculate_next_run(schedule_data)
                
                self.schedules_tree.insert("", "end", values=(
                    schedule_data["name"],
                    Path(schedule_data["database"]).name,
                    schedule_data["frequency"],
                    time_str,
                    "Sim" if schedule_data.get("compress", True) else "Não",
                    next_run
                ))
                
                self._setup_schedule(schedule_data)
            
            status_text = f"✅ {len(scheduled_backups)} agendamento(s) carregado(s)"
            if scheduled_backups:
                status_text += " | Selecione um agendamento para editar ou excluir"
            self.schedule_status.config(text=status_text)
            
            self.log(f"📅 {len(scheduled_backups)} agendamentos carregados", "info")
            
        except Exception as e:
            error_msg = f"❌ Erro ao carregar agendamentos: {e}"
            self.schedule_status.config(text=error_msg)
            self.log(error_msg, "error")

    def _calculate_next_run(self, schedule_data):
        """Calcula a próxima execução do agendamento"""
        try:
            now = datetime.now()
            frequency = schedule_data["frequency"]
            hour = schedule_data["hour"]
            minute = schedule_data["minute"]
            
            if frequency == "Diário":
                next_run = datetime(now.year, now.month, now.day, hour, minute)
                if next_run <= now:
                    next_run += timedelta(days=1)
                    
            elif frequency == "Semanal":
                # Mapeia dias da semana
                weekday_map = {
                    "Segunda": 0, "Terça": 1, "Quarta": 2, "Quinta": 3,
                    "Sexta": 4, "Sábado": 5, "Domingo": 6
                }
                target_weekday = weekday_map.get(schedule_data.get("weekday", "Segunda"), 0)
                current_weekday = now.weekday()
                
                days_ahead = target_weekday - current_weekday
                if days_ahead <= 0:
                    days_ahead += 7
                    
                next_run = datetime(now.year, now.month, now.day, hour, minute) + timedelta(days=days_ahead)
                
            elif frequency == "Mensal":
                target_day = int(schedule_data.get("monthday", 1))
                try:
                    next_run = datetime(now.year, now.month, target_day, hour, minute)
                    if next_run <= now:
                        if now.month == 12:
                            next_run = datetime(now.year + 1, 1, target_day, hour, minute)
                        else:
                            next_run = datetime(now.year, now.month + 1, target_day, hour, minute)
                except ValueError:
                    # Dia inválido para o mês, usa último dia do mês
                    if now.month == 12:
                        next_month = datetime(now.year + 1, 1, 1)
                    else:
                        next_month = datetime(now.year, now.month + 1, 1)
                    last_day = (next_month - timedelta(days=1)).day
                    target_day = min(target_day, last_day)
                    next_run = datetime(now.year, now.month, target_day, hour, minute)
                    if next_run <= now:
                        next_run = datetime(next_month.year, next_month.month, target_day, hour, minute)
            
            return next_run.strftime("%d/%m/%Y %H:%M")
            
        except Exception:
            return "Calculando..."

    def _setup_schedule(self, schedule_data):
        """Configura o agendamento"""
        try:
            # Remove agendamentos existentes com o mesmo nome
            schedule.clear(schedule_data["name"])
            
            job = None
            time_str = f"{schedule_data['hour']:02d}:{schedule_data['minute']:02d}"
            
            if schedule_data["frequency"] == "Diário":
                job = schedule.every().day.at(time_str).do(
                    self.execute_scheduled_backup,
                    schedule_data["database"],
                    schedule_data["name"],
                    schedule_data["compress"]
                ).tag(schedule_data["name"])
            
            elif schedule_data["frequency"] == "Semanal":
                # Mapeia dias da semana
                weekday_map = {
                    "Segunda": schedule.every().monday,
                    "Terça": schedule.every().tuesday,
                    "Quarta": schedule.every().wednesday,
                    "Quinta": schedule.every().thursday,
                    "Sexta": schedule.every().friday,
                    "Sábado": schedule.every().saturday,
                    "Domingo": schedule.every().sunday
                }
                
                weekday = schedule_data.get("weekday", "Segunda")
                if weekday in weekday_map:
                    job = weekday_map[weekday].at(time_str).do(
                        self.execute_scheduled_backup,
                        schedule_data["database"],
                        schedule_data["name"],
                        schedule_data["compress"]
                    ).tag(schedule_data["name"])
            
            elif schedule_data["frequency"] == "Mensal":
                day = int(schedule_data.get("monthday", 1))
                job = schedule.every(30).days.at(time_str).do(
                    self.execute_scheduled_backup,
                    schedule_data["database"],
                    schedule_data["name"],
                    schedule_data["compress"]
                ).tag(schedule_data["name"])
            
            if job:
                self.log(f"🕒 Agendamento configurado: {schedule_data['name']} - {schedule_data['frequency']} às {time_str}", "info")
                
        except Exception as e:
            self.log(f"❌ Erro ao configurar agendamento '{schedule_data['name']}': {e}", "error")

    def edit_schedule(self):
        """Edita agendamento selecionado"""
        if not self.check_permission("manage_schedules"):
            return
            
        selection = self.schedules_tree.selection()
        if not selection:
            messagebox.showwarning("Aviso", "Selecione um agendamento para editar.")
            return
        
        if len(selection) > 1:
            messagebox.showwarning("Aviso", "Selecione apenas um agendamento para editar.")
            return
        
        item = selection[0]
        values = self.schedules_tree.item(item, "values")
        schedule_name = values[0]
        
        schedule_data = None
        for sched in self.conf.get("scheduled_backups", []):
            if sched["name"] == schedule_name:
                schedule_data = sched
                break
        
        if not schedule_data:
            messagebox.showerror("Erro", "Agendamento não encontrado na configuração.")
            return
        
        # Cria janela de edição
        edit_win = tk.Toplevel(self)
        edit_win.title("Editar Agendamento")
        edit_win.geometry("500x550")
        edit_win.resizable(False, False)
        edit_win.transient(self)
        edit_win.grab_set()
        
        # Centraliza
        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 250
        y = self.winfo_y() + (self.winfo_height() // 2) - 225
        edit_win.geometry(f"+{x}+{y}")
        
        # Ícone
        icon_path = BASE_DIR / "images" / "icon.ico"
        if icon_path.exists():
            edit_win.iconbitmap(str(icon_path))
        
        # Frame principal
        main_frame = ttk.Frame(edit_win, padding=20)
        main_frame.pack(fill="both", expand=True)
        
        ttk.Label(main_frame, text="Editar Agendamento", font=("Arial", 14, "bold")).pack(pady=(0, 20))
        
        # Campos de edição
        ttk.Label(main_frame, text="Nome do agendamento:*", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
        edit_name_var = tk.StringVar(value=schedule_data["name"])
        edit_name_entry = ttk.Entry(main_frame, textvariable=edit_name_var, width=40, font=("Arial", 10))
        edit_name_entry.pack(fill="x", pady=(0, 10))
        edit_name_entry.focus()
        
        ttk.Label(main_frame, text="Banco de dados:*", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
        edit_db_var = tk.StringVar(value=schedule_data["database"])
        db_frame = ttk.Frame(main_frame)
        db_frame.pack(fill="x", pady=(0, 10))
        edit_db_entry = ttk.Entry(db_frame, textvariable=edit_db_var, width=35, font=("Arial", 10))
        edit_db_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(db_frame, text="📁", width=3, 
                command=lambda: self._pick_schedule_db(edit_db_var)).pack(side="left", padx=5)
        
        ttk.Label(main_frame, text="Frequência:*", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
        edit_freq_var = tk.StringVar(value=schedule_data["frequency"])
        freq_combo = ttk.Combobox(main_frame, textvariable=edit_freq_var, 
                                values=["Diário", "Semanal", "Mensal"], 
                                state="readonly", width=20, font=("Arial", 10))
        freq_combo.pack(fill="x", pady=(0, 10))
        
        # Frame para opções específicas da frequência
        edit_freq_options_frame = ttk.Frame(main_frame)
        edit_freq_options_frame.pack(fill="x", pady=(0, 10))
        
        # Horário
        ttk.Label(main_frame, text="Horário (HH:MM):*", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
        
        # Frame para o campo de horário
        time_frame = ttk.Frame(main_frame)
        time_frame.pack(anchor="w", pady=(0, 10))
        
        # Função de validação
        def validate_time_input(new_value):
            if new_value == "":
                return True
            if len(new_value) > 2:
                return False
            return new_value.isdigit()
        
        vcmd = (self.register(validate_time_input), "%P")
        
        # Campo de horas
        hour_var = tk.StringVar(value=f"{schedule_data['hour']:02d}")
        hour_entry = ttk.Entry(
            time_frame,
            textvariable=hour_var,
            width=3,
            font=("Arial", 10),
            justify="center",
            validate="key",
            validatecommand=vcmd
        )
        hour_entry.pack(side="left")
        
        # Separador
        ttk.Label(time_frame, text=":", font=("Arial", 10, "bold")).pack(side="left", padx=2)
        
        # Campo de minutos
        minute_var = tk.StringVar(value=f"{schedule_data['minute']:02d}")
        minute_entry = ttk.Entry(
            time_frame,
            textvariable=minute_var,
            width=3,
            font=("Arial", 10),
            justify="center",
            validate="key",
            validatecommand=vcmd
        )
        minute_entry.pack(side="left")
        
        # Tooltip
        ttk.Label(
            main_frame,
            text="Formato: HH:MM (24 horas). Ex: 14:30, 02:00, 23:45",
            foreground="gray",
            font=("Arial", 8)
        ).pack(anchor="w", pady=(0, 10))
        
        # Compactar backup
        compress_frame = ttk.Frame(main_frame)
        compress_frame.pack(fill="x", pady=10)
        edit_compress_var = tk.BooleanVar(value=schedule_data.get("compress", True))
        ttk.Checkbutton(
            compress_frame,
            variable=edit_compress_var,
            text="Compactar backup após gerar (recomendado)"
        ).pack(anchor="w")
        
        # Botões
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill="x", pady=20)
        
        def save_edit():
            """Salva as alterações do agendamento"""
            if not all([edit_name_var.get(), edit_db_var.get()]):
                messagebox.showerror("Erro", "Preencha todos os campos obrigatórios.")
                return
            
            hour_str = hour_var.get().strip()
            minute_str = minute_var.get().strip()
            
            if not hour_str or not minute_str:
                messagebox.showerror("Erro", "Preencha horas e minutos.")
                hour_entry.focus()
                return
                
            if not hour_str.isdigit() or not minute_str.isdigit():
                messagebox.showerror("Erro", "Horas e minutos devem conter apenas números.")
                hour_entry.focus()
                return
                
            if len(hour_str) > 2 or len(minute_str) > 2:
                messagebox.showerror("Erro", "Horas e minutos devem ter no máximo 2 dígitos.")
                hour_entry.focus()
                return
                
            try:
                hours_int = int(hour_str)
                minutes_int = int(minute_str)
                
                if not (0 <= hours_int <= 23):
                    raise ValueError("Hora deve estar entre 00 e 23")
                if not (0 <= minutes_int <= 59):
                    raise ValueError("Minutos devem estar entre 00 e 59")
                    
            except ValueError as e:
                messagebox.showerror("Erro", f"Horário inválido: {e}")
                hour_entry.focus()
                return
            
            hour_final = f"{hours_int:02d}"
            minute_final = f"{minutes_int:02d}"
            
            frequency = edit_freq_var.get()
            
            schedule_data.update({
                "name": edit_name_var.get().strip(),
                "database": edit_db_var.get().strip(),
                "frequency": frequency,
                "hour": int(hour_final),
                "minute": int(minute_final),
                "compress": edit_compress_var.get()
            })
            
            if frequency == "Semanal":
                if hasattr(self, 'sched_weekday_var'):
                    schedule_data["weekday"] = self.sched_weekday_var.get()
                else:
                    messagebox.showerror("Erro", "Selecione um dia da semana para o agendamento semanal.")
                    return
            elif frequency == "Mensal":
                if hasattr(self, 'sched_monthday_var'):
                    schedule_data["monthday"] = self.sched_monthday_var.get()
                else:
                    messagebox.showerror("Erro", "Selecione um dia do mês para o agendamento mensal.")
                    return
            
            save_config(self.conf)
            self.load_schedules()
            
            self.log(f"✏️ Agendamento editado: {schedule_data['name']}", "success")
            messagebox.showinfo("Sucesso", f"Agendamento '{schedule_data['name']}' editado com sucesso!")
            edit_win.destroy()
        
        def cancel_edit():
            edit_win.destroy()
        
        ttk.Button(btn_frame, text="💾 Salvar Alterações", 
                command=save_edit, cursor="hand2").pack(side="left", padx=5)
        ttk.Button(btn_frame, text="❌ Cancelar", 
                command=cancel_edit, cursor="hand2").pack(side="right", padx=5)
        
        self._update_edit_schedule_freq_options(edit_freq_options_frame, edit_freq_var.get(), schedule_data)
        
        freq_combo.bind(
            '<<ComboboxSelected>>',
            lambda e: self._update_edit_schedule_freq_options(edit_freq_options_frame, edit_freq_var.get(), schedule_data)
        )

    def _update_edit_schedule_freq_options(self, options_frame, frequency, schedule_data):
        """Atualiza opções de frequência na janela de edição"""
        for widget in options_frame.winfo_children():
            widget.destroy()
        
        if frequency == "Diário":
            ttk.Label(options_frame, text="O backup será executado diariamente no horário selecionado.",
                     foreground="gray", font=("Arial", 9)).pack(anchor="w")
            
        elif frequency == "Semanal":
            ttk.Label(options_frame, text="Dia da semana:*", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
            self.sched_weekday_var = tk.StringVar(value=schedule_data.get("weekday", "Segunda"))
            weekday_combo = ttk.Combobox(options_frame, textvariable=self.sched_weekday_var,
                                       values=["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"],
                                       state="readonly", width=15, font=("Arial", 10))
            weekday_combo.pack(anchor="w", pady=(0, 5))
            
        elif frequency == "Mensal":
            ttk.Label(options_frame, text="Dia do mês:*", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
            self.sched_monthday_var = tk.StringVar(value=schedule_data.get("monthday", "1"))
            monthday_combo = ttk.Combobox(options_frame, textvariable=self.sched_monthday_var,
                                        values=[str(i) for i in range(1, 32)], state="readonly", width=5, font=("Arial", 10))
            monthday_combo.pack(anchor="w", pady=(0, 5))
            ttk.Label(options_frame, text="(1-31)", foreground="gray", font=("Arial", 9)).pack(anchor="w")

    def remove_schedule(self):
        """Remove agendamento selecionado"""
        if not self.check_permission("manage_schedules"):
            return
            
        selection = self.schedules_tree.selection()
        if not selection:
            messagebox.showwarning("Aviso", "Selecione um agendamento para remover.")
            return
        
        selected_names = [self.schedules_tree.item(item, "values")[0] for item in selection]
        names_text = "\n".join([f"• {name}" for name in selected_names])
        
        if not messagebox.askyesno(
            "Confirmar Exclusão",
            f"🚨 TEM CERTEZA QUE DESEJA EXCLUIR O(S) AGENDAMENTO(S)?\n\n"
            f"Agendamentos selecionados:\n{names_text}\n\n"
            f"Esta ação não pode ser desfeita!",
            icon=messagebox.WARNING
        ):
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
        
        messagebox.showinfo("Sucesso", f"{len(selection)} agendamento(s) removido(s) com sucesso!")

    # ---------- FERRAMENTAS AVANÇADAS ----------
    def optimize_database(self):
        """Executa operações de otimização no banco"""
        if not self.check_permission("optimize"):
            return
            
        gfix = self.conf.get("gfix_path") or find_executable("gfix.exe")
        if not gfix:
            messagebox.showerror("Erro", "gfix.exe não encontrado. Configure o caminho do Firebird nas configurações.")
            return
        
        db = filedialog.askopenfilename(title="Selecione o banco para otimizar")
        if not db:
            return
        
        self.log("🔧 Iniciando otimização do banco...", "info")

        # Muda para aba Principal antes de começar
        self.switch_to_main_tab()
        
        # Comandos de otimização
        commands = [
            [gfix, "-sweep", db, "-user", self.conf["firebird_user"], "-pass", self.conf["firebird_password"]],
            [gfix, "-validate", "-full", db, "-user", self.conf["firebird_user"], "-pass", self.conf["firebird_password"]],
        ]
        
        def run_next_command(index=0):
            if index < len(commands):
                self.run_command(commands[index], lambda: run_next_command(index + 1))
            else:
                self.log("✅ Otimização concluída com sucesso!", "success")
                messagebox.showinfo(
                    "Otimização Concluída",
                    "✅ Otimização do banco concluída!\n\n"
                    "Foram executadas as seguintes operações:\n"
                    "• Limpeza de registros antigos (sweep)\n"
                    "• Validação completa do banco"
                )
    
        run_next_command()

    def migrate_database(self):
        """Migra banco entre versões do Firebird"""
        if not self.check_permission("migrate"):
            return
            
        messagebox.showinfo(
            "Migração de Banco de Dados",
            "🔄 MIGRAÇÃO DE BANCO DE DADOS FIREBIRD\n\n"
            "A migração entre versões do Firebird é feita através do processo de Backup & Restore.\n\n"
            "📋 COMO FUNCIONA:\n"
            "1. Um backup completo do banco atual é gerado\n"
            "2. O backup é restaurado criando um novo banco\n"
            "3. O novo banco estará na versão do Firebird configurado\n\n"
            "⚙️ CONFIGURAÇÃO NECESSÁRIA:\n"
            "• O Firebird selecionado nas configurações deve ser da versão DESEJADA\n"
            "• Certifique-se de que o caminho do Firebird nas configurações aponta para a versão correta\n"
            "• O processo preserva todos os dados e estrutura do banco\n\n"
            "⚠️ IMPORTANTE:\n"
            "• Faça um backup manual antes de migrar\n"
            "• Teste o banco migrado em ambiente de desenvolvimento\n"
            "• Consulte a documentação do Firebird para compatibilidade entre versões"
        )
        
        if not messagebox.askyesno(
            "Continuar com Migração",
            "Deseja prosseguir com o processo de migração?\n\n"
            "Será executado um backup completo seguido de restauração\n"
            "usando o Firebird atualmente configurado nas configurações."
        ):
            return
        
        gbak = self.conf.get("gbak_path") or find_executable("gbak.exe")
        if not gbak:
            messagebox.showerror("Erro", "gbak.exe não encontrado. Configure o caminho do Firebird nas configurações.")
            return
        
        source_db = filedialog.askopenfilename(
            title="Selecione o banco para migrar",
            filetypes=[("Firebird Database", "*.fdb"), ("Todos os arquivos", "*.*")]
        )
        if not source_db:
            return
        
        # Confirmação final
        if not messagebox.askyesno(
            "Confirmar Migração",
            f"🚨 CONFIRMAÇÃO DE MIGRAÇÃO 🚨\n\n"
            f"Banco selecionado: {Path(source_db).name}\n\n"
            f"O banco será migrado para a versão do Firebird configurado nas configurações.\n"
            f"Esta operação criará uma cópia do banco na nova versão.\n\n"
            f"✅ Continuar com a migração?"
        ):
            return

        # Muda para aba Principal antes de começar
        self.switch_to_main_tab()
        
        backup_dir = Path(self.conf.get("backup_dir", DEFAULT_BACKUP_DIR))
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_file = backup_dir / f"migration_backup_{timestamp}.fbk"
        migrated_file = backup_dir / f"migrated_{Path(source_db).name}"
        
        self.log(f"🔄 Iniciando processo de migração...", "info")
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
            "-user", self.conf["firebird_user"], "-pass", self.conf["firebird_password"],
            "-page_size", self.conf.get("page_size", "8192")
        ]
        
        def after_backup():
            self.log("✅ Backup para migração concluído", "success")
            self.run_command(restore_cmd, after_restore)
        
        def after_restore():
            self.log(f"✅ Migração concluída: {migrated_file}", "success")
            try:
                backup_file.unlink()
                self.log("🗑️ Arquivo de backup temporário removido", "info")
            except Exception as e:
                self.log(f"⚠️ Não foi possível remover arquivo temporário: {e}", "warning")
            
            messagebox.showinfo(
                "Migração Concluída",
                f"✅ MIGRAÇÃO CONCLUÍDA COM SUCESSO!\n\n"
                f"Banco migrado salvo como:\n{migrated_file}\n\n"
                f"O banco está pronto para uso na nova versão."
            )
        
        self.run_command(backup_cmd, after_backup)

    # ---------- RELATÓRIOS ----------
    def generate_gstat_report(self):
        """Gera relatório detalhado do banco"""
        if not self.check_permission("generate_reports"):
            return
            
        gstat = self.conf.get("gstat_path") or find_executable("gstat.exe")
        if not gstat:
            messagebox.showerror("Erro", "gstat.exe não encontrado. Configure o caminho do Firebird nas configurações.")
            return
        
        self.conf["gstat_path"] = gstat
        save_config(self.conf)

        db = filedialog.askopenfilename(
            title="Selecione o banco para análise",
            filetypes=[("Firebird Database", "*.fdb"), ("Todos os arquivos", "*.*")]
        )
        if not db:
            return

        # Cria pasta de relatórios se não existir
        REPORTS_DIR.mkdir(exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        db_name = Path(db).stem
        report_filename = f"relatorio_gstat_{db_name}_{timestamp}.txt"
        report_path = REPORTS_DIR / report_filename

        self.log(f"📈 Iniciando análise do banco com gstat: {db}", "info")
        self.set_status("Gerando relatório do banco...", "blue")

        # Comando gstat
        cmd = [
            gstat, "-h",
            db,
            "-user", self.conf.get("firebird_user", "SYSDBA"),
            "-pass", self.conf.get("firebird_password", "masterkey")
        ]

        def run_gstat_with_output():
            try:
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

                process.stdout.close()
                return_code = process.wait()

                # Salva o relatório em arquivo
                with open(report_path, 'w', encoding='utf-8') as f:
                    f.write(f"Relatório GSTAT - {db_name}\n")
                    f.write(f"Gerado em: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n")
                    f.write("=" * 50 + "\n\n")
                    f.write("\n".join(output_lines))

                report_lines = []
                report_lines.append(f"📈 RELATÓRIO GSTAT - {db_name}")
                report_lines.append("=" * 50)
                report_lines.append(f"Gerado em: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
                report_lines.append("")
                report_lines.extend(output_lines)

                if return_code == 0:
                    self.after(0, lambda: self.set_status("✅ Relatório gstat gerado", "green"))
                    self.after(0, lambda: self.log(f"✅ Relatório gstat salvo: {report_path}", "success"))
                    self.after(0, lambda: self._show_report_window("Relatório do Banco (GSTAT)", report_lines, report_path))
                else:
                    self.after(0, lambda: self.log(f"❌ Gstat retornou código de erro: {return_code}", "error"))

            except Exception as e:
                self.after(0, lambda: self.log(f"❌ Erro ao executar gstat: {e}", "error"))

        threading.Thread(target=run_gstat_with_output, daemon=True).start()

    def open_report_file(self, file_path):
        """Abre o arquivo de relatório no programa padrão do sistema"""
        try:
            if open_file_with_default_app(file_path):
                self.log(f"📂 Relatório aberto automaticamente: {file_path}", "success")
            else:
                self.log(f"⚠️ Não foi possível abrir o relatório automaticamente: {file_path}", "warning")
                messagebox.showwarning(
                    "Abrir Relatório", 
                    f"Não foi possível abrir o relatório automaticamente.\n\n"
                    f"Localização do arquivo:\n{file_path}"
                )
        except Exception as e:
            self.log(f"❌ Erro ao abrir relatório: {e}", "error")
            messagebox.showerror("Erro", f"Erro ao abrir relatório:\n{e}")

    def generate_system_report(self):
        """Gera relatório detalhado do sistema"""
        if not self.check_permission("generate_reports"):
            return
            
        try:
            # Cria pasta de relatórios se não existir
            REPORTS_DIR.mkdir(exist_ok=True)
            
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            report_path = REPORTS_DIR / f"relatorio_sistema_{timestamp}.txt"
            
            report_lines = []
            report_lines.append("=" * 60)
            report_lines.append("RELATÓRIO DO SISTEMA GERENCIADOR FIREBIRD")
            report_lines.append(f"Gerado em: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
            report_lines.append("=" * 60)
            
            # Informações do sistema
            report_lines.append("\n📊 INFORMAÇÕES DO SISTEMA:")
            report_lines.append(f"- Diretório base: {BASE_DIR}")
            report_lines.append(f"- Diretório de backups: {self.conf.get('backup_dir', 'Não definido')}")
            report_lines.append(f"- Diretório de relatórios: {REPORTS_DIR}")
            
            # Configurações Firebird
            report_lines.append(f"\n🔥 CONFIGURAÇÕES FIREBIRD:")
            report_lines.append(f"- Host: {self.conf.get('firebird_host', 'localhost')}")
            report_lines.append(f"- Porta: {self.conf.get('firebird_port', '26350')}")
            report_lines.append(f"- Usuário: {self.conf.get('firebird_user', 'SYSDBA')}")
            report_lines.append(f"- PageSize: {self.conf.get('page_size', '8192')}")
            report_lines.append(f"- Gbak: {self.conf.get('gbak_path', 'Não configurado')}")
            report_lines.append(f"- Gfix: {self.conf.get('gfix_path', 'Não configurado')}")
            report_lines.append(f"- Gstat: {self.conf.get('gstat_path', 'Não configurado')}")
            report_lines.append(f"- Isql: {self.conf.get('isql_path', 'Não configurado')}")
            
            # Espaço em disco
            backup_dir = Path(self.conf.get("backup_dir", DEFAULT_BACKUP_DIR))
            disk_info = get_disk_space(backup_dir)
            if disk_info:
                report_lines.append(f"\n💾 ESPAÇO EM DISCO:")
                report_lines.append(f"- Total: {disk_info['total_gb']:.1f} GB")
                report_lines.append(f"- Livre: {disk_info['free_gb']:.1f} GB")
                report_lines.append(f"- Usado: {disk_info['percent_used']:.1f}%")
            
            # Processos Firebird
            fb_processes = self._get_firebird_processes()
            report_lines.append(f"\n🔥 PROCESSOS FIREBIRD: {len(fb_processes)} encontrados")
            for proc in fb_processes:
                report_lines.append(f"  - {proc['name']} (PID: {proc['pid']})")
            
            # Backups
            backup_files = list(Path(self.conf.get("backup_dir", DEFAULT_BACKUP_DIR)).glob("*.fbk")) + \
                          list(Path(self.conf.get("backup_dir", DEFAULT_BACKUP_DIR)).glob("*.zip"))
            report_lines.append(f"\n📦 BACKUPS: {len(backup_files)} arquivos")
            if backup_files:
                latest = max(backup_files, key=lambda f: f.stat().st_mtime)
                report_lines.append(f"- Último backup: {latest.name}")
                report_lines.append(f"  Gerado em: {datetime.fromtimestamp(latest.stat().st_mtime).strftime('%d/%m/%Y %H:%M')}")
            
            # Agendamentos
            scheduled_backups = self.conf.get("scheduled_backups", [])
            report_lines.append(f"\n🕒 AGENDAMENTOS: {len(scheduled_backups)} configurados")
            for sched in scheduled_backups:
                time_str = f"{sched['hour']:02d}:{sched['minute']:02d}"
                if sched["frequency"] == "Semanal":
                    report_lines.append(f"- {sched['name']}: {sched['frequency']} ({sched.get('weekday', 'Segunda')}) às {time_str}")
                elif sched["frequency"] == "Mensal":
                    report_lines.append(f"- {sched['name']}: {sched['frequency']} (dia {sched.get('monthday', '1')}) às {time_str}")
                else:
                    report_lines.append(f"- {sched['name']}: {sched['frequency']} às {time_str}")
            
            # Inicialização com Windows
            startup_status = "Sim" if self.conf.get("start_with_windows", False) else "Não"
            report_lines.append(f"\n🪟 INICIALIZAÇÃO COM WINDOWS: {startup_status}")
            
            # Salva relatório
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(report_lines))
            
            self.log(f"📊 Relatório do sistema gerado: {report_path}", "success")
            
            # Mostra relatório em janela personalizada
            self._show_report_window("Relatório do Sistema", report_lines, report_path)
            
        except Exception as e:
            self.log(f"❌ Erro ao gerar relatório: {e}", "error")
            messagebox.showerror("Erro", f"Falha ao gerar relatório:\n{e}")

    def _get_firebird_processes(self):
        """Retorna lista de processos do Firebird"""
        processes = []
        firebird_procs = ["fb_inet_server.exe", "fbserver.exe", "fbguard.exe", "firebird.exe", "ibserver.exe", "gbak.exe", "gfix.exe", "gstat.exe", "isql.exe"]
        
        for proc in psutil.process_iter(['pid', 'name']):
            if proc.info['name'] and any(fb in proc.info['name'].lower() for fb in [p.lower() for p in firebird_procs]):
                processes.append({
                    'pid': proc.info['pid'],
                    'name': proc.info['name']
                })
        
        return processes

    def check_disk_space(self):
        """Verifica e exibe o espaço em disco de todas as unidades disponíveis"""
        if not self.check_permission("generate_reports"):
            return
            
        try:
            partitions = psutil.disk_partitions(all=False)  # all=False para ignorar partições virtuais
            
            if not partitions:
                messagebox.showinfo("Espaço em Disco", "Nenhuma unidade de disco encontrada.")
                return
            
            report_lines = []
            report_lines.append("💾 RELATÓRIO DE ESPAÇO EM DISCO")
            report_lines.append("=" * 50)
            report_lines.append(f"Gerado em: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
            report_lines.append("")
            
            for partition in partitions:
                try:
                    if partition.fstype and partition.device:
                        usage = psutil.disk_usage(partition.mountpoint)
                        
                        total_gb = usage.total / (1024**3)
                        used_gb = usage.used / (1024**3)
                        free_gb = usage.free / (1024**3)
                        percent_used = (usage.used / usage.total) * 100
                        
                        if free_gb < 1:
                            status_icon = "🚨"
                            status_text = "CRÍTICO"
                        elif free_gb < 5:
                            status_icon = "⚠️"
                            status_text = "LIMITADO"
                        else:
                            status_icon = "✅"
                            status_text = "SUFICIENTE"
                        
                        report_lines.append(f"{status_icon} Unidade: {partition.device}")
                        report_lines.append(f"   Ponto de montagem: {partition.mountpoint}")
                        report_lines.append(f"   Sistema de arquivos: {partition.fstype}")
                        report_lines.append(f"   Total: {total_gb:.2f} GB")
                        report_lines.append(f"   Usado: {used_gb:.2f} GB ({percent_used:.1f}%)")
                        report_lines.append(f"   Livre: {free_gb:.2f} GB")
                        report_lines.append(f"   Status: {status_text}")
                        report_lines.append("")
                        
                except PermissionError:
                    report_lines.append(f"🚫 Unidade: {partition.device}")
                    report_lines.append(f"   Ponto de montagem: {partition.mountpoint}")
                    report_lines.append(f"   Sistema de arquivos: {partition.fstype}")
                    report_lines.append("   ❌ Acesso negado")
                    report_lines.append("")
                except Exception as e:
                    report_lines.append(f"❌ Unidade: {partition.device}")
                    report_lines.append(f"   Ponto de montagem: {partition.mountpoint}")
                    report_lines.append(f"   Sistema de arquivos: {partition.fstype}")
                    report_lines.append(f"   Erro: {str(e)}")
                    report_lines.append("")
            
            accessible_partitions = [p for p in partitions if not p.fstype in ['cdrom', ''] and not p.device.startswith('\\\\')]
            total_disks = len(accessible_partitions)
            
            report_lines.append("📊 RESUMO:")
            report_lines.append(f"Total de unidades acessíveis: {total_disks}")
            
            # Salva relatório em arquivo
            REPORTS_DIR.mkdir(exist_ok=True)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            report_path = REPORTS_DIR / f"relatorio_espaco_disco_{timestamp}.txt"
            
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(report_lines))
            
            self._show_report_window("Relatório de Espaço em Disco", report_lines, report_path)
            
            self.log("💾 Relatório de espaço em disco gerado com sucesso", "success")
            
        except Exception as e:
            error_msg = f"❌ Erro ao verificar espaço em disco: {e}"
            self.log(error_msg, "error")
            messagebox.showerror("Erro", error_msg)

    def _show_report_window(self, title, report_lines, report_path):
        """Mostra relatório em janela personalizada"""
        report_win = tk.Toplevel(self)
        report_win.title(title)
        report_win.geometry("700x600")
        report_win.minsize(600, 400)
        
        # Centraliza
        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 350
        y = self.winfo_y() + (self.winfo_height() // 2) - 300
        report_win.geometry(f"+{x}+{y}")
        
        # Ícone
        icon_path = BASE_DIR / "images" / "icon.ico"
        if icon_path.exists():
            report_win.iconbitmap(str(icon_path))
        
        report_win.transient(self)
        report_win.grab_set()
        
        # Frame principal
        main_frame = ttk.Frame(report_win, padding=15)
        main_frame.pack(fill="both", expand=True)
        
        # Título
        title_label = ttk.Label(
            main_frame, 
            text=title,
            font=("Arial", 14, "bold")
        )
        title_label.pack(pady=(0, 10))
        
        # Área de texto com scroll
        text_frame = ttk.Frame(main_frame)
        text_frame.pack(fill="both", expand=True, pady=10)
        
        text_area = scrolledtext.ScrolledText(
            text_frame, 
            wrap=tk.WORD,
            font=("Consolas", 9),
            height=20
        )
        text_area.pack(fill="both", expand=True)
        text_area.insert("1.0", "\n".join(report_lines))
        text_area.config(state="disabled")
        
        # Frame de botões
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill="x", pady=10)
        
        def open_report():
            """Abre o relatório no programa padrão"""
            try:
                if open_file_with_default_app(report_path):
                    self.log(f"📂 Relatório aberto automaticamente: {report_path}", "success")
                else:
                    messagebox.showwarning(
                        "Abrir Relatório", 
                        f"Não foi possível abrir o relatório automaticamente.\n\n"
                        f"Localização:\n{report_path}"
                    )
            except Exception as e:
                messagebox.showerror("Erro", f"Erro ao abrir relatório:\n{e}")
        
        def close_window():
            report_win.destroy()
        
        ttk.Button(
            btn_frame, 
            text="📂 Abrir Relatório",
            command=open_report,
            cursor="hand2"
        ).pack(side="left", padx=5)
        
        ttk.Button(
            btn_frame,
            text="✅ Fechar",
            command=close_window,
            cursor="hand2"
        ).pack(side="right", padx=5)
        
        # Foca na janela
        report_win.focus_force()

    def export_config(self):
        """Exporta configurações para arquivo"""
        if not self.check_permission("export_import"):
            return
            
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
        if not self.check_permission("export_import"):
            return
            
        config_file = filedialog.askopenfilename(
            filetypes=[("JSON files", "*.json"), ("Todos os arquivos", "*.*")]
        )
        if config_file:
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    new_conf = json.load(f)
                
                keep_keys = ['backup_dir', 'gbak_path', 'gfix_path', 'gstat_path', 'isql_path', 'firebird_host', 'firebird_port', 'page_size']
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

    # ---------- GERENCIAMENTO DE USUÁRIOS ----------
    def manage_users(self):
        """Janela de gerenciamento de usuários"""
        if not self.check_permission("manage_users"):
            return
        
        win = tk.Toplevel(self)
        win.title("Gerenciamento de Usuários")
        win.geometry("800x600")
        win.resizable(True, True)
        
        # Centraliza
        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 400
        y = self.winfo_y() + (self.winfo_height() // 2) - 300
        win.geometry(f"+{x}+{y}")
        
        # Ícone
        icon_path = BASE_DIR / "images" / "icon.ico"
        if icon_path.exists():
            win.iconbitmap(str(icon_path))
        
        win.transient(self)
        win.grab_set()
        
        # Frame principal
        main_frame = ttk.Frame(win, padding=15)
        main_frame.pack(fill="both", expand=True)
        
        # Título
        ttk.Label(
            main_frame,
            text="👥 Gerenciamento de Usuários",
            font=("Arial", 14, "bold")
        ).pack(pady=(0, 20))
        
        # Frame de controles
        controls_frame = ttk.Frame(main_frame)
        controls_frame.pack(fill="x", pady=(0, 15))
        
        ttk.Button(
            controls_frame,
            text="➕ Novo Usuário",
            command=lambda: self._create_user_dialog(win),
            cursor="hand2"
        ).pack(side="left", padx=(0, 10))
        
        ttk.Button(
            controls_frame,
            text="✏️ Editar Usuário",
            command=lambda: self._edit_user_dialog(win),
            cursor="hand2"
        ).pack(side="left", padx=(0, 10))
        
        ttk.Button(
            controls_frame,
            text="🗑️ Excluir Usuário",
            command=lambda: self._delete_user_dialog(win),
            cursor="hand2"
        ).pack(side="left", padx=(0, 10))
        
        ttk.Button(
            controls_frame,
            text="🔐 Alterar Minha Senha",
            command=self.change_own_password,
            cursor="hand2"
        ).pack(side="left", padx=(0, 10))
        
        ttk.Button(
            controls_frame,
            text="🔄 Atualizar",
            command=lambda: refresh_list(),
            cursor="hand2"
        ).pack(side="left")
        
        # Lista de usuários
        list_frame = ttk.LabelFrame(main_frame, text="Usuários do Sistema", padding=10)
        list_frame.pack(fill="both", expand=True)
        
        # Treeview para usuários
        columns = ("Usuário", "Nome", "Função", "E-mail", "Último Login", "Status")
        users_tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=15)
        
        # Configurar cabeçalhos
        for col in columns:
            users_tree.heading(col, text=col)
            users_tree.column(col, width=100)
        
        users_tree.column("Usuário", width=120)
        users_tree.column("Nome", width=150)
        users_tree.column("E-mail", width=150)
        users_tree.column("Último Login", width=120)
        users_tree.column("Status", width=80)
        
        # Scrollbars
        v_scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=users_tree.yview)
        h_scrollbar = ttk.Scrollbar(list_frame, orient="horizontal", command=users_tree.xview)
        users_tree.configure(yscrollcommand=v_scrollbar.set, xscrollcommand=h_scrollbar.set)
        
        users_tree.pack(side="left", fill="both", expand=True)
        v_scrollbar.pack(side="right", fill="y")
        h_scrollbar.pack(side="bottom", fill="x")
        
        def refresh_list():
            """Atualiza lista de usuários"""
            for item in users_tree.get_children():
                users_tree.delete(item)
            
            users = self.user_manager.get_users_list()
            for user in users:
                last_login = user['last_login']
                if last_login:
                    try:
                        last_login = datetime.fromisoformat(last_login).strftime("%d/%m/%Y %H:%M")
                    except:
                        last_login = "Nunca"
                else:
                    last_login = "Nunca"
                
                status = "Ativo" if user['active'] else "Inativo"
                
                users_tree.insert("", "end", values=(
                    user['username'],
                    user['full_name'],
                    USER_ROLES.get(user['role'], user['role']),
                    user['email'],
                    last_login,
                    status
                ))
        
        # Carrega lista inicial
        refresh_list()
        
        # Frame de status
        status_frame = ttk.Frame(main_frame)
        status_frame.pack(fill="x", pady=10)
        
        user_count = len(self.user_manager.get_users_list())
        active_count = len([u for u in self.user_manager.get_users_list() if u['active']])
        
        ttk.Label(
            status_frame,
            text=f"Total: {user_count} usuários | Ativos: {active_count}",
            font=("Arial", 9),
            foreground="gray"
        ).pack(side="left")
        
        ttk.Label(
            status_frame,
            text=f"Usuário atual: {self.current_user['full_name']} ({USER_ROLES.get(self.current_user['role'])})",
            font=("Arial", 9),
            foreground="blue"
        ).pack(side="right")
        
        # Botão fechar
        ttk.Button(
            main_frame,
            text="✅ Fechar",
            command=win.destroy,
            cursor="hand2"
        ).pack(pady=10)
        
        return win

    def _create_user_dialog(self, parent_win):
        """Dialog para criar novo usuário"""
        dialog = tk.Toplevel(parent_win)
        dialog.title("Novo Usuário")
        dialog.geometry("500x600")
        dialog.resizable(False, False)
        dialog.transient(parent_win)
        dialog.grab_set()

        # Ícone
        icon_path = BASE_DIR / "images" / "icon.ico"
        if icon_path.exists():
            dialog.iconbitmap(str(icon_path))
        
        # Centraliza
        parent_win.update_idletasks()
        x = parent_win.winfo_x() + (parent_win.winfo_width() // 2) - 250
        y = parent_win.winfo_y() + (parent_win.winfo_height() // 2) - 300
        dialog.geometry(f"+{x}+{y}")
        
        # Frame principal
        main_frame = ttk.Frame(dialog, padding=20)
        main_frame.pack(fill="both", expand=True)
        
        ttk.Label(main_frame, text="Novo Usuário", font=("Arial", 14, "bold")).pack(pady=(0, 20))
        
        # Campos do formulário
        fields = []
        
        ttk.Label(main_frame, text="Nome de usuário:*", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
        username_var = tk.StringVar()
        username_entry = ttk.Entry(main_frame, textvariable=username_var, width=30, font=("Arial", 10))
        username_entry.pack(fill="x", pady=(0, 15))
        fields.append(("username", username_entry))
        username_entry.focus()
        
        ttk.Label(main_frame, text="Senha:*", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
        password_var = tk.StringVar()
        password_entry = ttk.Entry(main_frame, textvariable=password_var, show="•", width=30, font=("Arial", 10))
        password_entry.pack(fill="x", pady=(0, 15))
        fields.append(("password", password_entry))
        
        ttk.Label(main_frame, text="Confirmar senha:*", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
        confirm_var = tk.StringVar()
        confirm_entry = ttk.Entry(main_frame, textvariable=confirm_var, show="•", width=30, font=("Arial", 10))
        confirm_entry.pack(fill="x", pady=(0, 15))
        fields.append(("confirm", confirm_entry))
        
        ttk.Label(main_frame, text="Nome completo:*", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
        full_name_var = tk.StringVar()
        full_name_entry = ttk.Entry(main_frame, textvariable=full_name_var, width=30, font=("Arial", 10))
        full_name_entry.pack(fill="x", pady=(0, 15))
        fields.append(("full_name", full_name_entry))
        
        ttk.Label(main_frame, text="E-mail:", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
        email_var = tk.StringVar()
        email_entry = ttk.Entry(main_frame, textvariable=email_var, width=30, font=("Arial", 10))
        email_entry.pack(fill="x", pady=(0, 15))
        fields.append(("email", email_entry))
        
        ttk.Label(main_frame, text="Função:*", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
        role_var = tk.StringVar(value="operator")
        role_combo = ttk.Combobox(main_frame, textvariable=role_var, 
                                values=list(USER_ROLES.keys()), 
                                state="readonly", width=20, font=("Arial", 10))
        role_combo.pack(fill="x", pady=(0, 15))
        
        # Status
        status_label = ttk.Label(main_frame, text="", foreground="red")
        status_label.pack(pady=5)
        
        def create_user():
            """Cria o novo usuário"""
            username = username_var.get().strip()
            password = password_var.get()
            confirm = confirm_var.get()
            full_name = full_name_var.get().strip()
            email = email_var.get().strip()
            role = role_var.get()
            
            # Validações
            if not username:
                status_label.config(text="Digite um nome de usuário")
                username_entry.focus()
                return
                
            if not password:
                status_label.config(text="Digite uma senha")
                password_entry.focus()
                return
                
            if password != confirm:
                status_label.config(text="As senhas não coincidem")
                password_entry.focus()
                return
                
            if not full_name:
                status_label.config(text="Digite o nome completo")
                full_name_entry.focus()
                return
            
            # Tenta criar o usuário
            if self.user_manager.create_user(username, password, role, full_name, email):
                status_label.config(text="✅ Usuário criado com sucesso!", foreground="green")
                dialog.after(2000, dialog.destroy)
                # Atualiza a lista na janela principal
                if hasattr(parent_win, 'refresh_list'):
                    parent_win.refresh_list()
            else:
                status_label.config(text="❌ Erro ao criar usuário. Nome já existe.")
        
        # Botões
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill="x", pady=20)
        
        ttk.Button(btn_frame, text="💾 Criar Usuário", 
                  command=create_user, cursor="hand2").pack(side="left", padx=5)
        
        ttk.Button(btn_frame, text="❌ Cancelar", 
                  command=dialog.destroy, cursor="hand2").pack(side="right", padx=5)
        
        # Enter para criar
        confirm_entry.bind("<Return>", lambda e: create_user())

    def _edit_user_dialog(self, parent_win):
        """Dialog para editar usuário"""
        selection = self._get_selected_user_from_tree(parent_win)
        if not selection:
            messagebox.showwarning("Aviso", "Selecione um usuário para editar.")
            return
        
        username = selection[0]
        
        # Não permite editar o próprio usuário
        if username == self.current_user['username']:
            messagebox.showwarning("Aviso", "Não é possível editar o próprio usuário. Use a opção 'Alterar Senha'.")
            return
        
        user_details = self.user_manager.get_user_details(username)
        if not user_details:
            messagebox.showerror("Erro", "Usuário não encontrado.")
            return
        
        dialog = tk.Toplevel(parent_win)
        dialog.title(f"Editar Usuário - {username}")
        dialog.geometry("500x600")
        dialog.resizable(False, False)
        dialog.transient(parent_win)
        dialog.grab_set()

        # Ícone
        icon_path = BASE_DIR / "images" / "icon.ico"
        if icon_path.exists():
            dialog.iconbitmap(str(icon_path))
        
        # Centraliza
        parent_win.update_idletasks()
        x = parent_win.winfo_x() + (parent_win.winfo_width() // 2) - 250
        y = parent_win.winfo_y() + (parent_win.winfo_height() // 2) - 300
        dialog.geometry(f"+{x}+{y}")
        
        # Frame principal
        main_frame = ttk.Frame(dialog, padding=20)
        main_frame.pack(fill="both", expand=True)
        
        ttk.Label(main_frame, text=f"Editar Usuário: {username}", font=("Arial", 14, "bold")).pack(pady=(0, 20))
        
        # Campos do formulário
        ttk.Label(main_frame, text="Nome completo:*", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
        full_name_var = tk.StringVar(value=user_details.get('full_name', ''))
        full_name_entry = ttk.Entry(main_frame, textvariable=full_name_var, width=30, font=("Arial", 10))
        full_name_entry.pack(fill="x", pady=(0, 15))
        full_name_entry.focus()
        
        ttk.Label(main_frame, text="E-mail:", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
        email_var = tk.StringVar(value=user_details.get('email', ''))
        email_entry = ttk.Entry(main_frame, textvariable=email_var, width=30, font=("Arial", 10))
        email_entry.pack(fill="x", pady=(0, 15))
        
        ttk.Label(main_frame, text="Função:*", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
        role_var = tk.StringVar(value=user_details.get('role', 'operator'))
        role_combo = ttk.Combobox(main_frame, textvariable=role_var, 
                                values=list(USER_ROLES.keys()), 
                                state="readonly", width=20, font=("Arial", 10))
        role_combo.pack(fill="x", pady=(0, 15))
        
        # Frame para status do usuário
        status_frame = ttk.Frame(main_frame)
        status_frame.pack(fill="x", pady=10)
        
        active_var = tk.BooleanVar(value=user_details.get('active', True))
        ttk.Checkbutton(status_frame, variable=active_var, 
                    text="Usuário ativo").pack(anchor="w")
        
        # Alteração de senha
        ttk.Label(main_frame, text="Alterar senha (deixe em branco para manter a atual):", 
                font=("Arial", 9, "bold")).pack(anchor="w", pady=(15, 2))
        
        ttk.Label(main_frame, text="Nova senha:", font=("Arial", 9)).pack(anchor="w", pady=(5, 2))
        new_password_var = tk.StringVar()
        new_password_entry = ttk.Entry(main_frame, textvariable=new_password_var, show="•", width=30, font=("Arial", 10))
        new_password_entry.pack(fill="x", pady=(0, 10))
        
        ttk.Label(main_frame, text="Confirmar nova senha:", font=("Arial", 9)).pack(anchor="w", pady=(5, 2))
        confirm_password_var = tk.StringVar()
        confirm_password_entry = ttk.Entry(main_frame, textvariable=confirm_password_var, show="•", width=30, font=("Arial", 10))
        confirm_password_entry.pack(fill="x", pady=(0, 15))
        
        # Status
        status_label = ttk.Label(main_frame, text="", foreground="red")
        status_label.pack(pady=5)
        
        def save_changes():
            """Salva as alterações do usuário"""
            full_name = full_name_var.get().strip()
            email = email_var.get().strip()
            role = role_var.get()
            active = active_var.get()
            new_password = new_password_var.get()
            confirm_password = confirm_password_var.get()
            
            # Validações
            if not full_name:
                status_label.config(text="Digite o nome completo")
                full_name_entry.focus()
                return
            
            if new_password and new_password != confirm_password:
                status_label.config(text="As senhas não coincidem")
                new_password_entry.focus()
                return
            
            # Prepara os dados para atualização
            update_data = {
                'full_name': full_name,
                'email': email,
                'role': role,
                'active': active
            }
            
            # Se foi informada uma nova senha, adiciona aos dados
            if new_password:
                update_data['password'] = new_password
            
            # Tenta atualizar o usuário
            if self.user_manager.update_user(username, **update_data):
                status_label.config(text="✅ Usuário atualizado com sucesso!", foreground="green")
                dialog.after(2000, dialog.destroy)
                # Atualiza a lista na janela principal
                if hasattr(parent_win, 'refresh_list'):
                    parent_win.refresh_list()
            else:
                status_label.config(text="❌ Erro ao atualizar usuário")
        
        # Botões
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill="x", pady=20)
        
        ttk.Button(btn_frame, text="💾 Salvar Alterações", 
                command=save_changes, cursor="hand2").pack(side="left", padx=5)
        
        ttk.Button(btn_frame, text="❌ Cancelar", 
                command=dialog.destroy, cursor="hand2").pack(side="right", padx=5)
        
        # Enter para salvar
        confirm_password_entry.bind("<Return>", lambda e: save_changes())

    def _delete_user_dialog(self, parent_win):
        """Dialog para excluir usuário"""
        selection = self._get_selected_user_from_tree(parent_win)
        if not selection:
            messagebox.showwarning("Aviso", "Selecione um usuário para excluir.")
            return
        
        username = selection[0]
        user_details = self.user_manager.get_user_details(username)
        
        if not user_details:
            messagebox.showerror("Erro", "Usuário não encontrado.")
            return
        
        # Não permite excluir o próprio usuário
        if username == self.current_user['username']:
            messagebox.showwarning("Aviso", "Não é possível excluir o próprio usuário.")
            return
        
        # Verifica se é o último admin
        admin_count = sum(1 for u in self.user_manager.get_users_list() 
                        if u['role'] == 'admin' and u['active'])
        if user_details['role'] == 'admin' and admin_count <= 1:
            messagebox.showwarning("Aviso", 
                                "Não é possível excluir o último administrador ativo.\n"
                                "Promova outro usuário para administrador primeiro.")
            return
        
        # Confirmação de exclusão
        confirm = messagebox.askyesno(
            "Confirmar Exclusão",
            f"🚨 TEM CERTEZA QUE DESEJA EXCLUIR O USUÁRIO?\n\n"
            f"Usuário: {username}\n"
            f"Nome: {user_details.get('full_name', 'N/A')}\n"
            f"Função: {USER_ROLES.get(user_details.get('role'), user_details.get('role'))}\n\n"
            f"Esta ação não pode ser desfeita!",
            icon=messagebox.WARNING
        )
        
        if confirm:
            if self.user_manager.delete_user(username):
                messagebox.showinfo("Sucesso", f"Usuário '{username}' excluído com sucesso!")
                # Atualiza a lista na janela principal
                if hasattr(parent_win, 'refresh_list'):
                    parent_win.refresh_list()
            else:
                messagebox.showerror("Erro", f"Erro ao excluir usuário '{username}'")

    def _get_selected_user_from_tree(self, parent_win):
        """Obtém o usuário selecionado na treeview da janela de gerenciamento"""
        for widget in parent_win.winfo_children():
            if isinstance(widget, ttk.Frame):
                for child in widget.winfo_children():
                    if isinstance(child, ttk.LabelFrame):
                        for tree_child in child.winfo_children():
                            if isinstance(tree_child, ttk.Treeview):
                                selection = tree_child.selection()
                                if selection:
                                    values = tree_child.item(selection[0], "values")
                                    return values
        return None

    def change_own_password(self):
        """Permite ao usuário atual alterar sua própria senha"""
        dialog = tk.Toplevel(self)
        dialog.title("Alterar Minha Senha")
        dialog.geometry("400x400")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        # Ícone
        icon_path = BASE_DIR / "images" / "icon.ico"
        if icon_path.exists():
            dialog.iconbitmap(str(icon_path))
        
        # Centraliza
        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 200
        y = self.winfo_y() + (self.winfo_height() // 2) - 200
        dialog.geometry(f"+{x}+{y}")
        
        # Frame principal
        main_frame = ttk.Frame(dialog, padding=20)
        main_frame.pack(fill="both", expand=True)
        
        ttk.Label(main_frame, text="Alterar Minha Senha", font=("Arial", 14, "bold")).pack(pady=(0, 20))
        
        ttk.Label(main_frame, text=f"Usuário: {self.current_user['username']}", 
                font=("Arial", 10)).pack(anchor="w", pady=(0, 10))
        
        # Campos de senha
        ttk.Label(main_frame, text="Senha atual:*", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
        current_password_var = tk.StringVar()
        current_password_entry = ttk.Entry(main_frame, textvariable=current_password_var, show="•", width=30, font=("Arial", 10))
        current_password_entry.pack(fill="x", pady=(0, 15))
        current_password_entry.focus()
        
        ttk.Label(main_frame, text="Nova senha:*", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
        new_password_var = tk.StringVar()
        new_password_entry = ttk.Entry(main_frame, textvariable=new_password_var, show="•", width=30, font=("Arial", 10))
        new_password_entry.pack(fill="x", pady=(0, 10))
        
        ttk.Label(main_frame, text="Confirmar nova senha:*", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 2))
        confirm_password_var = tk.StringVar()
        confirm_password_entry = ttk.Entry(main_frame, textvariable=confirm_password_var, show="•", width=30, font=("Arial", 10))
        confirm_password_entry.pack(fill="x", pady=(0, 15))
        
        # Status
        status_label = ttk.Label(main_frame, text="", foreground="red")
        status_label.pack(pady=5)
        
        def save_password():
            """Salva a nova senha"""
            current_password = current_password_var.get()
            new_password = new_password_var.get()
            confirm_password = confirm_password_var.get()
            
            # Validações
            if not current_password:
                status_label.config(text="Digite a senha atual")
                current_password_entry.focus()
                return
            
            if not new_password:
                status_label.config(text="Digite a nova senha")
                new_password_entry.focus()
                return
            
            if new_password != confirm_password:
                status_label.config(text="As novas senhas não coincidem")
                new_password_entry.focus()
                return
            
            # Verifica se a senha atual está correta
            if not self.user_manager.verify_password(current_password, 
                                                self.user_manager.users[self.current_user['username']]['password']):
                status_label.config(text="Senha atual incorreta")
                current_password_entry.focus()
                return
            
            # Altera a senha
            if self.user_manager.change_password(self.current_user['username'], new_password):
                status_label.config(text="✅ Senha alterada com sucesso!", foreground="green")
                dialog.after(2000, dialog.destroy)
            else:
                status_label.config(text="❌ Erro ao alterar senha")
        
        # Botões
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill="x", pady=20)
        
        ttk.Button(btn_frame, text="💾 Alterar Senha", 
                command=save_password, cursor="hand2").pack(side="left", padx=5)
        
        ttk.Button(btn_frame, text="❌ Cancelar", 
                command=dialog.destroy, cursor="hand2").pack(side="right", padx=5)
        
        # Enter para salvar
        confirm_password_entry.bind("<Return>", lambda e: save_password())

    # ---------- CONFIGURAÇÕES ----------
    def config_window(self):
        """Janela de configurações"""
        if not self.check_permission("system_config"):
            return
            
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

        # Caminho da pasta do Firebird
        ttk.Label(firebird_frame, text="Pasta do Firebird:*", font=("Arial", 9, "bold")).grid(row=0, column=0, sticky="w", pady=8)
        firebird_path_var = tk.StringVar(value=self.conf.get("firebird_path", ""))
        firebird_path_entry = ttk.Entry(firebird_frame, textvariable=firebird_path_var, width=40)
        firebird_path_entry.grid(row=0, column=1, padx=5)
        ttk.Button(firebird_frame, text="📁", width=3, 
                  command=lambda: self.pick_firebird_folder(firebird_path_var)).grid(row=0, column=2)

        # Botão para buscar automaticamente
        ttk.Button(firebird_frame, text="🔍 Buscar Automaticamente", 
                  command=lambda: self.auto_detect_firebird(firebird_path_var),
                  cursor="hand2").grid(row=1, column=1, sticky="w", padx=5, pady=5)

        # Status dos executáveis encontrados
        self.exe_status_label = ttk.Label(firebird_frame, text="", foreground="gray", font=("Arial", 8))
        self.exe_status_label.grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 10))

        # Atualiza status inicial
        self._update_exe_status()

        ttk.Label(firebird_frame, text="Pasta de backups:").grid(row=3, column=0, sticky="w", pady=8)
        backup_var = tk.StringVar(value=self.conf.get("backup_dir", ""))
        backup_entry = ttk.Entry(firebird_frame, textvariable=backup_var, width=40)
        backup_entry.grid(row=3, column=1, padx=5)
        ttk.Button(firebird_frame, text="📁", width=3,
                  command=lambda: self.pick_dir(backup_var)).grid(row=3, column=2)

        ttk.Label(firebird_frame, text="Host do Firebird:").grid(row=4, column=0, sticky="w", pady=8)
        host_var = tk.StringVar(value=self.conf.get("firebird_host", "localhost"))
        ttk.Entry(firebird_frame, textvariable=host_var, width=40).grid(row=4, column=1, padx=5)

        ttk.Label(firebird_frame, text="Porta do Firebird:").grid(row=5, column=0, sticky="w", pady=8)
        port_var = tk.StringVar(value=self.conf.get("firebird_port", "26350"))
        ttk.Entry(firebird_frame, textvariable=port_var, width=40).grid(row=5, column=1, padx=5)

        ttk.Label(firebird_frame, text="Usuário:").grid(row=6, column=0, sticky="w", pady=8)
        user_var = tk.StringVar(value=self.conf.get("firebird_user", "SYSDBA"))
        ttk.Entry(firebird_frame, textvariable=user_var, width=40).grid(row=6, column=1, padx=5)

        ttk.Label(firebird_frame, text="Senha:").grid(row=7, column=0, sticky="w", pady=8)
        pass_var = tk.StringVar(value=self.conf.get("firebird_password", "masterkey"))
        ttk.Entry(firebird_frame, textvariable=pass_var, width=40, show="*").grid(row=7, column=1, padx=5)

        ttk.Label(firebird_frame, text="PageSize:").grid(row=8, column=0, sticky="w", pady=8)
        page_size_var = tk.StringVar(value=self.conf.get("page_size", "8192"))
        page_size_combo = ttk.Combobox(firebird_frame, textvariable=page_size_var, 
                                      values=PAGE_SIZE_OPTIONS, state="readonly", width=10)
        page_size_combo.grid(row=8, column=1, sticky="w", padx=5)
        ttk.Label(firebird_frame, text="(1KB, 2KB, 4KB, 8KB, 16KB)").grid(row=8, column=1, sticky="e", padx=5)

        ttk.Label(firebird_frame, text="Qtd. backups a manter:").grid(row=9, column=0, sticky="w", pady=8)
        keep_var = tk.IntVar(value=self.conf.get("keep_backups", DEFAULT_KEEP_BACKUPS))
        ttk.Spinbox(firebird_frame, from_=1, to=100, textvariable=keep_var, width=10).grid(row=9, column=1, sticky="w", padx=5)

        # Aba Sistema
        system_frame = ttk.Frame(notebook, padding=10)
        notebook.add(system_frame, text="Sistema")

        ttk.Label(system_frame, text="Monitoramento automático:").grid(row=0, column=0, sticky="w", pady=8)
        monitor_var = tk.BooleanVar(value=self.conf.get("auto_monitor", True))
        ttk.Checkbutton(system_frame, variable=monitor_var).grid(row=0, column=1, sticky="w", padx=5)

        ttk.Label(system_frame, text="Intervalo (segundos):").grid(row=1, column=0, sticky="w", pady=8)
        interval_var = tk.IntVar(value=self.conf.get("monitor_interval", 30))
        ttk.Spinbox(system_frame, from_=10, to=300, textvariable=interval_var, width=10).grid(row=1, column=1, sticky="w", padx=5)

        # Limpeza de Logs
        ttk.Label(system_frame, text="Manter logs por (dias):").grid(row=2, column=0, sticky="w", pady=8)
        log_retention_var = tk.IntVar(value=self.conf.get("log_retention_days", 30))
        log_spinbox = ttk.Spinbox(system_frame, from_=1, to=365, textvariable=log_retention_var, width=10)
        log_spinbox.grid(row=2, column=1, sticky="w", padx=5)
        ttk.Label(system_frame, text="(1-365 dias)").grid(row=2, column=2, sticky="e", padx=5)

        # Comportamento
        ttk.Label(system_frame, text="Minimizar para bandeja:").grid(row=3, column=0, sticky="w", pady=8)
        tray_var = tk.BooleanVar(value=self.conf.get("minimize_to_tray", True))
        ttk.Checkbutton(system_frame, variable=tray_var).grid(row=3, column=1, sticky="w", padx=5)

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
            # Atualiza o caminho do Firebird primeiro
            new_firebird_path = firebird_path_var.get().strip()
            
            # Se o caminho do Firebird mudou, busca os executáveis novamente
            if new_firebird_path != self.conf.get("firebird_path", ""):
                self.conf["firebird_path"] = new_firebird_path
                if new_firebird_path and os.path.exists(new_firebird_path):
                    executables = find_firebird_executables(new_firebird_path)
                    # Atualiza os caminhos dos executáveis
                    for exe_name, exe_path in executables.items():
                        if exe_path:
                            self.conf[exe_name] = exe_path
            
            self.conf.update({
                "backup_dir": backup_var.get(),
                "firebird_host": host_var.get(),
                "firebird_port": port_var.get(),
                "firebird_user": user_var.get(),
                "firebird_password": pass_var.get(),
                "page_size": page_size_var.get(),
                "keep_backups": keep_var.get(),
                "auto_monitor": monitor_var.get(),
                "monitor_interval": interval_var.get(),
                "minimize_to_tray": tray_var.get(),
                "start_with_windows": startup_var.get(),
                "log_retention_days": log_retention_var.get()
            })
            
            if save_config(self.conf):
                # Aplica a configuração de inicialização com Windows
                self.apply_startup_setting(startup_var.get())
                try:
                    cleanup_old_logs(LOG_FILE, log_retention_var.get())
                except Exception as e:
                    self.log(f"⚠️ Erro na limpeza de logs: {e}", "warning")
                
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

        # Atualiza status quando o caminho do Firebird muda
        def on_firebird_path_change(*args):
            self.after(500, self._update_exe_status)
        
        firebird_path_var.trace_add("write", on_firebird_path_change)

    def pick_firebird_folder(self, var):
        """Seleciona pasta do Firebird"""
        path = filedialog.askdirectory(title="Selecione a pasta do Firebird")
        if path:
            var.set(path)

    def auto_detect_firebird(self, var):
        """Tenta detectar automaticamente a pasta do Firebird"""
        common_paths = [
            "C:\\Program Files\\Firebird",
            "C:\\Program Files (x86)\\Firebird", 
            "C:\\Firebird",
            "D:\\Firebird",
            "E:\\Firebird"
        ]
        
        for path in common_paths:
            if os.path.exists(path):
                var.set(path)
                self.log(f"🔍 Firebird detectado automaticamente: {path}", "info")
                messagebox.showinfo("Detecção Automática", f"Firebird encontrado em:\n{path}")
                return
        
        # Se não encontrou, tenta encontrar no PATH
        gbak_path = find_executable("gbak.exe")
        if gbak_path:
            firebird_dir = os.path.dirname(os.path.dirname(gbak_path))
            var.set(firebird_dir)
            self.log(f"🔍 Firebird detectado via PATH: {firebird_dir}", "info")
            messagebox.showinfo("Detecção Automática", f"Firebird encontrado via PATH:\n{firebird_dir}")
            return
        
        messagebox.showinfo("Detecção Automática", "Não foi possível detectar automaticamente o Firebird.\nSelecione manualmente a pasta.")

    def _update_exe_status(self):
        """Atualiza o status dos executáveis encontrados"""
        firebird_path = self.conf.get("firebird_path", "")
        if not firebird_path or not os.path.exists(firebird_path):
            self.exe_status_label.config(text="❌ Pasta do Firebird não configurada ou inválida")
            return
        
        executables = find_firebird_executables(firebird_path)
        
        found = []
        missing = []
        
        for exe_name, exe_path in executables.items():
            if exe_path:
                found.append(exe_name.replace('_path', ''))
            else:
                missing.append(exe_name.replace('_path', ''))
        
        status_parts = []
        if found:
            status_parts.append(f"✅ {', '.join(found)}")
        if missing:
            status_parts.append(f"❌ {', '.join(missing)}")
        
        if status_parts:
            self.exe_status_label.config(text=" | ".join(status_parts))
        else:
            self.exe_status_label.config(text="❌ Nenhum executável encontrado")

    def pick_dir(self, var):
        """Seleciona diretório"""
        path = filedialog.askdirectory(title="Selecione diretório")
        if path:
            var.set(path)

    # ---------- EDITOR DE SQL ----------
    def open_sql_console(self):
        """Abre console SQL para executar consultas no banco de dados"""
        if not self.check_permission("sql_console"):
            return
            
        db_path = filedialog.askopenfilename(
            title="Selecione o banco de dados para conectar",
            filetypes=[("Firebird Database", "*.fdb"), ("Todos os arquivos", "*.*")]
        )
        
        if not db_path:
            return
        
        db_name = Path(db_path).name
        
        win = tk.Toplevel(self)
        win.title(f"Editor SQL - {db_name}")
        win.geometry("1200x800")
        win.minsize(1000, 600)

        # Centraliza
        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 600
        y = self.winfo_y() + (self.winfo_height() // 2) - 400
        win.geometry(f"+{x}+{y}")

        # Ícone
        icon_path = BASE_DIR / "images" / "icon.ico"
        if icon_path.exists():
            win.iconbitmap(str(icon_path))

        win.transient(self)
        win.grab_set()
        win.focus_force()

        # Frame principal
        main_frame = ttk.Frame(win)
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)

        # Cabeçalho com informações do banco
        header_frame = ttk.Frame(main_frame)
        header_frame.pack(fill="x", pady=(0, 10))

        ttk.Label(header_frame, 
                text=f"🔍 Editor SQL - Conectado a: {db_name}",
                font=("Arial", 11, "bold")).pack(anchor="w")

        ttk.Label(header_frame, 
                text=f"📍 {db_path}",
                font=("Arial", 9),
                foreground="gray").pack(anchor="w")

        # Frame do editor SQL
        editor_frame = ttk.LabelFrame(main_frame, text="Editor SQL", padding=10)
        editor_frame.pack(fill="both", expand=True, pady=(0, 10))

        # Container para editor e histórico
        editor_container = ttk.Frame(editor_frame)
        editor_container.pack(fill="both", expand=True)

        # Frame para controles do editor
        editor_controls_frame = ttk.Frame(editor_container)
        editor_controls_frame.pack(fill="x", pady=(0, 5))

        # Botão de histórico
        ttk.Button(editor_controls_frame, 
                text="📜 Histórico",
                command=lambda: show_history_window(),
                cursor="hand2",
                width=12).pack(side="left", padx=(0, 10))

        # Label de informações
        history_info_label = ttk.Label(editor_controls_frame, 
                                    text="F9 ou Ctrl+Enter para executar",
                                    foreground="gray",
                                    font=("Arial", 8))
        history_info_label.pack(side="left")

        # Área de edição SQL
        sql_text = scrolledtext.ScrolledText(
            editor_container, 
            font=("Consolas", 10),
            wrap=tk.WORD,
            height=10
        )
        sql_text.pack(fill="both", expand=True)

        # Inserir template básico
        template = """-- Digite suas consultas SQL aqui
    -- Use F9 ou Ctrl+Enter para executar toda a consulta

    -- Exemplo: Selecionar todas as tabelas
    SELECT 
        RDB$RELATION_NAME as tabela,
        RDB$OWNER_NAME as proprietario
    FROM RDB$RELATIONS 
    WHERE RDB$SYSTEM_FLAG = 0 
    ORDER BY RDB$RELATION_NAME;

    -- Exemplo: Contar registros em uma tabela
    -- SELECT COUNT(*) as total_registros FROM NOME_DA_TABELA;

    """
        sql_text.insert("1.0", template)

        # Frame de resultados
        results_frame = ttk.LabelFrame(main_frame, text="Resultados", padding=10)
        results_frame.pack(fill="both", expand=True, pady=(0, 10))

        # Container para treeview e scrollbars
        tree_container = ttk.Frame(results_frame)
        tree_container.pack(fill="both", expand=True)

        # Treeview para mostrar resultados em tabela
        results_tree = ttk.Treeview(tree_container, show="headings")
        
        # Scrollbars para o treeview
        v_scrollbar = ttk.Scrollbar(tree_container, orient="vertical", command=results_tree.yview)
        h_scrollbar = ttk.Scrollbar(tree_container, orient="horizontal", command=results_tree.xview)
        results_tree.configure(yscrollcommand=v_scrollbar.set, xscrollcommand=h_scrollbar.set)
        
        # Layout usando grid para melhor controle
        results_tree.grid(row=0, column=0, sticky="nsew")
        v_scrollbar.grid(row=0, column=1, sticky="ns")
        h_scrollbar.grid(row=1, column=0, sticky="ew")
        
        tree_container.grid_rowconfigure(0, weight=1)
        tree_container.grid_columnconfigure(0, weight=1)

        # Frame de status
        status_frame = ttk.Frame(main_frame)
        status_frame.pack(fill="x", pady=(0, 10))

        sql_status = ttk.Label(status_frame, text="Pronto para executar consultas...", foreground="gray")
        sql_status.pack(side="left")

        # Frame de botões
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill="x")

        # Histórico de comandos
        sql_history = []
        MAX_HISTORY_SIZE = 50

        def add_to_history(sql_command):
            """Adiciona comando ao histórico"""
            if not sql_command.strip():
                return
                
            # Remove do histórico se já existir
            if sql_command in sql_history:
                sql_history.remove(sql_command)
            
            # Adiciona no início
            sql_history.insert(0, sql_command)
            
            # Limita o tamanho do histórico
            if len(sql_history) > MAX_HISTORY_SIZE:
                sql_history.pop()

        def show_history_window():
            """Mostra janela com histórico completo de comandos"""
            if not sql_history:
                messagebox.showinfo("Histórico", "Nenhum comando no histórico.")
                return
            
            history_win = tk.Toplevel(win)
            history_win.title("Histórico de Comandos SQL")
            history_win.geometry("800x600")
            history_win.transient(win)
            history_win.grab_set()
            
            # Centraliza
            x = win.winfo_x() + (win.winfo_width() // 2) - 400
            y = win.winfo_y() + (win.winfo_height() // 2) - 300
            history_win.geometry(f"+{x}+{y}")

            # Ícone
            icon_path = BASE_DIR / "images" / "icon.ico"
            if icon_path.exists():
                history_win.iconbitmap(str(icon_path))
            
            # Frame principal
            main_history_frame = ttk.Frame(history_win, padding=15)
            main_history_frame.pack(fill="both", expand=True)
            
            ttk.Label(main_history_frame, 
                    text=f"📜 Histórico de Comandos ({len(sql_history)} comandos)",
                    font=("Arial", 12, "bold")).pack(pady=(0, 10))
            
            # Frame de controles
            history_controls_frame = ttk.Frame(main_history_frame)
            history_controls_frame.pack(fill="both", expand=True, pady=(0, 10))
            
            # Lista de comandos
            history_listbox = tk.Listbox(
                history_controls_frame,
                font=("Consolas", 9),
                height=15,
                selectmode="single"
            )
            history_listbox.pack(fill="both", expand=True, side="left")
            
            # Scrollbar para a lista
            history_scrollbar = ttk.Scrollbar(history_controls_frame, orient="vertical", command=history_listbox.yview)
            history_listbox.configure(yscrollcommand=history_scrollbar.set)
            history_scrollbar.pack(side="right", fill="y")
            
            # Preenche a lista com o histórico
            for i, cmd in enumerate(sql_history, 1):
                preview = cmd[:100] + "..." if len(cmd) > 100 else cmd
                history_listbox.insert(tk.END, f"{i:2d}. {preview}")
            
            # Frame de botões
            history_btn_frame = ttk.Frame(main_history_frame)
            history_btn_frame.pack(fill="x", pady=10)
            
            def load_selected_command():
                """Carrega o comando selecionado para o editor"""
                selection = history_listbox.curselection()
                if selection:
                    index = selection[0]
                    sql_text.delete("1.0", tk.END)
                    sql_text.insert("1.0", sql_history[index])
                    history_win.destroy()
                    sql_text.focus_set()
            
            def delete_selected_command():
                """Remove o comando selecionado do histórico"""
                selection = history_listbox.curselection()
                if selection:
                    index = selection[0]
                    removed_cmd = sql_history.pop(index)
                    history_listbox.delete(selection[0])
                    history_listbox.delete(0, tk.END)
                    for i, cmd in enumerate(sql_history, 1):
                        preview = cmd[:100] + "..." if len(cmd) > 100 else cmd
                        history_listbox.insert(tk.END, f"{i:2d}. {preview}")
            
            def clear_all_history():
                """Limpa todo o histórico"""
                if messagebox.askyesno("Confirmar", "Tem certeza que deseja limpar todo o histórico?"):
                    sql_history.clear()
                    history_listbox.delete(0, tk.END)
            
            ttk.Button(history_btn_frame, 
                    text="📥 Carregar Selecionado",
                    command=load_selected_command,
                    cursor="hand2").pack(side="left", padx=5)
            
            ttk.Button(history_btn_frame,
                    text="🗑️ Remover Selecionado",
                    command=delete_selected_command,
                    cursor="hand2").pack(side="left", padx=5)
            
            ttk.Button(history_btn_frame,
                    text="💥 Limpar Tudo",
                    command=clear_all_history,
                    cursor="hand2").pack(side="left", padx=5)
            
            ttk.Button(history_btn_frame,
                    text="❌ Fechar",
                    command=history_win.destroy,
                    cursor="hand2").pack(side="right", padx=5)
            
            # Duplo clique para carregar
            history_listbox.bind("<Double-Button-1>", lambda e: load_selected_command())

        def execute_query_with_fbclient():
            """Executa consulta"""
            try:
                import fdb
            except ImportError:
                messagebox.showerror("Erro", "Biblioteca fdb não encontrada. Instale com: pip install fdb")
                return None, None
            
            sql_code = sql_text.get("1.0", tk.END).strip()
            
            # Verifica se há texto selecionado
            try:
                selected_text = sql_text.get(tk.SEL_FIRST, tk.SEL_LAST).strip()
                if selected_text:
                    sql_code = selected_text
            except:
                pass
            
            if not sql_code:
                return None, None
            
            try:
                # Conecta ao banco usando fdb
                conn = fdb.connect(
                    host=self.conf.get('firebird_host', 'localhost'),
                    database=db_path,
                    user=self.conf.get("firebird_user", "SYSDBA"),
                    password=self.conf.get("firebird_password", "masterkey"),
                    port=int(self.conf.get("firebird_port", "26350"))
                )
                
                cursor = conn.cursor()
                cursor.execute(sql_code)
                
                # Obtém os nomes das colunas
                columns = [desc[0] for desc in cursor.description]
                
                # Obtém todos os resultados
                results = cursor.fetchall()
                
                cursor.close()
                conn.close()
                
                return columns, results
                
            except Exception as e:
                return None, f"Erro na consulta: {str(e)}"

        def calculate_optimal_column_widths(columns, data):
            """Calcula larguras baseadas no maior conteúdo de cada coluna"""
            widths = {}
            
            for i, col in enumerate(columns):
                # Largura baseada no cabeçalho
                header_width = len(str(col)) * 9 + 25
                
                # Largura baseada nos dados - encontra o maior conteúdo
                max_data_width = 0
                for row in data:
                    if i < len(row):
                        cell_content = str(row[i]) if row[i] is not None else ""
                        cell_width = len(cell_content) * 7 + 20
                        if cell_width > max_data_width:
                            max_data_width = cell_width
                
                optimal_width = max(header_width, max_data_width, 100)
                
                widths[col] = min(optimal_width, 800)
            
            return widths

        def setup_columns(columns, data):
            """Configura as colunas do treeview com larguras ótimas"""
            results_tree["columns"] = columns
            
            optimal_widths = calculate_optimal_column_widths(columns, data)
            
            for col in columns:
                results_tree.heading(col, text=col)
                results_tree.column(col, width=optimal_widths[col], anchor="w", minwidth=80, stretch=False)
            
            for row in data:
                results_tree.insert("", "end", values=row)

        def execute_query():
            """Executa a consulta SQL"""
            sql_code = sql_text.get("1.0", tk.END).strip()
            
            # Verifica se há texto selecionado
            try:
                selected_text = sql_text.get(tk.SEL_FIRST, tk.SEL_LAST).strip()
                if selected_text:
                    sql_code = selected_text
            except:
                pass
            
            if not sql_code:
                messagebox.showwarning("Aviso", "Digite uma consulta SQL para executar.")
                return
            
            # Adiciona ao histórico
            add_to_history(sql_code)
            
            sql_status.config(text="🔄 Executando consulta...", foreground="blue")
            win.update()
            
            def run_query():
                columns, results = execute_query_with_fbclient()
                
                def update_ui():
                    # Limpa resultados anteriores
                    for item in results_tree.get_children():
                        results_tree.delete(item)
                    
                    # Limpa colunas existentes
                    results_tree["columns"] = []
                    
                    if columns is not None and results is not None:
                        if isinstance(results, list):
                            # Sucesso
                            _show_tabular_results(columns, results)
                        else:
                            # Erro
                            _show_error_output(results)
                    else:
                        _execute_with_isql()
                
                self.after(0, update_ui)
            
            # Executa em thread separada
            threading.Thread(target=run_query, daemon=True).start()

        def _execute_with_isql():
            """Executa com ISQL como fallback"""
            try:
                sql_code = sql_text.get("1.0", tk.END).strip()
                
                # Verifica se há texto selecionado
                try:
                    selected_text = sql_text.get(tk.SEL_FIRST, tk.SEL_LAST).strip()
                    if selected_text:
                        sql_code = selected_text
                except:
                    pass
                
                isql = self.conf.get("isql_path") or find_executable("isql.exe")
                if not isql:
                    _show_error_output("isql.exe não encontrado.")
                    return
                
                # Cria arquivo temporário com o SQL
                temp_dir = Path(tempfile.gettempdir())
                temp_sql_file = temp_dir / f"temp_query_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sql"
                
                # Escreve o SQL no arquivo temporário
                with open(temp_sql_file, 'w', encoding='utf-8') as f:
                    f.write("SET HEADING ON;\n")
                    f.write("SET STATS OFF;\n")
                    f.write(sql_code)
                    if not sql_code.strip().endswith(';'):
                        f.write(";")
                
                # Comando ISQL
                connection_string = f"{self.conf.get('firebird_host', 'localhost')}/{self.conf.get('firebird_port', '26350')}:{db_path}"
                
                cmd = [
                    isql,
                    connection_string,
                    "-user", self.conf.get("firebird_user", "SYSDBA"),
                    "-pass", self.conf.get("firebird_password", "masterkey"),
                    "-i", str(temp_sql_file)
                ]
                
                CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
                
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    creationflags=CREATE_NO_WINDOW,
                    bufsize=1
                )
                
                try:
                    stdout, stderr = process.communicate(timeout=30)
                    return_code = process.returncode
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                    return_code = -1
                    stderr = "Timeout: A consulta excedeu o tempo limite de 30 segundos"
                
                # Limpa arquivo temporário
                try:
                    temp_sql_file.unlink()
                except:
                    pass
                
                if return_code == 0 and stdout:
                    _parse_isql_output(stdout)
                elif stderr:
                    _show_error_output(stderr)
                else:
                    _show_success_message("Consulta executada. Nenhuma saída retornada.")
                    
            except Exception as e:
                _show_error_output(f"Erro ao executar com ISQL: {str(e)}")

        def _parse_isql_output(output):
            """Tenta parsear a saída do ISQL em formato tabular"""
            lines = output.strip().split('\n')
            clean_lines = []
            
            for line in lines:
                clean_line = line.rstrip()
                if (clean_line and 
                    not clean_line.startswith('SQL>') and 
                    not clean_line.startswith('CON>') and
                    not clean_line.startswith('>')):
                    clean_lines.append(clean_line)
            
            if not clean_lines:
                _show_success_message("Consulta executada com sucesso. Nenhum resultado retornado.")
                return

            header_found = False
            headers = []
            data = []
            
            for line in clean_lines:
                if '----' in line and not header_found:
                    header_found = True
                    continue
                    
                if header_found:
                    if line.strip() and '----' not in line:
                        data.append([line.strip()])
                else:
                    if line.strip():
                        headers = [line.strip()]
            
            if headers and data:
                max_width = 0
                for line in data:
                    line_width = len(str(line[0])) * 7 + 20
                    if line_width > max_width:
                        max_width = line_width
                
                header_width = len(headers[0]) * 9 + 25
                optimal_width = min(max(max_width, header_width, 300), 1000)
                
                results_tree["columns"] = ["Resultado"]
                results_tree.heading("Resultado", text=headers[0])
                results_tree.column("Resultado", width=optimal_width, anchor="w", minwidth=100, stretch=False)
                
                for row in data:
                    results_tree.insert("", "end", values=row)
                
                sql_status.config(text=f"✅ Consulta executada - {len(data)} linha(s) retornada(s)", foreground="green")
            else:
                _show_text_output(clean_lines)

        def _show_tabular_results(columns, data):
            setup_columns(columns, data)
            sql_status.config(text=f"✅ Consulta executada - {len(data)} linha(s) retornada(s)", foreground="green")

        def _show_text_output(lines):
            max_width = 300
            for line in lines:
                line_width = len(str(line)) * 7 + 20
                if line_width > max_width:
                    max_width = line_width
            
            optimal_width = min(max_width, 1000)
            
            results_tree["columns"] = ["Resultado"]
            results_tree.heading("Resultado", text="Resultado")
            results_tree.column("Resultado", width=optimal_width, anchor="w", minwidth=100, stretch=False)
            
            for line in lines:
                if line.strip():
                    results_tree.insert("", "end", values=[line.strip()])
            
            sql_status.config(text=f"✅ Consulta executada - {len(lines)} linha(s) de saída", foreground="green")

        def _show_error_output(error_text):
            """Mostra mensagens de erro"""
            lines = error_text.split('\n')
            max_width = 300
            for line in lines:
                line_width = len(str(line)) * 7 + 20
                if line_width > max_width:
                    max_width = line_width
            
            optimal_width = min(max_width, 1000)
            
            results_tree["columns"] = ["Erro"]
            results_tree.heading("Erro", text="Erro")
            results_tree.column("Erro", width=optimal_width, anchor="w", minwidth=100, stretch=False)
            
            error_count = 0
            for line in lines:
                if line.strip():
                    results_tree.insert("", "end", values=[line.strip()])
                    error_count += 1
            
            sql_status.config(text=f"❌ Erro na execução - {error_count} mensagem(ns) de erro", foreground="red")

        def _show_success_message(message):
            """Mostra mensagem de sucesso"""
            message_width = min(len(message) * 7 + 20, 800)
            
            results_tree["columns"] = ["Informação"]
            results_tree.heading("Informação", text="Informação")
            results_tree.column("Informação", width=message_width, anchor="w", minwidth=100, stretch=False)
            
            results_tree.insert("", "end", values=[message])
            sql_status.config(text="✅ " + message, foreground="green")

        def clear_editor():
            """Limpa o editor SQL"""
            sql_text.delete("1.0", tk.END)

        def clear_results():
            """Limpa os resultados"""
            for item in results_tree.get_children():
                results_tree.delete(item)
            
            results_tree["columns"] = []
            sql_status.config(text="🗑️ Resultados limpos", foreground="gray")

        def format_sql():
            """Formata o código SQL"""
            try:
                text = sql_text.get("1.0", tk.END)
                
                text = text.replace("SELECT", "\nSELECT")
                text = text.replace("FROM", "\nFROM")
                text = text.replace("WHERE", "\nWHERE")
                text = text.replace("ORDER BY", "\nORDER BY")
                text = text.replace("GROUP BY", "\nGROUP BY")
                text = text.replace("HAVING", "\nHAVING")
                text = text.replace("JOIN", "\nJOIN")
                text = text.replace("LEFT JOIN", "\nLEFT JOIN")
                text = text.replace("RIGHT JOIN", "\nRIGHT JOIN")
                text = text.replace("INNER JOIN", "\nINNER JOIN")
                
                sql_text.delete("1.0", tk.END)
                sql_text.insert("1.0", text)
                
                sql_status.config(text="✅ SQL formatado", foreground="green")
            except Exception as e:
                messagebox.showerror("Erro", f"Erro ao formatar SQL: {e}")

        def show_tables():
            """Mostra todas as tabelas do banco"""
            tables_query = """SELECT 
        RDB$RELATION_NAME as Tabela,
        RDB$OWNER_NAME as Proprietario,
        RDB$DESCRIPTION as Descricao
    FROM RDB$RELATIONS 
    WHERE RDB$SYSTEM_FLAG = 0 
    ORDER BY RDB$RELATION_NAME;"""
            sql_text.delete("1.0", tk.END)
            sql_text.insert("1.0", tables_query)
            execute_query()

        def show_table_structure():
            """Mostra a estrutura de uma tabela específica"""
            table_name = simpledialog.askstring("Estrutura da Tabela", "Digite o nome da tabela:")
            if table_name:
                structure_query = f"""SELECT 
        R.RDB$FIELD_NAME as Campo,
        CASE F.RDB$FIELD_TYPE
            WHEN 7 THEN 'SMALLINT'
            WHEN 8 THEN 'INTEGER'
            WHEN 10 THEN 'FLOAT'
            WHEN 12 THEN 'DATE'
            WHEN 13 THEN 'TIME'
            WHEN 14 THEN 'CHAR'
            WHEN 16 THEN 'BIGINT'
            WHEN 27 THEN 'DOUBLE'
            WHEN 35 THEN 'TIMESTAMP'
            WHEN 37 THEN 'VARCHAR'
            WHEN 261 THEN 'BLOB'
            ELSE 'UNKNOWN'
        END as Tipo,
        F.RDB$FIELD_LENGTH as Tamanho,
        CASE WHEN R.RDB$NULL_FLAG = 1 THEN 'NÃO' ELSE 'SIM' END as Nulo,
        R.RDB$DEFAULT_SOURCE as Padrao
    FROM RDB$RELATION_FIELDS R
    JOIN RDB$FIELDS F ON R.RDB$FIELD_SOURCE = F.RDB$FIELD_NAME
    WHERE R.RDB$RELATION_NAME = '{table_name.upper()}'
    ORDER BY R.RDB$FIELD_POSITION;"""
                sql_text.delete("1.0", tk.END)
                sql_text.insert("1.0", structure_query)
                execute_query()

        def export_results():
            """Exporta resultados para arquivo CSV"""
            if not results_tree.get_children():
                messagebox.showwarning("Aviso", "Não há resultados para exportar.")
                return
            
            filename = filedialog.asksaveasfilename(
                defaultextension=".csv",
                filetypes=[("CSV files", "*.csv"), ("Todos os arquivos", "*.*")]
            )
            
            if filename:
                try:
                    with open(filename, 'w', encoding='utf-8', newline='') as f:
                        import csv
                        writer = csv.writer(f, delimiter=';')
                        
                        # Escreve cabeçalho
                        columns = results_tree["columns"]
                        if columns:
                            headers = [results_tree.heading(col)["text"] for col in columns]
                            writer.writerow(headers)
                        
                        # Escreve dados
                        for item in results_tree.get_children():
                            values = results_tree.item(item, "values")
                            writer.writerow(values)
                    
                    sql_status.config(text=f"✅ Resultados exportados para: {Path(filename).name}", foreground="green")
                except Exception as e:
                    messagebox.showerror("Erro", f"Erro ao exportar resultados: {e}")

        # Botões de ação
        ttk.Button(btn_frame, 
                text="▶️ Executar (Ctrl+Enter)", 
                command=execute_query,
                cursor="hand2").pack(side="left", padx=5)

        ttk.Button(btn_frame, 
                text="🗑️ Limpar Editor", 
                command=clear_editor,
                cursor="hand2").pack(side="left", padx=5)

        ttk.Button(btn_frame, 
                text="🗑️ Limpar Resultados", 
                command=clear_results,
                cursor="hand2").pack(side="left", padx=5)

        ttk.Button(btn_frame, 
                text="📊 Format SQL", 
                command=format_sql,
                cursor="hand2").pack(side="left", padx=5)

        ttk.Button(btn_frame, 
                text="📋 Listar Tabelas", 
                command=show_tables,
                cursor="hand2").pack(side="left", padx=5)

        ttk.Button(btn_frame, 
                text="🔍 Estrutura da Tabela", 
                command=show_table_structure,
                cursor="hand2").pack(side="left", padx=5)

        ttk.Button(btn_frame, 
                text="💾 Exportar CSV", 
                command=export_results,
                cursor="hand2").pack(side="left", padx=5)

        # Bindings de teclado
        def on_key_press(event):
            if event.state & 0x4 and event.keysym == 'Return':
                execute_query()
            elif event.keysym == 'F9':
                execute_query()

        sql_text.bind('<Control-Return>', on_key_press)
        sql_text.bind('<F9>', on_key_press)
        win.bind('<F9>', on_key_press)

        # Foca no editor
        sql_text.focus_set()

        self.log(f"💻 Editor SQL aberto para: {db_name}", "info")

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
