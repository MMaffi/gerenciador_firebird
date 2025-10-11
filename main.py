"""
Firebird Manager
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
from tkinter import ttk, filedialog, messagebox, scrolledtext

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
LOG_FILE = BASE_DIR / "firebird_manager.log"
DEFAULT_BACKUP_DIR = BASE_DIR / "backups"
DEFAULT_KEEP_BACKUPS = 5

# ---------- LOGGING ----------
def setup_logging():
    # Cria o diretório se não existir
    LOG_FILE.parent.mkdir(exist_ok=True)
    
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # Remove handlers existentes para evitar duplicação
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
    
    # Formatação padrão
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Handler para arquivo
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
        "firebird_host": "localhost"
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
    except Exception as e:
        logging.error(f"Falha ao salvar config.json: {e}")

# ---------- AUTOMAÇÕES ----------
def find_executable(name):

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
    """Mata processos do Firebird"""
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

# ------------ APP ------------
class FirebirdManagerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        
        self.logger = setup_logging()
        
        try:
            self.title("Firebird Manager")
            
            # Icon app
            icon_path = BASE_DIR / "images" / "icon.ico"
            self.iconbitmap(str(icon_path))

            self.geometry("800x700")
            self.resizable(True, True)
            self.configure(bg="#f5f5f5")
            
            self.conf = load_config()
            self.task_running = False

            self._create_widgets()
            self.logger.info("Aplicativo iniciado com sucesso")
            
        except Exception as e:
            self.logger.critical(f"Falha crítica ao iniciar aplicação: {e}")
            messagebox.showerror("Erro Fatal", f"Falha ao iniciar aplicação:\n{e}")
            sys.exit(1)

    def _create_widgets(self):
        # ---- HEADER ----
        header_frame = ttk.Frame(self)
        header_frame.pack(pady=10, fill="x")
        
        header = ttk.Label(
            header_frame, 
            text="🦅 Gerenciador de Backups Firebird",
            font=("Arial", 16, "bold")
        )
        header.pack()

        # ---- BOTÕES PRINCIPAIS ----
        btn_frame = ttk.LabelFrame(self, text="Ações", padding=10)
        btn_frame.pack(pady=5, padx=10, fill="x")

        self.btn_backup = ttk.Button(
            btn_frame, text="📦 Gerar Backup", 
            command=self.backup
        )
        self.btn_restore = ttk.Button(
            btn_frame, text="♻️ Restaurar Backup", 
            command=self.restore
        )
        self.btn_verify = ttk.Button(
            btn_frame, text="🩺 Verificar Integridade", 
            command=self.verify
        )
        self.btn_kill = ttk.Button(
            btn_frame, text="🔥 Matar Instâncias", 
            command=self.kill
        )
        self.btn_config = ttk.Button(
            btn_frame, text="⚙️ Configurações", 
            command=self.config_window
        )

        # Layout dos botões
        self.btn_backup.grid(row=0, column=0, padx=5, pady=5, sticky="ew")
        self.btn_restore.grid(row=0, column=1, padx=5, pady=5, sticky="ew")
        self.btn_verify.grid(row=0, column=2, padx=5, pady=5, sticky="ew")
        self.btn_kill.grid(row=0, column=3, padx=5, pady=5, sticky="ew")
        self.btn_config.grid(row=0, column=4, padx=5, pady=5, sticky="ew")
        
        for i in range(5):
            btn_frame.columnconfigure(i, weight=1)

        # ---- STATUS ----
        status_frame = ttk.Frame(self)
        status_frame.pack(pady=5, fill="x", padx=10)
        
        self.status_label = ttk.Label(
            status_frame, 
            text="Pronto para iniciar operações.", 
            foreground="gray",
            font=("Arial", 9)
        )
        self.status_label.pack()

        # ---- BARRA DE PROGRESSO ----
        self.progress = ttk.Progressbar(
            self, 
            mode="indeterminate", 
            length=500
        )
        self.progress.pack(pady=5)

        # ---- LOG ----
        log_frame = ttk.LabelFrame(self, text="Log de Execução", padding=10)
        log_frame.pack(padx=10, pady=10, fill="both", expand=True)

        self.output = scrolledtext.ScrolledText(log_frame)
        self.output.pack(fill="both", expand=True)
      
        self.output.tag_config("success", foreground="green")
        self.output.tag_config("error", foreground="red")
        self.output.tag_config("warning", foreground="orange")
        self.output.tag_config("info", foreground="blue")
        self.output.tag_config("debug", foreground="gray")

        self.log("✅ Aplicativo iniciado. Selecione uma ação acima.", "success")

        # ---- RODAPÉ ----
        APP_VERSION = "1.0.0"

        footer_frame = tk.Frame(self, bg="#f5f5f5", relief="ridge", borderwidth=1)
        footer_frame.pack(side="bottom", fill="x")

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

        self.status_label.config(text=text, foreground=color)
        self.update_idletasks()

    def disable_buttons(self):
        """Desabilita todos os botões durante operações"""
        buttons = [self.btn_backup, self.btn_restore, self.btn_verify, self.btn_kill, self.btn_config]
        for btn in buttons:
            btn.state(["disabled"])

    def enable_buttons(self):
        """Reabilita todos os botões"""
        buttons = [self.btn_backup, self.btn_restore, self.btn_verify, self.btn_kill, self.btn_config]
        for btn in buttons:
            btn.state(["!disabled"])

    # ---------- EXECUÇÃO ----------
    def run_command(self, cmd, on_finish=None):
        import threading

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

    # ---------- AÇÕES ----------
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
            "-se", f"{self.conf.get('firebird_host', 'localhost')}:service_mgr",
            db, 
            str(backup_path), 
            "-user", self.conf.get("firebird_user", "SYSDBA"), 
            "-pass", self.conf.get("firebird_password", "masterkey")
        ]

        self.log(f"🟦 Iniciando backup: {db} -> {backup_path}", "info")
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
        
        # Extrai se for arquivo ZIP
        if bkp.lower().endswith(".zip"):
            try:
                tmpdir = Path(tempfile.mkdtemp(prefix="fb_restore_"))
                self.log(f"Extraindo arquivo ZIP para: {tmpdir}", "info")
                
                with zipfile.ZipFile(bkp, "r") as z:
                    z.extractall(tmpdir)
                
                fbks = list(tmpdir.glob("*.fbk"))
                if not fbks:
                    messagebox.showerror("Erro", "Nenhum arquivo .fbk encontrado dentro do ZIP.")
                    if tmpdir:
                        shutil.rmtree(tmpdir, ignore_errors=True)
                    return
                
                actual_backup = str(fbks[0])
                self.log(f"Arquivo extraído: {actual_backup}", "success")
                
            except Exception as e:
                messagebox.showerror("Erro", f"Falha ao extrair arquivo ZIP: {e}")
                if tmpdir:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                return

        dest = filedialog.asksaveasfilename(
            title="Salvar banco restaurado como...",
            defaultextension=".fdb",
            filetypes=[("Firebird Database", "*.fdb")]
        )
        if not dest:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)
            return

        # Constrói comando gbak restauração
        cmd = [
            gbak, "-c", 
            "-se", f"{self.conf.get('firebird_host', 'localhost')}:service_mgr",
            actual_backup, 
            dest, 
            "-user", self.conf.get("firebird_user", "SYSDBA"), 
            "-pass", self.conf.get("firebird_password", "masterkey"),
            "-rep"
        ]

        self.log(f"🟦 Restaurando backup: {actual_backup} -> {dest}", "info")
        self.set_status("Restaurando banco, aguarde...", "blue")

        def cleanup_tmp():
            if tmpdir:
                try:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                    self.log("🗑️ Arquivos temporários removidos.", "info")
                except Exception as e:
                    self.log(f"⚠️ Erro ao remover temporários: {e}", "warning")

        self.run_command(cmd, on_finish=cleanup_tmp)

    def verify(self):
        """Verifica integridade do banco de dados"""
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
        self.run_command(cmd)

    def kill(self):
        """Finaliza processos do Firebird"""
        self.log("🚫 Iniciando finalização de processos do Firebird...", "warning")
        self.set_status("Finalizando processos do Firebird...", "orange")
        
        def kill_processes():
            success = kill_firebird_processes()
            self.after(0, lambda: self._on_kill_complete(success))
        
        threading.Thread(target=kill_processes, daemon=True).start()

    def _on_kill_complete(self, success):

        if success:
            self.set_status("✅ Processos do Firebird finalizados!", "green")
            self.log("✅ Todos os processos do Firebird foram finalizados com sucesso.", "success")
        else:
            self.set_status("ℹ️ Nenhum processo do Firebird encontrado ou erro ao finalizar.", "blue")
            self.log("ℹ️ Nenhum processo do Firebird em execução ou erro ao finalizar.", "info")

    # ---------- JANELA DE CONFIG ----------
    def config_window(self):
        
        win = tk.Toplevel(self)
        win.title("⚙️ Configurações do Sistema")
        win.geometry("500x400")
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()

        main_frame = ttk.Frame(win, padding=20)
        main_frame.pack(fill="both", expand=True)

        # Configurações de caminho
        ttk.Label(main_frame, text="Local do gbak.exe:").grid(row=0, column=0, sticky="w", pady=8)
        gbak_var = tk.StringVar(value=self.conf.get("gbak_path", ""))
        gbak_entry = ttk.Entry(main_frame, textvariable=gbak_var, width=40)
        gbak_entry.grid(row=0, column=1, padx=5)
        ttk.Button(main_frame, text="...", width=3, 
                  command=lambda: self.pick_exe(gbak_var, "gbak.exe")).grid(row=0, column=2)

        ttk.Label(main_frame, text="Local do gfix.exe:").grid(row=1, column=0, sticky="w", pady=8)
        gfix_var = tk.StringVar(value=self.conf.get("gfix_path", ""))
        gfix_entry = ttk.Entry(main_frame, textvariable=gfix_var, width=40)
        gfix_entry.grid(row=1, column=1, padx=5)
        ttk.Button(main_frame, text="...", width=3,
                  command=lambda: self.pick_exe(gfix_var, "gfix.exe")).grid(row=1, column=2)

        ttk.Label(main_frame, text="Pasta de backups:").grid(row=2, column=0, sticky="w", pady=8)
        backup_var = tk.StringVar(value=self.conf.get("backup_dir", ""))
        backup_entry = ttk.Entry(main_frame, textvariable=backup_var, width=40)
        backup_entry.grid(row=2, column=1, padx=5)
        ttk.Button(main_frame, text="...", width=3,
                  command=lambda: self.pick_dir(backup_var)).grid(row=2, column=2)

        # Configurações do Firebird
        ttk.Label(main_frame, text="Host do Firebird:").grid(row=3, column=0, sticky="w", pady=8)
        host_var = tk.StringVar(value=self.conf.get("firebird_host", "localhost"))
        ttk.Entry(main_frame, textvariable=host_var, width=40).grid(row=3, column=1, padx=5)

        ttk.Label(main_frame, text="Usuário:").grid(row=4, column=0, sticky="w", pady=8)
        user_var = tk.StringVar(value=self.conf.get("firebird_user", "SYSDBA"))
        ttk.Entry(main_frame, textvariable=user_var, width=40).grid(row=4, column=1, padx=5)

        ttk.Label(main_frame, text="Senha:").grid(row=5, column=0, sticky="w", pady=8)
        pass_var = tk.StringVar(value=self.conf.get("firebird_password", "masterkey"))
        ttk.Entry(main_frame, textvariable=pass_var, width=40, show="*").grid(row=5, column=1, padx=5)

        # Configurações de backup
        ttk.Label(main_frame, text="Qtd. backups a manter:").grid(row=6, column=0, sticky="w", pady=8)
        keep_var = tk.IntVar(value=self.conf.get("keep_backups", DEFAULT_KEEP_BACKUPS))
        ttk.Spinbox(main_frame, from_=1, to=100, textvariable=keep_var, width=10).grid(row=6, column=1, sticky="w", padx=5)

        # Botões
        btn_frame = ttk.Frame(main_frame)
        btn_frame.grid(row=7, column=0, columnspan=3, pady=20)

        ttk.Button(btn_frame, text="Salvar Configurações", 
                  command=lambda: self.save_config_from_window(
                      win, gbak_var, gfix_var, backup_var, host_var, user_var, pass_var, keep_var
                  )).pack(side="left", padx=10)
        
        ttk.Button(btn_frame, text="Cancelar", 
                  command=win.destroy).pack(side="left", padx=10)

    def save_config_from_window(self, win, gbak_var, gfix_var, backup_var, host_var, user_var, pass_var, keep_var):
        self.conf.update({
            "gbak_path": gbak_var.get(),
            "gfix_path": gfix_var.get(),
            "backup_dir": backup_var.get(),
            "firebird_host": host_var.get(),
            "firebird_user": user_var.get(),
            "firebird_password": pass_var.get(),
            "keep_backups": keep_var.get()
        })
        
        save_config(self.conf)
        messagebox.showinfo("Configurações", "Configurações salvas com sucesso!")
        win.destroy()

    def pick_exe(self, var, exe_name):
        path = filedialog.askopenfilename(
            title=f"Selecione {exe_name}", 
            filetypes=[("Executável", "*.exe"), ("Todos os arquivos", "*.*")]
        )
        if path:
            var.set(path)

    def pick_dir(self, var):
        path = filedialog.askdirectory(title="Selecione diretório de backups")
        if path:
            var.set(path)

# ---------- MAIN ----------
if __name__ == "__main__":
    try:
        # Solicitar modo administrador se necessário
        if not is_admin():
            response = messagebox.askyesno(
                "Permissão de Administrador",
                "Este programa requer permissões de administrador para \n"
                "gerenciar processos do Firebird.\n\n"
                "Deseja executar como administrador?",
                icon=messagebox.WARNING
            )
            if response:
                run_as_admin()
            else:
                messagebox.showinfo(
                    "Informação",
                    "Algumas funcionalidades podem não funcionar \n"
                    "sem permissões de administrador."
                )
        
        app = FirebirdManagerApp()
        app.mainloop()
        
    except Exception as e:
        print(f"Erro fatal: {e}")
        messagebox.showerror("Erro Fatal", f"Falha ao iniciar aplicação:\n{e}")