import sys
import os
import shutil
import stat
sys.path.insert(0, os.path.dirname(__file__))
import json
import subprocess
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QHBoxLayout,
    QWidget, QTextEdit, QPushButton, QLineEdit, QFileDialog, QLabel, QMessageBox, QCheckBox, QTabWidget
)
from PyQt6.QtGui import QPalette, QColor
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from gemini_api_client import GeminiAPIClient
from release_worker import ReleaseWorker

class Worker(QThread):
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal()
    error_signal = pyqtSignal(str)

    def __init__(self, project_path, repo_name, use_llm):
        super().__init__()
        self.project_path = project_path
        self.repo_name = repo_name
        self.use_llm = use_llm
        self.gemini_client = GeminiAPIClient(model_name="qwen3-coder:30b")

    def _remove_readonly(self, func, path, exc_info):
        import stat
        # exc_info contains (type, value, traceback)
        if issubclass(exc_info[0], PermissionError):
            try:
                self.log_signal.emit(f"Попытка изменить разрешения файла: {path}")
                os.chmod(path, stat.S_IWRITE)
                func(path)
            except Exception as e:
                self.log_signal.emit(f"Не удалось изменить разрешения и удалить файл {path}: {e}")
                raise e
        else:
            raise exc_info[1]

    def _run_command(self, command, cwd=None):
        if cwd is None:
            cwd = self.project_path
        self.log_signal.emit(f"Выполнение команды: {command} в {cwd}")
        process = subprocess.run(command, cwd=cwd, shell=True, capture_output=True, text=True, encoding='utf-8')
        if process.stdout:
            self.log_signal.emit(f"Stdout: {process.stdout.strip()}")
        if process.stderr:
            self.log_signal.emit(f"Stderr: {process.stderr.strip()}")
        if process.returncode != 0:
            raise Exception(f"Команда завершилась с ошибкой (код {process.returncode}): {command}")
        return process.stdout

    def run(self):
        try:
            self.log_signal.emit(f"Рабочий поток запущен для публикации проекта '{self.project_path}' в репозиторий '{self.repo_name}'.")

            # Шаг 6: Анализ проекта и генерация README.md
            self.log_signal.emit("Анализ проекта и генерация README.md...")
            project_info = self.analyze_project()
            readme_content = self.generate_readme_content(project_info)
            readme_path = os.path.join(self.project_path, "README.md")
            with open(readme_path, "w", encoding="utf-8") as f:
                f.write(readme_content)
            self.log_signal.emit(f"Файл README.md сгенерирован и сохранен: {readme_path}")

            # Шаг 7: Git-операции будут выполнены командой gh repo create --source=. --push
            self.log_signal.emit("Git-операции (init, add, commit) будут выполнены GitHub CLI.")

            # Проверка и удаление существующего .git репозитория
            git_folder_path = os.path.join(self.project_path, ".git")
            if os.path.exists(git_folder_path):
                self.log_signal.emit(f"Обнаружен существующий Git репозиторий в {git_folder_path}. Попытка удалить его для чистой инициализации.")
                try:
                    shutil.rmtree(git_folder_path, onerror=self._remove_readonly)
                    self.log_signal.emit("Существующий Git репозиторий удален.")
                except Exception as e:
                    self.log_signal.emit(f"Ошибка при удалении существующего Git репозитория: {e}. Возможно, некоторые файлы заблокированы.")
                    raise # Re-raise the exception to stop the process if deletion fails

            # Инициализация нового Git репозитория
            self.log_signal.emit("Инициализация нового Git репозитория...")
            self._run_command("git init")
            
            # Добавление файлов и создание первого коммита
            self.log_signal.emit("Добавление файлов и создание первого коммита...")
            self._run_command("git add .")
            self._run_command('git commit -m "Initial commit"')
            
            # Шаг 8: Проверка существования и создание/настройка репозитория
            self.log_signal.emit(f"Проверка репозитория '{self.repo_name}' на GitHub...")
            try:
                # Попытка получить информацию о репозитории. Если команда падает, репозитория нет.
                self._run_command(f"gh repo view {self.repo_name}")
                self.log_signal.emit(f"Репозиторий '{self.repo_name}' уже существует. Настраиваю remote...")
                
                # Получаем URL существующего репозитория
                repo_url = self._run_command(f"gh repo view {self.repo_name} --json url -q .url").strip()

                # Удаляем старый origin, если он есть, и добавляем новый
                try:
                    self._run_command("git remote remove origin")
                except Exception:
                    # Игнорируем ошибку, если remote origin не существует
                    pass
                self._run_command(f"git remote add origin {repo_url}")
                
                # Пушим изменения в существующий репозиторий
                # Используем -f (force), так как мы всегда начинаем с чистого листа локально.
                # Это перезапишет историю на удаленном репозитории.
                self.log_signal.emit("Отправка коммитов в существующий репозиторий (с перезаписью)...")
                self._run_command("git push --force --set-upstream origin master")
                self.log_signal.emit("Проект успешно синхронизирован с существующим репозиторием на GitHub.")

            except Exception as e:
                # Если `gh repo view` упал, значит репозитория нет. Создаем его.
                self.log_signal.emit(f"Репозиторий '{self.repo_name}' не найден. Создаю новый...")
                self._run_command(f"gh repo create {self.repo_name} --private --source=. --push")
                self.log_signal.emit(f"Приватный репозиторий '{self.repo_name}' успешно создан на GitHub и проект загружен.")

            self.log_signal.emit("Операция завершена.")
            self.finished_signal.emit()
        except Exception as e:
            self.error_signal.emit(f"Произошла ошибка в рабочем потоке: {e}")

    def analyze_project(self):
        project_info = {
            "name": self.repo_name,
            "type": "Неизвестно",
            "os_specific": [],  # Для определения целевой ОС
            "description": "Автоматически сгенерированный проект.",
            "main_files": [],
            "dependencies": [],
            "technologies": set(),  # Для сбора используемых технологий
            "entry_point": None,
            "has_tests": False,
            "license": "Не указано",
            "existing_readme": None
        }

        # Чтение существующего README.md
        try:
            readme_path = os.path.join(self.project_path, "README.md")
            if os.path.exists(readme_path):
                self.log_signal.emit("Обнаружен существующий README.md. Читаю его содержимое...")
                with open(readme_path, "r", encoding="utf-8") as f:
                    project_info["existing_readme"] = f.read()
        except Exception as e:
            self.log_signal.emit(f"Ошибка при чтении существующего README.md: {e}")

        # Проверка лицензии
        license_files = ["LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "COPYING.md"]
        for license_file in license_files:
            if os.path.exists(os.path.join(self.project_path, license_file)):
                project_info["license"] = license_file
                break

        # Проверка на наличие тестов
        test_dirs = ["tests", "test", "__tests__"]
        for test_dir in test_dirs:
            if os.path.exists(os.path.join(self.project_path, test_dir)):
                project_info["has_tests"] = True
                break

        # Проверка на наличие ключевых файлов для определения типа проекта и целевой ОС
        
        # Проверка на Windows-приложения (C# проекты)
        csproj_files = [f for f in os.listdir(self.project_path) if f.endswith(".csproj")]
        if csproj_files:
            self.log_signal.emit(f"Обнаружен C# проект ({csproj_files[0]}). Устанавливаю целевую ОС как Windows.")
            project_info["type"] = "C# (.NET)"
            project_info["technologies"].add("C#")
            project_info["technologies"].add(".NET")
            project_info["os_specific"] = ["Windows"]  # C# проекты по умолчанию для Windows
            # Попробуем извлечь немного информации из .csproj файла
            try:
                csproj_path = os.path.join(self.project_path, csproj_files[0])
                with open(csproj_path, "r", encoding="utf-8") as f:
                    content = f.read()
                    # Простой поиск тегов (в реальном проекте лучше использовать XML-парсер)
                    if "<Description>" in content:
                        start = content.find("<Description>") + len("<Description>")
                        end = content.find("</Description>")
                        if start > -1 and end > -1:
                            project_info["description"] = content[start:end].strip()
            except Exception as e:
                self.log_signal.emit(f"Ошибка при чтении .csproj файла: {e}")
        elif os.path.exists(os.path.join(self.project_path, "package.json")):
            self.log_signal.emit("Обнаружен package.json. Анализирую JavaScript/Node.js проект...")
            project_info["type"] = "JavaScript/Node.js"
            project_info["technologies"].add("JavaScript")
            project_info["technologies"].add("Node.js")
            
            # Проверка на Electron или другие специфические фреймворки
            try:
                with open(os.path.join(self.project_path, "package.json"), "r", encoding="utf-8") as f:
                    package_json = json.load(f)
                    if "description" in package_json: 
                        project_info["description"] = package_json["description"]
                    if "dependencies" in package_json: 
                        project_info["dependencies"] = list(package_json["dependencies"].keys())
                        # Определение типа приложения по зависимостям
                        if "electron" in package_json["dependencies"]:
                            project_info["type"] = "Electron Desktop App"
                            project_info["os_specific"] = ["Windows", "macOS", "Linux"]
                            self.log_signal.emit("Определен как Electron приложение по зависимостям")
                        elif "react" in package_json["dependencies"]:
                            project_info["type"] = "React Web App"
                            self.log_signal.emit("Определен как React приложение по зависимостям")
                        elif "@angular/core" in package_json["dependencies"]:
                            project_info["type"] = "Angular Web App"
                            self.log_signal.emit("Определен как Angular приложение по зависимостям")
                        elif "vue" in package_json["dependencies"]:
                            project_info["type"] = "Vue.js Web App"
                            self.log_signal.emit("Определен как Vue.js приложение по зависимостям")
                    if "devDependencies" in package_json: 
                        project_info["dependencies"].extend(list(package_json["devDependencies"].keys()))
                        if "electron" in package_json["devDependencies"]:
                            project_info["type"] = "Electron Desktop App"
                            project_info["os_specific"] = ["Windows", "macOS", "Linux"]
                            self.log_signal.emit("Определен как Electron приложение по devDependencies")
                        elif "@angular/core" in package_json["devDependencies"]:
                            project_info["type"] = "Angular Web App"
                            self.log_signal.emit("Определен как Angular приложение по devDependencies")
                        elif "vue" in package_json["devDependencies"]:
                            project_info["type"] = "Vue.js Web App"
                            self.log_signal.emit("Определен как Vue.js приложение по devDependencies")
                            
                    # Проверка скриптов для определения Electron
                    if "scripts" in package_json:
                        scripts = package_json["scripts"]
                        for script_name, script in scripts.items():
                            if "electron" in script:
                                project_info["type"] = "Electron Desktop App"
                                project_info["os_specific"] = ["Windows", "macOS", "Linux"]
                                self.log_signal.emit(f"Определен как Electron приложение по скрипту '{script_name}': {script}")
                                break
                                
            except Exception as e:
                self.log_signal.emit(f"Ошибка при чтении package.json: {e}")
                
        elif os.path.exists(os.path.join(self.project_path, "requirements.txt")):
            self.log_signal.emit("Обнаружен requirements.txt. Анализирую Python проект...")
            project_info["type"] = "Python"
            project_info["technologies"].add("Python")
            project_info["os_specific"] = ["Windows", "macOS", "Linux"]  # Python кроссплатформенный
            try:
                with open(os.path.join(self.project_path, "requirements.txt"), "r", encoding="utf-8") as f:
                    project_info["dependencies"] = [line.strip() for line in f if line.strip() and not line.startswith('#')]
            except Exception as e:
                self.log_signal.emit(f"Ошибка при чтении requirements.txt: {e}")
                
        elif os.path.exists(os.path.join(self.project_path, "pom.xml")):
            self.log_signal.emit("Обнаружен pom.xml. Анализирую Java проект...")
            project_info["type"] = "Java"
            project_info["technologies"].add("Java")
            project_info["os_specific"] = ["Windows", "macOS", "Linux"]  # Java кроссплатформенный
            # Попробуем извлечь зависимости из pom.xml (упрощенный парсинг)
            try:
                with open(os.path.join(self.project_path, "pom.xml"), "r", encoding="utf-8") as f:
                    content = f.read()
                    # Простой поиск тегов dependencies (в реальном проекте лучше использовать XML-парсер)
                    if "<dependencies>" in content:
                        project_info["dependencies"] = ["Зависимости определены в pom.xml"]
            except Exception as e:
                self.log_signal.emit(f"Ошибка при чтении pom.xml: {e}")
                
        elif os.path.exists(os.path.join(self.project_path, "CMakeLists.txt")):
            self.log_signal.emit("Обнаружен CMakeLists.txt. Анализирую C++ проект...")
            project_info["type"] = "C++"
            project_info["technologies"].add("C++")
            project_info["technologies"].add("CMake")
            project_info["os_specific"] = ["Windows", "macOS", "Linux"]  # CMake кроссплатформенный
            # CMake не содержит информации о зависимостях в самом файле, 
            # они могут быть в других файлах или устанавливаться отдельно
            
        elif os.path.exists(os.path.join(self.project_path, "index.html")) or \
             os.path.exists(os.path.join(self.project_path, "main.html")):
            self.log_signal.emit("Обнаружен HTML файл. Анализирую веб-проект...")
            project_info["type"] = "HTML/CSS/JS"
            project_info["technologies"].add("HTML")
            project_info["technologies"].add("CSS")
            project_info["technologies"].add("JavaScript")
            project_info["os_specific"] = ["Windows", "macOS", "Linux"]  # Веб-приложения кроссплатформенные

        # Если не определена целевая ОС, используем универсальные инструкции
        if not project_info["os_specific"]:
            project_info["os_specific"] = ["Windows", "macOS", "Linux"]  # По умолчанию для кроссплатформенных приложений
            self.log_signal.emit("Целевая ОС не определена. Использую кроссплатформенные инструкции.")

        # Сбор списка основных файлов и технологий
        python_entry_points = ["main.py", "app.py", "run.py", "start.py", "manage.py"]
        js_entry_points = ["index.js", "main.js", "server.js", "app.js"]
        java_entry_points = ["Main.java", "App.java"]
        cpp_entry_points = ["main.cpp", "main.cc"]
        
        self.log_signal.emit("Сканирую файлы проекта...")
        for root, _, files in os.walk(self.project_path):
            for file in files:
                relative_path = os.path.relpath(os.path.join(root, file), self.project_path)
                if any(ext in file for ext in [".py", ".js", ".html", ".css", ".json", ".md", ".java", ".cpp", ".cc", ".h"]):
                    project_info["main_files"].append(relative_path)
                if file.endswith(".py"): 
                    project_info["technologies"].add("Python")
                    # Поиск точки входа
                    if file in python_entry_points and not project_info["entry_point"]:
                        project_info["entry_point"] = relative_path
                if file.endswith(".js"): 
                    project_info["technologies"].add("JavaScript")
                    # Поиск точки входа
                    if file in js_entry_points and not project_info["entry_point"]:
                        project_info["entry_point"] = relative_path
                if file.endswith(".html"): 
                    project_info["technologies"].add("HTML")
                if file.endswith(".css"): 
                    project_info["technologies"].add("CSS")
                if file.endswith(".java"): 
                    project_info["technologies"].add("Java")
                    # Поиск точки входа
                    if file in java_entry_points and not project_info["entry_point"]:
                        project_info["entry_point"] = relative_path
                if file.endswith((".cpp", ".cc")): 
                    project_info["technologies"].add("C++")
                    # Поиск точки входа
                    if file in cpp_entry_points and not project_info["entry_point"]:
                        project_info["entry_point"] = relative_path
                if file.endswith(".h"): 
                    project_info["technologies"].add("C++")
                    
                # Проверка на специфичные файлы для ОС
                if file.endswith((".exe", ".msi")) and "Windows" not in project_info["os_specific"]:
                    project_info["os_specific"].append("Windows")
                if file.endswith(".app") and "macOS" not in project_info["os_specific"]:
                    project_info["os_specific"].append("macOS")
                if file.endswith((".deb", ".rpm", ".sh")) and "Linux" not in project_info["os_specific"]:
                    project_info["os_specific"].append("Linux")
                    
        project_info["technologies"] = list(project_info["technologies"])
        self.log_signal.emit(f"Анализ завершен. Тип проекта: {project_info['type']}, Целевые ОС: {project_info['os_specific']}")
        return project_info

    def generate_readme_content(self, project_info):
        llm_description = None
        if self.use_llm:
            self.log_signal.emit("Генерация README.md с помощью LLM...")
            prompt = self._construct_llm_prompt(project_info)
            try:
                llm_description = self.gemini_client.generate_readme_description(prompt)
            except Exception as e:
                self.log_signal.emit(f"Ошибка при генерации описания с помощью LLM: {e}")
                self.log_signal.emit("Используется описание по умолчанию.")
        else:
            self.log_signal.emit("Генерация README.md без использования LLM.")

        # Создаем структурированный README.md
        if llm_description:
            # Используем сгенерированное описание от LLM, предполагая, что оно содержит все необходимое.
            content = f"{llm_description}\n\n"
        else:
            # Создаем базовую структуру вручную
            content = f"# {project_info['name']}\n\n"
            content += f"{project_info['description']}\n\n"             
            # Краткое описание
            content += "## 📌 Краткое описание\n\n"
            content += f"{project_info['description']}\n\n"
            
            # Особенности
            content += "## 🚀 Особенности\n\n"
            # Добавляем информацию о поддерживаемых ОС
            if project_info['os_specific']:
                if project_info['os_specific'] == ["Windows"]:
                    content += "- Работает на Windows\n"
                else:
                    os_list = ", ".join(project_info['os_specific'])
                    content += f"- Работает на {os_list}\n"
            content += "- Современный пользовательский интерфейс\n"
            content += "- Интуитивно понятное управление\n\n"
            
            # Технологии
            if project_info['technologies']:
                content += "## 🛠️ Используемые технологии\n\n"
                for tech in project_info['technologies']:
                    content += f"- {tech}\n"
                content += "\n"
            
            # Зависимости
            if project_info['dependencies']:
                content += "## 📦 Зависимости\n\n"
                for dep in project_info['dependencies'][:10]:  # Ограничим до 10 зависимостей
                    content += f"- {dep}\n"
                if len(project_info['dependencies']) > 10:
                    content += "- ...\n"
                content += "\n"

            # Предварительная настройка
            content += "## ⚙️ Предварительная настройка\n\n"
            content += "Перед первым использованием убедитесь, что у вас установлен [GitHub CLI](https://cli.github.com/) и вы авторизованы.\n\n"
            content += "Для корректной работы функции создания релизов может потребоваться расширить права доступа. Выполните в терминале команду:\n"
            content += "```bash\n"
            content += "gh auth refresh -h github.com -s workflow\n"
            content += "```\n\n"
            
            # Установка
            content += "## 💾 Установка\n\n"
            if "Python" in project_info['technologies']:
                content += "```bash\n"
                content += "pip install -r requirements.txt\n"
                content += "```\n\n"
            elif "Node.js" in project_info['technologies'] or "JavaScript" in project_info['technologies']:
                content += "```bash\n"
                content += "npm install\n"
                content += "```\n\n"
            elif "Java" in project_info['technologies']:
                content += "```bash\n"
                content += "mvn install\n"
                content += "```\n\n"
            else:
                content += "Следуйте инструкциям в разделе 'Запуск'.\n\n"
            
            # Запуск
            content += "## ▶️ Запуск\n\n"
            if project_info["entry_point"]:
                if "Python" in project_info['technologies']:
                    content += f"```bash\npython {project_info['entry_point']}\n```\n\n"
                elif "JavaScript" in project_info['technologies']:
                    content += f"```bash\nnode {project_info['entry_point']}\n```\n\n"
                elif "Java" in project_info['technologies']:
                    # Для Java сложнее определить точную команду запуска
                    content += "```bash\n"
                    content += "javac *.java\njava Main\n"
                    content += "```\n\n"
                else:
                    content += f"Запустите файл: {project_info['entry_point']}\n\n"
            else:
                if "Electron" in project_info['type']:
                    content += "```bash\nnpm start\n```\n\n"
                elif "Python" in project_info['technologies']:
                    content += "```bash\npython main.py\n```\n\n"
                elif "Node.js" in project_info['technologies']:
                    content += "```bash\nnpm start\n```\n\n"
                else:
                    content += "Следуйте инструкциям в разделе 'Использование'.\n\n"
            
            # Лицензия
            if project_info['license'] != "Не указано":
                content += f"## 📄 Лицензия\n\n"
                content += f"Этот проект лицензирован под {project_info['license']}.\n\n"
        
        # Убираем автоматическую надпись о генерации
        content += "\n"
        return content

    def _construct_llm_prompt(self, project_info):
        prompt = f"Сгенерируй креативное и привлекательное описание для проекта GitHub с названием '{project_info['name']}'. "

        if project_info.get("existing_readme"):
            prompt += ("Используй следующий текст из существующего файла README.md как ОСНОВУ для создания нового описания. " \
                      "Твоя задача — взять эту информацию, структурировать ее, улучшить и оформить в красивый README.md для GitHub. " \
                      "Особенно обрати внимание на любые упоминания требований, таких как серверы или специфические зависимости (например, Ollama). " \
                      f"Вот содержимое существующего README.md:\n\n'''\n{project_info['existing_readme']}\n'''\n\n")
        else:
            prompt += f"Тип проекта: {project_info['type']}. "
            if project_info['technologies']:
                prompt += f"Используемые технологии: {', '.join(project_info['technologies'])}. "
            if project_info['dependencies']:
                prompt += f"Основные зависимости: {', '.join(project_info['dependencies'][:10])}. "
            
        # Информация о целевой ОС
        if project_info['os_specific']:
            # Если проект работает только на Windows, не упоминаем macOS и Linux
            if project_info['os_specific'] == ["Windows"]:
                prompt += f"Целевая операционная система: Windows. "
            else:
                os_info = ', '.join(project_info['os_specific'])
                prompt += f"Целевые операционные системы: {os_info}. "
            
        prompt += ("Создай структурированное описание в формате README.md для GitHub репозитория. " \
                  "Включи раздел '## ⚙️ Предварительная настройка' ПЕРЕД разделом 'Установка'. В этом разделе укажи, что пользователю нужно установить GitHub CLI и выполнить команду `gh auth refresh -h github.com -s workflow` для предоставления прав на создание релизов. " \
                  "Включи следующие разделы (каждый раздел начинается с ##): " \
                  "1. Краткое описание проекта (2-3 предложения с эмодзи) " \
                  "2. Особенности (список 3-5 ключевых особенностей с эмодзи, НЕ указывай информацию о поддерживаемых операционных системах, НЕ используй слова 'кроссплатформенный', НЕ упоминай macOS и Linux) " \
                  "3. Технологии (список используемых технологий) " \
                  "4. Зависимости (список основных зависимостей, если есть) " \
                  "5. Установка (общие инструкции по установке) " \
                  "6. Запуск (общие инструкции по запуску) " \
                  "7. Использование (примеры использования, если возможно определить) " \
                  "8. Конфигурация (информация о настройке, если применимо) " \
                  "9. Разработка (инструкции для разработчиков, если проект имеет тесты) " \
                  "НЕ используй markdown для заголовков, просто начинай каждый раздел с ##. " \
                  "НЕ дублируй название проекта в описании. " \
                  "Используй эмодзи для украшения текста. " \
                  "Сделай описание информативным и привлекательным. " \
                  "Пиши от лица частного разработчика, избегай официального тона речи (например, фраз типа 'Представляем вам!'). " \
                  "НЕ упоминай лицензию, если она не определена. " \
                  "НЕ упоминай macOS и Linux, если проект работает только на Windows.")
        return prompt

class GitHubPublisherApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GitHub Publisher")
        self.setGeometry(100, 100, 800, 600) # x, y, width, height

        self.init_ui()
        self.apply_dark_theme()

    def init_ui(self):
        # Central widget and layout
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QVBoxLayout(self.central_widget)

        # Log display (now at the top/middle)
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.main_layout.addWidget(self.log_output)

        # --- Control Panel (at the bottom) ---
        self.control_panel_widget = QWidget()
        self.control_panel_layout = QVBoxLayout(self.control_panel_widget) # Use QVBoxLayout for controls

        # Project Folder Selection
        self.project_folder_layout = QHBoxLayout()
        self.project_path_input = QLineEdit()
        self.project_path_input.setPlaceholderText("Выберите папку проекта...")
        self.project_path_input.setReadOnly(True)
        self.project_folder_layout.addWidget(self.project_path_input)

        self.select_folder_button = QPushButton("Выбрать папку проекта")
        self.select_folder_button.clicked.connect(self.select_project_folder)
        self.project_folder_layout.addWidget(self.select_folder_button)
        self.control_panel_layout.addLayout(self.project_folder_layout) # Add to control panel layout

        # Repository Name Input
        self.repo_name_layout = QHBoxLayout()
        self.repo_name_label = QLabel("Имя репозитория GitHub:")
        self.repo_name_input = QLineEdit()
        self.repo_name_input.setPlaceholderText("Например: my-awesome-project")
        self.repo_name_layout.addWidget(self.repo_name_label)
        self.repo_name_layout.addWidget(self.repo_name_input)
        self.control_panel_layout.addLayout(self.repo_name_layout)

        # Screenshot Selection
        self.screenshot_layout = QHBoxLayout()
        self.screenshot_path_input = QLineEdit()
        self.screenshot_path_input.setPlaceholderText("Выберите скриншот программы...")
        self.screenshot_path_input.setReadOnly(True)
        self.screenshot_layout.addWidget(self.screenshot_path_input)

        self.select_screenshot_button = QPushButton("Выбрать скриншот")
        self.select_screenshot_button.clicked.connect(self.select_screenshot)
        self.screenshot_layout.addWidget(self.select_screenshot_button)
        self.control_panel_layout.addLayout(self.screenshot_layout)

        # LLM Toggle
        self.use_llm_checkbox = QCheckBox("Использовать ИИ для генерации README.md")
        self.use_llm_checkbox.setChecked(True) # По умолчанию включено
        self.control_panel_layout.addWidget(self.use_llm_checkbox)
        
        # Auto Release Toggle
        self.auto_release_checkbox = QCheckBox("Автоматически создавать релиз после публикации")
        self.auto_release_checkbox.setChecked(True) # По умолчанию включено
        self.control_panel_layout.addWidget(self.auto_release_checkbox)

        # Buttons layout
        self.buttons_layout = QHBoxLayout()
        
        # Publish Button
        self.publish_button = QPushButton("Опубликовать на GitHub")
        self.publish_button.clicked.connect(self.publish_project)
        self.buttons_layout.addWidget(self.publish_button)

        # Release Button
        self.release_button = QPushButton("Создать релиз")
        self.release_button.clicked.connect(self.create_release)
        self.release_button.setEnabled(False)  # Отключаем до публикации проекта
        self.buttons_layout.addWidget(self.release_button)

        self.control_panel_layout.addLayout(self.buttons_layout)

        # Add the control panel to the main layout (at the bottom)
        self.main_layout.addWidget(self.control_panel_widget)

        self.log_message("Приложение GitHub Publisher запущено.")
        self.selected_screenshot = None

    def apply_dark_theme(self):
        app.setStyle("Fusion")
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(255, 255, 255))
        palette.setColor(QPalette.ColorRole.Base, QColor(25, 25, 25))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
        palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(255, 255, 255))
        palette.setColor(QPalette.ColorRole.ToolTipText, QColor(255, 255, 255))
        palette.setColor(QPalette.ColorRole.Text, QColor(255, 255, 255))
        palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(255, 255, 255))
        palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 0, 0))
        palette.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(0, 0, 0))
        app.setPalette(palette)

    def log_message(self, message):
        self.log_output.append(message)

    def select_project_folder(self):
        folder_path = QFileDialog.getExistingDirectory(self, "Выберите папку проекта")
        if folder_path:
            self.project_path_input.setText(folder_path)
            self.log_message(f"Выбрана папка проекта: {folder_path}")
            repo_name = os.path.basename(folder_path)
            self.repo_name_input.setText(repo_name)
            self.log_message(f"Предложено имя репозитория: {repo_name}")

    def select_screenshot(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Выберите скриншот программы", "", "Изображения (*.png *.jpg *.jpeg *.gif *.bmp)")
        if file_path:
            self.screenshot_path_input.setText(file_path)
            self.selected_screenshot = file_path
            self.log_message(f"Выбран скриншот: {file_path}")

    def publish_project(self):
        project_path = self.project_path_input.text()
        repo_name = self.repo_name_input.text()

        if not project_path:
            self.log_message("Ошибка: Папка проекта не выбрана.")
            return
        if not repo_name:
            self.log_message("Ошибка: Имя репозитория не указано.")
            return

        self.log_message(f"Начинаю публикацию проекта '{project_path}' в репозиторий '{repo_name}' на GitHub...")
        self.publish_button.setEnabled(False) # Отключаем кнопку на время выполнения

        use_llm = self.use_llm_checkbox.isChecked()
        self.worker = Worker(project_path, repo_name, use_llm)
        self.worker.log_signal.connect(self.log_message)
        self.worker.finished_signal.connect(self.on_publish_finished)
        self.worker.error_signal.connect(self.on_publish_error)
        self.worker.start()

    def on_publish_finished(self):
        self.publish_button.setEnabled(True) # Включаем кнопку обратно
        self.release_button.setEnabled(True)  # Включаем кнопку релиза
        self.log_message("Процесс публикации завершен. Кнопка 'Создать релиз' должна быть активна.")
        self.log_message(f"Состояние кнопки 'Создать релиз': {self.release_button.isEnabled()}")
        
        # Проверяем, нужно ли автоматически создавать релиз
        if self.auto_release_checkbox.isChecked():
            self.log_message("Автоматическое создание релиза...")
            self.create_release_automatically()
        else:
            QMessageBox.information(self, "Публикация завершена", "Проект успешно опубликован на GitHub!")

    def on_publish_error(self, message):
        self.publish_button.setEnabled(True) # Включаем кнопку обратно
        self.log_message(f"ОШИБКА: {message}")
        QMessageBox.critical(self, "Ошибка публикации", f"Произошла ошибка во время публикации: {message}")

    def create_release_automatically(self):
        """Автоматически создает релиз с использованием ИИ"""
        self.log_message("Начало автоматического создания релиза")
        project_path = self.project_path_input.text()
        repo_name = self.repo_name_input.text()
        screenshot_path = self.selected_screenshot
        
        if not project_path:
            self.log_message("Ошибка: Папка проекта не выбрана.")
            return
        if not repo_name:
            self.log_message("Ошибка: Имя репозитория не указано.")
            return
            
        # Анализируем проект для генерации информации о релизе
        self.log_message("Анализ проекта для генерации информации о релизе...")
        worker = Worker(project_path, repo_name, True)  # Используем ИИ
        project_info = worker.analyze_project()
        
        # Генерируем информацию о релизе с помощью ИИ
        try:
            self.log_message("Генерация информации о релизе с помощью ИИ...")
            gemini_client = GeminiAPIClient(model_name="qwen3-coder:30b")
            release_info_json = gemini_client.generate_release_info(project_info)
            
            # Парсим JSON с информацией о релизе
            import json
            import re
            
            # Извлекаем JSON из ответа ИИ
            # Ищем первую открывающуюся скобку и последнюю закрывающуюся
            json_match = re.search(r'\{.*\}', release_info_json, re.DOTALL)
            if json_match:
                json_str = json_match.group(0)
                try:
                    release_info = json.loads(json_str)
                except json.JSONDecodeError as e:
                    self.log_message(f"Ошибка парсинга JSON от ИИ: {e}")
                    # Если ИИ не вернул корректный JSON, создаем значения по умолчанию
                    release_info = {
                        "tag": "v1.0.0",
                        "title": "Релиз v1.0.0",
                        "notes": "## Что нового\n- Реализована основная функциональность\n- Исправлены критические ошибки"
                    }
            else:
                self.log_message("Ошибка: Не удалось найти JSON в ответе ИИ. Используются значения по умолчанию.")
                release_info = {
                    "tag": "v1.0.0",
                    "title": "Релиз v1.0.0",
                    "notes": "## Что нового\\n- Реализована основная функциональность\\n- Исправлены критические ошибки"
                }
            
            # Создаем данные для релиза
            release_data = {
                "tag": release_info.get("tag", "v1.0.0"),
                "title": release_info.get("title", "Релиз v1.0.0"),
                "notes": release_info.get("notes", "## Что нового\\n- Реализована основная функциональность")
            }
            
            self.log_message(f"Начинаю автоматическое создание релиза '{release_data['tag']}' для репозитория '{repo_name}'...")
            
            # Создаем рабочий поток для создания релиза
            self.release_worker = ReleaseWorker(project_path, repo_name, release_data, screenshot_path)
            self.release_worker.log_signal.connect(self.log_message)
            self.release_worker.finished_signal.connect(self.on_auto_release_finished)
            self.release_worker.error_signal.connect(self.on_auto_release_error)
            self.release_worker.start()
            
        except Exception as e:
            self.log_message(f"Ошибка при генерации информации о релизе: {e}")
            QMessageBox.critical(self, "Ошибка", f"Ошибка при генерации информации о релизе: {e}")

    def on_auto_release_finished(self):
        self.release_button.setEnabled(True)  # Включаем кнопку обратно
        self.log_message("Процесс автоматического создания релиза завершен.")
        self.log_message("Кнопка 'Создать релиз' должна быть активна после завершения.")
        QMessageBox.information(self, "Релиз создан", "Релиз успешно создан на GitHub!")

    def on_auto_release_error(self, message):
        self.release_button.setEnabled(True)  # Включаем кнопку обратно
        self.log_message(f"ОШИБКА при автоматическом создании релиза: {message}")
        self.log_message("Кнопка 'Создать релиз' должна быть активна после ошибки.")
        QMessageBox.critical(self, "Ошибка создания релиза", f"Произошла ошибка во время создания релиза: {message}")

    def create_release(self):
        project_path = self.project_path_input.text()
        repo_name = self.repo_name_input.text()
        screenshot_path = self.selected_screenshot

        if not project_path:
            self.log_message("Ошибка: Папка проекта не выбрана.")
            return
        if not repo_name:
            self.log_message("Ошибка: Имя репозитория не указано.")
            return

        # Запрашиваем у пользователя информацию о релизе
        from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QTextEdit
        
        class ReleaseDialog(QDialog):
            def __init__(self, parent=None):
                super().__init__(parent)
                self.setWindowTitle("Создание релиза")
                self.setGeometry(200, 200, 400, 300)
                
                layout = QVBoxLayout(self)
                
                # Tag
                tag_layout = QHBoxLayout()
                tag_layout.addWidget(QLabel("Тег релиза (например, v1.0.0):"))
                self.tag_input = QLineEdit()
                tag_layout.addWidget(self.tag_input)
                layout.addLayout(tag_layout)
                
                # Title
                title_layout = QHBoxLayout()
                title_layout.addWidget(QLabel("Заголовок релиза:"))
                self.title_input = QLineEdit()
                title_layout.addWidget(self.title_input)
                layout.addLayout(title_layout)
                
                # Notes
                layout.addWidget(QLabel("Примечания к релизу:"))
                self.notes_input = QTextEdit()
                layout.addWidget(self.notes_input)
                
                # Buttons
                button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
                button_box.accepted.connect(self.accept)
                button_box.rejected.connect(self.reject)
                layout.addWidget(button_box)
                
            def get_data(self):
                return {
                    "tag": self.tag_input.text(),
                    "title": self.title_input.text(),
                    "notes": self.notes_input.toPlainText()
                }
        
        dialog = ReleaseDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            release_data = dialog.get_data()
            
            if not release_data["tag"]:
                self.log_message("Ошибка: Не указан тег релиза.")
                return
                
            self.log_message(f"Начинаю создание релиза '{release_data['tag']}' для репозитория '{repo_name}'...")
            self.release_button.setEnabled(False) # Отключаем кнопку на время выполнения
            self.log_message("Кнопка 'Создать релиз' отключена во время выполнения.")

            # Создаем рабочий поток для создания релиза
            self.release_worker = ReleaseWorker(project_path, repo_name, release_data, screenshot_path)
            self.release_worker.log_signal.connect(self.log_message)
            self.release_worker.finished_signal.connect(self.on_release_finished)
            self.release_worker.error_signal.connect(self.on_release_error)
            self.release_worker.start()

    def on_release_finished(self):
        self.release_button.setEnabled(True) # Включаем кнопку обратно
        self.log_message("Процесс создания релиза завершен.")
        self.log_message("Кнопка 'Создать релиз' должна быть активна после завершения.")
        QMessageBox.information(self, "Релиз создан", "Релиз успешно создан на GitHub!")

    def on_release_error(self, message):
        self.release_button.setEnabled(True) # Включаем кнопку обратно
        self.log_message(f"ОШИБКА: {message}")
        self.log_message("Кнопка 'Создать релиз' должна быть активна после ошибки.")
        QMessageBox.critical(self, "Ошибка создания релиза", f"Произошла ошибка во время создания релиза: {message}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = GitHubPublisherApp()
    window.show()
    sys.exit(app.exec())
